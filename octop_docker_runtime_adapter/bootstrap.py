from __future__ import annotations

import os

from . import __version__
from .audit import Audit
from .baseline import load_baseline
from .compat import evidence
from .config import Config, PolicyError, private_dir
from .hooks import install_hooks
from .runtime import Runtime

_runtime = None


def install():
    global _runtime
    if _runtime is not None:
        return _runtime
    cfg = Config.load()
    if os.environ.get("OCTOP_IN_CONTAINER", "").lower() in {"1", "true", "yes"}:
        raise PolicyError("host_install_must_not_set_OCTOP_IN_CONTAINER")
    compatibility = evidence()
    baseline = load_baseline(cfg)
    if baseline["compatibility"] != compatibility:
        raise PolicyError("baseline_compatibility_changed")
    for path in (cfg.workspace_root, cfg.state_dir, cfg.state_dir / "users", cfg.state_dir / "locks", cfg.log_file.parent):
        private_dir(path)
    audit = Audit(cfg.log_file)
    runtime = Runtime(cfg, baseline, audit)
    install_hooks(runtime)
    _runtime = runtime
    audit.emit("adapter_hooks_registered", version=__version__, instance=cfg.instance_id, prefix=cfg.sandbox_prefix)
    return runtime
