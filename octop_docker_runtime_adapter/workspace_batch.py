"""Template selection -> verified user mounts -> backed-up workspace ZIP overlay.

Runtime adapter version and container labels deliberately remain unchanged.
This module never creates containers, edits agent rows or changes user profiles.
"""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .baseline import load_baseline
from .config import PolicyError
from .runtime import Runtime, digest
from .workspace_files import (atomic_json, clone_tree, exchange_directories, fsync_dir,
                              no_symlinks, overlay, sync_tree, tree_manifest, zip_manifest)

FEATURE_VERSION = "1.0.0"


def error_code(exc):
    return str(exc) if isinstance(exc, PolicyError) else type(exc).__name__


def now():
    return datetime.now(timezone.utc).isoformat()


def config_digest(cfg):
    return digest({k: str(v) for k, v in cfg.__dict__.items()})


def checked_record(cfg, rt, row):
    if row.get("source_error"):
        raise PolicyError(row["source_error"])
    if (row.get("kind") or "expert") != "expert":
        raise PolicyError("only_expert_rows_supported")
    ident = rt.identity(row.get("user_id"), row.get("username"), row["agent_id"])
    spec = row.get("backend")
    if not rt.managed_spec(spec):
        raise PolicyError("expert_not_managed_by_this_adapter")
    rt.authorize(spec, ident.owner_id, ident.username, ident.agent_id)
    if row.get("workspace_dir") != str(cfg.container_workspace_root / ident.agent_id):
        raise PolicyError("expert_workspace_config_mismatch")
    path = no_symlinks(cfg.state_dir / "users" / (ident.key + ".json"))
    record = json.loads(path.read_text())
    required = {"schema": 2, "instance": cfg.instance_id, "owner_id": ident.owner_id,
                "username": ident.username, "name": ident.name, "key": ident.key,
                "workspace": str(cfg.workspace_root / ident.key)}
    if any(record.get(k) != v for k, v in required.items()):
        raise PolicyError("user_record_identity_or_workspace_mismatch")
    target = no_symlinks(cfg.workspace_root / ident.key / "agents" / ident.agent_id)
    if not target.is_dir():
        raise PolicyError("expert_directory_missing_initialize_expert_first")
    return ident, record, target


def build_plan(cfg, catalog, client, *, archive=None, source=None):
    baseline = load_baseline(cfg)
    rt = Runtime(cfg, baseline, None)
    rt.check_daemon(client)
    targets = []
    for row in catalog["agents"]:
        out = {"agent_id": row["agent_id"], "user_id": row.get("user_id"),
               "username": row.get("username"), "name": row.get("name"),
               "enabled": row.get("enabled"), "agent_state": row.get("last_state"),
               "ready": False}
        try:
            ident, record, target = checked_record(cfg, rt, row)
            spec = row["backend"]
            desired = rt.profile(SimpleNamespace(
                _client=client, _image=spec.get("image", "python:3.12-slim"),
                _allow_network=spec.get("allow_network", False), _memory=spec.get("memory", "512m"),
                _cpus=float(spec.get("cpus", 1)), _pids_limit=spec.get("pids_limit", 256),
                _auto_remove=spec.get("auto_remove", False), _volumes=spec.get("volumes")))
            if desired != record.get("profile"):
                raise PolicyError("backend_profile_differs_from_registered_user")
            obj = client.containers.get(ident.name)
            obj.reload()
            rt.validate_container(obj, ident, desired)
            status = obj.attrs.get("State", {}).get("Status")
            if status not in {"running", "exited", "created"}:
                raise PolicyError("container_state_not_supported_for_restore")
            out.update(ready=True, user_key=ident.key, adapter_hash=ident.key[2:],
                       host_workspace_dir=str(target), container_workspace_dir=row["workspace_dir"],
                       container_id=obj.id, container_name=ident.name, container_status=status,
                       profile_digest=digest(desired), backend_digest=digest(spec),
                       user_record_digest=digest(record), mounts=rt.mounts(ident))
        except Exception as exc:
            out["error"] = error_code(exc)
        targets.append(out)
    plan = {"schema": 1, "feature_version": FEATURE_VERSION, "created_at": now(),
            "instance": cfg.instance_id, "config_digest": config_digest(cfg),
            "daemon_id": baseline["docker_daemon_id"], "source": source,
            "template_id": catalog["template_id"], "template_kind": catalog["template_kind"],
            "template": catalog["template"], "mode": "overlay", "archive": archive,
            "targets": targets, "target_count": len(targets),
            "ready_count": sum(t["ready"] for t in targets)}
    plan["plan_digest"] = digest(plan)
    return plan


