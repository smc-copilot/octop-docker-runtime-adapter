import concurrent.futures
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import docker

from octop_docker_runtime_adapter.cli import prepare_dirs
from octop_docker_runtime_adapter.config import Config, PolicyError
from octop_docker_runtime_adapter.hooks import patch_manager, patch_probe, patch_sandbox
from octop_docker_runtime_adapter.runtime import LABEL, Runtime
from support import fake_client, harness_class


class UserRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config("instance", "octop_bc01", root / "users", root / "host-runtime", root / "state", root / "logs/adapter.log")
        prepare_dirs(self.cfg)
        self.baseline = {"docker_daemon_id": "daemon", "protected_ids": set(), "protected_names": set()}
        self.audit = Mock()
        self.rt = Runtime(self.cfg, self.baseline, self.audit)
        self.client = fake_client(self.cfg)
        self.cls = harness_class()
        patch_sandbox(self.cls, self.rt)
        self.boxes = []

    def tearDown(self):
        for box in self.boxes:
            box.close()
        self.tmp.cleanup()

    def args(self, aid="A1", username="alice", owner=1, **extra):
        spec = {"type": "docker", "sandbox_scope": "user", "sandbox_prefix": self.cfg.sandbox_prefix,
                "image": "python:3.12-slim", "allow_network": True, "memory": "2g", "cpus": 2, "pids_limit": 512, **extra}
        data = self.rt.authorize(spec, owner, username, aid)
        data.pop("type")
        return {**data, "workspace_dir": str(self.cfg.container_workspace_root / aid), "client": self.client}

    def build(self, aid="A1", username="alice", owner=1, **extra):
        box = self.cls(**self.args(aid, username, owner, **extra))
        self.boxes.append(box)
        return box

    def test_two_experts_share_one_user_container_with_separate_exec_workdir(self):
        a, b = self.build("A1"), self.build("A2")
        self.assertIs(a._container, b._container)
        self.assertEqual(self.client.containers.run_count, 1)
        self.assertNotEqual(a._workspace_root, b._workspace_root)
        a.execute("pwd")
        self.assertEqual(self.client.api.exec_create.call_args.kwargs["workdir"], a._workspace_root)
        b.execute("pwd")
        self.assertEqual(self.client.api.exec_create.call_args.kwargs["workdir"], b._workspace_root)
        attrs = a._container.attrs
        self.assertNotIn("harness.agent.id", attrs["Config"]["Labels"])
        self.assertEqual(attrs["Config"]["WorkingDir"], str(self.cfg.container_workspace_root))
        self.assertEqual(len(attrs["Mounts"]), 2)

    def test_late_third_expert_keeps_same_mounts_and_container(self):
        a = self.build()
        mounts = copy.deepcopy(a._container.attrs["Mounts"])
        b = self.build("A3")
        self.assertEqual(a._container.id, b._container.id)
        self.assertEqual(b._container.attrs["Mounts"], mounts)

    def test_different_users_different_sources_and_containers(self):
        a, b = self.build(), self.build("B1", "bob", 2)
        self.assertNotEqual(a._container.id, b._container.id)
        self.assertNotEqual(a._container.attrs["Mounts"][0]["Source"], b._container.attrs["Mounts"][0]["Source"])

    def test_parallel_first_acquire_single_create(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            boxes = list(pool.map(lambda i: self.build("A" + str(i)), range(8)))
        self.assertEqual(len({b._container.id for b in boxes}), 1)
        self.assertEqual(self.client.containers.run_count, 1)

    def test_close_and_destroy_do_not_remove_shared_container(self):
        a, b = self.build(), self.build("A2")
        a.close()
        self.assertEqual(b.execute("true").exit_code, 0)
        with self.assertRaisesRegex(PolicyError, "user_level_maintenance"):
            b.destroy()
        self.assertEqual(b._container.removes, 0)

    def test_failed_start_never_renames_or_recreates(self):
        a = self.build()
        obj = a._container
        obj.attrs["State"]["Status"] = "exited"
        obj.fail_start = True
        with self.assertRaises(docker.errors.APIError):
            self.build("A2")
        self.assertEqual((obj.renames, obj.removes, self.client.containers.run_count), (0, 0, 1))

    def test_missing_container_rebind_uses_same_sources(self):
        a = self.build()
        old = a._container
        mounts = copy.deepcopy(old.attrs["Mounts"])
        self.client.containers.boxes.clear()
        self.assertEqual(a.execute("true").exit_code, 0)
        self.assertNotEqual(old.id, a._container.id)
        self.assertEqual(a._container.attrs["Mounts"], mounts)

    def test_sdk_failure_no_unmounted_retry(self):
        self.client.containers.fail_run = True
        with self.assertRaises(docker.errors.APIError):
            self.build()
        self.assertEqual(self.client.containers.run_count, 1)

    def test_409_race_only_accepts_exact_managed_container(self):
        import requests
        real_run = self.client.containers.run
        def raced(**kwargs):
            real_run(**kwargs)
            response = requests.Response()
            response.status_code = 409
            raise docker.errors.APIError("conflict", response=response)
        self.client.containers.run = raced
        box = self.build()
        self.assertTrue(box._reused)
        self.assertEqual(self.client.containers.run_count, 1)

    def test_agent_scope_routes_to_original_lifecycle(self):
        # No global Docker SDK patch; agent/fixed modes call the original methods.
        calls = []
        class Native:
            def __init__(self, **kwargs):
                self._auto_remove = False
                calls.append("init")
            def _acquire_container(self): calls.append("acquire")
            def _rebind_if_missing(self): calls.append("rebind")
            def destroy(self): calls.append("destroy")
        patch_sandbox(Native, self.rt)
        obj = Native(sandbox_scope="agent", sandbox_prefix=self.cfg.sandbox_prefix)
        obj._acquire_container(); obj._rebind_if_missing(); obj.destroy()
        self.assertEqual(calls, ["init", "acquire", "rebind", "destroy"])

    def test_volume_drift_blocks_before_start(self):
        a = self.build()
        a._container.attrs["Mounts"][0]["Source"] = "/foreign/user"
        a._container.attrs["State"]["Status"] = "exited"
        with self.assertRaises(PolicyError): self.build("A2")
        self.assertEqual(a._container.starts, 0)

    def test_native_build_docker_preserves_internal_binding_and_workspace(self):
        import ast
        import importlib.metadata
        import sys
        from unittest.mock import patch
        source = importlib.metadata.distribution("octop-harness").locate_file("octop_harness/backends/__init__.py").read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_build_docker")
        scope = {}
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "<native-build-docker>", "exec"), scope)
        args = self.args()
        ws = args.pop("workspace_dir")
        with patch.dict(sys.modules, {"octop_harness.backends.docker_sandbox": SimpleNamespace(DockerSandbox=self.cls)}):
            box = scope["_build_docker"](args, workspace_dir=ws)
        self.boxes.append(box)
        self.assertEqual(box._dra_identity.owner_id, "1")
        self.assertEqual(box._workspace_root, ws)

    def test_existing_unmanaged_container_is_not_started(self):
        a = self.build()
        obj = a._container
        obj.attrs["Config"]["Labels"] = {"harness.agent.sandbox_scope": "user"}
        obj.attrs["State"]["Status"] = "exited"
        with self.assertRaises(PolicyError):
            self.build("A2")
        self.assertEqual((obj.starts, obj.renames, obj.removes), (0, 0, 0))

    def test_protected_name_blocks_even_when_old_container_missing(self):
        self.baseline["protected_names"].add("octop_bc01_alice")
        with self.assertRaisesRegex(PolicyError, "migration"):
            self.build()
        self.assertEqual(self.client.containers.run_count, 0)

    def test_user_limits_network_and_image_conflicts_fail(self):
        self.build()
        for change in ({"memory": "3g"}, {"cpus": 3}, {"pids_limit": 256}, {"allow_network": False}, {"image": "different"}):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                self.build("A2", **change)
        self.assertEqual(self.client.containers.run_count, 1)

    def test_allow_network_false_is_preserved(self):
        a = self.build(allow_network=False)
        self.assertEqual(a._container.attrs["HostConfig"]["NetworkMode"], "none")
        self.assertNotIn("network", self.client.containers.calls[0])

    def test_container_drift_blocks_execute(self):
        a = self.build()
        a._container.attrs["NetworkSettings"]["Networks"]["extra"] = {}
        with self.assertRaises(PolicyError):
            a.execute("true")
        self.client.api.exec_create.assert_not_called()

    def test_same_username_different_database_owner_rejected(self):
        self.build()
        with self.assertRaises(PolicyError):
            self.build("B1", owner=2)
        self.assertEqual(self.client.containers.run_count, 1)

    def test_username_rename_cannot_create_second_container_for_same_user(self):
        self.build()
        with self.assertRaisesRegex(PolicyError, "user_name_or_profile_changed"):
            self.build("A2", username="alice_new")
        self.assertEqual(self.client.containers.run_count, 1)

    def test_identity_and_path_rejections(self):
        for username in ("a/b", "中文", " alice", "a b", "", "a" * 65):
            with self.subTest(username=username), self.assertRaises(PolicyError):
                self.build(username=username)
        with self.assertRaises(PolicyError): self.build(owner=None)
        for change in ({"auto_remove": True}, {"workspace_path": "/tmp/a"}, {"container_name": "fixed"},
                       {"sandbox_id": "fixed"}, {"volumes": {"/": {"bind": "/host"}}}):
            with self.subTest(change=change), self.assertRaises(PolicyError): self.build(**change)
        args = self.args()
        args["workspace_dir"] = "/unexpected"
        with self.assertRaises(PolicyError): self.cls(**args)
        args = self.args()
        args["_dra_signature"] = "forged"
        with self.assertRaises(PolicyError): self.cls(**args)
        self.assertEqual(self.client.containers.run_count, 0)

    def test_manager_uses_owner_not_supplied_username_and_adds_agent_id(self):
        class Manager:
            def _prepare_docker_backend(self, backend, row):
                return dict(backend)
        patch_manager(Manager, self.rt)
        m = Manager()
        m._repos = SimpleNamespace(user_repo=SimpleNamespace(get=lambda uid: SimpleNamespace(username="alice")))
        row = SimpleNamespace(user_id=27, agent_id="A27")
        spec = {"type": "docker", "sandbox_scope": "user", "sandbox_prefix": self.cfg.sandbox_prefix}
        out = m._prepare_docker_backend(spec, row)
        self.assertEqual((out["username"], out["agent_id"], out["_dra_owner_id"]), ("alice", "A27", "27"))
        with self.assertRaises(PolicyError): m._prepare_docker_backend({**spec, "username": "bob"}, row)
        with self.assertRaises(PolicyError): m._prepare_docker_backend(spec, SimpleNamespace(user_id=None, agent_id="PUBLIC"))

    def test_probe_uses_unique_fixed_name_and_no_user_mounts(self):
        spec = {"type": "docker", "sandbox_scope": "user", "sandbox_prefix": self.cfg.sandbox_prefix,
                "username": "alice", "container_name": "octop_bc01_alice", "volumes": {"/bad": "/bad"}}
        module = SimpleNamespace(_docker_probe_spec=lambda row: spec)
        patch_probe(module, self.rt)
        a, b = module._docker_probe_spec(None), module._docker_probe_spec(None)
        self.assertEqual(a["sandbox_scope"], "fixed")
        self.assertNotEqual(a["sandbox_id"], b["sandbox_id"])
        self.assertNotIn("container_name", a)
        self.assertNotIn("volumes", a)
        self.assertTrue(a["auto_remove"])

    def test_source_symlink_does_not_expose_foreign_host_tree(self):
        ident = self.rt.identity(1, "alice", "A1")
        (self.cfg.workspace_root / ident.key).symlink_to(self.cfg.state_dir, target_is_directory=True)
        with self.assertRaises((OSError, PolicyError)):
            self.build()
        self.assertEqual(self.client.containers.run_count, 0)


if __name__ == "__main__":
    unittest.main()
