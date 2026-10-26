"""Disk scanning: large files, directory hotspots, directory sizes."""

from __future__ import annotations

import os
import time
from typing import Callable, Iterable, Optional

from .util import dir_size, human_size, is_reparse_point, long_path, safe_iterdir

SKIP_NAMES = {
    "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
    "recovery", "config.msi", "windowsapps", "winsxs",
}

SYSTEM_FILES = {
    "pagefile.sys": "虚拟内存分页文件",
    "hiberfil.sys": "休眠文件",
    "swapfile.sys": "系统交换文件",
    "dumpstack.log.tmp": "系统转储文件",
}


def system_root_files(drive: str = "C:") -> list[dict]:
    out = []
    for name, desc in SYSTEM_FILES.items():
        p = os.path.join(drive + "\\", name)
        try:
            size = os.path.getsize(long_path(p))
        except OSError:
            continue
        out.append({"path": p, "name": name, "desc": desc, "size": size,
                    "size_human": human_size(size)})
    return out


def path_size(path: str, budget: float = 30) -> dict:
    if not os.path.exists(path):
        return {"path": path, "size": 0, "size_human": "0 B", "count": 0, "exists": False}
    try:
        if is_reparse_point(path):
            target = os.readlink(long_path(path))
            return {"path": path, "size": 0, "size_human": "0 B", "count": 0,
                    "exists": True, "junction": target}
        if os.path.isfile(path):
            s = os.path.getsize(long_path(path))
            return {"path": path, "size": s, "size_human": human_size(s),
                    "count": 1, "exists": True}
        s, c = dir_size(path, budget_seconds=budget)
        return {"path": path, "size": s, "size_human": human_size(s),
                "count": c, "exists": True}
    except OSError as exc:
        return {"path": path, "size": 0, "size_human": "0 B", "count": 0,
                "exists": False, "error": str(exc)}


def scan_large_files(
    roots: Optional[Iterable[str]] = None,
    min_bytes: int = 200 * 1024 * 1024,
    limit: int = 200,
    *,
    include_windows: bool = False,
    report: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    max_seconds: float = 240.0,
) -> dict:
    """Find the biggest files on the given roots (default: system-user areas)."""
    if roots is None:
        user = os.environ.get("USERPROFILE", r"C:\Users\Default")
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        prog = os.environ.get("ProgramData", r"C:\ProgramData")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        roots = [user, prog, pf, pf86]
        if include_windows:
            roots = [sysroot] + list(roots)
    roots = [r for r in roots if r and os.path.isdir(r)]

    deadline = time.monotonic() + max_seconds
    started = time.monotonic()
    found: list[dict] = []
    visited = 0
    stack: list[str] = list(roots)
    last_report = 0.0

    while stack:
        if cancel and cancel():
            break
        now = time.monotonic()
        if now > deadline:
            break
        if report and now - last_report > 0.7:
            pct = min(97.0, (now - started) / max(1.0, max_seconds) * 100)
            report(pct, f"已扫描 {visited:,} 个文件，发现 {len(found)} 个大文件")
            last_report = now
        cur = stack.pop()
        for entry in safe_iterdir(cur):
            try:
                if entry.is_dir(follow_symlinks=False):
                    name = entry.name.lower()
                    if name in SKIP_NAMES or is_reparse_point(entry.path):
                        continue
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    visited += 1
                    st = entry.stat(follow_symlinks=False)
                    if st.st_size >= min_bytes:
                        found.append(
                            {
                                "path": entry.path,
                                "name": entry.name,
                                "dir": os.path.dirname(entry.path),
                                "size": st.st_size,
                                "size_human": human_size(st.st_size),
                                "modified": st.st_mtime,
                                "ext": os.path.splitext(entry.name)[1].lower(),
                            }
                        )
                        if len(found) > limit * 4:
                            found.sort(key=lambda f: -f["size"])
                            del found[limit:]
            except OSError:
                continue

    found.sort(key=lambda f: -f["size"])
    found = found[:limit]
    total = sum(f["size"] for f in found)
    truncated = time.monotonic() > deadline
    if report:
        report(100, f"扫描完成：共 {len(found)} 个大文件")
    return {
        "files": found,
        "count": len(found),
        "total": total,
        "total_human": human_size(total),
        "scanned": visited,
        "min_bytes": min_bytes,
        "min_human": human_size(min_bytes),
        "truncated": truncated,
    }


