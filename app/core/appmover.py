"""Application discovery and migration (folder + registry + shortcuts)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import winreg
from typing import Callable, Iterable, Optional

from . import journal, mover, registry, winutil
from .util import dir_size, expand, human_size, is_reparse_point, long_path, path_in, safe_iterdir

UNINSTALL_KEYS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 64),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 32),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 64),
]

QUOTED = re.compile(r'"([^"]+)"')
EXE_RE = re.compile(r"([A-Za-z]:\\[^\"'>|]+?\.exe)", re.I)
GUID_RE = re.compile(r"^\{[0-9A-Fa-f\-]{36}\}$")

# Directories that only hold per-user programs.
USER_APP_DIRS = [r"%LOCALAPPDATA%\Programs", r"%APPDATA%", r"%LOCALAPPDATA%"]

NO_MIGRATE_SUBSTR = (
    "\\windowsapps", "\\windows\\", "\\microsoft\\windows\\",
    "\\program files\\windows", "\\program files (x86)\\windows",
    "\\$recycle.bin", "\\system volume information",
)

MICROSOFT_HINTS = (
    "microsoft corporation", "microsoft visual c++", "windows sdk",
    "windows software development kit", "microsoft .net", "windows driver kit",
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _read_values(key_path: str, view: int) -> dict:
    hive, sub = key_path.split("\\", 1)
    try:
        access = winreg.KEY_READ | (winreg.KEY_WOW64_64KEY if view == 64 else winreg.KEY_WOW64_32KEY)
        with winreg.OpenKey(registry.HIVES[hive], sub, 0, access) as k:
            out = {}
            for i in range(winreg.QueryInfoKey(k)[1]):
                try:
                    name, value, _t = winreg.EnumValue(k, i)
                    out[name] = value
                except OSError:
                    continue
            return out
    except OSError:
        return {}


def _iter_uninstall_subkeys() -> Iterable[tuple[str, int]]:
    seen: set[str] = set()
    for hive, base, view in UNINSTALL_KEYS:
        for v in (view, 64 if view == 32 else 32):
            access = winreg.KEY_READ | (winreg.KEY_WOW64_64KEY if v == 64 else winreg.KEY_WOW64_32KEY)
            try:
                with winreg.OpenKey(registry.HIVES[hive], base, 0, access) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            name = winreg.EnumKey(k, i)
                        except OSError:
                            continue
                        key_path = f"{hive}\\{base}\\{name}"
                        if key_path in seen:
                            continue
                        seen.add(key_path)
                        yield key_path, v
            except OSError:
                continue


def _extract_dir(text: str) -> Optional[str]:
    if not text:
        return None
    cands = QUOTED.findall(text) or EXE_RE.findall(text)
    if not cands:
        first = text.strip().split(" ")[0]
        cands = [first]
    for cand in cands:
        cand = expand(cand.strip().strip('"'))
        cand = re.sub(r",\d+$", "", cand)  # strip ",0" icon index
        if os.path.isfile(cand):
            folder = os.path.dirname(cand)
        else:
            folder = cand
        if os.path.isdir(folder):
            return folder
    return None


def _is_migratable(folder: str, name: str, publisher: str) -> tuple[bool, str, str]:
    if not folder:
        return False, "unknown", "未找到安装目录"
    low = os.path.normcase(folder)
    for bad in NO_MIGRATE_SUBSTR:
        if bad in low:
            if "windowsapps" in low:
                return False, "store", "Windows 应用商店应用（受系统保护）"
            return False, "system", "Windows 系统组件或受保护目录"
    drive = os.path.splitdrive(folder)[0].upper()
    if drive != winutil.system_drive():
        return False, "other", f"已位于 {drive} 盘，无需迁移"
    if is_reparse_point(folder):
        return False, "linked", "已经是联接（已迁移过）"
    pub = (publisher or "").lower()
    if any(h in pub for h in MICROSOFT_HINTS) and not name:
        return False, "system", "微软系统组件"
    if len(folder) <= 4:
        return False, "system", "目录层级过浅，不建议迁移"
    return True, "desktop", ""


def _contains_exe(folder: str, depth: int = 2, cancel=None) -> bool:
    """True when the folder (or a direct subfolder) contains an executable."""
    stack = [(folder, 0)]
    while stack:
        cur, level = stack.pop()
        for entry in safe_iterdir(cur):
            try:
                if entry.is_file(follow_symlinks=False):
                    if entry.name.lower().endswith(".exe"):
                        return True
                elif entry.is_dir(follow_symlinks=False) and level < depth:
                    if not is_reparse_point(entry.path):
                        stack.append((entry.path, level + 1))
            except OSError:
                continue
    return False


def _app_id(folder: str, reg_key: str) -> str:
    h = hashlib.md5((os.path.normcase(folder or reg_key)).encode("utf-8"))
    return h.hexdigest()[:12]


def list_apps(*, include_user_apps: bool = True) -> dict:
    """Enumerate installed applications and whether they can be migrated."""
    apps: dict[str, dict] = {}
    skipped = 0

    for key_path, view in _iter_uninstall_subkeys():
        vals = _read_values(key_path, view)
        name = str(vals.get("DisplayName") or "").strip()
        if not name:
            skipped += 1
            continue
        if vals.get("SystemComponent") in (1, "1"):
            skipped += 1
            continue
        if vals.get("ReleaseType") in ("Security Update", "Update", "Hotfix"):
            skipped += 1
            continue
        if vals.get("ParentKeyName"):
            skipped += 1
            continue
        if name.lower().startswith(("security update", "update for", "hotfix for")):
            skipped += 1
            continue

        folder = None
        for field in ("InstallLocation", "InstallDir", "InstallPath"):
            raw = vals.get(field)
            if isinstance(raw, str) and raw.strip():
                raw = expand(raw.strip().strip('"'))
                if os.path.isdir(raw):
                    folder = os.path.abspath(raw)
                    break
        if not folder:
            folder = _extract_dir(str(vals.get("UninstallString") or "")) or \
                _extract_dir(str(vals.get("QuietUninstallString") or "")) or \
                _extract_dir(str(vals.get("DisplayIcon") or ""))

        migratable, kind, reason = _is_migratable(folder or "", name, str(vals.get("Publisher") or ""))
        est = vals.get("EstimatedSize")
        try:
            est_bytes = int(est) * 1024 if est else 0
        except (TypeError, ValueError):
            est_bytes = 0

        app = {
            "id": _app_id(folder or "", key_path),
            "name": name,
            "version": str(vals.get("DisplayVersion") or "").strip(),
            "publisher": str(vals.get("Publisher") or "").strip(),
            "folder": folder,
            "registry_key": key_path,
            "registry_view": view,
            "uninstall": str(vals.get("UninstallString") or "").strip(),
            "icon": str(vals.get("DisplayIcon") or "").strip(),
            "install_date": str(vals.get("InstallDate") or "").strip(),
            "estimated_size": est_bytes,
            "estimated_human": human_size(est_bytes),
            "migratable": migratable,
            "kind": kind,
            "reason": reason,
            "source": "registry",
        }
        key = os.path.normcase(folder) if folder else (key_path + name)
        if key in apps and not migratable:
            continue
        apps[key] = app

    if include_user_apps:
        for pattern in [expand(r"%LOCALAPPDATA%\Programs\*"), expand(r"%APPDATA%\*")]:
            import glob

            for folder in glob.glob(pattern):
                if not os.path.isdir(folder) or is_reparse_point(folder):
                    continue
                key = os.path.normcase(folder)
                if any(key == os.path.normcase(a.get("folder") or "") for a in apps.values()):
                    continue
                if os.path.basename(folder).lower() in (
                    "microsoft", "temp", "packages", "adobe", "nvidia", "intel",
                    "mozilla", "google", "microsoft corporation", "common files",
                ):
                    continue
                # Only treat a folder as an application when it actually ships an exe.
                if not _contains_exe(folder):
                    continue
                migratable, kind, reason = _is_migratable(folder, "", "")
                apps[key] = {
                    "id": _app_id(folder, folder),
                    "name": os.path.basename(folder),
                    "version": "",
                    "publisher": "",
                    "folder": folder,
                    "registry_key": "",
                    "registry_view": 64,
                    "uninstall": "",
                    "icon": "",
                    "install_date": "",
                    "estimated_size": 0,
                    "estimated_human": "未知",
                    "migratable": migratable,
                    "kind": kind if kind != "desktop" else "user",
                    "reason": reason or "位于用户目录的应用",
                    "source": "folder",
                }

    result = list(apps.values())
    for a in result:
        a["score"] = int(
            (2 if a["migratable"] else 0)
            + (1 if a.get("folder") else 0)
        )
    result.sort(key=lambda a: (not a["migratable"], a["name"].lower()))
    return {
        "apps": result,
        "total": len(result),
        "migratable": len([a for a in result if a["migratable"]]),
        "skipped": skipped,
    }


def app_size(folder: str) -> dict:
    if not folder or not os.path.isdir(folder):
        return {"size": 0, "size_human": "0 B", "count": 0}
    s, c = dir_size(folder, budget_seconds=45)
    return {"size": s, "size_human": human_size(s), "count": c}


def analyse(folder: str, dst_root: str, *, name: str = "", registry_key: str = "") -> dict:
    """Full pre-flight analysis for one application."""
    plan = mover.plan(folder, dst_root, include_registry=True, group="Apps")
    plan["app_name"] = name or os.path.basename(folder)
    plan["registry_key"] = registry_key
    plan["exe_names"] = _app_executables(folder)
    plan["running"] = winutil.watch_processes(folder, plan["exe_names"])
    if plan["running"]:
        plan["warnings"].append(
            "应用正在运行：" + "、".join(plan["running"][:5]) + "，建议先退出以免文件被占用。"
        )
    plan["services"] = find_services(folder)
    if plan["services"]:
        plan["warnings"].append(
            "检测到相关系统服务：" + "、".join(plan["services"][:5]) +
            "。服务路径会一并更新，但迁移前请确认服务已停止。"
        )
    plan["store_app"] = "windowsapps" in os.path.normcase(folder)
    return plan


def _app_executables(folder: str, limit: int = 12) -> list[str]:
    """Top-level exe names of an application (used to detect running instances)."""
    names: list[str] = []
    if not os.path.isdir(folder):
        return names
    for entry in safe_iterdir(folder):
        try:
            if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".exe"):
                names.append(entry.name)
        except OSError:
            continue
    if not names:
        for sub in safe_iterdir(folder)[:12]:
            if sub.is_dir(follow_symlinks=False):
                for entry in safe_iterdir(sub.path):
                    try:
                        if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".exe"):
                            names.append(entry.name)
                    except OSError:
                        continue
    return names[:limit]


def find_services(folder: str, limit: int = 8) -> list[str]:
    """Service names whose ImagePath points into ``folder``."""
    hits: list[str] = []
    root = os.path.normcase(os.path.abspath(folder))
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services", 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    svc = winreg.EnumKey(k, i)
                    with winreg.OpenKey(k, svc) as sk:
                        try:
                            image, _t = winreg.QueryValueEx(sk, "ImagePath")
                        except OSError:
                            continue
                        if isinstance(image, str) and root in os.path.normcase(expand(image)):
                            hits.append(svc)
                except OSError:
                    continue
                if len(hits) >= limit:
                    break
    except OSError:
        pass
    return hits


def terminate_app(folder: str) -> int:
    """Terminate processes whose executable lives inside ``folder``."""
    killed = 0
    try:
        import psutil

        root = os.path.normcase(os.path.abspath(folder))
        for p in psutil.process_iter(["pid", "exe"]):
            try:
                exe = (p.info.get("exe") or "")
                if exe and root in os.path.normcase(exe):
                    p.terminate()
                    killed += 1
            except Exception:
                continue
    except Exception:
        pass
    return killed


def migrate(app: dict, dst_root: str, *, link: bool = True, update_registry: bool = True,
            verify_hash: bool = False, kill_first: bool = False,
            report: Optional[Callable[[float, str, dict], None]] = None,
            cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Migrate an application folder and re-point everything that references it."""
    folder = app.get("folder")
    if not folder or not os.path.isdir(folder):
        raise RuntimeError("未找到该应用的安装目录")

    if kill_first:
        if report:
            report(1, "正在结束应用进程…", {})
        killed = terminate_app(folder)
        if killed and report:
            report(2, f"已结束 {killed} 个进程", {})
        time.sleep(1.0)

    dst = mover.destination_for(folder, dst_root, "Apps")
    result = mover.execute(
        folder, dst, link=link, update_registry=update_registry,
        verify_hash=verify_hash, clean_source=True, action="move_app",
        label=app.get("name") or os.path.basename(folder), report=report, cancel=cancel,
    )
    extra = {
        "app_name": app.get("name"),
        "app_id": app.get("id"),
        "registry_key": app.get("registry_key"),
        "uninstall": app.get("uninstall"),
        "kind": "app",
        "strategy": "链接+注册表" if link else "注册表改写",
    }
    result.update(extra)
    # mover.execute already wrote the base record (and stamped result["id"]).
    # Enrich that entry instead of adding a second one, so a single application
    # migration produces exactly one journal record.
    if result.get("id"):
        journal.update(result["id"], **extra)
    else:  # pragma: no cover - defensive fallback
        journal.add(result)
    return result
