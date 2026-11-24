from __future__ import annotations

import functools
import importlib.abc
import importlib.machinery
import sys
import uuid

from . import __version__
from .config import PolicyError


def patch_manager(cls, runtime):
    original = cls._prepare_docker_backend
    if getattr(original, "_dra_version", None):
        return

    @functools.wraps(original)
    def prepare(self, backend, row):
        result = original(self, backend, row)
        if not runtime.managed_spec(result):
            return result
        # DB owner, never the calling/chatting viewer of a public/shared expert.
        owner = self._repos.user_repo.get(row.user_id) if row.user_id is not None else None
        if owner is None:
            raise PolicyError("public_or_ownerless_agent_requires_separate_backend")
        return runtime.authorize(result, row.user_id, owner.username, row.agent_id)

    prepare._dra_version = __version__
    cls._prepare_docker_backend = prepare


def patch_probe(module, runtime):
    original = module._docker_probe_spec
    if getattr(original, "_dra_version", None):
        return

    @functools.wraps(original)
    def probe(row):
        spec = original(row)
        if not runtime.managed_spec(spec):
            return spec
        # Native user-mode probes can otherwise reuse prefix_test, then close()
        # with auto_remove=True. A UUID fixed-scope probe can never use a user box.
        out = dict(spec)
        for key in ("container_name", "agent_id", "username", "workspace_path", "volumes",
                    "_dra_owner_id", "_dra_signature"):
            out.pop(key, None)
        out.update(sandbox_scope="fixed", sandbox_id="dra_probe_" + uuid.uuid4().hex, auto_remove=True)
        return out

    probe._dra_version = __version__
    module._docker_probe_spec = probe


def patch_sandbox(cls, runtime):
    if getattr(cls.__init__, "_dra_version", None):
        return
    original_init, original_acquire = cls.__init__, cls._acquire_container
    original_rebind, original_destroy = cls._rebind_if_missing, cls.destroy

    @functools.wraps(original_init)
    def init(self, *args, **kwargs):
        self._dra_identity = runtime.bind(kwargs)
        kwargs = dict(kwargs)
        kwargs.pop("_dra_owner_id", None)
        kwargs.pop("_dra_signature", None)
        original_init(self, *args, **kwargs)

    @functools.wraps(original_acquire)
    def acquire(self):
        if not getattr(self, "_dra_identity", None):
            return original_acquire(self)
        try:
            result = runtime.acquire(self)
            # runtime.acquire stores the effective profile (backend for a new
            # container, actual container profile for pinned reuse).
            if getattr(self, "_dra_profile", None) is None:
                self._dra_profile = runtime.profile(self)
            return result
        except Exception as exc:
            runtime.audit.emit("user_acquire_blocked", level="error", agent_id=self._dra_identity.agent_id,
                               reason=str(exc) if isinstance(exc, PolicyError) else type(exc).__name__)
            raise

    @functools.wraps(original_rebind)
    def rebind(self):
        if not getattr(self, "_dra_identity", None):
            return original_rebind(self)
        try:
            self._container.reload()
        except self._docker.errors.NotFound:
            pass
        else:
            runtime.validate_container(self._container, self._dra_identity, self._dra_profile)
            if self._container.attrs.get("State", {}).get("Status") == "running":
                return
        self._container = acquire(self)
        self._container_id = str(self._container.id)

    @functools.wraps(original_destroy)
    def destroy(self):
        if getattr(self, "_dra_identity", None):
            runtime.audit.emit("shared_user_destroy_blocked", level="error", user_key=self._dra_identity.key)
            raise PolicyError("shared_user_container_requires_user_level_maintenance")
        return original_destroy(self)

    init._dra_version = __version__
    cls.__init__, cls._acquire_container = init, acquire
    cls._rebind_if_missing, cls.destroy = rebind, destroy


class Loader(importlib.abc.Loader):
    def __init__(self, original, callback):
        self.original, self.callback = original, callback

    def create_module(self, spec):
        method = getattr(self.original, "create_module", None)
        return method(spec) if method else None

    def exec_module(self, module):
        self.original.exec_module(module)
        self.callback(module)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, callbacks):
        self.callbacks = callbacks

    def find_spec(self, fullname, path=None, target=None):
        callback = self.callbacks.get(fullname)
        if callback is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not hasattr(spec.loader, "exec_module"):
            raise PolicyError("unsupported_import_loader")
        spec.loader = Loader(spec.loader, callback)
        return spec


def install_hooks(runtime):
    callbacks = {
        "octop.infra.agents.manager": lambda m: patch_manager(m.AgentManager, runtime),
        "octop.infra.backend.probe": lambda m: patch_probe(m, runtime),
        "octop_harness.backends.docker_sandbox": lambda m: patch_sandbox(m.DockerSandbox, runtime),
    }
    def logged(name, callback):
        def apply(module):
            callback(module)
            runtime.audit.emit("hook_installed", module=name, version=__version__)
        return apply
    callbacks = {name: logged(name, fn) for name, fn in callbacks.items()}
    for name, fn in callbacks.items():
        if name in sys.modules:
            fn(sys.modules[name])
    finder = Finder(callbacks)
    sys.meta_path.insert(0, finder)
    return finder
