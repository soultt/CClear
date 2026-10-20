"""Registry junk scanning and cleaning.

Only entries that point at files which no longer exist are reported.  Every
clean-up exports the affected keys first (``reg export``); if a backup cannot
be written the entry is skipped instead of deleted.

Registry views matter here: a 32-bit value may name ``%CommonProgramFiles%``,
which expands differently for 32-bit and 64-bit clients.  Findings therefore
carry the view they came from and are backed up with the matching ``/reg:32``
or ``/reg:64`` switch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import winreg
from typing import Callable, Iterable, Optional

from . import journal, registry, winutil
from .util import long_path

_NO_WINDOW = 0x08000000

RISK_ORDER = {"safe": 0, "caution": 1, "advanced": 2}
MAX_FINDINGS = 300          # per category, keeps backups/UI bounded
MAX_COM_SECONDS = 60.0

_WOW = {"64": winreg.KEY_WOW64_64KEY, "32": winreg.KEY_WOW64_32KEY}

_EXE_RE = re.compile(
    r"(?i)^(.*?\.(?:exe|dll|com|bat|cmd|scr|msc|cpl|ocx|sys))(?:\s|,|$)"
)
_ABS_RE = re.compile(r"(?i)^(?:[a-z]:[\\/]|%[^%]+%|\\\\)")
_VAR_RE = re.compile(r"%([^%]+)%")


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def _open(hive: str, subkey: str, access: int, view: str = "64"):
    flag = _WOW.get(view, winreg.KEY_WOW64_64KEY)
    return winreg.OpenKey(registry.HIVES[hive], subkey, 0, access | flag)


def _enum_values(hive: str, subkey: str, view: str = "64") -> list[tuple[str, object, int]]:
    out: list[tuple[str, object, int]] = []
    try:
        with _open(hive, subkey, winreg.KEY_READ, view) as k:
            count = winreg.QueryInfoKey(k)[1]
            for i in range(count):
                try:
                    out.append(winreg.EnumValue(k, i))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _enum_subkeys(hive: str, subkey: str, view: str = "64") -> list[str]:
    out: list[str] = []
    try:
        with _open(hive, subkey, winreg.KEY_READ, view) as k:
            count = winreg.QueryInfoKey(k)[0]
            for i in range(count):
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _read(hive: str, subkey: str, name: str = "", view: str = "64"):
    try:
        with _open(hive, subkey, winreg.KEY_READ, view) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _key_counts(hive: str, subkey: str, view: str = "64") -> tuple[int, int]:
    try:
        with _open(hive, subkey, winreg.KEY_READ, view) as k:
            sub, val = winreg.QueryInfoKey(k)[:2]
            return sub, val
    except OSError:
        return 0, 0


def _key_exists(hive: str, subkey: str, view: str = "64") -> bool:
    try:
        with _open(hive, subkey, winreg.KEY_READ, view):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def command_target(command) -> str:
    """Extract the executable path from a command line.

    Returns "" when the command is relative (``rundll32.exe ...``) or cannot be
    judged safely - those entries are never reported as junk.
    """
    text = str(command or "").strip()
    if not text:
        return ""
    if text[0] == '"':
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text[1:]
    quoted = re.search(r'"([^"]+)"', text)
    if quoted:
        inner = quoted.group(1).strip()
        if _ABS_RE.match(inner):
            return inner
    match = _EXE_RE.match(text)
    if not match:
        return ""
    candidate = match.group(1).strip()
    return candidate if _ABS_RE.match(candidate) else ""


def _expand_env(text: str, view: str) -> str:
    """Expand %VARS% the way a client of the given registry view would."""
    overrides: dict[str, str] = {}
    if view == "32":
        pf = os.environ.get("ProgramFiles(x86)")
        cf = os.environ.get("CommonProgramFiles(x86)")
        if pf:
            overrides["programfiles"] = pf
            overrides["programw6432"] = os.environ.get("ProgramFiles", pf)
        if cf:
            overrides["commonprogramfiles"] = cf
            overrides["commonprogramw6432"] = os.environ.get("CommonProgramFiles", cf)
    if not overrides:
        return os.path.expandvars(text)

    def repl(match: re.Match) -> str:
        name = match.group(1).lower()
        if name in overrides:
            return overrides[name]
        return os.environ.get(match.group(1), match.group(0))

    return _VAR_RE.sub(repl, text)


def _normalize(raw, view: str = "64") -> str:
    """Expand a registry path; "" when it cannot be resolved safely."""
    text = str(raw or "").strip().strip('"')
    if not text:
        return ""
    if text[:4].lower() == "\\??\\":
        text = text[4:]
    if text.lower().startswith("\\systemroot\\"):
        text = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            text[len("\\systemroot\\"):])
    if text.startswith("\\\\?\\"):
        return text
    text = _expand_env(text, view)
    if "%" in text or not os.path.isabs(text):
        return ""
    return text


def _missing(path: str) -> bool:
    if not path:
        return False
    try:
        return not os.path.exists(long_path(path))
    except (OSError, ValueError):
        return False


def _candidates(raw) -> set[str]:
    """Every concrete path a registry value could resolve to.

    A 32-bit client is redirected from ``System32`` to ``SysWOW64``, and the
    value itself may use a per-view environment variable, so both views and
    both system directories are considered before calling a file missing.
    """
    out: set[str] = set()
    for view in ("64", "32"):
        path = _normalize(raw, view)
        if not path:
            continue
        out.add(path)
        low = path.lower()
        for sysdir, other in (("\\system32\\", "\\SysWOW64\\"),
                              ("\\syswow64\\", "\\System32\\")):
            if sysdir in low:
                idx = low.index(sysdir)
                out.add(path[:idx] + other + path[idx + len(sysdir):])
    return out


def _missing_any_view(raw) -> bool:
    """True only when the path is missing under *both* registry views.

    A 32-bit value may say ``%CommonProgramFiles%`` while the file only exists
    in the 64-bit location (or the other way round).  Reporting such a value as
    junk would delete a working registration, so the check is conservative.
    """
    candidates = _candidates(raw)
    if not candidates:
        return False
    return all(_missing(c) for c in candidates)


def _command_missing(command, view: str = "64") -> bool:
    target = command_target(command)
    if not _normalize(target, view):
        return False
    return _missing_any_view(target)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _finding(hive: str, key: str, name: str, target, issue: str, *,
             value_name: Optional[str] = None, delete_key: bool = False,
             view: str = "64", admin: bool = False) -> dict:
    return {
        "hive": hive,
        "key": key,
        "key_path": f"{hive}\\{key}",
        "name": name or "(默认)",
        "value_name": value_name,
        "delete_key": delete_key,
        "view": view,
        "views": [view],
        "target": str(target or ""),
        "issue": issue,
        "admin": admin,
    }


def _dedupe(findings: Iterable[dict]) -> list[dict]:
    """Merge the 64-bit and 32-bit view findings of the same registry entry.

    ``HKLM\\SOFTWARE`` keys are largely shared between views, so the same
    orphan shows up twice.  Merging them keeps the item count meaningful and
    makes the clean-up step delete the entry in *every* view it exists in.
    """
    merged: dict[tuple[str, str], dict] = {}
    for f in findings:
        sig = (f["key_path"].lower(), (f["value_name"] or "").lower())
        prev = merged.get(sig)
        if prev is None:
            merged[sig] = f
            continue
        for view in f.get("views") or [f["view"]]:
            if view not in prev["views"]:
                prev["views"].append(view)
    return list(merged.values())


# ---------------------------------------------------------------------------
# Scanners (each returns a list of findings)
# ---------------------------------------------------------------------------

_RUN_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\Run",
    r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
)


def _scan_startup(hive: str, admin: bool) -> Callable[..., list[dict]]:
    def scanner(cancel=None) -> list[dict]:
        out: list[dict] = []
        views = ("64", "32") if hive == "HKLM" else ("64",)
        for view in views:
            for base in _RUN_KEYS:
                for name, value, vtype in _enum_values(hive, base, view):
                    if cancel and cancel():
                        return out
                    if vtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        continue
                    if not _command_missing(value, view):
                        continue
                    out.append(_finding(hive, base, name, value, "启动目标不存在",
                                        value_name=name, view=view, admin=admin))
                    if len(out) >= MAX_FINDINGS:
                        return out
        return out

    return scanner


def _scan_app_paths(cancel=None) -> list[dict]:
    out: list[dict] = []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for view in ("64", "32"):
        for leaf in _enum_subkeys("HKLM", base, view):
            if cancel and cancel():
                return out
            sub = f"{base}\\{leaf}"
            exe = _read("HKLM", sub, "", view)
            exe_text = exe.strip() if isinstance(exe, str) else ""
            target = command_target(exe_text)
            if target:
                if not _missing_any_view(target):
                    continue
                issue = "应用路径不存在"
            elif exe_text:
                continue                    # relative / non-path value: cannot judge
            else:
                path = _read("HKLM", sub, "Path", view)
                if isinstance(path, str) and any(
                        _normalize(path, v) and not _missing(_normalize(path, v))
                        for v in ("64", "32")):
                    continue
                if _key_counts("HKLM", sub, view)[1] > 0:
                    continue                # metadata-only entries are legitimate
                issue = "空的路径项"
            out.append(_finding("HKLM", sub, leaf, target, issue,
                                delete_key=True, view=view, admin=True))
            if len(out) >= MAX_FINDINGS:
                return out
    return out


def _scan_shared_dlls(cancel=None) -> list[dict]:
    out: list[dict] = []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs"
    for view in ("64", "32"):
        for name, _value, _vtype in _enum_values("HKLM", base, view):
            if cancel and cancel():
                return out
            if not _missing_any_view(name):
                continue
            out.append(_finding("HKLM", base, name, name, "文件不存在",
                                value_name=name, view=view, admin=True))
            if len(out) >= MAX_FINDINGS:
                return out
    return out


def _scan_fonts(cancel=None) -> list[dict]:
    out: list[dict] = []
    base = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    fonts_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")
    for view in ("64", "32"):
        for name, value, _vtype in _enum_values("HKLM", base, view):
            if cancel and cancel():
                return out
            raw = str(value or "").strip().strip('"')
            if not raw:
                continue
            path = _normalize(raw, view)
            if not path:
                if "\\" in raw or "/" in raw:
                    continue                # relative with a folder: cannot judge
                path = os.path.join(fonts_dir, raw)
                if not _missing(path):
                    continue
            elif not _missing_any_view(raw):
                continue
            out.append(_finding("HKLM", base, name, raw, "字体文件不存在",
                                value_name=name, view=view, admin=True))
            if len(out) >= MAX_FINDINGS:
                return out
    return out


def _scan_uninstall(hive: str, admin: bool) -> Callable[..., list[dict]]:
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

    def scanner(cancel=None) -> list[dict]:
        out: list[dict] = []
        views = ("64", "32") if hive == "HKLM" else ("64",)
        for view in views:
            for leaf in _enum_subkeys(hive, base, view):
                if cancel and cancel():
                    return out
                sub = f"{base}\\{leaf}"
                display = _read(hive, sub, "DisplayName", view)
                loc = _read(hive, sub, "InstallLocation", view)
                uninst = _read(hive, sub, "UninstallString", view)
                icon = _read(hive, sub, "DisplayIcon", view)

                # Entries owned by Windows Installer or a Burn bundle must not be
                # deleted from the registry directly: the MSI database / bundle
                # cache would still reference them and repair or uninstall would
                # break.  Same for updates and system components.
                if _read(hive, sub, "WindowsInstaller", view) in (1, "1"):
                    continue
                if _read(hive, sub, "SystemComponent", view) in (1, "1"):
                    continue
                if _read(hive, sub, "NoRemove", view) in (1, "1"):
                    continue
                if _read(hive, sub, "BundleProviderKey", view):
                    continue
                if _read(hive, sub, "ParentKeyName", view) or \
                        _read(hive, sub, "ParentDisplayName", view):
                    continue

                display_text = display.strip() if isinstance(display, str) else ""
                loc_text = loc.strip() if isinstance(loc, str) else ""
                uninst_text = uninst.strip() if isinstance(uninst, str) else ""
                icon_text = icon.strip() if isinstance(icon, str) else ""

                loc_missing = bool(_normalize(loc_text, view)) and \
                    _missing(_normalize(loc_text, view))
                uninst_missing = _command_missing(uninst_text, view)
                icon_ok = bool(_normalize(command_target(icon_text), view)) and \
                    not _command_missing(icon_text, view)

                orphan = False
                issue = ""
                if loc_missing and (uninst_missing or not uninst_text):
                    orphan, issue = True, "安装目录与卸载程序均不存在"
                elif uninst_missing and not loc_text and not icon_ok:
                    orphan, issue = True, "卸载程序不存在"
                elif not display_text and not loc_text and not uninst_text:
                    # Only a truly empty shell is junk - MSI/update entries
                    # carry metadata values (EstimatedSize, SystemComponent…).
                    sub_count, val_count = _key_counts(hive, sub, view)
                    if sub_count == 0 and val_count == 0:
                        orphan, issue = True, "空的卸载项"
                if not orphan:
                    continue
                out.append(_finding(hive, sub, display_text or leaf,
                                    loc_text or uninst_text, issue,
                                    delete_key=True, view=view, admin=admin))
                if len(out) >= MAX_FINDINGS:
                    return out
        return out

    return scanner


_COM_SKIP = ("LocalServer32", "InprocHandler32", "TreatAs")


def _com_server_ok(hive: str, sub: str, view: str) -> bool:
    """True when this CLSID still has a usable server in the given view."""
    dll = _read(hive, f"{sub}\\InprocServer32", "", view)
    if isinstance(dll, str):
        target = command_target(dll) or dll
        if _normalize(target, view) and not _missing_any_view(target):
            return True
    for leaf in _COM_SKIP:
        if _key_exists(hive, f"{sub}\\{leaf}", view):
            return True
    return False


def _scan_com(cancel=None) -> list[dict]:
    out: list[dict] = []
    base = r"SOFTWARE\Classes\CLSID"
    deadline = time.monotonic() + MAX_COM_SECONDS
    for view in ("64", "32"):
        other = "32" if view == "64" else "64"
        for clsid in _enum_subkeys("HKLM", base, view):
            if cancel and cancel():
                return out
            if time.monotonic() > deadline or len(out) >= MAX_FINDINGS:
                return out
            sub = f"{base}\\{clsid}"
            dll = _read("HKLM", f"{sub}\\InprocServer32", "", view)
            if not isinstance(dll, str):
                continue
            target = command_target(dll) or dll
            if not _normalize(target, view):
                continue
            # Only an orphan when *neither* registry view has a working server:
            # the 32-bit view often points at SysWOW64 while the 64-bit entry is
            # already broken (or the other way round).
            if _com_server_ok("HKLM", sub, view) or _com_server_ok("HKLM", sub, other):
                continue
            out.append(_finding("HKLM", sub, clsid, dll, "组件 DLL 不存在",
                                delete_key=True, view=view, admin=True))
    return out


def _scan_mru(cancel=None) -> list[dict]:
    out: list[dict] = []
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer"
    sources = (
        (f"{base}\\RunMRU", "“运行”对话框历史"),
        (f"{base}\\TypedPaths", "地址栏输入记录"),
    )
    for sub, label in sources:
        for name, value, _vtype in _enum_values("HKCU", sub):
            if cancel and cancel():
                return out
            if name.lower() == "mrulist":
                continue
            out.append(_finding("HKCU", sub, name, value, label, value_name=name))
            if len(out) >= MAX_FINDINGS:
                return out
    return out


# ---------------------------------------------------------------------------
# Categories (declared safe -> caution -> advanced)
# ---------------------------------------------------------------------------


def _cat(**kw) -> dict:
    base = {
        "id": "",
        "name": "",
        "desc": "",
        "group": "注册表",
        "icon": "shield",
        "risk": "safe",          # safe | caution | advanced
        "admin": False,
        "default": False,
        "scanner": None,
    }
    base.update(kw)
    return base


CATEGORIES: list[dict] = [
    _cat(
        id="reg_startup_user", name="用户启动项残留", risk="safe", default=True,
        desc="当前用户“启动”列表（Run / RunOnce）中指向已删除程序的条目。删除后不影响任何现存程序的自启动。",
        scanner=_scan_startup("HKCU", False),
    ),
    _cat(
        id="reg_startup_system", name="系统启动项残留", risk="safe", admin=True,
        desc="所有用户共享的启动项（HKLM Run / RunOnce）中指向已删除程序的条目。需要管理员权限。",
        scanner=_scan_startup("HKLM", True),
    ),
    _cat(
        id="reg_app_paths", name="失效的应用路径", risk="safe", admin=True,
        desc="App Paths 注册项指向不存在的程序，通常是卸载不干净留下的“运行”命令入口。需要管理员权限。",
        scanner=_scan_app_paths,
    ),
    _cat(
        id="reg_shared_dlls", name="失效的共享 DLL 记录", risk="safe", admin=True,
        desc="SharedDLLs 中记录已不存在的 DLL 文件，属于安装/卸载残留的引用计数。需要管理员权限。",
        scanner=_scan_shared_dlls,
    ),
    _cat(
        id="reg_fonts", name="失效的字体注册", risk="safe", admin=True,
        desc="字体注册表中指向已删除字体文件的条目，删除后系统会忽略这些无效字体名。需要管理员权限。",
        scanner=_scan_fonts,
    ),
    _cat(
        id="reg_mru", name="最近打开记录", risk="safe", default=True,
        desc="“运行”对话框历史与资源管理器地址栏输入记录，属于隐私痕迹，删除不影响系统功能。",
        scanner=_scan_mru,
    ),
    _cat(
        id="reg_uninstall_user", name="用户卸载信息残留", risk="caution",
        desc="当前用户卸载列表中安装目录与卸载程序都已不存在的条目。删除后“应用和功能”列表会更干净，请确认程序确实已卸载。",
        scanner=_scan_uninstall("HKCU", False),
    ),
    _cat(
        id="reg_uninstall_system", name="系统卸载信息残留", risk="caution", admin=True,
        desc="所有用户卸载列表中安装目录与卸载程序都已不存在的条目。删除前请确认程序确实已卸载。需要管理员权限。",
        scanner=_scan_uninstall("HKLM", True),
    ),
    _cat(
        id="reg_com", name="失效的 COM 组件", risk="advanced", admin=True,
        desc="CLSID 注册的 InprocServer32 指向不存在的 DLL，且没有本地服务器作为后备。属于高级项目：可能影响老旧程序的组件加载，"
             "清理前会自动导出完整注册表项。需要管理员权限。",
        scanner=_scan_com,
    ),
]

CATEGORY_INDEX = {c["id"]: c for c in CATEGORIES}


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _item_view(f: dict) -> dict:
    return {
        "label": f["name"],
        "detail": f"{f['key_path']} → {f['target']}" if f["target"] else f["key_path"],
        "key": f["key_path"],
        "target": f["target"],
        "issue": f["issue"],
        "admin": f["admin"],
    }


def _sort_key(cat: dict):
    """Categories keep their declaration order within a safety level."""
    return RISK_ORDER.get(cat["risk"], 9)


def scan(
    ids: Optional[list[str]] = None,
    *,
    report: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    if ids:
        wanted = [i for i in ids if i in CATEGORY_INDEX]
        cats = [c for c in CATEGORIES if c["id"] in wanted]
    else:
        cats = list(CATEGORIES)
    results: list[dict] = []
    total = 0
    for i, cat in enumerate(cats):
        if cancel and cancel():
            break
        if report:
            report((i / max(1, len(cats))) * 100, f"正在扫描注册表：{cat['name']}")
        findings = cat["scanner"](cancel=cancel) if cat.get("scanner") else []
        findings = _dedupe(findings)
        findings.sort(key=lambda f: (f["key_path"].lower(), f["name"].lower()))
        results.append({
            "id": cat["id"],
            "name": cat["name"],
            "desc": cat["desc"],
            "group": cat["group"],
            "icon": cat["icon"],
            "risk": cat["risk"],
            "admin": cat["admin"],
            "default": cat["default"],
            "count": len(findings),
            "items": [_item_view(f) for f in findings[:40]],
        })
        total += len(findings)
        if report:
            report(((i + 1) / max(1, len(cats))) * 100,
                   f"已扫描：{cat['name']}（{len(findings)} 项）")
    results.sort(key=_sort_key)
    return {"categories": results, "total": total, "total_human": f"{total} 项"}


# ---------------------------------------------------------------------------
# Backup (view-aware)
# ---------------------------------------------------------------------------


def _export(f: dict, out_file: str) -> tuple[bool, str]:
    args = ["reg", "export", f["key_path"], out_file, "/y"]
    if f["hive"] in ("HKLM", "HKCR"):
        args.append("/reg:32" if f["view"] == "32" else "/reg:64")
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="mbcs", errors="replace",
                              creationflags=_NO_WINDOW)
    except Exception as exc:  # pragma: no cover - depends on the environment
        return False, str(exc)
    if proc.returncode == 0 and os.path.exists(out_file):
        return True, ""
    return False, (proc.stderr or proc.stdout or "reg export 失败").strip()[:200]


def _backup(findings: Iterable[dict], tag: str) -> tuple[str, set[tuple[str, str]]]:
    """Export every affected key with the correct registry view.

    Returns ``(folder, backed_up_keys)`` where each key is ``(key_path, view)``.
    Keys that could not be exported are absent from the set and therefore never
    deleted.
    """
    import datetime

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(journal.backup_dir(), f"{tag}-{stamp}")
    os.makedirs(folder, exist_ok=True)

    unique: dict[tuple[str, str], dict] = {}
    for f in findings:
        for view in f.get("views") or [f["view"]]:
            unique.setdefault((f["key_path"].lower(), view), {**f, "view": view})

    index: list[dict] = []
    backed: set[tuple[str, str]] = set()
    for i, ((_key, view), f) in enumerate(unique.items()):
        if not _key_exists(f["hive"], f["key"], view):
            # Nothing to delete in this view - no export needed.
            index.append({"key": f["key_path"], "view": view,
                          "file": None, "error": None, "missing": True})
            backed.add((f["key_path"].lower(), view))
            continue
        safe = f["key_path"].replace("\\", "_").replace(":", "")
        out = os.path.join(folder, f"{i:03d}-{safe}-{view}.reg")
        ok, err = _export(f, out)
        index.append({"key": f["key_path"], "view": view,
                      "file": out if ok else None, "error": err or None})
        if ok:
            backed.add((f["key_path"].lower(), view))
    with open(os.path.join(folder, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    return folder, backed


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


def _delete_key_tree(hive: str, subkey: str, view: str = "64") -> None:
    with _open(hive, subkey, winreg.KEY_READ | winreg.KEY_WRITE, view) as k:
        while True:
            try:
                child = winreg.EnumKey(k, 0)
            except OSError:
                break
            _delete_key_tree(hive, f"{subkey}\\{child}", view)
    parent, _, leaf = subkey.rpartition("\\")
    if not parent:
        return
    with _open(hive, parent, winreg.KEY_WRITE, view) as pk:
        winreg.DeleteKey(pk, leaf)


def _remove(f: dict) -> tuple[bool, str]:
    try:
        if f.get("delete_key"):
            _delete_key_tree(f["hive"], f["key"], f["view"])
        else:
            with _open(f["hive"], f["key"], winreg.KEY_SET_VALUE, f["view"]) as k:
                winreg.DeleteValue(k, f["value_name"] or "")
        return True, ""
    except FileNotFoundError:
        return True, ""
    except OSError as exc:
        return False, str(exc)


def clean(
    ids: list[str],
    *,
    report: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    cats = [CATEGORY_INDEX[i] for i in ids if i in CATEGORY_INDEX]
    removed = failed = skipped = 0
    details: list[dict] = []
    backups: list[str] = []
    per_category: list[dict] = []

    for i, cat in enumerate(cats):
        if cancel and cancel():
            break
        if report:
            report((i / max(1, len(cats))) * 100, f"正在检查：{cat['name']}")
        if cat.get("admin") and not winutil.is_admin():
            skipped += 1
            per_category.append({"id": cat["id"], "name": cat["name"], "risk": cat["risk"],
                                 "removed": 0, "failed": 0, "backup": "",
                                 "message": "需要管理员权限，已跳过"})
            continue
        findings = _dedupe(cat["scanner"](cancel=cancel) if cat.get("scanner") else [])
        if not findings:
            per_category.append({"id": cat["id"], "name": cat["name"], "risk": cat["risk"],
                                 "removed": 0, "failed": 0, "backup": ""})
            continue

        if report:
            report(((i + 0.15) / max(1, len(cats))) * 100,
                   f"正在备份 {cat['name']}（{len(findings)} 项）…")
        try:
            folder, backed = _backup(findings, tag=f"regclean-{cat['id']}")
        except Exception as exc:  # noqa: BLE001 - backup must never break the run
            failed += len(findings)
            details.append({"category": cat["name"], "path": "", "ok": False,
                            "message": f"备份失败，已跳过整组：{exc}"})
            per_category.append({"id": cat["id"], "name": cat["name"], "risk": cat["risk"],
                                 "removed": 0, "failed": len(findings), "backup": ""})
            continue
        backups.append(folder)

        cat_removed = cat_failed = 0
        for j, f in enumerate(findings):
            if cancel and cancel():
                break
            if report:
                report(((i + 0.2 + 0.75 * (j / max(1, len(findings))))
                        / max(1, len(cats))) * 100,
                       f"正在清理：{f['name']}")
            for view in f.get("views") or [f["view"]]:
                if (f["key_path"].lower(), view) not in backed:
                    cat_failed += 1
                    details.append({"category": cat["name"], "path": f["key_path"],
                                    "name": f["name"], "ok": False,
                                    "message": "备份未成功，已跳过该项"})
                    continue
                ok, err = _remove({**f, "view": view})
                if ok:
                    cat_removed += 1
                else:
                    cat_failed += 1
                    details.append({"category": cat["name"], "path": f["key_path"],
                                    "name": f["name"], "ok": False, "message": err})
        removed += cat_removed
        failed += cat_failed
        per_category.append({"id": cat["id"], "name": cat["name"], "risk": cat["risk"],
                             "removed": cat_removed, "failed": cat_failed,
                             "backup": folder})

    if report:
        report(100, f"注册表清理完成：{removed} 项")
    return {
        "removed": removed,
        "failed": failed,
        "skipped": skipped,
        "total": removed + failed,
        "backups": backups,
        "details": details[:300],
        "per_category": per_category,
    }


def category_meta() -> list[dict]:
    """Category list for the UI, already ordered safe -> caution -> advanced."""
    return [
        {
            "id": c["id"], "name": c["name"], "desc": c["desc"], "group": c["group"],
            "icon": c["icon"], "risk": c["risk"], "admin": c["admin"],
            "default": c["default"],
        }
        for c in sorted(CATEGORIES, key=lambda x: RISK_ORDER.get(x["risk"], 9))
    ]
