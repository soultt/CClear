"""FastAPI backend for CClear."""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
import threading
import time
import webbrowser
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import appmover, cleaner, journal, mover, regclean, registry, scanner, winutil
from .core.jobs import JobManager
from .core.util import expand, human_size, is_reparse_point, long_path, unique_path
from . import __version__

BASE_DIR = (
    os.path.join(sys._MEIPASS, "app") if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
WEB_DIR = os.path.join(BASE_DIR, "web")
SETTINGS_FILE = os.path.join(journal.data_dir(), "settings.json")

DEFAULT_SETTINGS = {
    "target_drive": "",
    "group": "Migrated",
    "app_group": "Apps",
    "large_file_min_mb": 200,
    "create_junction": True,
    "update_registry": True,
    "verify_hash": False,
    "kill_before_migrate": True,
    "keep_clean_source": True,
    "theme": "dark",
}

jobs = JobManager()
app = FastAPI(title="CClear", version="1.2.0")
_cache: dict[str, Any] = {}

# Elevation hand-over: the launcher registers a callback so that a successful
# relaunch can shut this instance down instead of leaving it (and its window and
# HTTP server) running next to the elevated copy.
_relaunch_hook: Optional[Callable[[], None]] = None
_elevate_args: list[str] = []
_elevating = False


def set_relaunch_hook(hook: Optional[Callable[[], None]]) -> None:
    """Register what to do once an elevated copy has been started."""
    global _relaunch_hook
    _relaunch_hook = hook


def set_elevate_args(args: list[str]) -> None:
    """Flags the elevated copy should be started with (e.g. ``--browser``)."""
    global _elevate_args
    _elevate_args = list(args)


_shutdown_hook: Optional[Callable[[], None]] = None


def set_shutdown_hook(hook: Optional[Callable[[], None]]) -> None:
    """Register how to stop the HTTP server (wired by the launcher)."""
    global _shutdown_hook
    _shutdown_hook = hook


# ``/api/shutdown`` must never be reachable from a web page: any site could
# otherwise POST to localhost while CClear is open. Require a non-simple header
# (cross-origin JS cannot send one without a CORS preflight, which this app does
# not answer) and a loopback Host (blocks DNS rebinding).
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _url_hostname(value: str, *, has_scheme: bool) -> str:
    try:
        parts = urlsplit(value if has_scheme else f"//{value}")
    except ValueError:
        return ""
    return (parts.hostname or "").lower()


def _is_local_request(request: Request) -> bool:
    if request.headers.get("x-cclear-request", "").lower() != "local":
        return False
    host = _url_hostname(request.headers.get("host", ""), has_scheme=False)
    if host not in _LOOPBACK_NAMES:
        return False
    origin = request.headers.get("origin", "")
    if origin and _url_hostname(origin, has_scheme=True) not in _LOOPBACK_NAMES:
        return False
    return True


@app.post("/api/shutdown")
def shutdown(request: Request) -> dict:
    """Stop this backend - the Electron shell uses it to reap stale copies."""
    if not _is_local_request(request):
        raise HTTPException(403, "拒绝来自非本机页面的关闭请求")
    hook = _shutdown_hook
    if hook is None:
        return {"ok": False, "message": "本实例不支持远程关闭"}
    threading.Timer(0.2, hook).start()
    return {"ok": True}


@app.on_event("startup")
def _cleanup_stale_pickers() -> None:
    """Close picker dialogs orphaned by a previous run (they are invisible)."""
    try:
        winutil.cleanup_stale_pickers()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data.update(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    if not data.get("target_drive"):
        targets = winutil.relocation_targets()
        if targets:
            best = max(targets, key=lambda d: d["free"])
            data["target_drive"] = best["letter"]
    return data


def save_settings(new: dict) -> dict:
    data = load_settings()
    for k, v in (new or {}).items():
        if k in DEFAULT_SETTINGS:
            data[k] = v
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return data


def resolve_target(explicit: Optional[str]) -> str:
    settings = load_settings()
    drive = (explicit or settings.get("target_drive") or "").strip().upper()
    if not drive:
        raise HTTPException(400, "请先选择目标磁盘")
    if not drive.endswith(":"):
        drive += ":"
    root = drive + "\\"
    if not os.path.isdir(root):
        raise HTTPException(400, f"目标磁盘不存在：{drive}")
    if drive == winutil.system_drive():
        raise HTTPException(400, "目标磁盘不能是系统盘")
    return root


# ---------------------------------------------------------------------------
# overview / system
# ---------------------------------------------------------------------------


@app.get("/api/overview")
def overview() -> dict:
    drives = winutil.list_drives()
    system = next((d for d in drives if d["is_system"]), None)
    stats = journal.stats()
    cats = cleaner.category_meta()
    return {
        "admin": winutil.is_admin(),
        "version": __version__,
        "system_drive": winutil.system_drive(),
        "drives": drives,
        "targets": winutil.relocation_targets(),
        "system": system,
        "journal": stats,
        "categories": cats,
        "settings": load_settings(),
        "quick": _cache.get("junk", {}),
        "hotspots": _cache.get("hotspots", []),
        "system_files": scanner.system_root_files(winutil.system_drive()),
        "last_scan": _cache.get("junk_time"),
        "paths": {"data": journal.data_dir(), "backups": journal.backup_dir(),
                  "log": journal.log_path()},
        "os": {
            "name": platform.platform(),
            "release": platform.release(),
            "machine": platform.machine(),
            "hostname": platform.node(),
            "python": platform.python_version(),
        },
    }


@app.get("/api/system")
def system_info() -> dict:
    return {
        "admin": winutil.is_admin(),
        "system_drive": winutil.system_drive(),
        "drives": winutil.list_drives(),
    }


@app.post("/api/elevate")
def elevate() -> dict:
    global _elevating

    if winutil.is_admin():
        return {"ok": True, "message": "当前已是管理员权限"}
    if _elevating:
        return {"ok": True, "message": "正在提升权限，请稍候…"}

    _elevating = True
    try:
        ok = winutil.relaunch_as_admin(_elevate_args)
    except Exception as exc:  # pragma: no cover - shell/shell32 failure
        print(f"CClear: 提升权限失败：{exc}")
        ok = False

    if not ok:
        _elevating = False          # cancelled UAC: let the user try again
        return {"ok": False, "message": "无法提升权限：请在 UAC 提示中点击“是”，"
                                       "或右键 CClear 图标选择「以管理员身份运行」。"}

    hook = _relaunch_hook
    if hook is not None:
        # The page needs a moment to show the toast before the window goes away,
        # and the new instance must be up: close/stop shortly after replying.
        timer = threading.Timer(1.0, hook)
        timer.daemon = True
        timer.start()
    return {
        "ok": True,
        "message": "已请求提升权限，请在 UAC 窗口中确认；确认后本窗口会自动关闭，"
                   "由管理员权限的新窗口接管。",
    }


@app.post("/api/settings")
def update_settings(payload: dict = Body(...)) -> dict:
    return {"settings": save_settings(payload)}


@app.get("/api/settings")
def get_settings() -> dict:
    return {"settings": load_settings()}


@app.post("/api/reveal")
def reveal(payload: dict = Body(...)) -> dict:
    path = expand(str(payload.get("path") or ""))
    if not path:
        raise HTTPException(400, "缺少路径")
    if not os.path.exists(path):
        # A missing file (no log yet, deleted backup) should still land somewhere
        # useful instead of letting Explorer open its default folder.
        parent = os.path.dirname(path)
        if parent and os.path.isdir(parent):
            path = parent
    winutil.open_in_explorer(path)
    return {"ok": True}


# External links (about page, docs) must never load inside the desktop window:
# it has no tabs and no address bar, so navigating away would strand the UI.
_URL_MAX = 2048


@app.post("/api/open-url")
def open_url(payload: dict = Body(...)) -> dict:
    """Open an http(s) link in the user's default browser."""
    url = str(payload.get("url") or "").strip()
    if len(url) > _URL_MAX or not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "只允许打开 http / https 链接")
    try:
        opened = webbrowser.open(url, new=2)
    except Exception as exc:  # pragma: no cover - depends on the environment
        raise HTTPException(500, f"无法打开链接：{exc}") from exc
    if not opened:
        return {"ok": False, "url": url,
                "message": "系统未找到可用的浏览器，请手动复制链接后访问。"}
    return {"ok": True, "url": url}


@app.post("/api/pick/folder")
def pick_folder(payload: dict = Body(default={})) -> dict:
    initial = expand(str(payload.get("initial") or ""))
    return winutil.pick_path("folder", initial)


@app.post("/api/pick/file")
def pick_file(payload: dict = Body(default={})) -> dict:
    initial = expand(str(payload.get("initial") or ""))
    keyword = str(payload.get("filter") or "所有文件|*.*")
    return winutil.pick_path("file", initial, keyword)


# ---------------------------------------------------------------------------
# in-page folder browser
# ---------------------------------------------------------------------------


@app.get("/api/fs/dirs")
def fs_dirs(path: str = Query(default="")) -> dict:
    """List drives (root) or the sub-directories of ``path``.

    Powers the in-page folder picker so the UI never depends on a native
    Windows dialog (which opens behind the browser window).
    """
    drives = winutil.list_drives()
    raw = str(path or "").strip()
    if not raw:
        return {"path": "", "parent": None, "drives": drives, "dirs": [], "error": ""}

    current = expand(raw)
    if not current or not os.path.isdir(current):
        return {"path": "", "parent": None, "drives": drives, "dirs": [],
                "error": "目录不存在或无法访问"}

    current = os.path.abspath(current)
    parent = os.path.dirname(current.rstrip("\\/"))
    if len(parent) == 2 and parent.endswith(":"):
        parent += "\\"
    if parent and os.path.abspath(parent) == current:
        parent = None

    dirs: list[dict] = []
    error = ""
    try:
        with os.scandir(current) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                hidden = False
                try:
                    attrs = entry.stat(follow_symlinks=False).st_file_attributes
                    hidden = bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN
                                           | stat.FILE_ATTRIBUTE_SYSTEM))
                except (OSError, AttributeError):
                    hidden = entry.name.startswith(".")
                dirs.append({
                    "name": entry.name,
                    "path": os.path.join(current, entry.name),
                    "hidden": hidden,
                    "link": is_reparse_point(entry.path),
                })
    except PermissionError:
        error = "没有权限读取该目录"
    except OSError as exc:
        error = str(exc)

    dirs.sort(key=lambda d: d["name"].lower())
    return {"path": current, "parent": parent, "drives": drives, "dirs": dirs, "error": error}


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": jobs.list()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, offset: int = Query(0)) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    return job.as_dict(log_offset=max(0, offset))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    job.cancel()
    return {"ok": True}


