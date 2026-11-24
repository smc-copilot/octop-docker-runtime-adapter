from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .baseline import fingerprint, load_baseline, write_new
from .compat import evidence
from .config import Config, PolicyError, TOKEN, private_dir
from .runtime import LABEL, Runtime, digest, validate_network


class SilentAudit:
    def emit(self, *args, **kwargs):
        pass


class _ClosingClient:
    """Context-manager shim: Docker SDK 7.2.0's DockerClient has no __enter__."""

    def __init__(self, inner):
        self._inner = inner

    def __enter__(self):
        return self._inner

    def __exit__(self, exc_type, exc, tb):
        self._inner.close()
        return False


def client():
    import docker
    obj = docker.from_env(timeout=30)
    if obj.api.base_url != "http+docker://localhost":
        obj.close()
        raise PolicyError("only_local_unix_daemon_supported")
    obj.ping()
    return _ClosingClient(obj)


def prepare_dirs(cfg):
    for path in (cfg.workspace_root, cfg.state_dir, cfg.state_dir / "users", cfg.state_dir / "locks", cfg.log_file.parent):
        private_dir(path, create=True)
    return {"directories_ready": True}


def inventory(cfg):
    with client() as c:
        rows = []
        for obj in c.containers.list(all=True, filters={"label": "harness.agent.sandbox=1"}):
            obj.reload()
            a = obj.attrs
            labels = a.get("Config", {}).get("Labels") or {}
            rows.append({"id": obj.id, "name": a["Name"].lstrip("/"),
                         "scope": labels.get("harness.agent.sandbox_scope"),
                         "status": a.get("State", {}).get("Status"),
                         "adapter_version": labels.get(LABEL + ".version"),
                         "mounts": [{k: m.get(k) for k in ("Type", "Source", "Destination", "RW")}
                                    for m in a.get("Mounts", [])]})
    return {"count": len(rows), "containers": rows}


def snapshot(cfg):
    if cfg.baseline_file.exists():
        raise PolicyError("baseline_already_exists_do_not_overwrite")
    compat = evidence()
    with client() as c:
        items = c.containers.list(all=True, filters={"label": "harness.agent.sandbox=1"})
        rows = []
        for obj in items:
            obj.reload()
            labels = obj.attrs.get("Config", {}).get("Labels") or {}
            if labels.get(LABEL + ".version") == "2.0.0" and labels.get(LABEL + ".instance") == cfg.instance_id:
                raise PolicyError("v2_already_active_restore_original_baseline")
            rows.append(fingerprint(obj.attrs))
        raw = {"schema": 2, "instance": cfg.instance_id, "prefix": cfg.sandbox_prefix,
               "config_digest": digest({k: str(v) for k, v in cfg.__dict__.items()}),
               "captured_at": datetime.now(timezone.utc).isoformat(), "docker_daemon_id": c.info()["ID"],
               "compatibility": compat, "containers": sorted(rows, key=lambda x: x["id"])}
    write_new(cfg.baseline_file, raw)
    return {"snapshot_created": str(cfg.baseline_file), "protected_containers": len(rows)}


def assert_legacy(cfg):
    import docker
    baseline = load_baseline(cfg)
    problems = []
    with client() as c:
        Runtime(cfg, baseline, SilentAudit()).check_daemon(c)
        for old in baseline["containers"]:
            try:
                now = fingerprint(c.containers.get(old["id"]).attrs)
            except docker.errors.NotFound:
                problems.append({"name": old["name"], "changed": ["missing"]})
                continue
            changed = [k for k in old if old[k] != now[k]]
            if changed:
                problems.append({"name": old["name"], "changed": changed})
    if problems:
        print(json.dumps({"legacy_unchanged": False, "differences": problems}))
        raise PolicyError("legacy_changed_do_not_auto_repair")
    return {"legacy_unchanged": True, "checked": len(baseline["containers"])}


