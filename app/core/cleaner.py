"""Junk / cache definitions, scanning and cleaning."""

from __future__ import annotations

import ctypes
import glob
import os
import re
import shutil
import subprocess
import time
from typing import Callable, Iterable, Optional

from .util import dir_size, expand, human_size, is_reparse_point, long_path, safe_iterdir

La = "%LOCALAPPDATA%"
Ra = "%APPDATA%"
Up = "%USERPROFILE%"
Win = "%SystemRoot%"
Prog = "%ProgramData%"

_NO_WINDOW = 0x08000000


def _cat(**kw) -> dict:
    base = {
        "id": "",
        "name": "",
        "desc": "",
        "group": "系统",
        "icon": "trash",
        "risk": "safe",       # safe | caution | advanced
        "admin": False,
        "processes": [],
        "paths": [],          # folder kept, contents removed
        "remove": [],         # folder/file removed entirely
        "globs": [],          # glob patterns -> matches removed entirely
        "mode": "contents",   # contents | special
        "special": None,
        "default": False,
    }
    base.update(kw)
    return base


CATEGORIES: list[dict] = [
    # ------------------------------------------------------------------ 临时文件
    _cat(
        id="user_temp", name="用户临时文件", group="临时文件", icon="temp",
        desc="当前用户 %TEMP% 目录下的临时文件。随用随删，是清理收益最稳定的项目。",
        paths=[r"%TEMP%"],
        default=True,
        processes=["chrome.exe", "msedge.exe", "firefox.exe", "Code.exe", "python.exe"],
    ),
    _cat(
        id="windows_temp", name="系统临时文件", group="临时文件", icon="temp",
        desc=r"C:\Windows\Temp —— 系统组件、安装程序、服务产生的临时文件。需要管理员权限。",
        admin=True, paths=[rf"{Win}\Temp"],
    ),
    _cat(
        id="crash_dumps", name="崩溃转储与错误报告", group="临时文件", icon="bug",
        desc="应用程序崩溃转储 (.dmp) 与 Windows 错误报告 (WER)。仅在排查崩溃时需要。",
        paths=[rf"{La}\CrashDumps"],
        remove=[rf"{Win}\Minidump", rf"{Win}\MEMORY.DMP"],
        globs=[rf"{La}\Microsoft\Windows\WER\*", rf"{Prog}\Microsoft\Windows\WER\*"],
        default=True,
    ),
    _cat(
        id="thumbs", name="缩略图与图标缓存", group="临时文件", icon="image",
        desc="资源管理器的缩略图数据库，删除后会自动重建（首次浏览稍慢）。",
        globs=[
            rf"{La}\Microsoft\Windows\Explorer\thumbcache_*.db",
            rf"{La}\Microsoft\Windows\Explorer\iconcache_*.db",
        ],
        default=True,
    ),

    # ------------------------------------------------------------------ 系统缓存
    _cat(
        id="windows_update", name="Windows 更新缓存", group="系统缓存", icon="update",
        desc=r"C:\Windows\SoftwareDistribution\Download —— 已下载的更新包，安装完成后可清理。需要管理员权限。",
        admin=True, risk="caution", paths=[rf"{Win}\SoftwareDistribution\Download"],
        processes=["TrustedInstaller.exe", "TiWorker.exe", "wuauserv"],
    ),
    _cat(
        id="delivery_opt", name="传递优化缓存", group="系统缓存", icon="update",
        desc="Windows 传递优化（P2P 更新分发）所占用的缓存文件。",
        admin=True, risk="caution",
        paths=[rf"{Win}\SoftwareDistribution\DeliveryOptimization",
               rf"{Win}\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization"],
    ),
    _cat(
        id="windows_old", name="旧版 Windows 文件 (Windows.old)", group="系统缓存", icon="windows",
        desc="系统升级后保留的旧系统文件，删除后将无法回退到上一版本 Windows。",
        admin=True, risk="advanced", remove=[r"C:\Windows.old"],
        processes=["TrustedInstaller.exe"],
    ),
    _cat(
        id="component_store", name="组件存储清理 (DISM)", group="系统缓存", icon="windows",
        desc="使用 DISM 清理并压缩 WinSxS 组件存储中的旧组件。需要管理员权限，耗时较长，"
             "实际释放量取决于可回收组件的多少。",
        admin=True, risk="advanced", mode="special", special="dism",
    ),
    _cat(
        id="windows_logs", name="系统日志与安装日志", group="系统缓存", icon="log",
        desc="CBS / DISM / WindowsUpdate 等日志，排查问题才需要。",
        admin=True, risk="caution", remove=[rf"{Win}\Logs\CBS", rf"{Win}\Logs\DISM",
                                             rf"{Win}\Logs\MoSetup", rf"{Win}\Logs\SIH"],
        globs=[rf"{Win}\Logs\*.log", rf"{Win}\Logs\*.etl", rf"{Win}\*.log"],
    ),
    _cat(
        id="prefetch", name="预读文件 (Prefetch)", group="系统缓存", icon="speed",
        desc="Prefetch 会随使用自动重建，删除后程序首次启动会略慢。",
        admin=True, risk="caution", globs=[rf"{Win}\Prefetch\*.pf"],
    ),

    # ------------------------------------------------------------------ 隐私清理
    _cat(
        id="recycle_bin", name="回收站", group="隐私清理", icon="recycle",
        desc="清空所有磁盘的回收站内容（无法再恢复）。",
        mode="special", special="recycle", default=True,
    ),
    _cat(
        id="recent", name="最近使用记录", group="隐私清理", icon="history",
        desc="“最近使用的文件”、跳转列表与快速访问历史记录。",
        globs=[rf"{Ra}\Microsoft\Windows\Recent\*"],
        default=True,
    ),
    _cat(
        id="inet_cache", name="浏览器上网缓存 (系统)", group="隐私清理", icon="globe",
        desc=r"IE / 系统 INetCache 与 Cookie、历史记录。",
        remove=[rf"{La}\Microsoft\Windows\INetCache", rf"{La}\Microsoft\Windows\WebCache",
                rf"{La}\Microsoft\Windows\History"],
        globs=[rf"{La}\Microsoft\Windows\Cookies\*"],
    ),

    # ------------------------------------------------------------------ 浏览器缓存
    _cat(
        id="browser_chrome", name="Chrome 缓存", group="浏览器", icon="chrome",
        desc="Google Chrome 各配置文件的 Cache / Code Cache / GPUCache。不会清除书签与密码。",
        globs=[
            rf"{La}\Google\Chrome\User Data\*\Cache\*",
            rf"{La}\Google\Chrome\User Data\*\Code Cache\*",
            rf"{La}\Google\Chrome\User Data\*\GPUCache\*",
            rf"{La}\Google\Chrome\User Data\ShaderCache\*",
        ],
        processes=["chrome.exe"],
    ),
    _cat(
        id="browser_edge", name="Edge 缓存", group="浏览器", icon="edge",
        desc="Microsoft Edge 各配置文件的缓存目录。不会清除收藏夹与密码。",
        globs=[
            rf"{La}\Microsoft\Edge\User Data\*\Cache\*",
            rf"{La}\Microsoft\Edge\User Data\*\Code Cache\*",
            rf"{La}\Microsoft\Edge\User Data\*\GPUCache\*",
        ],
        processes=["msedge.exe"],
    ),
    _cat(
        id="browser_firefox", name="Firefox 缓存", group="浏览器", icon="firefox",
        desc="Mozilla Firefox 配置文件的 cache2 目录。",
        globs=[rf"{La}\Mozilla\Firefox\Profiles\*\cache2\*",
               rf"{La}\Mozilla\Firefox\Profiles\*\startupCache\*"],
        processes=["firefox.exe"],
    ),
    _cat(
        id="browser_others", name="其他浏览器缓存", group="浏览器", icon="globe",
        desc="Brave / Opera / 360 / QQ 浏览器的缓存目录（存在才清理）。",
        globs=[
            rf"{La}\BraveSoftware\Brave-Browser\User Data\*\Cache\*",
            rf"{La}\Programs\Opera\*\cache\*",
            rf"{La}\360Chrome\Chrome\User Data\*\Cache\*",
            rf"{La}\Tencent\QQBrowser\User Data\Default\Cache\*",
        ],
    ),

    # ------------------------------------------------------------------ 开发缓存
    _cat(
        id="dev_temp", name="开发工具临时目录", group="开发缓存", icon="code",
        desc="pip / PyInstaller / npm 等工具在 %TEMP% 下留下的构建临时目录。",
        risk="caution",
        globs=[r"%TEMP%\pip-*", r"%TEMP%\pyi-*", r"%TEMP%\npm-*",
               r"%TEMP%\scoped_dir*", r"%TEMP%\tmp*"],
    ),
    _cat(
        id="dev_cache", name="包管理器缓存", group="开发缓存", icon="package",
        desc="pip / npm / yarn / NuGet 的下载缓存。删除后下次安装依赖需重新下载。",
        risk="caution",
        remove=[rf"{La}\pip\Cache", rf"{Ra}\npm-cache", rf"{La}\npm-cache",
                rf"{La}\Yarn\Cache", rf"{Up}\.cache\pip"],
    ),
    _cat(
        id="dev_cache_heavy", name="构建缓存（Gradle / Maven / HuggingFace）", group="开发缓存", icon="package",
        desc="Gradle、Maven、HuggingFace 等大型构建与模型缓存，体积可能非常大。",
        risk="advanced",
        remove=[rf"{Up}\.gradle\caches", rf"{Up}\.m2\repository",
                rf"{Up}\.cache\huggingface", rf"{Up}\AppData\Local\Temp\huggingface"],
    ),
]