def _job_request(payload: dict) -> tuple[dict, dict]:
    return load_settings(), payload


# ---------------------------------------------------------------------------
# junk cleaner
# ---------------------------------------------------------------------------


@app.get("/api/junk/categories")
def junk_categories() -> dict:
    return {"categories": cleaner.category_meta()}


@app.post("/api/junk/scan")
def junk_scan(payload: dict = Body(default={})) -> dict:
    ids = payload.get("ids") or None

    def task(job):
        res = cleaner.scan(
            ids,
            report=lambda p, m: job.progress(p, m),
            cancel=lambda: job.cancelled,
        )
        _cache["junk"] = {"total": res["total"], "total_human": res["total_human"],
                          "categories": res["categories"]}
        _cache["junk_time"] = time.time()
        job.progress(100, f"扫描完成，可清理 {res['total_human']}")
        return res

    j = jobs.run("junk-scan", task, "扫描垃圾文件")
    return {"job_id": j.id}


@app.post("/api/junk/clean")
def junk_clean(payload: dict = Body(...)) -> dict:
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(400, "请至少选择一个清理项")

    def task(job):
        res = cleaner.clean(ids, report=lambda p, m: job.progress(p, m),
                            cancel=lambda: job.cancelled)
        journal.add({
            "action": "clean",
            "label": f"清理 {len(ids)} 个项目",
            "categories": ids,
            "size_bytes": res["freed"],
            "status": "done",
        })
        job.progress(100, f"清理完成，释放 {res['freed_human']}")
        return res

    j = jobs.run("junk-clean", task, "清理垃圾文件")
    return {"job_id": j.id}


