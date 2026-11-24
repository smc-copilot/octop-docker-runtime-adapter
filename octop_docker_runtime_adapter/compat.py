from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

from .config import PolicyError

VERSIONS = {"octop": "1.0.3b7", "octop-harness": "1.0.11", "docker": "7.2.0"}
FILES = {
    "octop": ["octop/infra/agents/manager.py", "octop/infra/backend/docker_spec.py", "octop/infra/backend/probe.py"],
    "octop-harness": ["octop_harness/backends/docker_sandbox.py", "octop_harness/backends/__init__.py"],
    "docker": ["docker/models/containers.py"],
}


def _package_root(dist_name):
    """Root that holds ``octop/``, ``octop_harness/`` or ``docker/``.

    Editable installs (``pip install -e``) keep the package outside the dist-info
    directory, so ``Distribution.locate_file`` cannot resolve modules. Resolve the
    real import location first; the reviewed SHA-256 comparison below is unchanged.
    """
    import importlib.util

    module = dist_name.replace("-", "_")
    spec = importlib.util.find_spec(module)
    if spec and spec.submodule_search_locations:
        first = next(iter(spec.submodule_search_locations))
        return Path(first).resolve().parent
    return Path(importlib.metadata.distribution(dist_name).locate_file("")).resolve()


def evidence():
    versions = {key: importlib.metadata.version(key) for key in VERSIONS}
    if versions != VERSIONS or sys.version_info[:2] != (3, 12):
        raise PolicyError("unsupported_versions_require_code_review")
    hashes = {}
    for dist_name, paths in FILES.items():
        dist = importlib.metadata.distribution(dist_name)
        root = _package_root(dist_name)
        for path in paths:
            candidate = root / path
            if not candidate.is_file():
                candidate = dist.locate_file(path)
            hashes[path] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    expected = json.loads((Path(__file__).parent.parent / "config/reviewed-source-hashes.json").read_text())
    if hashes != expected:
        raise PolicyError("installed_source_differs_from_reviewed_contract")
    return {"versions": versions, "hashes": hashes, "python_major_minor": "3.12"}