CATEGORY_INDEX = {c["id"]: c for c in CATEGORIES}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _expand_paths(items: Iterable[str]) -> list[str]:
    out = []
    for raw in items:
        if not raw:
            continue
        p = expand(raw)
        p = p.rstrip("\\/") or p
        if p and p not in out:
            out.append(p)
    return out


def _expand_globs(patterns: Iterable[str]) -> list[str]:
    hits: list[str] = []
    seen = set()
    for pat in patterns:
        p = expand(pat)
        try:
            for hit in glob.glob(p):
                key = os.path.normcase(hit)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(hit)
        except Exception:
            continue
    return hits


def _targets(cat: dict) -> list[str]:
    """Everything the category would delete."""
    targets: list[str] = []
    for folder in _expand_paths(cat.get("paths", [])):
        if os.path.isdir(folder):
            for entry in safe_iterdir(folder):
                targets.append(entry.path)
    targets.extend(_expand_paths(cat.get("remove", [])))
    targets.extend(_expand_globs(cat.get("globs", [])))
    return [t for t in targets if os.path.exists(t)]


def available(cat: dict) -> bool:
    if cat.get("mode") == "special":
        return True
    return True


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan(
    ids: Optional[Iterable[str]] = None,
    *,
    report: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    cats = [CATEGORY_INDEX[i] for i in ids if i in CATEGORY_INDEX] if ids else CATEGORIES
    results = []
    total = 0
    for i, cat in enumerate(cats):
        if cancel and cancel():
            break
        if report:
            report((i / max(1, len(cats))) * 100, f"正在分析：{cat['name']}")
        size = 0
        count = 0
        items = []
        if cat.get("mode") == "special":
            if cat.get("special") == "recycle":
                size, count = recycle_bin_size()
            elif cat.get("special") == "dism":
                info = component_store_info()
                # Report the *reclaimable* amount when DISM analysis is available
                # (admin); otherwise fall back to the WinSxS folder estimate.
                size = info["reclaimable"] if info["analyzed"] else info["folder"]
                count = info["folder_count"]
                items = info["items"]
        else:
            for target in _targets(cat):
                if cancel and cancel():
                    break
                try:
                    if os.path.isdir(target) and not is_reparse_point(target):
                        s, c = dir_size(target, budget_seconds=25)
                    else:
                        s, c = os.path.getsize(long_path(target)), 1
                except OSError:
                    continue
                size += s
                count += c
                items.append({"path": target, "size": s, "human": human_size(s)})
        items.sort(key=lambda x: -x["size"])
        results.append(
            {
                "id": cat["id"],
                "name": cat["name"],
                "desc": cat["desc"],
                "group": cat["group"],
                "icon": cat["icon"],
                "risk": cat["risk"],
                "admin": cat["admin"],
                "default": cat["default"],
                "processes": cat["processes"],
                "size": size,
                "size_human": human_size(size),
                "count": count,
                "items": items[:40],
            }
        )
        total += size
        if report:
            report(((i + 1) / max(1, len(cats))) * 100, f"已分析：{cat['name']}")
    results.sort(key=lambda r: -r["size"])
    return {"categories": results, "total": total, "total_human": human_size(total)}


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def clean(
    ids: Iterable[str],
    *,
    report: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    cats = [CATEGORY_INDEX[i] for i in ids if i in CATEGORY_INDEX]
    freed = 0
    removed = 0
    skipped = 0
    details: list[dict] = []
    per_category: list[dict] = []
    start = time.time()

    for i, cat in enumerate(cats):
        if cancel and cancel():
            break
        cat_freed = 0
        cat_failed = 0
        if report:
            report((i / max(1, len(cats))) * 100, f"正在清理：{cat['name']}")

        if cat.get("mode") == "special":
            special = cat.get("special")
            if special == "recycle":
                ok, size = empty_recycle_bin()
                cat_freed += size if ok else 0
                details.append({"category": cat["name"], "path": "回收站",
                                "size": size, "ok": ok})
                freed += cat_freed
                removed += 1 if ok else 0
                per_category.append({"id": cat["id"], "name": cat["name"],
                                     "freed": cat_freed, "freed_human": human_size(cat_freed),
                                     "failed": 0 if ok else 1})
                continue
            if special == "dism":
                from .winutil import is_admin

                if not is_admin():
                    msg = ("此操作需要管理员权限：请点击界面右上角「获取管理员权限」，"
                           "确认 UAC 后以管理员身份重新执行。")
                    skipped += 1
                    details.append({"category": cat["name"], "path": "WinSxS 组件存储",
                                    "size": 0, "ok": False, "message": msg})
                    per_category.append({"id": cat["id"], "name": cat["name"],
                                         "freed": 0, "freed_human": "需管理员权限",
                                         "failed": 1})
                    continue

                base = (i / max(1, len(cats))) * 100
                span = 100 / max(1, len(cats))
                if report:
                    report(base + span * 0.15, "正在测量组件存储…")
                before = _component_store_bytes()
                if report:
                    report(base + span * 0.3, "正在运行 DISM 清理（可能耗时数分钟）…")
                ok, output = run_dism_cleanup(cancel)
                after = _component_store_bytes() if ok else before
                got = max(0, before - after)

                if ok:
                    cat_freed += got
                    freed += cat_freed
                    removed += 1
                    details.append({"category": cat["name"], "path": "WinSxS 组件存储",
                                    "size": got, "ok": True,
                                    "message": f"已释放 {human_size(got)}"})
                    per_category.append({"id": cat["id"], "name": cat["name"],
                                         "freed": got,
                                         "freed_human": human_size(got) if got else "已执行",
                                         "failed": 0})
                else:
                    cat_failed += 1
                    skipped += 1
                    hint = dism_error_hint(output)
                    message = ((hint + " ") if hint else "") + (output or "").strip()[:300]
                    details.append({"category": cat["name"], "path": "WinSxS 组件存储",
                                    "size": 0, "ok": False, "message": message})
                    per_category.append({"id": cat["id"], "name": cat["name"],
                                         "freed": 0, "freed_human": "失败",
                                         "failed": 1})
                continue

        targets = _targets(cat)
        total_targets = max(1, len(targets))
        for j, target in enumerate(targets):
            if cancel and cancel():
                break
            if report:
                report(
                    ((i + (j / total_targets)) / max(1, len(cats))) * 100,
                    f"正在清理：{os.path.basename(target) or target}",
                )
            size = 0
            try:
                if os.path.isdir(target) and not is_reparse_point(target):
                    size = dir_size(target, budget_seconds=10)[0]
                else:
                    size = os.path.getsize(long_path(target))
            except OSError:
                size = 0
            ok, err = _delete(target, keep_root=target in _expand_paths(cat.get("paths", [])))
            if ok:
                cat_freed += size
                removed += 1
            else:
                cat_failed += 1
                skipped += 1
                details.append({"category": cat["name"], "path": target,
                                "size": size, "ok": False, "message": err})
        freed += cat_freed
        per_category.append({"id": cat["id"], "name": cat["name"], "freed": cat_freed,
                             "freed_human": human_size(cat_freed), "failed": cat_failed})

    if report:
        report(100, "清理完成")
    return {
        "freed": freed,
        "freed_human": human_size(freed),
        "removed": removed,
        "skipped": skipped,
        "seconds": round(time.time() - start, 1),
        "details": details[:200],
        "per_category": per_category,
    }


def _delete(path: str, *, keep_root: bool) -> tuple[bool, str]:
    """Delete a path. ``keep_root`` empties a folder instead of removing it."""
    try:
        if keep_root and os.path.isdir(path) and not is_reparse_point(path):
            for entry in safe_iterdir(path):
                _delete_any(entry.path)
            return True, ""
        return _delete_any(path)
    except OSError as exc:
        return False, str(exc)


def _delete_any(path: str) -> tuple[bool, str]:
    try:
        if is_reparse_point(path):
            os.rmdir(long_path(path))
        elif os.path.isdir(path):
            shutil.rmtree(long_path(path), ignore_errors=True)
            if os.path.exists(path):
                _force_remove_tree(path)
        else:
            os.remove(long_path(path))
        return (not os.path.exists(path)), ("" if not os.path.exists(path) else "文件被占用")
    except OSError as exc:
        return False, str(exc)


def _force_remove_tree(path: str) -> None:
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", path], capture_output=True,
                   creationflags=_NO_WINDOW, text=True, encoding="mbcs", errors="replace")


# ---------------------------------------------------------------------------
# Special actions
# ---------------------------------------------------------------------------


def recycle_bin_size() -> tuple[int, int]:
    total = 0
    count = 0
    for drive in _drive_letters():
        for sub in (r"$Recycle.Bin", r"$RECYCLE.BIN"):
            folder = os.path.join(drive + "\\", sub)
            if os.path.isdir(folder):
                s, c = dir_size(folder, budget_seconds=8)
                total += s
                count += c
    return total, count


def _drive_letters() -> list[str]:
    mask = ctypes.windll.kernel32.GetLogicalDrives() if os.name == "nt" else 0
    return [chr(65 + i) for i in range(26) if (mask >> i) & 1]


def empty_recycle_bin() -> tuple[bool, int]:
    size, _ = recycle_bin_size()
    flags = 0x00000001 | 0x00000002 | 0x00000004  # NOCONFIRMATION | NOPROGRESS | NOSOUND
    try:
        rc = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
        return rc == 0, size
    except Exception:
        return False, size


def winsxs_estimate() -> tuple[int, int]:
    """Folder-level size of WinSxS.

    NOTE: this counts hard links, so it massively over-reports the amount that
    can actually be reclaimed. Use :func:`dism_analyze` for the real figure.
    """
    folder = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "WinSxS")
    if not os.path.isdir(folder):
        return 0, 0
    return dir_size(folder, budget_seconds=12)