@app.post("/api/junk/clean-preview")
def junk_preview(payload: dict = Body(...)) -> dict:
    """Which of the selected categories are locked by running processes."""
    ids = payload.get("ids") or []
    running = winutil.running_processes()
    block = []
    for cid in ids:
        cat = cleaner.CATEGORY_INDEX.get(cid)
        if not cat:
            continue
        procs = [p for p in cat.get("processes", []) if p.lower() in running]
        if procs:
            block.append({"id": cid, "name": cat["name"], "processes": procs})
    return {"blocking": block, "admin": winutil.is_admin()}


# ---------------------------------------------------------------------------
# registry cleaner
# ---------------------------------------------------------------------------


@app.get("/api/regclean/categories")
def regclean_categories() -> dict:
    """Registry junk categories, ordered by safety level (safe → caution → advanced)."""
    return {"categories": regclean.category_meta(), "admin": winutil.is_admin()}


@app.post("/api/regclean/scan")
def regclean_scan(payload: dict = Body(default={})) -> dict:
    ids = payload.get("ids") or None

    def task(job):
        res = regclean.scan(
            ids,
            report=lambda p, m: job.progress(p, m),
            cancel=lambda: job.cancelled,
        )
        _cache["regclean"] = {"total": res["total"],
                              "categories": res["categories"],
                              "time": time.time()}
        job.progress(100, f"扫描完成，发现 {res['total']} 项注册表残留")
        return res

    j = jobs.run("regclean-scan", task, "扫描注册表残留")
    return {"job_id": j.id}


