"""Generic helpers: paths, sizes, safe file-system walking, long paths."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from typing import Callable, Iterable, Iterator, Optional

# ---------------------------------------------------------------------------
# Long path support (Windows MAX_PATH workaround)
# ---------------------------------------------------------------------------

_LONG_PREFIX = "\\\\?\\"


def long_path(path: str) -> str:
    """Return a path usable with the Win32 API for paths longer than 260 chars."""
    if not path:
        return path
    if path.startswith(_LONG_PREFIX):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):  # UNC
        return "\\\\?\\UNC\\" + path[2:]
    return _LONG_PREFIX + path


def human_size(num: float) -> str:
    """1234567 -> '1.18 MB'."""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "0 B"
    neg = num < 0
    num = abs(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024.0 or unit == "PB":
            break
        num /= 1024.0
    if unit == "B":
        text = f"{int(num)} {unit}"
    else:
        text = f"{num:.2f} {unit}"
    return ("-" + text) if neg else text


def lexists(path: str) -> bool:
    """Like os.path.exists but True for dangling links too."""
    try:
        os.lstat(long_path(path))
        return True
    except OSError:
        return False


def is_reparse_point(path: str) -> bool:
    """True for symlinks / junctions / mount points (do not follow, do not count)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if hasattr(
        st, "st_file_attributes"
    ) else stat.S_ISLNK(st.st_mode)


class Entry:
    """Lightweight scandir entry whose ``path`` never carries the \\\\?\\ prefix."""

    __slots__ = ("name", "path", "_e")

    def __init__(self, name: str, path: str, entry):
        self.name = name
        self.path = path
        self._e = entry

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self._e.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return self._e.is_file(follow_symlinks=follow_symlinks)

    def is_symlink(self) -> bool:
        return self._e.is_symlink()

    def stat(self, follow_symlinks: bool = True):
        return self._e.stat(follow_symlinks=follow_symlinks)

    def __fspath__(self) -> str:
        return self.path

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Entry {self.path!r}>"


def safe_iterdir(path: str) -> list[Entry]:
    """List a directory (long-path safe) returning entries with clean paths."""
    try:
        with os.scandir(long_path(path)) as it:
            return [Entry(e.name, os.path.join(path, e.name), e) for e in it]
    except OSError:
        return []


def dir_size(
    path: str,
    *,
    follow_links: bool = False,
    on_file: Optional[Callable[[str, int], None]] = None,
    budget_seconds: Optional[float] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> tuple[int, int]:
    """Return (total_bytes, file_count) for a directory tree.

    Reparse points are skipped by default so junctions/symlinks are not
    counted twice (or recursed into forever).
    """
    total = 0
    count = 0
    deadline = (time.monotonic() + budget_seconds) if budget_seconds else None
    stack = [path]
    while stack:
        cur = stack.pop()
        if cancel and cancel():
            break
        if deadline and time.monotonic() > deadline:
            break
        try:
            with os.scandir(long_path(cur)) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=follow_links):
                            if not follow_links and is_reparse_point(entry.path):
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=follow_links):
                            size = entry.stat(follow_symlinks=False).st_size
                            total += size
                            count += 1
                            if on_file:
                                on_file(entry.path, size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


def iter_files(
    path: str,
    *,
    follow_links: bool = False,
    cancel: Optional[Callable[[], bool]] = None,
) -> Iterator[tuple[str, int]]:
    """Yield (file_path, size) for every file under ``path``."""
    stack = [path]
    while stack:
        cur = stack.pop()
        if cancel and cancel():
            return
        for entry in safe_iterdir(cur):
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not follow_links and is_reparse_point(entry.path):
                        continue
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    yield entry.path, entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue


def expand(path: str) -> str:
    """Expand %VARS% and ~ in a path."""
    return os.path.expandvars(os.path.expanduser(path))


def same_drive(a: str, b: str) -> bool:
    da = os.path.splitdrive(os.path.abspath(a))[0].upper()
    db = os.path.splitdrive(os.path.abspath(b))[0].upper()
    return da == db and da != ""


def path_in(child: str, parent: str) -> bool:
    """True when ``child`` is inside ``parent`` (case-insensitive, path-aware)."""
    try:
        c = os.path.normcase(os.path.abspath(child)).rstrip("\\")
        p = os.path.normcase(os.path.abspath(parent)).rstrip("\\")
    except Exception:
        return False
    return c == p or c.startswith(p + "\\")


def free_space(path: str) -> int:
    try:
        import shutil

        return shutil.disk_usage(_drive_root(path)).free
    except Exception:
        return 0


def _drive_root(path: str) -> str:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return (drive + "\\") if drive else os.path.abspath(path)


def fingerprint(path: str, sample_mb: int = 4) -> tuple[int, int, str]:
    """(size, file_count, digest) digest samples head/tail of files + names+sizes."""
    h = hashlib.blake2b(digest_size=16)
    total = 0
    count = 0
    sample = max(1, sample_mb) * 1024 * 1024
    for f, size in iter_files(path):
        total += size
        count += 1
        h.update(os.path.relpath(f, path).encode("utf-8", "replace"))
        h.update(str(size).encode())
        if size <= sample * 2:
            chunk_start, chunk_end = 0, size
        else:
            chunk_start, chunk_end = sample, sample
        try:
            with open(long_path(f), "rb") as fh:
                if chunk_end == size and size > 0:
                    fh.seek(0)
                    h.update(fh.read(sample))
                    fh.seek(max(0, size - sample))
                    h.update(fh.read(sample))
                else:
                    h.update(fh.read(sample))
        except OSError:
            pass
    return total, count, h.hexdigest()


def sanitize_name(name: str, max_len: int = 60) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "item")[:max_len]


def unique_path(path: str) -> str:
    """Return a path that does not exist yet by appending ' (n)'."""
    if not lexists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{base} ({i}){ext}"
        if not lexists(cand):
            return cand
        i += 1
