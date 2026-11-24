"""Enable only for the explicitly configured Octop server process."""
import os as _os
import sys as _sys

if (_os.environ.get("OCTOP_DRA_ENABLED") == "1"
        and _os.path.basename(_sys.argv[0]) == "octop" and "run" in _sys.argv[1:]):
    try:
        from octop_docker_runtime_adapter.bootstrap import install
        install()
    except Exception as _exc:
        _sys.stderr.write("OCTOP_DRA bootstrap_failed " + type(_exc).__name__ + "\n")
        _sys.stderr.flush()
        # Python normally swallows sitecustomize exceptions. Refuse an unadapted
        # server in this explicitly enabled profile instead of silently losing mounts.
        _os._exit(78)