def validate_plan(plan):
    if plan.get("schema") != 1 or plan.get("mode") != "overlay":
        raise PolicyError("unsupported_restore_plan")
    if plan.get("plan_digest") != digest({k: v for k, v in plan.items() if k != "plan_digest"}):
        raise PolicyError("plan_changed_regenerate_instead_of_editing")
    if not plan.get("archive") or not plan.get("targets"):
        raise PolicyError("plan_requires_zip_and_at_least_one_target")
    if any(not t.get("ready") for t in plan["targets"]):
        raise PolicyError("plan_contains_blocked_targets")


def stable_targets(plan):
    ignore = {"agent_state", "container_status"}
    return [{k: v for k, v in t.items() if k not in ignore} for t in plan["targets"]]


def verify_fresh(cfg, old, fresh):
    if old["config_digest"] != config_digest(cfg):
        raise PolicyError("adapter_config_changed_since_plan")
    keys = ("instance", "daemon_id", "template_id", "template_kind", "template")
    if any(old[k] != fresh[k] for k in keys) or stable_targets(old) != stable_targets(fresh):
        raise PolicyError("targets_backend_or_mounts_changed_regenerate_plan")


def ensure_private_dir(path, owner):
    no_symlinks(path, exists=False)
    try:
        path.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(path, owner, -1)
    except FileExistsError:
        pass
    st = path.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or stat.S_IMODE(st.st_mode) != 0o700:
        raise PolicyError("batch_state_requires_service_owned_0700_directory")