def scan_hotspots(
    root: str = "C:\\",
    *,
    depth: int = 2,
    top: int = 12,
    report: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    max_seconds: float = 200.0,
) -> dict:
    """Break a drive down into its largest folders (depth levels)."""
    deadline = time.monotonic() + max_seconds
    root = os.path.abspath(root)

    def analyse(folder: str, level: int) -> dict:
        node = {"name": os.path.basename(folder) or folder, "path": folder,
                "size": 0, "size_human": "0 B", "children": [], "level": level}
        if cancel and cancel() or time.monotonic() > deadline:
            return node
        direct = 0
        children = []
        for entry in safe_iterdir(folder):
            try:
                if entry.is_dir(follow_symlinks=False):
                    if is_reparse_point(entry.path) or entry.name.lower() in SKIP_NAMES:
                        continue
                    children.append((entry.path, None))
                elif entry.is_file(follow_symlinks=False):
                    direct += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        node["size"] = direct
        sub_total = 0
        scored: list[tuple[str, int]] = []
        for path, _ in children:
            if cancel and cancel() or time.monotonic() > deadline:
                break
            s, _c = dir_size(path, budget_seconds=max(3, max_seconds / 6))
            scored.append((path, s))
            sub_total += s
        node["size"] += sub_total
        scored.sort(key=lambda x: -x[1])
        # Keep the recursive breakdown: previously the nested analyse() results
        # were computed and then thrown away (only the flat list was returned),
        # which made depth=2 orders of magnitude slower for no benefit.
        for path, s in scored[:top]:
            if level < depth:
                child = analyse(path, level + 1)
                child["size"] = max(child["size"], s)
                child["size_human"] = human_size(child["size"])
            else:
                child = {"name": os.path.basename(path) or path, "path": path,
                         "size": s, "size_human": human_size(s),
                         "children": [], "level": level + 1}
            node["children"].append(child)
            if report:
                report(min(99.0, 5 + len(node["children"]) * 3),
                       f"正在分析 {child['path']}")
        node["size_human"] = human_size(node["size"])
        return node

    tree = analyse(root, 0)
    if report:
        report(100, "磁盘占用分析完成")
    return {"root": root, "tree": tree, "truncated": time.monotonic() > deadline}


def quick_top_dirs(root: str = "C:\\", top: int = 14) -> list[dict]:
    """Fast one-level listing used by the dashboard."""
    out = []
    for entry in safe_iterdir(root):
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if is_reparse_point(entry.path) or entry.name.lower() in SKIP_NAMES:
                continue
        except OSError:
            continue
        size, count = dir_size(entry.path, budget_seconds=20)
        out.append({"name": entry.name, "path": entry.path, "size": size,
                    "size_human": human_size(size), "count": count})
    out.sort(key=lambda d: -d["size"])
    return out[:top]


def folder_summary(path: str) -> dict:
    """Everything the UI needs to describe a folder before migrating it."""
    info = path_size(path)
    info["is_junction"] = is_reparse_point(path) if os.path.exists(path) else False
    if info["is_junction"]:
        info["junction_target"] = os.readlink(long_path(path))
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    info["drive"] = drive
    try:
        usage = __import__("shutil").disk_usage(drive + "\\")
        info["drive_free"] = usage.free
        info["drive_free_human"] = human_size(usage.free)
    except OSError:
        info["drive_free"] = 0
    return info
