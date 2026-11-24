import copy
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from octop_docker_runtime_adapter.baseline import write_new
from octop_docker_runtime_adapter.cli import prepare_dirs
from octop_docker_runtime_adapter.config import Config, PolicyError
from octop_docker_runtime_adapter.runtime import Runtime, digest
from octop_docker_runtime_adapter.workspace_batch import (
    Service, build_plan, config_digest, execute_batch, rollback_batch, validate_plan, verify_fresh,
)
from octop_docker_runtime_adapter.workspace_files import (
    exchange_directories, tree_manifest, zip_manifest,
)
from octop_docker_runtime_adapter.workspace_source import catalog, readonly_database
from support import fake_client


class FakeService:
    unit, scope = "octop.service", "user"
    def __init__(self, active=True):
        self.running, self.calls = active, []
    def active(self):
        return self.running
    def stop(self):
        self.calls.append("stop")
        self.running = False
    def start(self):
        self.calls.append("start")
        self.running = True


class WorkspaceBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = Config("octop-bc01", "octop_sandbox_bc01", self.root / "users",
                          self.root / "runtime", self.root / "state", self.root / "logs/a.log")
        prepare_dirs(self.cfg)
        self.baseline = {"schema": 2, "instance": self.cfg.instance_id, "prefix": self.cfg.sandbox_prefix,
                         "config_digest": config_digest(self.cfg), "docker_daemon_id": "daemon",
                         "containers": [], "compatibility": {}}
        write_new(self.cfg.baseline_file, self.baseline)
        self.client = fake_client(self.cfg)
        self.rt = Runtime(self.cfg, {**self.baseline, "protected_ids": set(), "protected_names": set()}, None)
        self.spec = {"type": "docker", "sandbox_scope": "user", "sandbox_prefix": self.cfg.sandbox_prefix,
                     "image": "python:3.12-slim", "allow_network": True, "memory": "2g", "cpus": 2, "pids_limit": 512}
        self.rows = [self.agent("A1", 1, "alice"), self.agent("B1", 2, "bob")]
        self.cat = {"template_id": "published-123", "template_kind": "published",
                    "template": {"name": "超级助手", "source_agent_id": "SOURCE"}, "agents": self.rows}
        self.archive = self.make_zip({"AGENTS.md": b"new instructions", ".octop/skills/demo/SKILL.md": b"skill",
                                      "vendor/test.bin": bytes(range(256)), "scripts/run.py": b"print(42)"})
        self.source = {"octop_home": str(self.root), "sqlite_path": None,
                       "service_unit": "octop.service", "service_scope": "user", "agent_ids": []}
        self.service = FakeService()

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, aid, owner, username):
        ident = self.rt.identity(owner, username, aid)
        box = SimpleNamespace(_client=self.client, _image="python:3.12-slim", _allow_network=True,
                              _memory="2g", _cpus=2, _pids_limit=512, _auto_remove=False, _volumes=None)
        profile = self.rt.profile(box)
        self.rt.register(ident, profile)
        self.rt.create(box, ident, profile)
        target = self.cfg.workspace_root / ident.key / "agents" / aid
        target.mkdir()
        (target / "AGENTS.md").write_text("old instructions")
        (target / "hello.md").write_text("keep me")
        return {"agent_id": aid, "user_id": owner, "username": username, "name": aid,
                "kind": "expert", "enabled": 1, "last_state": "running", "backend": self.spec.copy(),
                "workspace_dir": str(self.cfg.container_workspace_root / aid)}

    def make_zip(self, files):
        path = self.root / "超级助手.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return zip_manifest(path)

    def plan(self):
        return build_plan(self.cfg, self.cat, self.client, archive=self.archive, source=self.source)

    def refresh(self):
        return build_plan(self.cfg, self.cat, self.client, source=self.source)

    def apply(self, plan=None, manage=True):
        return execute_batch(self.cfg, plan or self.plan(), self.client, self.service, os.geteuid(),
                             manage_runtime=manage, refresh=self.refresh)

    def test_owner_hash_mount_and_directory_mapping(self):
        plan = self.plan()
        self.assertEqual(plan["ready_count"], 2)
        a, b = plan["targets"]
        self.assertNotEqual(a["user_key"], b["user_key"])
        self.assertEqual(a["user_key"], "u_" + digest(["octop-bc01", "1"])[:32])
        self.assertTrue(a["host_workspace_dir"].endswith("/agents/A1"))
        self.assertEqual(a["adapter_hash"], a["user_key"][2:])

    def test_plan_does_not_modify_expert_files(self):
        before = tree_manifest(self.cfg.workspace_root)
        self.plan()
        self.assertEqual(before, tree_manifest(self.cfg.workspace_root))

    def test_full_overlay_binary_hidden_and_extra_files_preserved(self):
        plan = self.plan()
        neighbor = Path(plan["targets"][0]["host_workspace_dir"]).parent / "NEIGHBOR"
        neighbor.mkdir()
        (neighbor / "data").write_text("unselected")
        path, report = self.apply(plan)
        self.assertEqual(report["status"], "completed", report)
        self.assertEqual(self.service.calls, ["stop", "start"])
        self.assertTrue(report["runtime_resumed"])
        for target in plan["targets"]:
            root = Path(target["host_workspace_dir"])
            self.assertEqual((root / "hello.md").read_text(), "keep me")
            self.assertEqual((root / "AGENTS.md").read_text(), "new instructions")
            self.assertEqual((root / ".octop/skills/demo/SKILL.md").read_text(), "skill")
            self.assertEqual((root / "vendor/test.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((neighbor / "data").read_text(), "unselected")
        self.assertTrue(path.is_file())

    def test_rollback_restores_original_trees(self):
        plan = self.plan()
        before = [tree_manifest(Path(t["host_workspace_dir"])) for t in plan["targets"]]
        path, _ = self.apply(plan)
        _, report = rollback_batch(self.cfg, path, self.client, self.service, os.geteuid(),
                                   manage_runtime=True, refresh=self.refresh)
        self.assertEqual(report["status"], "rolled_back", report)
        self.assertEqual(before, [tree_manifest(Path(t["host_workspace_dir"])) for t in plan["targets"]])

    def test_rollback_refuses_later_edits_and_override_backs_them_up(self):
        plan = self.plan()
        path, _ = self.apply(plan)
        target = Path(plan["targets"][-1]["host_workspace_dir"])
        (target / "new-user-data").write_text("after rollout")
        _, report = rollback_batch(self.cfg, path, self.client, self.service, os.geteuid(),
                                   manage_runtime=True, refresh=self.refresh)
        self.assertEqual(report["status"], "rollback_failed")
        self.assertTrue((target / "new-user-data").exists())
        _, report = rollback_batch(self.cfg, path, self.client, self.service, os.geteuid(),
                                   manage_runtime=True, overwrite_changed=True, refresh=self.refresh)
        self.assertEqual(report["status"], "rolled_back", report)
        saved = Path(report["results"][-1]["before_rollback_backup"])
        self.assertEqual((saved / "new-user-data").read_text(), "after rollout")

    def test_stopped_service_and_container_not_started(self):
        self.service.running = False
        for obj in self.client.containers.boxes.values():
            obj.stop()
        _, report = self.apply()
        self.assertEqual(report["status"], "completed", report)
        self.assertEqual(self.service.calls, [])
        self.assertTrue(all(obj.status == "exited" for obj in self.client.containers.boxes.values()))

    def test_no_maintenance_flag_refuses_running_service(self):
        before = tree_manifest(self.cfg.workspace_root)
        _, report = self.apply(manage=False)
        self.assertEqual(report["error"], "stop_octop_service_or_use_stop_runtime")
        self.assertEqual(before, tree_manifest(self.cfg.workspace_root))
        self.assertEqual(self.service.calls, [])

    def test_stop_failure_has_no_workspace_mutation(self):
        before = tree_manifest(self.cfg.workspace_root)
        with patch.object(self.service, "stop", side_effect=PolicyError("injected_stop_error")):
            _, report = self.apply()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(before, tree_manifest(self.cfg.workspace_root))

    def test_resume_failure_reported(self):
        with patch.object(self.service, "start", side_effect=PolicyError("injected_start_error")):
            _, report = self.apply()
        self.assertEqual(report["status"], "runtime_resume_failed")
        self.assertTrue(report["runtime_resume_errors"])

    def test_failed_exchange_keeps_original(self):
        before = tree_manifest(self.cfg.workspace_root)
        with patch("octop_docker_runtime_adapter.workspace_batch.exchange_directories",
                   side_effect=OSError("injected unsupported filesystem")):
            _, report = self.apply()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(before, tree_manifest(self.cfg.workspace_root))
        self.assertTrue(self.service.running)

    def test_failure_after_exchange_rolls_back_current_target(self):
        before = tree_manifest(self.cfg.workspace_root)
        count = 0
        def interrupted(left, right):
            nonlocal count
            count += 1
            exchange_directories(left, right)
            if count == 1:
                raise OSError("injected failure after rename")
        with patch("octop_docker_runtime_adapter.workspace_batch.exchange_directories", side_effect=interrupted):
            _, report = self.apply()
        self.assertEqual(report["status"], "failed", report)
        self.assertEqual(before, tree_manifest(self.cfg.workspace_root))

    def test_archive_changed_since_plan_refused(self):
        plan = self.plan()
        self.make_zip({"AGENTS.md": b"changed"})
        _, report = self.apply(plan)
        self.assertEqual(report["error"], "archive_changed_since_plan")
        self.assertEqual(self.service.calls, [])

    def test_plan_edit_detected(self):
        plan = self.plan()
        plan["targets"][0]["host_workspace_dir"] = "/tmp/wrong"
        with self.assertRaisesRegex(PolicyError, "plan_changed"):
            validate_plan(plan)

    def test_owner_changed_after_plan_refused(self):
        plan = self.plan()
        self.rows[0]["user_id"] = 7
        with self.assertRaisesRegex(PolicyError, "targets_backend_or_mounts_changed"):
            self.apply(plan)
        self.assertEqual(self.service.calls, [])

    def test_mount_drift_blocked(self):
        next(iter(self.client.containers.boxes.values())).attrs["Mounts"][0]["Source"] = "/tmp/wrong"
        plan = self.plan()
        self.assertEqual(plan["ready_count"], 1)
        self.assertEqual(plan["targets"][0]["error"], "container_mounts_mismatch")

    def test_wrong_backend_prefix_not_silently_restored(self):
        self.rows[0]["backend"]["sandbox_prefix"] = "other"
        self.assertEqual(self.plan()["targets"][0]["error"], "expert_not_managed_by_this_adapter")

    def test_profile_drift_blocked(self):
        self.rows[0]["backend"]["memory"] = "3g"
        self.assertEqual(self.plan()["targets"][0]["error"], "backend_profile_differs_from_registered_user")

    def test_ownerless_blocked(self):
        self.rows[0]["user_id"] = None
        self.assertFalse(self.plan()["targets"][0]["ready"])

    def test_missing_directory_not_created(self):
        target = Path(self.plan()["targets"][0]["host_workspace_dir"])
        import shutil
        shutil.rmtree(target)
        self.assertFalse(self.plan()["targets"][0]["ready"])
        self.assertFalse(target.exists())

    def test_target_symlink_blocked(self):
        target = Path(self.plan()["targets"][0]["host_workspace_dir"])
        (target / "escape").symlink_to(self.root / "outside")
        _, report = self.apply()
        self.assertEqual(report["status"], "failed")
        self.assertTrue((target / "escape").is_symlink())

    def test_file_directory_conflict_preserves_target(self):
        target = Path(self.plan()["targets"][0]["host_workspace_dir"])
        (target / "vendor").write_text("old file")
        _, report = self.apply()
        self.assertEqual(report["status"], "failed")
        self.assertEqual((target / "vendor").read_text(), "old file")

    def test_traversal_absolute_and_backslash_zip_refused(self):
        for name in ("../escape", "/absolute", "a/../../escape", "a\\b", "C:/escape"):
            with self.subTest(name=name), self.assertRaises(PolicyError):
                self.make_zip({name: b"bad"})

    def test_symlink_zip_refused(self):
        path = self.root / "bad.zip"
        info = zipfile.ZipInfo("escape")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(info, "../outside")
        with self.assertRaisesRegex(PolicyError, "zip_symlink"):
            zip_manifest(path)

    def test_expansion_limit_refused(self):
        with self.assertRaisesRegex(PolicyError, "zip_expanded_size"):
            zip_manifest(self.archive["path"], max_bytes=10)

    def test_entry_count_limit_refused(self):
        with self.assertRaisesRegex(PolicyError, "zip_entry_limit"):
            zip_manifest(self.archive["path"], max_files=1)

    def test_strip_components_is_explicit(self):
        archive = self.make_zip({"超级助手/AGENTS.md": b"ok", "超级助手/.octop/x": b"hidden"})
        stripped = zip_manifest(archive["path"], strip_components=1)
        self.assertEqual({e["path"] for e in stripped["entries"]}, {"AGENTS.md", ".octop/x"})

    def test_zip_file_directory_collision(self):
        with self.assertRaisesRegex(PolicyError, "zip_file_directory_collision"):
            self.make_zip({"a": b"file", "a/x": b"child"})

    def test_real_directory_exchange(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir(); right.mkdir()
        (left / "a").write_text("before"); (right / "b").write_text("after")
        exchange_directories(left, right)
        self.assertTrue((left / "b").exists())
        self.assertTrue((right / "a").exists())

    def test_two_experts_in_same_user_container_both_restore(self):
        row = {**self.rows[0], "agent_id": "A2", "workspace_dir": str(self.cfg.container_workspace_root / 'A2')}
        root = Path(self.plan()['targets'][0]['host_workspace_dir']).parent / 'A2'
        root.mkdir()
        (root / 'AGENTS.md').write_text('second old')
        self.rows.append(row)
        plan = self.plan()
        self.assertEqual(plan['targets'][0]['user_key'], plan['targets'][2]['user_key'])
        _, report = self.apply(plan)
        self.assertEqual(report['status'], 'completed', report)
        self.assertEqual((root / 'AGENTS.md').read_text(), 'new instructions')
        self.assertEqual(self.client.containers.run_count, 2)

    def test_cli_plan_and_apply_with_real_sqlite_and_zip(self):
        from contextlib import nullcontext, redirect_stdout, redirect_stderr
        import io
        from octop_docker_runtime_adapter.workspace_cli import main
        db = self.root / 'cli.sqlite'
        with sqlite3.connect(db) as conn:
            conn.executescript('''
              CREATE TABLE users(id INTEGER, username TEXT);
              CREATE TABLE agents(agent_id TEXT, user_id INTEGER, name TEXT, enabled INTEGER,
                kind TEXT, config_json TEXT, last_state TEXT, published_expert_id TEXT, template_name TEXT);
              CREATE TABLE published_experts(published_expert_id TEXT, slug TEXT, name TEXT, source_agent_id TEXT);
              CREATE TABLE storage_backends(name TEXT, kind TEXT, config_json TEXT, bucket TEXT);
              INSERT INTO users VALUES(1, 'alice'), (2, 'bob');
              INSERT INTO published_experts VALUES('published-123', 'super', '超级助手', 'SOURCE');
            ''')
            for row in self.rows:
                conn.execute('INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?)',
                             (row['agent_id'], row['user_id'], row['name'], 1, 'expert',
                              json.dumps({'backend': self.spec, 'workspace_dir': row['workspace_dir']}),
                              'running', 'published-123', None))
        config_path = self.root / 'adapter.json'
        config_path.write_text(json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in self.cfg.__dict__.items()}))
        plan_file = self.root / 'plan.json'
        args = ['--config', str(config_path), '--sqlite', str(db)]
        with patch('octop_docker_runtime_adapter.workspace_cli.evidence', return_value={}), \
             patch('octop_docker_runtime_adapter.workspace_cli.client', side_effect=lambda: nullcontext(self.client)), \
             patch('octop_docker_runtime_adapter.workspace_cli.Service', return_value=self.service), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            result = main(args + ['plan', '--template-id', 'published-123', '--zip', self.archive['path'], '--output', str(plan_file)])
            self.assertEqual(result, 0, err.getvalue())
            planned = json.loads(plan_file.read_text())
            self.assertEqual(planned['ready_count'], 2)
            result = main(args + ['apply', '--plan', str(plan_file), '--stop-runtime'])
            self.assertEqual(result, 0, out.getvalue() + err.getvalue())
            for target in planned['targets']:
                self.assertEqual((Path(target['host_workspace_dir']) / 'AGENTS.md').read_text(), 'new instructions')

    def test_directory_mode_and_script_execute_bit_preserved(self):
        target = Path(self.plan()['targets'][0]['host_workspace_dir'])
        (target / 'scripts').mkdir()
        (target / 'scripts/run.py').write_text('old')
        (target / 'scripts/run.py').chmod(0o750)
        target.chmod(0o750)
        original = target.stat()
        _, report = self.apply()
        self.assertEqual(report['status'], 'completed', report)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((target / 'scripts/run.py').stat().st_mode), 0o750)
        self.assertEqual((target.stat().st_uid, target.stat().st_gid), (original.st_uid, original.st_gid))

    def test_partial_batch_can_rollback_prior_successful_target(self):
        plan = self.plan()
        before = [tree_manifest(Path(t['host_workspace_dir'])) for t in plan['targets']]
        original_exchange = exchange_directories
        count = 0
        def fail_second(left, right):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('second target disk failure')
            original_exchange(left, right)
        with patch('octop_docker_runtime_adapter.workspace_batch.exchange_directories', side_effect=fail_second):
            path, report = self.apply(plan)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['results'][0]['status'], 'verified')
        _, report = rollback_batch(self.cfg, path, self.client, self.service, os.geteuid(),
                                   manage_runtime=True, refresh=self.refresh)
        self.assertEqual(report['status'], 'rolled_back', report)
        self.assertEqual(before, [tree_manifest(Path(t['host_workspace_dir'])) for t in plan['targets']])

    def test_process_exit_before_and_after_atomic_commit_is_recoverable(self):
        import multiprocessing
        for after_exchange in (False, True):
            with self.subTest(after_exchange=after_exchange):
                before = [tree_manifest(Path(t['host_workspace_dir'])) for t in self.plan()['targets']]
                batch_root = self.cfg.state_dir / 'workspace-batches'
                existing = set(batch_root.glob('*/report.json')) if batch_root.exists() else set()
                def child():
                    def abrupt(left, right):
                        if after_exchange:
                            exchange_directories(left, right)
                        os._exit(77)
                    with patch('octop_docker_runtime_adapter.workspace_batch.exchange_directories', side_effect=abrupt):
                        self.apply()
                proc = multiprocessing.get_context('fork').Process(target=child)
                proc.start()
                proc.join(10)
                if proc.is_alive():
                    proc.terminate(); proc.join()
                    self.fail('crash probe did not exit')
                self.assertEqual(proc.exitcode, 77)
                created = set(batch_root.glob('*/report.json')) - existing
                self.assertEqual(len(created), 1)
                path = created.pop()
                interrupted = json.loads(path.read_text())
                self.assertEqual(interrupted['results'][0]['status'], 'committing')
                _, report = rollback_batch(self.cfg, path, self.client, self.service, os.geteuid(),
                                           manage_runtime=True, refresh=self.refresh)
                self.assertEqual(report['status'], 'rolled_back', report)
                self.assertEqual(before, [tree_manifest(Path(t['host_workspace_dir'])) for t in report['plan']['targets']])
                for target in report['plan']['targets']:
                    self.assertEqual(list(Path(target['host_workspace_dir']).parent.glob('.dra-*')), [])

    def test_user_locks_are_released_before_service_start(self):
        import fcntl
        original = self.service.start
        def start_with_lock_check():
            for target in self.plan()['targets']:
                with open(self.cfg.state_dir / 'locks' / (target['user_key'] + '.lock'), 'rb') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
            original()
        with patch.object(self.service, 'start', side_effect=start_with_lock_check):
            _, report = self.apply()
        self.assertEqual(report['status'], 'completed', report)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "control.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.executescript('''
              CREATE TABLE users(id INTEGER, username TEXT);
              CREATE TABLE agents(agent_id TEXT, user_id INTEGER, name TEXT, enabled INTEGER,
                kind TEXT, config_json TEXT, last_state TEXT, published_expert_id TEXT, template_name TEXT);
              CREATE TABLE published_experts(published_expert_id TEXT, slug TEXT, name TEXT, source_agent_id TEXT);
              CREATE TABLE storage_backends(name TEXT, kind TEXT, config_json TEXT, bucket TEXT);
              INSERT INTO users VALUES(1, 'alice'), (2, 'bob');
              INSERT INTO published_experts VALUES('P1', 'super', '超级助手', 'SOURCE');
            ''')
            for aid, uid, published, builtin in [('A1', 1, 'P1', None), ('B1', 2, 'P1', None),
                                               ('SOURCE', 1, 'P1', None), ('OTHER', 1, 'P2', None),
                                               ('BUILTIN', 1, None, 'assistant')]:
                conn.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?)", (aid, uid, aid, 1, 'expert',
                             json.dumps({'backend': {'type': 'docker', 'sandbox_scope': 'user'}}),
                             'running', published, builtin))
    def tearDown(self):
        self.temp.cleanup()

    def test_published_id_selection_owner_join_and_source_excluded(self):
        data = catalog(self.root, 'P1', sqlite_path=self.db)
        self.assertEqual([(a['agent_id'], a['username']) for a in data['agents']], [('A1', 'alice'), ('B1', 'bob')])

    def test_builtin_template_selection(self):
        data = catalog(self.root, 'assistant', 'builtin', self.db)
        self.assertEqual([a['agent_id'] for a in data['agents']], ['BUILTIN'])

    def test_explicit_agent_subset_cannot_escape_template(self):
        with self.assertRaisesRegex(PolicyError, 'requested_agent_not_created'):
            catalog(self.root, 'P1', sqlite_path=self.db, agent_ids=['OTHER'])

    def test_unknown_published_id(self):
        with self.assertRaisesRegex(PolicyError, 'published_template_id_not_found'):
            catalog(self.root, "P1' OR 1=1 --", sqlite_path=self.db)

    def test_database_is_really_read_only(self):
        with readonly_database(self.root, self.db) as query:
            with self.assertRaises(sqlite3.OperationalError):
                query("DELETE FROM agents")
        self.assertEqual(len(catalog(self.root, 'P1', sqlite_path=self.db)['agents']), 2)

    def test_missing_database_never_created(self):
        missing = self.root / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            with readonly_database(self.root, missing):
                pass
        self.assertFalse(missing.exists())

    def test_named_docker_backend_uses_pinned_octop_conversion(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO storage_backends VALUES(?,?,?,?)", ('sandbox', 'docker',
                         json.dumps({'sandbox_scope': 'user', 'sandbox_prefix': 'octop_bc', 'cpus': 2}), 'image:tag'))
            conn.execute("UPDATE agents SET config_json=? WHERE agent_id='A1'",
                         (json.dumps({'backend': {'type': 'named', 'name': 'sandbox'}}),))
        row = catalog(self.root, 'P1', sqlite_path=self.db)['agents'][0]
        self.assertNotIn('source_error', row, row)
        self.assertEqual(row['backend']['image'], 'image:tag')
        self.assertEqual(row['backend']['sandbox_scope'], 'user')

    def test_docker_passthrough_keys_match_installed_pinned_source(self):
        import ast
        import importlib.metadata
        from octop_docker_runtime_adapter.workspace_source import DOCKER_KEYS
        path = importlib.metadata.distribution('octop').locate_file('octop/infra/backend/docker_spec.py')
        tree = ast.parse(path.read_text())
        expected = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == '_DOCKER_PASSTHROUGH_KEYS' for t in n.targets))
        self.assertEqual(DOCKER_KEYS, expected)

    def test_default_config_resolves_existing_database_without_writes(self):
        (self.root / 'config.json').write_text(json.dumps({'database': {'driver': 'sqlite', 'sqlite_path': 'control.sqlite'}}))
        with patch.dict(os.environ, {}, clear=True):
            data = catalog(self.root, 'P1')
        self.assertEqual(len(data['agents']), 2)

    def test_missing_default_config_not_created(self):
        with self.assertRaisesRegex(PolicyError, 'octop_config_missing'):
            with readonly_database(self.root):
                pass
        self.assertFalse((self.root / 'config.json').exists())

    def test_postgresql_connection_explicitly_read_only(self):
        from unittest.mock import MagicMock
        (self.root / 'config.json').write_text(json.dumps({'database': {'driver': 'postgresql'}}))
        postgres = MagicMock()
        conn = postgres.connect.return_value.__enter__.return_value
        conn.execute.return_value = []
        with patch.dict(os.environ, {}, clear=True), \
             patch.dict('sys.modules', {'psycopg': postgres, 'psycopg.rows': MagicMock()}):
            with readonly_database(self.root) as query:
                query('SELECT name FROM agents WHERE agent_id = ?', ('A1',))
        calls = [c.args for c in conn.execute.call_args_list]
        self.assertEqual(calls[0], ('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY',))
        self.assertIn(('SELECT name FROM agents WHERE agent_id = %s', ('A1',)), calls)


if __name__ == '__main__':
    unittest.main()