# -- DISM component store (WinSxS) -------------------------------------------

# Report-line labels, matched case-insensitively (Chinese + English DISM).
_DISM_LABELS = {
    "explorer": ("windows 资源管理器报告的组件存储大小", "size reported by windows explorer",
                 "component store (winsxs) size"),
    "actual": ("实际大小", "actual size"),
    "shared": ("共享", "shared with windows"),
    "backups": ("备份和已禁用", "backups and disabled"),
    "cache": ("缓存和临时数据", "cache and temporary data"),
    "reclaimable": ("可回收的组件", "reclaimable packages"),
}

_SIZE_UNITS = {
    "B": 1, "BYTE": 1, "BYTES": 1,
    "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
    "字节": 1,
}


def _parse_size(text: str) -> int:
    """Parse a DISM size value such as '8.23 GB' or '0 bytes' into bytes."""
    match = re.search(r"([\d.,]+)\s*([A-Za-z\u4e00-\u9fff]*)", text or "")
    if not match:
        return 0
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = (match.group(2) or "B").upper()
    return int(value * _SIZE_UNITS.get(unit, 1))


def dism_error_code(text: str) -> int:
    """Extract a DISM error code (decimal or 0x…) from command output."""
    match = re.search(r"0x([0-9a-fA-F]{1,8})", text or "")
    if match:
        return int(match.group(1), 16)
    match = re.search(r"(?:错误|error)\s*[:：]?\s*(\d+)", text or "", re.IGNORECASE)
    return int(match.group(1)) if match else 0