@app.post("/api/regclean/clean")
def regclean_clean(payload: dict = Body(...)) -> dict:
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(400, "请至少选择一个注册表清理项")

    def task(job):
        res = regclean.clean(ids, report=lambda p, m: job.progress(p, m),
                             cancel=lambda: job.cancelled)
        journal.add({
            "action": "regclean",
            "label": f"清理注册表 {len(ids)} 组（删除 {res['removed']} 项）",
            "categories": ids,
            "removed": res["removed"],
            "status": "done",
        })
        _cache.pop("regclean", None)
        job.progress(100, f"清理完成，删除 {res['removed']} 项")
        return res

    j = jobs.run("regclean-clean", task, "清理注册表残留")
    return {"job_id": j.id}


# ---------------------------------------------------------------------------
# large files / hotspots
# ---------------------------------------------------------------------------


@app.post("/api/files/scan")
def files_scan(payload: dict = Body(default={})) -> dict:
    settings = load_settings()
    min_mb = int(payload.get("min_mb") or settings["large_file_min_mb"])
    include_windows = bool(payload.get("include_windows"))
    roots = payload.get("roots") or None

    def task(job):
        res = scanner.scan_large_files(
            roots, min_bytes=min_mb * 1024 * 1024,
            report=lambda p, m: job.progress(p, m),
            cancel=lambda: job.cancelled,
            include_windows=include_windows,
        )
        job.progress(100, f"发现 {res['count']} 个大于 {res['min_human']} 的文件")
        return res

    j = jobs.run("files-scan", task, "扫描大文件")
    return {"job_id": j.id}


@app.post("/api/dir/hotspots")
def dir_hotspots(payload: dict = Body(default={})) -> dict:
    root = payload.get("root") or (winutil.system_drive() + "\\")
    depth = int(payload.get("depth") or 2)

    def task(job):
        res = scanner.scan_hotspots(
            root, depth=depth,
            report=lambda p, m: job.progress(p, m),
            cancel=lambda: job.cancelled,
        )
        _cache["hotspots"] = res["tree"].get("children", [])
        return res

    j = jobs.run("hotspots", task, "分析磁盘占用")
    return {"job_id": j.id}


@app.post("/api/dir/size")
def dir_size_api(payload: dict = Body(...)) -> dict:
    path = expand(str(payload.get("path") or ""))
    if not path or not os.path.exists(path):
        raise HTTPException(404, "路径不存在")
    return scanner.folder_summary(path)


