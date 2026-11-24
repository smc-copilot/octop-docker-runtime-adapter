from __future__ import annotations

import json
import os
from pathlib import Path

from .config import PolicyError
from .runtime import digest


def write_new(path, data):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def fingerprint(a):
    return {"id": a["Id"], "name": a["Name"].lstrip("/"), "created": a.get("Created"),
            "config": digest(a.get("Config")), "host_config": digest(a.get("HostConfig")),
            "mounts": digest(a.get("Mounts")), "networks": digest(a.get("NetworkSettings", {}).get("Networks")),
            "state": {k: a.get("State", {}).get(k) for k in ("Status", "StartedAt", "FinishedAt", "Paused", "Restarting")}}


def load_baseline(cfg):
    raw = json.loads(cfg.baseline_file.read_text())
    if raw.get("schema") != 2 or raw.get("instance") != cfg.instance_id or raw.get("prefix") != cfg.sandbox_prefix:
        raise PolicyError("baseline_instance_or_schema_mismatch")
    if raw.get("config_digest") != digest({k: str(v) for k, v in cfg.__dict__.items()}):
        raise PolicyError("config_changed_since_baseline")
    if not raw.get("docker_daemon_id") or not isinstance(raw.get("containers"), list):
        raise PolicyError("invalid_baseline")
    raw["protected_ids"] = {x["id"] for x in raw["containers"]}
    raw["protected_names"] = {x["name"] for x in raw["containers"]}
    return raw