def dism_error_hint(text: str) -> str:
    """Human-friendly explanation for a common DISM failure code."""
    return {
        740: "此操作需要管理员权限：请点击右上角「获取管理员权限」，确认 UAC 后重新执行。",
        5: "访问被拒绝：请点击右上角「获取管理员权限」后重新执行。",
        1726: "RPC 调用失败：通常是 Windows Update / TrustedInstaller 服务未运行。",
        0x800F0806: "存在挂起的更新操作：请先完成 Windows 更新或重启后再试。",
        0x800F081F: "找不到源文件：请先执行 DISM /Online /Cleanup-Image /RestoreHealth。",
    }.get(dism_error_code(text), "")


def _parse_dism_report(out: str) -> dict:
    """Parse the size lines of a DISM /AnalyzeComponentStore report."""
    found: dict = {}
    for line in out.splitlines():
        low = line.lower()
        for key, needles in _DISM_LABELS.items():
            if any(n in low for n in needles):
                found[key] = _parse_size(re.split(r"[:：]", line)[-1])
    return found


def dism_analyze() -> dict:
    """Run DISM /AnalyzeComponentStore and parse the real component-store sizes."""
    info = {"ok": False, "error": 0, "raw": "", "explorer": 0, "actual": 0,
            "shared": 0, "backups": 0, "cache": 0, "reclaimable": 0}
    args = ["dism", "/Online", "/Cleanup-Image", "/AnalyzeComponentStore"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="mbcs",
                              errors="replace", creationflags=_NO_WINDOW, timeout=1800)
    except Exception as exc:
        info["raw"] = str(exc)
        return info
    out = (proc.stdout or "") + (proc.stderr or "")
    info["raw"] = out
    info["ok"] = proc.returncode == 0
    if not info["ok"]:
        info["error"] = dism_error_code(out) or proc.returncode
        return info
    info.update(_parse_dism_report(out))
    return info


