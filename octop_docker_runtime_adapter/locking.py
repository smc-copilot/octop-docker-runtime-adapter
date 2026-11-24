from __future__ import annotations

import fcntl
import os
import stat
import threading
import time
from contextlib import contextmanager

from .config import PolicyError, private_dir

_guard = threading.Lock()
_threads = {}


@contextmanager
def user_lock(cfg, key):
    folder = cfg.state_dir / "locks"
    private_dir(folder)
    path = folder / (key + ".lock")
    with _guard:
        mutex = _threads.setdefault(str(path), threading.RLock())
    deadline = time.monotonic() + cfg.lock_timeout
    if not mutex.acquire(timeout=cfg.lock_timeout):
        raise PolicyError("user_lock_timeout")
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise PolicyError("unsafe_lock_file")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PolicyError("user_lock_timeout")
                time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            os.close(fd)
        mutex.release()