@app.post("/api/files/delete")
def files_delete(payload: dict = Body(...)) -> dict:
    paths = [expand(p) for p in (payload.get("paths") or [])]
    if not paths:
        raise HTTPException(400, "未选择文件")
    to_recycle = bool(payload.get("recycle", True))

    def task(job):
        freed = 0
        deleted, failed = [], []
        for i, p in enumerate(paths):
            if job.cancelled:
                break
            job.progress(i / max(1, len(paths)) * 100, f"正在删除 {os.path.basename(p)}")
            try:
                size = os.path.getsize(long_path(p))
            except OSError:
                size = 0
            ok = _delete_to_recycle(p) if to_recycle else _hard_delete(p)
            if ok:
                freed += size
                deleted.append(p)
            else:
                failed.append(p)
        journal.add({"action": "delete_files", "label": f"删除 {len(deleted)} 个文件",
                     "size_bytes": freed, "paths": deleted[:200], "status": "done"})
        job.progress(100, f"已删除 {len(deleted)} 个文件，释放 {human_size(freed)}")
        return {"deleted": deleted, "failed": failed, "freed": freed,
                "freed_human": human_size(freed)}

    j = jobs.run("files-delete", task, "删除文件")
    return {"job_id": j.id}


def _delete_to_recycle(path: str) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_NOCONFIRMATION = 0x10
        FOF_SILENT = 0x4
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = os.path.abspath(path) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0
    except Exception:
        return False


def _hard_delete(path: str) -> bool:
    return winutil.delete_tree(path)[0] if os.path.isdir(path) else _rm(path)


def _rm(path: str) -> bool:
    try:
        os.remove(long_path(path))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# directory migration
# ---------------------------------------------------------------------------


@app.post("/api/move/plan")
def move_plan(payload: dict = Body(...)) -> dict:
    src = expand(str(payload.get("src") or ""))
    if not src:
        raise HTTPException(400, "请选择要迁移的目录")
    dst_root = resolve_target(payload.get("target"))

    def task(job):
        job.progress(5, "正在检查路径与空间…")
        res = mover.plan(src, dst_root, include_registry=True,
                         group=load_settings().get("group", "Migrated"))
        job.progress(100, "分析完成")
        return res

    j = jobs.run("move-plan", task, "分析迁移方案")
    return {"job_id": j.id}


@app.post("/api/move/execute")
def move_execute(payload: dict = Body(...)) -> dict:
    src = expand(str(payload.get("src") or ""))
    dst = expand(str(payload.get("dst") or ""))
    if not src or not dst:
        raise HTTPException(400, "缺少源路径或目标路径")
    settings = load_settings()
    options = {
        "link": bool(payload.get("link", settings["create_junction"])),
        "update_registry": bool(payload.get("update_registry", settings["update_registry"])),
        "verify_hash": bool(payload.get("verify_hash", settings["verify_hash"])),
        "action": payload.get("action") or ("move_file" if os.path.isfile(src) else "move_dir"),
    }
    if not os.path.exists(src):
        raise HTTPException(404, "源路径不存在")

    def task(job):
        # ``dst`` (target\group\name) and its group folder usually do not exist
        # yet on a first migration, so validate against the nearest existing
        # ancestor instead of failing the "target directory missing" check.
        dst_root = os.path.dirname(dst)
        while dst_root and not os.path.isdir(dst_root):
            parent = os.path.dirname(dst_root)
            if parent == dst_root:
                break
            dst_root = parent
        ok, errors, warnings = mover.validate(src, dst_root)
        for w in warnings:
            job.log(w, level="warn")
        if not ok and not payload.get("force"):
            raise RuntimeError("；".join(errors))
        return mover.execute(
            src, dst, link=options["link"], update_registry=options["update_registry"],
            verify_hash=options["verify_hash"], action=options["action"],
            report=lambda p, m, d=None: job.progress(p, m, **(d or {})),
            cancel=lambda: job.cancelled,
        )

    j = jobs.run("move", task, f"迁移 {os.path.basename(src.rstrip(chr(92)))}")
    return {"job_id": j.id}