@contextmanager
def file_lock(path, owner, timeout=30):
    no_symlinks(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if os.geteuid() == 0 and st.st_uid == 0:
            os.fchown(fd, owner, -1)
            st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner or st.st_mode & 0o077:
            raise PolicyError("unsafe_batch_lock")
        until = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise PolicyError("batch_or_user_lock_timeout")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


class Service:
    def __init__(self, owner, unit="octop.service", scope="user", runner=subprocess.run):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit):
            raise PolicyError("invalid_systemd_service_unit")
        self.owner, self.unit, self.scope, self.runner = owner, unit, scope, runner

    def call(self, *args):
        command = ["systemctl"] + (["--user"] if self.scope == "user" else []) + list(args)
        env = dict(os.environ)
        if self.scope == "user":
            env.update(XDG_RUNTIME_DIR=f"/run/user/{self.owner}",
                       DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{self.owner}/bus")
            if os.geteuid() == 0 and self.owner != 0:
                command = ["runuser", "-u", pwd.getpwuid(self.owner).pw_name, "--", "env",
                           "XDG_RUNTIME_DIR=" + env["XDG_RUNTIME_DIR"],
                           "DBUS_SESSION_BUS_ADDRESS=" + env["DBUS_SESSION_BUS_ADDRESS"]] + command
        proc = self.runner(command, env=env, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode:
            raise PolicyError("systemd_operation_failed_" + args[0])
        return proc.stdout.strip()

    def active(self):
        result = self.call("show", self.unit, "--property=ActiveState", "--property=LoadState")
        values = dict(line.split("=", 1) for line in result.splitlines() if "=" in line)
        if values.get("LoadState") != "loaded":
            raise PolicyError("systemd_service_not_loaded")
        state = values.get("ActiveState")
        if state not in {"active", "inactive", "failed"}:
            raise PolicyError("systemd_service_in_transition")
        return state == "active"

    def stop(self):
        self.call("stop", self.unit)

    def start(self):
        self.call("start", self.unit)


class RuntimePause:
    def __init__(self, client, service, targets, report, save):
        self.client, self.service, self.targets, self.report, self.save = client, service, targets, report, save

    def snapshot(self):
        containers = {}
        for target in self.targets:
            obj = self.client.containers.get(target["container_id"])
            obj.reload()
            state = obj.attrs.get("State", {}).get("Status")
            if state not in {"running", "exited", "created"}:
                raise PolicyError("container_state_not_supported_for_restore")
            containers[obj.id] = {"name": target["container_name"], "was_running": state == "running"}
        return {"service_was_active": self.service.active(), "containers": containers,
                "service_unit": self.service.unit, "service_scope": self.service.scope}

    def quiet(self):
        if self.service.active():
            raise PolicyError("stop_octop_service_or_use_stop_runtime")
        for target in self.targets:
            obj = self.client.containers.get(target["container_id"])
            obj.reload()
            if obj.attrs.get("State", {}).get("Status") not in {"exited", "created"}:
                raise PolicyError("stop_affected_user_containers_or_use_stop_runtime")

    def stop(self, manage):
        self.report["runtime_before"] = self.snapshot()
        self.report["manage_runtime"] = manage
        self.save()
        if manage:
            if self.report["runtime_before"]["service_was_active"]:
                self.service.stop()
            for cid, info in self.report["runtime_before"]["containers"].items():
                if info["was_running"]:
                    self.client.containers.get(cid).stop(timeout=20)
        self.quiet()

    def resume(self):
        errors = []
        before = self.report.get("runtime_before", {})
        for cid, info in before.get("containers", {}).items():
            if not info["was_running"]:
                continue
            try:
                obj = self.client.containers.get(cid)
                obj.reload()
                if obj.attrs.get("State", {}).get("Status") in {"exited", "created"}:
                    obj.start()
                obj.reload()
                if obj.attrs.get("State", {}).get("Status") != "running":
                    raise PolicyError("container_not_running_after_start")
            except Exception as exc:
                errors.append({"container_id": cid, "error": error_code(exc)})
        if before.get("service_was_active") and not errors:
            try:
                self.service.start()
                if not self.service.active():
                    raise PolicyError("service_not_active_after_start")
            except Exception as exc:
                errors.append({"service": self.service.unit, "error": error_code(exc)})
        self.report["runtime_resume_errors"] = errors
        self.report["runtime_resumed"] = not errors
        self.save()
        return errors


def restore_one(target, extracted, archive, folder, entry, save, quiet):
    destination = Path(target["host_workspace_dir"])
    stage = destination.parent / (".dra-stage-" + uuid.uuid4().hex)
    backup = folder / "backup"
    original = tree_manifest(destination)
    entry.update(status="backing_up", stage=str(stage), backup=str(backup), before_digest=digest(original))
    save()
    clone_tree(destination, backup, original)
    sync_tree(backup)
    clone_tree(backup, stage, original)
    updated = overlay(stage, extracted, archive)
    sync_tree(stage)
    entry.update(status="prepared", after_digest=digest(updated))
    save()
    quiet()
    if digest(tree_manifest(destination)) != entry["before_digest"]:
        raise PolicyError("workspace_changed_during_restore")
    entry["status"] = "committing"
    save()
    exchange_directories(stage, destination)
    entry["status"] = "committed"
    save()
    if digest(tree_manifest(destination)) != entry["after_digest"]:
        raise PolicyError("restored_workspace_verification_failed")
    shutil.rmtree(stage)
    fsync_dir(destination.parent)
    entry["status"] = "verified"
    save()


def clean_staging(target, entry):
    destination = Path(target["host_workspace_dir"])
    for field, prefix in (("stage", ".dra-stage-"), ("rollback_stage", ".dra-rollback-")):
        value = entry.get(field)
        if not value:
            continue
        path = Path(value)
        if path.parent != destination.parent or not path.name.startswith(prefix):
            raise PolicyError("staging_path_mismatch")
        if path.exists():
            no_symlinks(path)
            shutil.rmtree(path)
            fsync_dir(path.parent)


def rollback_one(target, folder, entry, save, quiet, *, overwrite_changed=False):
    destination = Path(target["host_workspace_dir"])
    backup = Path(entry["backup"])
    no_symlinks(backup)
    if backup != folder / "backup":
        raise PolicyError("backup_path_mismatch")
    original = tree_manifest(backup)
    if digest(original) != entry["before_digest"]:
        raise PolicyError("backup_changed_or_incomplete")
    current = tree_manifest(destination)
    current_digest = digest(current)
    if current_digest == entry["before_digest"]:
        entry["status"] = "rolled_back"
        save()
        clean_staging(target, entry)
        return
    if current_digest != entry.get("after_digest") and not overwrite_changed:
        raise PolicyError("workspace_changed_after_restore_use_overwrite_changed_to_backup_and_rollback")
    stage = destination.parent / (".dra-rollback-" + uuid.uuid4().hex)
    entry["rollback_stage"] = str(stage)
    save()
    clone_tree(backup, stage, original)
    sync_tree(stage)
    # Preserve all current data, including post-deployment edits, outside the mount.
    discarded = folder / ("before-rollback-" + uuid.uuid4().hex)
    clone_tree(destination, discarded, current)
    sync_tree(discarded)
    quiet()
    if digest(tree_manifest(destination)) != current_digest:
        raise PolicyError("workspace_changed_during_rollback")
    entry.update(status="rolling_back", before_rollback_backup=str(discarded))
    save()
    exchange_directories(stage, destination)
    if digest(tree_manifest(destination)) != entry["before_digest"]:
        raise PolicyError("rollback_verification_failed")
    entry["status"] = "rolled_back"
    save()
    clean_staging(target, entry)


def execute_batch(cfg, plan, client, service, owner, *, manage_runtime=False, refresh=None):
    validate_plan(plan)
    root = cfg.state_dir / "workspace-batches"
    ensure_private_dir(root, owner)
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    batch = root / batch_id
    report = {"schema": 1, "batch_id": batch_id, "created_at": now(), "status": "preparing",
              "plan": plan, "results": [], "manage_runtime": manage_runtime}
    with file_lock(root / "batch.lock", owner, cfg.lock_timeout), ExitStack() as stack:
        for key in sorted({t["user_key"] for t in plan["targets"]}):
            stack.enter_context(file_lock(cfg.state_dir / "locks" / (key + ".lock"), owner, cfg.lock_timeout))
        if refresh:
            verify_fresh(cfg, plan, refresh())
        ensure_private_dir(batch, owner)
        report_path = batch / "report.json"
        def save():
            atomic_json(report_path, report, owner=owner)
        save()
        pause = RuntimePause(client, service, plan["targets"], report, save)
        resume_safe = True
        try:
            extracted = batch / "archive"
            ensure_private_dir(extracted, owner)
            source = plan["archive"]
            actual = zip_manifest(source["path"], strip_components=source["strip_components"],
                                  max_bytes=source["max_bytes"], max_files=source["max_files"], extract_to=extracted)
            if actual != source:
                raise PolicyError("archive_changed_since_plan")
            pause.stop(manage_runtime)
            if refresh:
                verify_fresh(cfg, plan, refresh())
            report["status"] = "restoring"
            save()
            for target in plan["targets"]:
                folder = batch / target["agent_id"]
                ensure_private_dir(folder, owner)
                entry = {"agent_id": target["agent_id"], "status": "pending"}
                report["results"].append(entry)
                save()
                try:
                    restore_one(target, extracted, source, folder, entry, save, pause.quiet)
                except Exception as exc:
                    entry["error"] = error_code(exc)
                    # If an exchange happened (including a journal fsync failure),
                    # restore the verified backup before bringing the runtime back.
                    if entry.get("after_digest") and entry.get("status") in {"committing", "committed", "verified"}:
                        try:
                            rollback_one(target, folder, entry, save, pause.quiet)
                        except Exception as rollback_exc:
                            entry["rollback_error"] = error_code(rollback_exc)
                            resume_safe = False
                    if entry["status"] != "rolled_back":
                        entry["status"] = "failed"
                    if resume_safe and entry.get("stage"):
                        clean_staging(target, entry)
                    save()
                    raise
            report["status"] = "completed"
        except Exception as exc:
            report.update(status="failed", error=error_code(exc))
        finally:
            report["finished_at"] = now()
            save()
            # Octop startup acquires these same user locks. Release them before start.
            stack.close()
            if manage_runtime and report.get("runtime_before") and resume_safe:
                if pause.resume():
                    report["status"] = "runtime_resume_failed"
                    save()
            elif not resume_safe:
                report["runtime_left_stopped"] = True
                save()
        return report_path, report


def rollback_batch(cfg, path, client, service, owner, *, manage_runtime=False,
                   overwrite_changed=False, refresh=None):
    no_symlinks(path)
    report = json.loads(path.read_text())
    plan = report["plan"]
    validate_plan(plan)
    if path.parent.parent != cfg.state_dir / "workspace-batches" or path.name != "report.json":
        raise PolicyError("batch_report_must_be_in_adapter_state")
    if report.get("batch_id") != path.parent.name:
        raise PolicyError("batch_report_identity_mismatch")
    root = path.parent.parent
    with file_lock(root / "batch.lock", owner, cfg.lock_timeout), ExitStack() as stack:
        for key in sorted({t["user_key"] for t in plan["targets"]}):
            stack.enter_context(file_lock(cfg.state_dir / "locks" / (key + ".lock"), owner, cfg.lock_timeout))
        if refresh:
            verify_fresh(cfg, plan, refresh())
        report.setdefault("original_runtime_before", report.get("runtime_before"))
        def save():
            atomic_json(path, report, owner=owner)
        pause = RuntimePause(client, service, plan["targets"], report, save)
        ok = False
        try:
            pause.stop(manage_runtime)
            if refresh:
                verify_fresh(cfg, plan, refresh())
            by_id = {t["agent_id"]: t for t in plan["targets"]}
            for entry in reversed(report["results"]):
                if entry.get("after_digest") and entry.get("backup"):
                    target = by_id[entry["agent_id"]]
                    rollback_one(target, path.parent / entry["agent_id"], entry, save,
                                 pause.quiet, overwrite_changed=overwrite_changed)
                else:
                    clean_staging(by_id[entry["agent_id"]], entry)
            report["status"] = "rolled_back"
            ok = True
        except Exception as exc:
            report.update(status="rollback_failed", rollback_error=error_code(exc))
        finally:
            save()
            stack.close()
            if ok and manage_runtime:
                # For crash recovery use the original, pre-restoration runtime state.
                report["runtime_before"] = report.get("original_runtime_before") or report["runtime_before"]
                if pause.resume():
                    report["status"] = "runtime_resume_failed"
                    save()
        return path, report
