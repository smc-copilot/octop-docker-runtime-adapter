"""Real daemon smoke. Only containers in a random isolated test namespace are removed."""
from __future__ import annotations

import argparse
import json
import shlex
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from .audit import Audit
from .baseline import load_baseline
from .cli import assert_legacy, client, prepare_dirs
from .compat import evidence
from .config import Config, PolicyError
from .hooks import patch_sandbox
from .runtime import LABEL, Runtime


def run(cfg, image, url):
    evidence()
    baseline = load_baseline(cfg)
    assert_legacy(cfg)
    token = uuid.uuid4().hex[:12]
    test_cfg = replace(cfg, instance_id="smoke-" + token, sandbox_prefix="dra_smoke_" + token,
                       state_dir=cfg.state_dir / "smoke" / token,
                       workspace_root=cfg.workspace_root / ("smoke_" + token))
    prepare_dirs(test_cfg)
    rt = Runtime(test_cfg, baseline, Audit())
    import docker
    from octop_harness.backends.docker_sandbox import DockerSandbox
    patch_sandbox(DockerSandbox, rt)
    boxes = []
    with client() as c:
        # No image pulls or installs during smoke; use the reviewed local image.
        c.images.get(image)
        def build(owner, username, aid):
            args = rt.authorize({"type": "docker", "sandbox_scope": "user", "sandbox_prefix": test_cfg.sandbox_prefix,
                                 "image": image, "allow_network": True, "memory": "2g", "cpus": 2,
                                 "pids_limit": 512}, owner, username, aid)
            args.pop("type")
            box = DockerSandbox(**args, workspace_dir=str(cfg.container_workspace_root / aid), client=c)
            boxes.append(box)
            return box
        def clean_container(obj):
            obj.reload()
            labels = obj.attrs.get("Config", {}).get("Labels") or {}
            if (labels.get(LABEL + ".instance") != test_cfg.instance_id
                    or labels.get(LABEL + ".version") != "2.0.0"
                    or not obj.name.startswith(test_cfg.sandbox_prefix + "_")):
                raise PolicyError("smoke_cleanup_identity_mismatch")
            obj.remove(force=True)
        def execute(box, command):
            result = box.execute(command, timeout=60)
            if result.exit_code != 0:
                raise PolicyError("smoke_command_failed")
            return result.output.strip()
        result = {}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fa = pool.submit(build, "smoke-owner-a", "alice", "SMOKE_A1")
                fb = pool.submit(build, "smoke-owner-a", "alice", "SMOKE_A2")
                a, b = fa.result(), fb.result()
            other = build("smoke-owner-b", "bob", "SMOKE_B1")
            assert a._container.id == b._container.id != other._container.id
            for box, mark in ((a, "alpha"), (b, "beta")):
                command = "python -c " + shlex.quote("from pathlib import Path; Path('persist.txt').write_text(" + repr(mark) + ")")
                execute(box, command)
            assert execute(a, "pwd") != execute(b, "pwd")
            # A second expert, created after the container exists, needs no new mount.
            before = a._container.id
            third = build("smoke-owner-a", "alice", "SMOKE_A3")
            assert third._container.id == before
            execute(third, "python -c \"from pathlib import Path; Path('late.txt').write_text('late')\"")
            shared = test_cfg.shared_target
            execute(a, "python -m venv " + shlex.quote(shared + "/venvs/default"))
            py = shlex.quote(shared + "/venvs/default/bin/python")
            execute(a, py + " -c " + shlex.quote("import sysconfig; from pathlib import Path; Path(sysconfig.get_paths()['purelib'], 'dra_smoke_module.py').write_text('VALUE=42\\n')"))
            assert execute(b, py + " -c 'import dra_smoke_module; print(dra_smoke_module.VALUE)'") == "42"
            code = "import urllib.request; print(urllib.request.urlopen(" + repr(url) + ", timeout=20).status)"
            assert execute(a, "python -c " + shlex.quote(code)).startswith("2")
            # Logical content visibility between experts of SAME user is intended.
            assert execute(b, "cat " + shlex.quote(a._workspace_root + "/persist.txt")) == "alpha"
            assert execute(other, "test ! -e " + shlex.quote(a._workspace_root + "/persist.txt") + " && printf isolated") == "isolated"
            # close() leaves the shared container running. destroy() is refused.
            a.close()
            assert execute(b, "cat persist.txt") == "beta"
            try:
                b.destroy()
            except PolicyError:
                pass
            else:
                raise AssertionError("destroy must be blocked")
            # Only delete this dedicated smoke container to prove persistence.
            clean_container(b._container)
            rebuilt = build("smoke-owner-a", "alice", "SMOKE_A1")
            assert rebuilt._container.id != before
            assert execute(rebuilt, "cat persist.txt") == "alpha"
            assert execute(b, "cat persist.txt") == "beta"  # existing wrapper rebinds
            assert execute(rebuilt, py + " -c 'import dra_smoke_module; print(dra_smoke_module.VALUE)'") == "42"
            result = {"smoke_passed": True, "run": token, "same_user_shared": True, "different_users_separate": True,
                      "parallel_initial_create": True, "late_agent_same_container": True,
                      "workspaces_and_shared_venv_survive_recreate": True, "close_keeps_container": True,
                      "destroy_blocked": True, "https": True, "evidence_root": str(test_cfg.workspace_root)}
        finally:
            for box in boxes:
                box.close()
            # Recover containers even if construction failed after create/start.
            for obj in c.containers.list(all=True, filters={"label": LABEL + ".instance=" + test_cfg.instance_id}):
                clean_container(obj)
    assert_legacy(cfg)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--url", default="https://api.github.com/zen")
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        raise SystemExit("smoke requires an approved unauthenticated HTTPS URL")
    try:
        print(json.dumps(run(Config.load(args.config), args.image, args.url), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"smoke_passed": False, "reason": str(exc) if isinstance(exc, PolicyError) else type(exc).__name__}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
