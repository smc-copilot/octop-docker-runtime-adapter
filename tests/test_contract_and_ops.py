import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docker.models.containers import ContainerCollection
from octop_docker_runtime_adapter.baseline import load_baseline, write_new
from octop_docker_runtime_adapter.cli import prepare_dirs, snapshot
from octop_docker_runtime_adapter.compat import evidence
from octop_docker_runtime_adapter.config import Config, PolicyError
from octop_docker_runtime_adapter.hooks import Finder
from octop_docker_runtime_adapter.locking import user_lock
from octop_docker_runtime_adapter.runtime import Runtime, digest


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config("instance", "octop_bc01", self.root / "users", self.root / "runtime", self.root / "state", self.root / "logs/a.log", lock_timeout=1)
        prepare_dirs(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_sdk_translates_user_mounts_and_limits(self):
        self.assertEqual(importlib.metadata.version("docker"), "7.2.0")
        api = SimpleNamespace(_version="1.57", create_container=Mock(return_value={"Id": "new"}),
                              inspect_container=Mock(return_value={"Id": "new"}), start=Mock())
        client = SimpleNamespace(api=api)
        client.containers = ContainerCollection(client=client)
        rt = Runtime(self.cfg, {}, Mock())
        ident = rt.identity("owner-1", "alice", "A1")
        profile = {"image": "python:3.12-slim", "image_id": "sha256:x", "memory": 2147483648,
                   "nano_cpus": 2000000000, "pids_limit": 512, "network": self.cfg.network_name}
        rt.create(SimpleNamespace(_client=client, _image="python:3.12-slim"), ident, profile)
        kwargs = api.create_container.call_args.kwargs
        host = kwargs["host_config"]
        self.assertEqual(host["NetworkMode"], self.cfg.network_name)
        self.assertEqual((host["Memory"], host["NanoCpus"], host["PidsLimit"]), (2147483648, 2000000000, 512))
        self.assertEqual(set(host["Binds"]), {f"{s}:{t['bind']}:rw" for s, t in rt.mounts(ident).items()})
        self.assertEqual(kwargs["working_dir"], str(self.cfg.container_workspace_root))
        self.assertEqual(kwargs["networking_config"], {self.cfg.network_name: None})
        api.create_container.assert_called_once()
        api.start.assert_called_once_with("new")

    def test_compatibility_matches_reviewed_real_distribution_files(self):
        info = evidence()
        self.assertEqual(len(info["hashes"]), 6)
        self.assertEqual(info["versions"]["octop-harness"], "1.0.11")

    def test_lazy_import_hook_runs_after_module_definitions(self):
        (self.root / "dra_import_fixture.py").write_text("class Subject: pass\n")
        finder = Finder({"dra_import_fixture": lambda module: setattr(module.Subject, "patched", True)})
        sys.path.insert(0, str(self.root))
        sys.meta_path.insert(0, finder)
        try:
            module = importlib.import_module("dra_import_fixture")
            self.assertTrue(module.Subject.patched)
        finally:
            sys.meta_path.remove(finder)
            sys.path.remove(str(self.root))
            sys.modules.pop("dra_import_fixture", None)

    def test_cross_process_lock_enforces_timeout(self):
        data = {k: str(v) if isinstance(v, Path) else v for k, v in self.cfg.__dict__.items()}
        config = self.root / "config.json"
        config.write_text(json.dumps(data))
        code = """import sys
from octop_docker_runtime_adapter.config import Config, PolicyError
from octop_docker_runtime_adapter.locking import user_lock
try:
    with user_lock(Config.load(sys.argv[1]), 'owner'):
        print('acquired')
except PolicyError as exc:
    print(str(exc)); sys.exit(3)
"""
        with user_lock(self.cfg, "owner"):
            result = subprocess.run([sys.executable, "-c", code, str(config)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("user_lock_timeout", result.stdout)
        result = subprocess.run([sys.executable, "-c", code, str(config)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_baseline_write_never_overwrites(self):
        path = self.root / "baseline.json"
        write_new(path, {"keep": True})
        with self.assertRaises(FileExistsError): write_new(path, {"keep": False})
        self.assertEqual(json.loads(path.read_text()), {"keep": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_empty_environment_snapshot_is_allowed_and_config_drift_blocked(self):
        c = Mock()
        c.__enter__ = Mock(return_value=c)
        c.__exit__ = Mock(return_value=False)
        c.containers.list.return_value = []
        c.info.return_value = {"ID": "daemon"}
        with patch("octop_docker_runtime_adapter.cli.client", return_value=c):
            snapshot(self.cfg)
        baseline = load_baseline(self.cfg)
        self.assertEqual(baseline["protected_names"], set())
        raw = json.loads(self.cfg.baseline_file.read_text())
        raw["config_digest"] = "wrong"
        self.cfg.baseline_file.write_text(json.dumps(raw))
        with self.assertRaises(PolicyError): load_baseline(self.cfg)

    def test_sitecustomize_validates_before_enabling_and_aborts_bad_config(self):
        cfg_path = self.root / "adapter.json"
        cfg_path.write_text(json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in self.cfg.__dict__.items()}))
        baseline = {"schema": 2, "instance": self.cfg.instance_id, "prefix": self.cfg.sandbox_prefix,
                    "config_digest": digest({k: str(v) for k, v in self.cfg.__dict__.items()}),
                    "docker_daemon_id": "daemon", "compatibility": evidence(), "containers": []}
        write_new(self.cfg.baseline_file, baseline)
        script = self.root / "octop"
        script.write_text("from octop_docker_runtime_adapter.bootstrap import _runtime\nprint(_runtime is not None)\n")
        env = {**os.environ, "OCTOP_DRA_ENABLED": "1", "OCTOP_DRA_CONFIG": str(cfg_path)}
        env.pop("OCTOP_IN_CONTAINER", None)
        result = subprocess.run([sys.executable, str(script), "run"], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")
        self.assertIn("adapter_hooks_registered", result.stderr)
        cfg_path.write_text('{}')
        result = subprocess.run([sys.executable, str(script), "run"], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 78)
        self.assertNotIn("True", result.stdout)
        self.assertIn("bootstrap_failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