@app.post("/api/move/batch")
def move_batch(payload: dict = Body(...)) -> dict:
    sources = [expand(str(s)) for s in (payload.get("sources") or []) if s]
    if not sources:
        raise HTTPException(400, "未选择要迁移的项目")
    dst_root = resolve_target(payload.get("target"))
    settings = load_settings()
    group = str(payload.get("group") or settings.get("group", "Migrated"))
    options = {
        "link": bool(payload.get("link", settings["create_junction"])),
        "update_registry": bool(payload.get("update_registry", settings["update_registry"])),
        "verify_hash": bool(payload.get("verify_hash", settings["verify_hash"])),
    }

    def task(job):
        results, succeeded, failed = [], 0, 0
        total = len(sources)
        for i, src in enumerate(sources):
            if job.cancelled:
                break
            base = (i / total) * 100
            step = 100.0 / total
            if not os.path.exists(src):
                failed += 1
                results.append({"src": src, "error": "路径不存在"})
                continue
            dst = mover.destination_for(src, dst_root, group)
            if os.path.exists(dst):
                dst = unique_path(dst)
            # Same guard rails as a single migration (system dirs, links, space).
            ok, errors, warnings = mover.validate(src, dst_root)
            for w in warnings:
                job.log(w, level="warn")
            if not ok and not payload.get("force"):
                failed += 1
                results.append({"src": src, "dst": dst, "error": "；".join(errors)})
                job.log(f"✗ {os.path.basename(src)}：{'；'.join(errors)}", level="error")
                continue
            job.progress(base, f"({i + 1}/{total}) 正在迁移 {os.path.basename(src.rstrip(chr(92)))}")
            try:
                res = mover.execute(
                    src, dst, link=options["link"], update_registry=options["update_registry"],
                    verify_hash=options["verify_hash"],
                    action="move_file" if os.path.isfile(src) else "move_dir",
                    report=lambda p, m, d=None, b=base, s=step: job.progress(
                        b + min(s, p * s / 100.0), m, **(d or {})),
                    cancel=lambda: job.cancelled,
                )
                results.append(res)
                succeeded += 1
                job.log(f"✓ {os.path.basename(src)} → {dst}（{res.get('files', 0)} 个文件）")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                results.append({"src": src, "dst": dst, "error": str(exc)})
                job.log(f"✗ {os.path.basename(src)}：{exc}", level="error")
        job.progress(100, f"完成：成功 {succeeded} 项，失败 {failed} 项")
        return {"total": total, "succeeded": succeeded, "failed": failed,
                "results": results,
                "freed_bytes": sum(r.get("size_bytes", 0) for r in results if not r.get("error")),
                "freed_human": human_size(sum(r.get("size_bytes", 0) for r in results if not r.get("error")))}

    j = jobs.run("move-batch", task, f"批量迁移 {len(sources)} 个项目")
    return {"job_id": j.id}


# ---------------------------------------------------------------------------
# application migration
# ---------------------------------------------------------------------------


@app.get("/api/apps")
def apps_list(refresh: bool = Query(False)) -> dict:
    if refresh or "apps" not in _cache:
        _cache["apps"] = appmover.list_apps()
    return _cache["apps"]


@app.post("/api/apps/size")
def app_size(payload: dict = Body(...)) -> dict:
    folder = expand(str(payload.get("folder") or ""))
    if not folder or not os.path.isdir(folder):
        raise HTTPException(404, "安装目录不存在")
    res = appmover.app_size(folder)
    res["path"] = folder
    return res


@app.post("/api/apps/analyse")
def app_analyse(payload: dict = Body(...)) -> dict:
    folder = expand(str(payload.get("folder") or ""))
    if not folder or not os.path.isdir(folder):
        raise HTTPException(404, "安装目录不存在")
    dst_root = resolve_target(payload.get("target"))
    name = str(payload.get("name") or os.path.basename(folder))
    registry_key = str(payload.get("registry_key") or "")

    def task(job):
        job.progress(10, "正在检查目录与空间…")
        res = appmover.analyse(folder, dst_root, name=name, registry_key=registry_key)
        job.progress(60, "正在检索注册表引用…")
        job.progress(100, f"分析完成，发现 {res['registry_count']} 处注册表引用")
        return res

    j = jobs.run("app-analyse", task, f"分析应用 {name}")
    return {"job_id": j.id}


