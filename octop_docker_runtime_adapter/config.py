from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = "/data/octop-docker-runtime-adapter/config/adapter.json"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class PolicyError(RuntimeError):
    """Public, non-secret diagnostic code."""


def private_dir(path: Path, create=False):
    """No symlink traversal, recursive chmod, or changes to existing ownership."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PolicyError("directory_requires_service_owner_and_0700")
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Config:
    instance_id: str
    sandbox_prefix: str
    workspace_root: Path
    container_workspace_root: Path
    state_dir: Path
    log_file: Path
    network_name: str = "octop-agent-egress"
    shared_target: str = "/opt/octop-user"
    lock_timeout: int = 30

    @property
    def baseline_file(self):
        return self.state_dir / "baseline.json"

    @classmethod
    def load(cls, filename=None):
        data = json.loads(Path(filename or os.environ.get("OCTOP_DRA_CONFIG", DEFAULT_CONFIG)).read_text())
        if set(data) - set(cls.__dataclass_fields__):
            raise PolicyError("unknown_config_key")
        for key in ("workspace_root", "container_workspace_root", "state_dir", "log_file"):
            data[key] = Path(data[key])
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self):
        for value in (self.instance_id, self.sandbox_prefix, self.network_name):
            if not TOKEN.fullmatch(value):
                raise PolicyError("invalid_instance_prefix_or_network")
        if self.network_name in {"bridge", "host", "none"}:
            raise PolicyError("named_bridge_required")
        paths = (self.workspace_root, self.container_workspace_root, self.state_dir,
                 self.log_file.parent, Path(self.shared_target))
        for path in paths:
            if not path.is_absolute() or path == Path("/") or ".." in path.parts or str(path) != str(path.resolve()):
                raise PolicyError("absolute_canonical_nonsymlink_paths_required")
        # The content mount MUST NOT expose host sessions, credentials or runtime state.
        def overlap(a, b):
            return a == b or a in b.parents or b in a.parents
        if overlap(self.workspace_root, self.container_workspace_root) or overlap(self.workspace_root, self.state_dir):
            raise PolicyError("workspace_runtime_and_state_must_be_separate")
        if overlap(Path(self.shared_target), self.container_workspace_root):
            raise PolicyError("container_mount_targets_overlap")
        if type(self.lock_timeout) is not int or not 1 <= self.lock_timeout <= 120:
            raise PolicyError("invalid_lock_timeout")