def ensure_network(cfg):
    import docker
    baseline = load_baseline(cfg)
    with client() as c:
        Runtime(cfg, baseline, SilentAudit()).check_daemon(c)
        created = False
        try:
            network = c.networks.get(cfg.network_name)
        except docker.errors.NotFound:
            network = c.networks.create(cfg.network_name, driver="bridge", internal=False,
                                        check_duplicate=True, labels={LABEL + ".purpose": "shared-egress"})
            created = True
        validate_network(network, cfg.network_name)
        return {"network": cfg.network_name, "created": created,
                "subnets": [n.get("Subnet") for n in network.attrs.get("IPAM", {}).get("Config", [])]}


def doctor(cfg):
    baseline = load_baseline(cfg)
    if os.environ.get("OCTOP_IN_CONTAINER", "").lower() in {"1", "true", "yes"}:
        raise PolicyError("OCTOP_IN_CONTAINER_must_be_unset")
    if evidence() != baseline["compatibility"]:
        raise PolicyError("compatibility_changed")
    for path in (cfg.workspace_root, cfg.state_dir, cfg.state_dir / "users", cfg.state_dir / "locks", cfg.log_file.parent):
        private_dir(path)
    with client() as c:
        Runtime(cfg, baseline, SilentAudit()).check_daemon(c)
        validate_network(c.networks.get(cfg.network_name), cfg.network_name)
        collisions = []
        for old in baseline["containers"]:
            obj = c.containers.get(old["id"])
            labels = obj.attrs.get("Config", {}).get("Labels") or {}
            if labels.get("harness.agent.sandbox_scope") == "user" and old["name"].startswith(cfg.sandbox_prefix + "_"):
                collisions.append(old["name"])
        if collisions:
            print(json.dumps({"preexisting_user_names": collisions}))
            raise PolicyError("existing_user_containers_need_separate_migration_or_new_namespace")
    return {"ready": True, "instance": cfg.instance_id, "prefix": cfg.sandbox_prefix,
            "protected_containers": len(baseline["containers"]), "compatibility": baseline["compatibility"]["versions"]}


def inspect_user(cfg, username):
    if not TOKEN.fullmatch(username):
        raise PolicyError("invalid_username")
    records = [json.loads(p.read_text()) for p in (cfg.state_dir / "users").glob("u_*.json")]
    matches = [x for x in records if x["username"] == username]
    if len(matches) != 1:
        raise PolicyError("unique_registered_user_required")
    row = matches[0]
    rt = Runtime(cfg, load_baseline(cfg), SilentAudit())
    ident = rt.identity(row["owner_id"], row["username"], "INSPECT")
    with client() as c:
        rt.check_daemon(c)
        obj = c.containers.get(ident.name)
        rt.validate_container(obj, ident, row["profile"])
        return {"verified": True, "name": ident.name, "container_id": obj.id, "user_key": ident.key,
                "profile": row["profile"], "mounts": rt.mounts(ident),
                "status": obj.attrs.get("State", {}).get("Status")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("init-config", "prepare-dirs", "inventory", "snapshot", "ensure-network", "doctor", "assert-legacy"):
        sub.add_parser(name)
    inspect = sub.add_parser("inspect-user")
    inspect.add_argument("--username", required=True)
    args = parser.parse_args()
    try:
        if args.action == "init-config":
            root = Path(__file__).parent.parent
            path = root / "config/adapter.json"
            write_new(path, json.loads((root / "config/adapter.example.json").read_text()))
            result = {"created": str(path), "next": "review_instance_prefix_and_paths_before_snapshot"}
        else:
            cfg = Config.load(args.config)
            if args.action == "inspect-user":
                result = inspect_user(cfg, args.username)
            else:
                result = {"prepare-dirs": prepare_dirs, "inventory": inventory, "snapshot": snapshot,
                          "ensure-network": ensure_network, "doctor": doctor, "assert-legacy": assert_legacy}[args.action](cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": str(exc) if isinstance(exc, PolicyError) else type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