def component_store_info() -> dict:
    """Honest component-store figures for the UI.

    Prefers DISM analysis (admin only) which reports the truly *reclaimable*
    amount; otherwise falls back to the WinSxS folder size with a clear caveat.
    """
    from .winutil import is_admin

    folder_size, folder_count = winsxs_estimate()
    info = {"analyzed": False, "reclaimable": 0, "actual": 0,
            "folder": folder_size, "folder_count": folder_count, "items": []}

    analysis = dism_analyze() if is_admin() else {"ok": False}
    if analysis.get("ok"):
        info["analyzed"] = True
        info["reclaimable"] = analysis.get("reclaimable", 0)
        info["actual"] = analysis.get("actual") or analysis.get("explorer") or 0
        info["items"] = [
            {"path": "可回收的组件（DISM 分析，清理后预计释放）",
             "size": info["reclaimable"], "human": human_size(info["reclaimable"])},
            {"path": "组件存储实际大小（DISM 分析）",
             "size": info["actual"], "human": human_size(info["actual"])},
            {"path": f"WinSxS 目录扫描大小 {human_size(folder_size)}（含硬链接，非可回收量）",
             "size": 0, "human": ""},
        ]
    else:
        info["items"] = [
            {"path": "需要管理员权限才能分析真实可回收量；下列为 WinSxS 目录估算值",
             "size": 0, "human": ""},
            {"path": f"WinSxS 目录扫描大小 {human_size(folder_size)}（含硬链接，实际可回收通常远小于此）",
             "size": 0, "human": ""},
        ]
    return info


def run_dism_cleanup(cancel=None) -> tuple[bool, str]:
    args = ["dism", "/Online", "/Cleanup-Image", "/StartComponentCleanup"]
    if cancel and cancel():
        return False, "已取消"
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="mbcs",
                              errors="replace", creationflags=_NO_WINDOW, timeout=3600)
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return False, str(exc)


def _component_store_bytes() -> int:
    """Best available component-store footprint, used to measure freed space."""
    analysis = dism_analyze()
    if analysis.get("ok") and (analysis.get("actual") or analysis.get("explorer")):
        return analysis["actual"] or analysis["explorer"]
    return winsxs_estimate()[0]


def category_meta() -> list[dict]:
    return [
        {
            "id": c["id"], "name": c["name"], "desc": c["desc"], "group": c["group"],
            "icon": c["icon"], "risk": c["risk"], "admin": c["admin"],
            "default": c["default"], "processes": c["processes"],
        }
        for c in CATEGORIES
    ]