@app.post("/api/apps/migrate")
def app_migrate(payload: dict = Body(...)) -> dict:
    folder = expand(str(payload.get("folder") or ""))
    if not folder or not os.path.isdir(folder):
        raise HTTPException(404, "安装目录不存在")
    dst_root = resolve_target(payload.get("target"))
    settings = load_settings()
    options = {
        "link": bool(payload.get("link", settings["create_junction"])),
        "update_registry": bool(payload.get("update_registry", settings["update_registry"])),
        "verify_hash": bool(payload.get("verify_hash", settings["verify_hash"])),
        "kill_first": bool(payload.get("kill_first", settings["kill_before_migrate"])),
    }
    app_meta = {
        "folder": folder,
        "name": str(payload.get("name") or os.path.basename(folder)),
        "id": payload.get("id"),
        "registry_key": payload.get("registry_key") or "",
        "uninstall": payload.get("uninstall") or "",
    }

    def task(job):
        res = appmover.migrate(
            app_meta, dst_root,
            link=options["link"], update_registry=options["update_registry"],
            verify_hash=options["verify_hash"], kill_first=options["kill_first"],
            report=lambda p, m, d=None: job.progress(p, m, **(d or {})),
            cancel=lambda: job.cancelled,
        )
        _cache.pop("apps", None)
        return res

    j = jobs.run("app-migrate", task, f"迁移应用 {app_meta['name']}")
    return {"job_id": j.id}


# ---------------------------------------------------------------------------
# journal / rollback
# ---------------------------------------------------------------------------


@app.get("/api/journal")
def journal_list() -> dict:
    return {"records": journal.all_records(), "stats": journal.stats()}


@app.post("/api/journal/{record_id}/revert")
def journal_revert(record_id: str) -> dict:
    record = journal.get(record_id)
    if not record:
        raise HTTPException(404, "记录不存在")
    if record.get("action") not in ("move_file", "move_dir", "move_app"):
        raise HTTPException(400, "该记录不支持还原")
    if record.get("status") == "running":
        raise HTTPException(409, "该迁移仍在进行中，请等待任务结束后再还原")

    def task(job):
        return mover.revert(record,
                            report=lambda p, m, d=None: job.progress(p, m, **(d or {})),
                            cancel=lambda: job.cancelled)

    j = jobs.run("revert", task, f"还原 {record.get('label') or record_id}")
    return {"job_id": j.id}


@app.delete("/api/journal")
def journal_clear() -> dict:
    return {"removed": journal.clear()}


@app.post("/api/registry/search")
def registry_search(payload: dict = Body(...)) -> dict:
    path = expand(str(payload.get("path") or ""))
    if not path:
        raise HTTPException(400, "缺少路径")

    def task(job):
        refs = registry.find_references(path, max_seconds=60,
                                        cancel=lambda: job.cancelled)
        job.progress(100, f"发现 {len(refs)} 处引用")
        return {"path": path, "refs": refs, "count": len(refs)}

    j = jobs.run("registry-search", task, "检索注册表引用")
    return {"job_id": j.id}


@app.post("/api/registry/backup")
def registry_backup() -> dict:
    """Export the main registry hives as a background job.

    Exporting ``HKCU\\Software`` + ``HKLM\\SOFTWARE`` can take a minute or more;
    running it in the job manager keeps the request short and lets the UI show
    real progress instead of a button that looks frozen.
    """
    paths = [r"HKCU\Software", r"HKLM\SOFTWARE"]

    def task(job) -> dict:
        total = len(paths)

        def report(done: int, count: int, key: str) -> None:
            pct = 5 + 90 * (done / count) if count else 95
            job.progress(min(pct, 95.0),
                         f"正在导出 {key}（{done}/{count}）" if key else "正在写入备份索引…")

        job.log("开始导出注册表，可能需要 1-2 分钟…")
        folder = registry.backup_keys(paths, tag="manual", on_progress=report)
        job.log(f"备份已写入 {folder}")
        return {"folder": folder, "keys": total}

    j = jobs.run("registry-backup", task, "备份注册表")
    return {"job_id": j.id}


# ---------------------------------------------------------------------------
# static files
# ---------------------------------------------------------------------------


class _NoCacheStatic(StaticFiles):
    """Revalidate every load so a updated UI is never served from stale cache."""

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if os.path.isdir(WEB_DIR):
    app.mount("/static", _NoCacheStatic(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> Any:
    index_file = os.path.join(WEB_DIR, "index.html")
    if os.path.isfile(index_file):
        return FileResponse(index_file, media_type="text/html",
                            headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "web/index.html 缺失"}, status_code=500)


@app.get("/favicon.ico")
def favicon() -> Any:
    path = os.path.join(WEB_DIR, "favicon.ico")
    if os.path.isfile(path):
        return FileResponse(path)
    return JSONResponse({}, status_code=204)
