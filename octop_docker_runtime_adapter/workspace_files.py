"""Bounded ZIP inspection, full-file overlay, durable journals and Linux directory exchange."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from .config import PolicyError
from .runtime import digest


def no_symlinks(path, *, exists=True):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PolicyError("absolute_path_without_parent_segments_required")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if exists:
                raise
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PolicyError("symlink_path_refused")
    return path


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value, *, owner=None):
    path = no_symlinks(Path(path).absolute(), exists=False)
    fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        if owner is not None and os.geteuid() == 0:
            os.fchown(fd, owner, -1)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def zip_manifest(path, *, strip_components=0, max_bytes=4 * 1024**3,
                 max_files=100000, extract_to=None):
    path = no_symlinks(Path(path).absolute())
    if not path.is_file() or strip_components < 0:
        raise PolicyError("invalid_zip_path_or_strip_components")
    before = sha256_file(path)
    entries, seen, total = [], set(), 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > max_files:
            raise PolicyError("zip_entry_limit_exceeded")
        for info in infos:
            name = info.filename
            if (not name or "\x00" in name or "\\" in name or name.startswith("/")
                    or re.match(r"^[A-Za-z]:", name)):
                raise PolicyError("unsafe_zip_name")
            parts = name.rstrip("/").split("/")
            if any(p in {"", ".", ".."} for p in parts):
                raise PolicyError("unsafe_zip_name")
            if len(parts) <= strip_components:
                if info.is_dir():
                    continue
                raise PolicyError("strip_components_would_discard_file")
            rel = "/".join(parts[strip_components:])
            if rel in seen:
                raise PolicyError("duplicate_zip_path")
            seen.add(rel)
            unix_mode = info.external_attr >> 16
            filetype = stat.S_IFMT(unix_mode)
            if filetype not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise PolicyError("zip_symlink_or_special_file_refused")
            if info.flag_bits & 1:
                raise PolicyError("encrypted_zip_not_supported")
            is_dir = info.is_dir()
            if filetype == stat.S_IFDIR and not is_dir or filetype == stat.S_IFREG and is_dir:
                raise PolicyError("zip_file_type_mismatch")
            total += info.file_size
            if total > max_bytes:
                raise PolicyError("zip_expanded_size_limit_exceeded")
            entry = {"path": rel, "directory": is_dir, "size": info.file_size,
                     "mode": (unix_mode & 0o777) or (0o755 if is_dir else 0o644)}
            dest = Path(extract_to) / rel if extract_to is not None else None
            if is_dir:
                if dest is not None:
                    dest.mkdir(parents=True, exist_ok=True)
                entries.append(entry)
                continue
            h, actual = hashlib.sha256(), 0
            if dest is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                output = open(dest, "xb")
            else:
                output = None
            try:
                with archive.open(info) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        actual += len(block)
                        if actual > info.file_size or actual > max_bytes:
                            raise PolicyError("zip_size_mismatch")
                        h.update(block)
                        if output:
                            output.write(block)
                if output:
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if output:
                    output.close()
            if actual != info.file_size:
                raise PolicyError("zip_size_mismatch")
            entry["sha256"] = h.hexdigest()
            entries.append(entry)
        files = {e["path"] for e in entries if not e["directory"]}
        for entry in entries:
            if any(str(p) in files for p in Path(entry["path"]).parents if str(p) != "."):
                raise PolicyError("zip_file_directory_collision")
    if not files:
        raise PolicyError("zip_contains_no_files")
    if sha256_file(path) != before:
        raise PolicyError("zip_changed_during_read")
    return {"path": str(path), "sha256": before, "strip_components": strip_components,
            "max_bytes": max_bytes, "max_files": max_files, "expanded_bytes": total,
            "file_count": len(files), "entries": sorted(entries, key=lambda x: x["path"])}


def tree_manifest(root):
    root = no_symlinks(Path(root).absolute())
    if not root.is_dir():
        raise PolicyError("workspace_directory_missing")
    records = []
    for base, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(base)] + [Path(base) / name for name in files]:
            st = path.lstat()
            is_dir = stat.S_ISDIR(st.st_mode)
            if not is_dir and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
                raise PolicyError("workspace_special_or_hardlinked_file_refused")
            item = {"path": path.relative_to(root).as_posix(), "directory": is_dir,
                    "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid}
            if not is_dir:
                item.update(size=st.st_size, sha256=sha256_file(path))
            records.append(item)
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise PolicyError("workspace_symlink_refused")
    return sorted(records, key=lambda x: x["path"])


def set_metadata(path, info):
    if os.geteuid() == 0:
        os.chown(path, info["uid"], info["gid"], follow_symlinks=False)
    else:
        st = os.stat(path, follow_symlinks=False)
        if st.st_uid != info["uid"]:
            raise PolicyError("root_required_to_preserve_file_ownership")
        if st.st_gid != info["gid"]:
            os.chown(path, -1, info["gid"], follow_symlinks=False)
    os.chmod(path, info["mode"], follow_symlinks=False)


def clone_tree(source, dest, manifest=None):
    manifest = manifest if manifest is not None else tree_manifest(source)
    dest.mkdir(mode=0o700)
    for info in manifest:
        path = dest / info["path"]
        if info["directory"]:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / info["path"], path)
            set_metadata(path, info)
    for info in reversed(manifest):
        if info["directory"]:
            set_metadata(dest / info["path"], info)
    if digest(tree_manifest(dest)) != digest(manifest):
        raise PolicyError("backup_copy_verification_failed")
    return manifest


def overlay(stage, extracted, archive):
    original = {x["path"]: x for x in tree_manifest(stage)}
    root_meta = original["."]
    for item in archive["entries"]:
        path = stage / item["path"]
        if path.exists() and path.is_dir() != item["directory"]:
            raise PolicyError("target_file_directory_type_conflict")
        if item["directory"]:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            # The stage is private and inactive; original live files stay untouched.
            if path.exists():
                path.unlink()
            shutil.copyfile(extracted / item["path"], path)
    archive_meta = {e["path"]: e for e in archive["entries"]}
    for base, dirs, files in os.walk(stage):
        for path in [Path(base)] + [Path(base) / name for name in files]:
            rel = path.relative_to(stage).as_posix()
            info = original.get(rel)
            if info is None:
                default_mode = 0o755 if path.is_dir() else 0o644
                info = {"uid": root_meta["uid"], "gid": root_meta["gid"],
                        "mode": archive_meta.get(rel, {}).get("mode", default_mode)}
            set_metadata(path, info)
    return tree_manifest(stage)


def sync_tree(root):
    for base, dirs, files in os.walk(root, topdown=False):
        for name in files:
            fd = os.open(Path(base) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(base)


def exchange_directories(left, right):
    """An interrupted commit always leaves a complete old or complete new tree."""
    lib = ctypes.CDLL(None, use_errno=True)
    fn = getattr(lib, "renameat2", None)
    if fn is None:
        raise PolicyError("linux_renameat2_required")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
            raise PolicyError("filesystem_atomic_directory_exchange_not_supported")
        raise OSError(error, os.strerror(error))
    fsync_dir(Path(left).parent)
    if Path(right).parent != Path(left).parent:
        fsync_dir(Path(right).parent)
