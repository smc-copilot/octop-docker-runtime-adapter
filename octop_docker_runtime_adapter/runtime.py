from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass

from . import __version__
from .config import AGENT, TOKEN, PolicyError, private_dir
from .locking import user_lock

LABEL = "com.superic.octop.adapter"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    owner_id: str
    username: str
    agent_id: str
    key: str
    name: str


def validate_network(network, name):
    a = network.attrs
    if (a.get("Name") != name or a.get("Driver") != "bridge" or a.get("Scope") != "local"
            or a.get("Internal") or str((a.get("Options") or {}).get(
                "com.docker.network.bridge.enable_ip_masquerade", "true")).lower() == "false"):
        raise PolicyError("shared_network_must_be_local_egress_bridge")


class Runtime:
    def __init__(self, cfg, baseline, audit):
        self.cfg, self.baseline, self.audit = cfg, baseline, audit
        # Authentication of the INTERNAL manager->backend binding, not a user token.
        self._secret = secrets.token_bytes(32)

    def managed_spec(self, spec):
        return (isinstance(spec, dict) and spec.get("type") == "docker"
                and str(spec.get("sandbox_scope", "agent")).strip().lower() == "user"
                and str(spec.get("sandbox_prefix", "octop_sandbox")).strip() == self.cfg.sandbox_prefix)

    def identity(self, owner, username, aid):
        if owner is None or not str(owner).strip():
            raise PolicyError("public_or_ownerless_agent_requires_separate_backend")
        if not isinstance(username, str) or not TOKEN.fullmatch(username):
            raise PolicyError("username_requires_unambiguous_ascii_docker_name")
        if not isinstance(aid, str) or not AGENT.fullmatch(aid):
            raise PolicyError("invalid_agent_id")
        owner = str(owner)
        key = "u_" + digest([self.cfg.instance_id, owner])[:32]
        return Identity(owner, username, aid, key, self.cfg.sandbox_prefix + "_" + username)

    def signature(self, ident):
        payload = json.dumps([self.cfg.instance_id, ident.owner_id, ident.username,
                              ident.agent_id, self.cfg.sandbox_prefix], separators=(",", ":")).encode()
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def authorize(self, spec, owner, username, aid):
        ident = self.identity(owner, username, aid)
        # These fields can change scope/naming or bypass per-expert directories.
        for key in ("container_name", "sandbox_id", "workspace_path"):
            if spec.get(key):
                raise PolicyError("user_backend_must_not_override_" + key)
        if spec.get("username") not in (None, "", username):
            raise PolicyError("configured_username_differs_from_agent_owner")
        if spec.get("agent_id") not in (None, "", aid):
            raise PolicyError("configured_agent_id_differs_from_control_plane")
        if spec.get("auto_remove") or spec.get("volumes"):
            raise PolicyError("user_backend_cannot_autoremove_or_supply_host_mounts")
        return {**spec, "agent_id": aid, "username": username, "_dra_owner_id": ident.owner_id,
                "_dra_signature": self.signature(ident)}

    def bind(self, kwargs):
        spec = {"type": "docker", **kwargs}
        if not self.managed_spec(spec):
            return None
        ident = self.identity(kwargs.get("_dra_owner_id"), kwargs.get("username"), kwargs.get("agent_id"))
        if not hmac.compare_digest(str(kwargs.get("_dra_signature", "")), self.signature(ident)):
            raise PolicyError("user_backend_requires_trusted_owner_binding")
        self.authorize(spec, ident.owner_id, ident.username, ident.agent_id)
        expected = str(self.cfg.container_workspace_root / ident.agent_id)
        if str(kwargs.get("workspace_dir")) != expected:
            raise PolicyError("custom_workspace_dir_not_supported_by_user_mount_layout")
        return ident

    def check_daemon(self, client):
        if client.api.base_url != "http+docker://localhost":
            raise PolicyError("only_local_unix_daemon_supported")
        info = client.info()
        if info.get("ID") != self.baseline["docker_daemon_id"]:
            raise PolicyError("docker_daemon_changed")
        if any("rootless" in str(v) or "userns" in str(v) for v in info.get("SecurityOptions", [])):
            raise PolicyError("rootless_or_userns_requires_separate_review")

    def profile(self, box):
        from docker.utils import parse_bytes
        if (not isinstance(box._allow_network, bool) or box._memory is None or box._cpus is None
                or box._pids_limit is None or box._auto_remove or box._volumes):
            raise PolicyError("explicit_limits_and_no_additional_mounts_required")
        memory, cpus, pids = parse_bytes(box._memory), int(box._cpus * 1e9), int(box._pids_limit)
        if memory <= 0 or cpus <= 0 or pids <= 0:
            raise PolicyError("positive_resource_limits_required")
        return {"image": box._image, "image_id": box._client.images.get(box._image).id,
                "memory": memory, "nano_cpus": cpus, "pids_limit": pids,
                "network": self.cfg.network_name if box._allow_network else "none"}

    def actual_profile(self, container):
        """Effective profile of an existing container (pinned reuse).

        The backend spec describes how NEW user containers are created. An
        existing user container keeps the image/resources/network it was created
        with; new experts of that user join it. This is what makes an image or
        resource change on the backend safe for existing users.
        """
        a = container.attrs
        config, host = a.get("Config") or {}, a.get("HostConfig") or {}
        profile = {
            "image": str(config.get("Image") or ""),
            "image_id": str(a.get("Image") or ""),
            "memory": int(host.get("Memory") or 0),
            "nano_cpus": int(host.get("NanoCpus") or 0),
            "pids_limit": int(host.get("PidsLimit") or 0),
            "network": str(host.get("NetworkMode") or "none"),
        }
        if (not profile["image"] or not profile["image_id"]
                or profile["memory"] <= 0 or profile["nano_cpus"] <= 0
                or profile["pids_limit"] <= 0):
            raise PolicyError("existing_container_has_incomplete_profile")
        return profile

    def pin_existing(self):
        return os.environ.get("OCTOP_DRA_PIN_EXISTING", "0").strip().lower() in {"1", "true", "yes"}

    def mounts(self, ident):
        root = self.cfg.workspace_root / ident.key
        return {str(root / "agents"): {"bind": str(self.cfg.container_workspace_root), "mode": "rw"},
                str(root / "shared"): {"bind": self.cfg.shared_target, "mode": "rw"}}

    def labels(self, ident, profile):
        # Native harness.agent.id belongs to one backend, not to the shared container.
        return {"harness.agent.sandbox": "1", "harness.agent.sandbox_scope": "user",
                LABEL + ".version": __version__, LABEL + ".instance": self.cfg.instance_id,
                LABEL + ".user_key": ident.key, LABEL + ".username": ident.username,
                LABEL + ".profile": digest(profile)}

    def register(self, ident, profile):
        # Runs under the per-user process AND thread lock. State is outside mounts.
        record = {"schema": 2, "instance": self.cfg.instance_id, "owner_id": ident.owner_id,
                  "username": ident.username, "name": ident.name, "key": ident.key,
                  "profile": profile, "workspace": str(self.cfg.workspace_root / ident.key)}
        path = self.cfg.state_dir / "users" / (ident.key + ".json")
        if path.exists():
            if path.is_symlink() or json.loads(path.read_text()) != record:
                raise PolicyError("user_name_or_profile_changed_requires_explicit_maintenance")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        for folder in (self.cfg.workspace_root, self.cfg.workspace_root / ident.key):
            private_dir(folder, create=True)
        for sub in ("agents", "shared"):
            private_dir(self.cfg.workspace_root / ident.key / sub, create=True)

    def validate_container(self, container, ident, profile):
        a = container.attrs
        if (container.id in self.baseline["protected_ids"] or ident.name in self.baseline["protected_names"]):
            raise PolicyError("preexisting_container_requires_explicit_migration")
        config, host = a.get("Config", {}), a.get("HostConfig", {})
        if a.get("Name", "").lstrip("/") != ident.name:
            raise PolicyError("container_name_mismatch")
        labels = config.get("Labels") or {}
        if any(labels.get(k) != v for k, v in self.labels(ident, profile).items()):
            raise PolicyError("container_identity_or_profile_mismatch")
        expected = {(src, dst["bind"]) for src, dst in self.mounts(ident).items()}
        actual = {(m.get("Source"), m.get("Destination")) for m in a.get("Mounts", [])
                  if m.get("Type") == "bind" and m.get("RW") is True
                  and m.get("Propagation", "rprivate") == "rprivate"}
        if actual != expected or len(a.get("Mounts", [])) != 2:
            raise PolicyError("container_mounts_mismatch")
        if (a.get("Image") != profile["image_id"] or config.get("Image") != profile["image"]
                or config.get("WorkingDir") != str(self.cfg.container_workspace_root)):
            raise PolicyError("container_image_or_workdir_mismatch")
        if (host.get("Memory") != profile["memory"] or host.get("NanoCpus") != profile["nano_cpus"]
                or host.get("PidsLimit") != profile["pids_limit"] or host.get("AutoRemove")
                or host.get("Privileged") or host.get("CapAdd") or host.get("Binds", []) and not a.get("Mounts")):
            raise PolicyError("container_resource_or_security_settings_mismatch")
        if (host.get("NetworkMode") != profile["network"]
                or set(a.get("NetworkSettings", {}).get("Networks", {})) != {profile["network"]}):
            raise PolicyError("container_network_mismatch")

    def create(self, box, ident, profile):
        kwargs = {"image": box._image, "command": ["sleep", "infinity"], "name": ident.name,
                  "detach": True, "working_dir": str(self.cfg.container_workspace_root),
                  "labels": self.labels(ident, profile), "volumes": self.mounts(ident),
                  "mem_limit": profile["memory"], "nano_cpus": profile["nano_cpus"],
                  "pids_limit": profile["pids_limit"], "tty": False, "stdin_open": False,
                  "auto_remove": False}
        if profile["network"] == "none":
            kwargs["network_mode"] = "none"
        else:
            kwargs["network"] = profile["network"]
        # Never retry as an unmounted sandbox. An SDK failure stays a failure.
        return box._client.containers.run(**kwargs)

    def acquire(self, box):
        ident = box._dra_identity
        if box._container_name != ident.name or box._workspace_root != str(self.cfg.container_workspace_root / ident.agent_id):
            raise PolicyError("harness_identity_or_workspace_drift")
        self.check_daemon(box._client)
        with user_lock(self.cfg, ident.key):
            if ident.name in self.baseline["protected_names"]:
                raise PolicyError("preexisting_container_requires_explicit_migration")
            try:
                container = box._client.containers.get(ident.name)
            except box._docker.errors.NotFound:
                container = None
            # Existing containers are authoritative when pinning is enabled:
            # the user keeps the image/resources/network they were created with.
            # Only users without a container are built from the current backend.
            if container is not None and self.pin_existing():
                profile = self.actual_profile(container)
                self.validate_container(container, ident, profile)
                self.audit.emit("user_profile_pinned", user_key=ident.key, agent_id=ident.agent_id,
                                container_id=container.id, image=profile["image"],
                                image_id=profile["image_id"], memory=profile["memory"],
                                nano_cpus=profile["nano_cpus"], pids_limit=profile["pids_limit"],
                                network=profile["network"])
            else:
                profile = self.profile(box)
                if profile["network"] != "none":
                    validate_network(box._client.networks.get(profile["network"]), profile["network"])
                # Validate existing containers before touching them, even when stopped.
                if container is not None:
                    self.validate_container(container, ident, profile)
            self.register(ident, profile)
            reused = container is not None
            if container is None:
                try:
                    container = self.create(box, ident, profile)
                except box._docker.errors.APIError as exc:
                    if exc.status_code != 409:
                        raise
                    # A creator outside this lock may race. Validate its identity.
                    container = box._client.containers.get(ident.name)
                    reused = True
                container.reload()
                self.validate_container(container, ident, profile)
            state = container.attrs.get("State", {})
            if state.get("Status") != "running":
                if state.get("Status") not in ("created", "exited"):
                    raise PolicyError("container_not_startable")
                # Failed start must NOT rename an orphan or create a replacement.
                container.start()
                container.reload()
                if container.attrs.get("State", {}).get("Status") != "running":
                    raise PolicyError("container_failed_to_start")
            # Each wrapper has its own workdir; never use Config.WorkingDir for execution.
            result = container.exec_run(["mkdir", "-p", "--", box._workspace_root], workdir="/")
            if result.exit_code != 0:
                raise PolicyError("agent_workspace_creation_failed")
            box._reused = reused
            box._dra_profile = profile
            self.audit.emit("user_sandbox_reused" if reused else "user_sandbox_created",
                            user_key=ident.key, agent_id=ident.agent_id, container_id=container.id)
            return container
