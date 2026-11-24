"""Execute the installed Harness class body. Protocol base is stubbed, Docker API is fake."""
import ast
import copy
import importlib.metadata
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import docker


def harness_class():
    source = importlib.metadata.distribution("octop-harness").locate_file("octop_harness/backends/docker_sandbox.py").read_text()
    tree = ast.parse(source)
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and (
        n.module.startswith("deepagents") or n.module.startswith("octop_harness")))]
    class Response:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    scope = {"__name__": "contract_harness", "BaseSandbox": object, "ExecuteResponse": Response,
             "FileUploadResponse": Response, "FileDownloadResponse": Response,
             "load_dotenv_path": lambda _: {}, "parse_env_text": lambda _: {},
             "resolve_docker_execute_env": lambda **_: {}}
    exec(compile(tree, "<installed-harness-1.0.11>", "exec"), scope)
    return scope["DockerSandbox"]


class FakeContainer:
    def __init__(self, collection, kwargs):
        self.collection = collection
        self.id = (str(collection.run_count) * 64)[:64]
        self.name = kwargs["name"]
        self.starts = self.removes = self.renames = 0
        self.fail_start = False
        self.execs = []
        network = kwargs.get("network", kwargs.get("network_mode", "bridge"))
        self.attrs = {"Id": self.id, "Name": "/" + self.name, "Image": "sha256:image",
                      "Config": {"Image": kwargs["image"], "Labels": copy.deepcopy(kwargs["labels"]),
                                 "WorkingDir": kwargs["working_dir"]},
                      "HostConfig": {"Memory": kwargs["mem_limit"], "NanoCpus": kwargs["nano_cpus"],
                                     "PidsLimit": kwargs["pids_limit"], "AutoRemove": False, "NetworkMode": network},
                      "State": {"Status": "running"}, "NetworkSettings": {"Networks": {network: {}}},
                      "Mounts": [{"Type": "bind", "Source": src, "Destination": data["bind"], "RW": True,
                                  "Propagation": "rprivate"} for src, data in kwargs["volumes"].items()]}

    @property
    def status(self):
        return self.attrs["State"]["Status"]

    def reload(self):
        if self.collection.boxes.get(self.name) is not self:
            raise docker.errors.NotFound("gone")

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise docker.errors.APIError("start failed")
        self.attrs["State"]["Status"] = "running"

    def rename(self, name):
        self.renames += 1
        raise AssertionError("managed containers must never rename")

    def stop(self, **kwargs):
        self.attrs["State"]["Status"] = "exited"

    def remove(self, **kwargs):
        self.removes += 1
        self.collection.boxes.pop(self.name, None)

    def exec_run(self, cmd=None, workdir=None, **kwargs):
        self.execs.append({"cmd": cmd, "workdir": workdir})
        return SimpleNamespace(exit_code=0, output=b"")


class FakeCollection:
    def __init__(self):
        self.boxes, self.calls = {}, []
        self.run_count = 0
        self.fail_run = False
    def get(self, name):
        for obj in self.boxes.values():
            if obj.name == name or obj.id == name:
                return obj
        raise docker.errors.NotFound("missing")
    def run(self, **kwargs):
        self.run_count += 1
        self.calls.append(copy.deepcopy(kwargs))
        if self.fail_run:
            raise docker.errors.APIError("create failed")
        if kwargs["name"] in self.boxes:
            raise AssertionError("duplicate create under user lock")
        obj = FakeContainer(self, kwargs)
        self.boxes[obj.name] = obj
        return obj
    def list(self, **kwargs):
        return list(self.boxes.values())


def fake_client(cfg):
    c = SimpleNamespace()
    c.api = SimpleNamespace(base_url="http+docker://localhost", exec_create=Mock(return_value={"Id": "exec-id"}),
                            exec_start=Mock(return_value=b"ok"), exec_inspect=Mock(return_value={"ExitCode": 0}))
    c.info = lambda: {"ID": "daemon", "SecurityOptions": []}
    c.images = SimpleNamespace(get=lambda _: SimpleNamespace(id="sha256:image"))
    c.networks = SimpleNamespace(get=lambda _: SimpleNamespace(attrs={"Name": cfg.network_name, "Driver": "bridge",
                                                                      "Scope": "local", "Internal": False}))
    c.containers = FakeCollection()
    return c
