"""Registry access: find / back up / rewrite path references."""

from __future__ import annotations

import os
import subprocess
import time
import winreg
from typing import Callable, Iterable, Iterator, Optional

from . import journal
from .util import human_size, long_path

# Hives we know how to walk with a friendly display name.
HIVES = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
    "HKCR": winreg.HKEY_CLASSES_ROOT,
    "HKU": winreg.HKEY_USERS,
}

# Default places worth searching for application paths.
# (hive, subkey, max_depth) - depth 0 means "values of this key only".
DEFAULT_ROOTS = [
    ("HKCU", r"Software", 6),
    ("HKLM", r"SOFTWARE", 6),
    ("HKLM", r"SOFTWARE\WOW6432Node", 6),
    ("HKLM", r"SYSTEM\CurrentControlSet\Services", 1),
    ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", 0),
    ("HKCU", r"Environment", 0),
]

# Huge branches that never hold application install paths (and COM registration
# keeps working through the junction anyway).
SKIP_LEAF = {
    "classes", "policies", "clsid", "appmodel", "cloudstore", "wow6432node",
    "installer", "setup", "licenses", "odbc", "wine", "microsoft\\windows\\currentversion\\explorer",
}
SKIP_CONTAINS = (
    "\\microsoft\\windows\\currentversion\\explorer",
    "\\microsoft\\windows\\currentversion\\appmodel",
    "\\microsoft\\windows nt\\currentversion\\",
    "\\microsoft\\windows\\currentversion\\cloudstore",
    "\\policies\\",
)

STRING_TYPES = (winreg.REG_SZ, winreg.REG_EXPAND_SZ, winreg.REG_MULTI_SZ)

WOW64_FLAGS = [
    (winreg.KEY_READ | winreg.KEY_WOW64_64KEY, "64"),
    (winreg.KEY_READ | winreg.KEY_WOW64_32KEY, "32"),
]

_NO_WINDOW = 0x08000000

# path -> (timestamp, refs)
_REF_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_REF_CACHE_TTL = 300.0


def clear_cache() -> None:
    _REF_CACHE.clear()


def _open(hive_name: str, subkey: str, access: int = winreg.KEY_READ):
    hive = HIVES[hive_name]
    return winreg.OpenKey(hive, subkey, 0, access)


def key_exists(hive_name: str, subkey: str) -> bool:
    try:
        with _open(hive_name, subkey):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _values_of(hive_name: str, subkey: str) -> list[tuple[str, object, int]]:
    out = []
    for access, _view in WOW64_FLAGS:
        try:
            with winreg.OpenKey(HIVES[hive_name], subkey, 0, access) as k:
                n_values = winreg.QueryInfoKey(k)[1]
                for i in range(n_values):
                    try:
                        name, value, vtype = winreg.EnumValue(k, i)
                        out.append((name, value, vtype))
                    except OSError:
                        continue
        except OSError:
            continue
        if out:
            break
    return out


def _text_matches(value: object, needle: str) -> bool:
    needle = needle.lower().rstrip("\\")
    if isinstance(value, str):
        return needle in value.lower()
    if isinstance(value, (list, tuple)):
        return any(needle in str(v).lower() for v in value)
    return False


def _should_skip(base: str, child: str) -> bool:
    leaf = child.rsplit("\\", 1)[-1].lower()
    if leaf in SKIP_LEAF:
        return True
    rel = child[len(base):].lower()
    return any(token in rel for token in SKIP_CONTAINS)


def find_references(
    old_path: str,
    roots: Optional[Iterable] = None,
    *,
    max_seconds: float = 12.0,
    cancel=None,
    use_cache: bool = True,
) -> list[dict]:
    """Find registry values whose text contains ``old_path``.

    Walks a depth-limited, pruned set of registry branches (the whole of
    HKLM\\SOFTWARE is far too slow to enumerate on real machines).
    """
    roots = list(roots or DEFAULT_ROOTS)
    normalized = []
    for entry in roots:
        if isinstance(entry, str):
            hive, _, sub = entry.partition("\\")
            normalized.append((hive, sub, 6))
        else:
            hive, sub = entry[0], entry[1]
            depth = entry[2] if len(entry) > 2 else 6
            normalized.append((hive, sub, depth))
    roots = normalized

    path_norm = os.path.abspath(old_path) if old_path else old_path
    cache_key = (os.path.normcase(path_norm), tuple(roots))
    if use_cache:
        hit = _REF_CACHE.get(cache_key)
        if hit and (time.time() - hit[0]) < _REF_CACHE_TTL:
            return [dict(r) for r in hit[1]]

    needle = old_path.lower().rstrip("\\")
    deadline = time.monotonic() + max_seconds
    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for hive_name, base, max_depth in roots:
        stack = [(base, 0)]
        while stack:
            if cancel and cancel():
                stack = []
                break
            if time.monotonic() > deadline:
                stack = []
                break
            subkey, depth = stack.pop()
            try:
                with winreg.OpenKey(HIVES[hive_name], subkey, 0,
                                    winreg.KEY_READ) as k:
                    if depth < max_depth:
                        n_sub = winreg.QueryInfoKey(k)[0]
                        for i in range(n_sub):
                            try:
                                name = winreg.EnumKey(k, i)
                            except OSError:
                                continue
                            child = f"{subkey}\\{name}"
                            if len(child) > 200 or _should_skip(subkey, child):
                                continue
                            stack.append((child, depth + 1))
            except OSError:
                continue

            for name, value, vtype in _values_of(hive_name, subkey):
                if vtype not in STRING_TYPES:
                    continue
                if not _text_matches(value, needle):
                    continue
                key = (hive_name, subkey, name)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "hive": hive_name,
                        "key": subkey,
                        "key_path": f"{hive_name}\\{subkey}",
                        "name": name or "(默认)",
                        "value": value if isinstance(value, str) else list(value),
                        "type": vtype,
                        "type_name": _type_name(vtype),
                    }
                )
    if use_cache and found:
        _REF_CACHE[cache_key] = (time.time(), [dict(r) for r in found])
    return found


def _type_name(vtype: int) -> str:
    return {
        winreg.REG_SZ: "REG_SZ",
        winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
        winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
        winreg.REG_DWORD: "REG_DWORD",
        winreg.REG_BINARY: "REG_BINARY",
    }.get(vtype, str(vtype))


def search_keys(term: str, *, max_results: int = 60, hints: Optional[list[str]] = None) -> list[dict]:
    """Search key *names* (and values) matching a term - used to locate app settings."""
    term_l = term.lower()
    hits: list[dict] = []
    roots = hints or DEFAULT_ROOTS
    for hive_name, base in roots:
        stack = [base]
        while stack and len(hits) < max_results:
            subkey = stack.pop()
            if term_l in subkey.lower():
                hits.append({"hive": hive_name, "key": subkey, "reason": "key-name"})
            try:
                with winreg.OpenKey(HIVES[hive_name], subkey, 0, winreg.KEY_READ) as k:
                    n_sub, n_val = winreg.QueryInfoKey(k)[0], winreg.QueryInfoKey(k)[1]
                    for i in range(n_sub):
                        try:
                            child = f"{subkey}\\{winreg.EnumKey(k, i)}"
                        except OSError:
                            continue
                        if len(child) < 200:
                            stack.append(child)
            except OSError:
                continue
    return hits[:max_results]


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------


def backup_keys(key_paths: Iterable[str], tag: str = "migration",
                on_progress: Optional[Callable[[int, int, str], None]] = None) -> str:
    """Export registry keys to a single backup folder. Returns folder path.

    ``on_progress(done, total, key_path)`` is called before each export so a
    long backup can report its progress to the UI.
    """
    import datetime

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(journal.backup_dir(), f"{tag}-{stamp}")
    os.makedirs(folder, exist_ok=True)
    index = []
    keys = sorted(set(key_paths))
    for i, key_path in enumerate(keys):
        if on_progress:
            on_progress(i, len(keys), key_path)
        hive = key_path.split("\\", 1)[0]
        exported = False
        for mirror in (["/reg:64", "/reg:32"] if hive in ("HKLM", "HKCR") else [""]):
            out = os.path.join(folder, f"{i:03d}-{key_path.replace(chr(92), '_')}{mirror.replace(':', '').replace('/', '-')}.reg")
            args = ["reg", "export", key_path, out, "/y"] + ([mirror] if mirror else [])
            try:
                proc = subprocess.run(args, capture_output=True, text=True,
                                      encoding="mbcs", errors="replace",
                                      creationflags=_NO_WINDOW)
            except Exception as exc:  # pragma: no cover
                index.append({"key": key_path, "file": None, "error": str(exc)})
                break
            if proc.returncode == 0 and os.path.exists(out):
                index.append({"key": key_path, "file": out, "error": None})
                exported = True
                break
        if not exported and not any(e["key"] == key_path for e in index):
            index.append({"key": key_path, "file": None, "error": "reg export 失败"})
    if on_progress:
        on_progress(len(keys), len(keys), "")
    with open(os.path.join(folder, "index.json"), "w", encoding="utf-8") as fh:
        import json

        json.dump(index, fh, ensure_ascii=False, indent=2)
    return folder


def restore_backup(folder: str) -> tuple[int, int, list[str]]:
    """Import every .reg file in a backup folder. -> (ok, total, errors)."""
    import json

    index_path = os.path.join(folder, "index.json")
    files: list[str] = []
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as fh:
                files = [e["file"] for e in json.load(fh) if e.get("file")]
        except Exception:
            files = []
    if not files:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".reg")]
    ok, errors = 0, []
    for f in files:
        if not os.path.exists(f):
            continue
        proc = subprocess.run(["reg", "import", f], capture_output=True, text=True,
                              encoding="mbcs", errors="replace", creationflags=_NO_WINDOW)
        if proc.returncode == 0:
            ok += 1
        else:
            errors.append(f"{os.path.basename(f)}: {proc.stderr.strip()}")
    return ok, len(files), errors


# ---------------------------------------------------------------------------
# Rewriting
# ---------------------------------------------------------------------------


def replace_in_value(value, old: str, new: str):
    """Replace ``old`` prefix/substring with ``new`` keeping the value type shape."""
    old_l = old.lower().rstrip("\\")
    if isinstance(value, str):
        if old_l in value.lower():
            # case-insensitive substring replacement, preserving the rest
            out = []
            lower_src, i = value.lower(), 0
            while True:
                j = lower_src.find(old_l, i)
                if j < 0:
                    out.append(value[i:])
                    break
                out.append(value[i:j])
                out.append(new)
                i = j + len(old_l)
            return "".join(out)
        return value
    if isinstance(value, (list, tuple)):
        return [replace_in_value(v, old, new) for v in value]
    return value


def rewrite_references(entries: Iterable[dict], old: str, new: str) -> tuple[int, list[str]]:
    """Apply new paths to the entries produced by :func:`find_references`."""
    changed, errors = 0, []
    for entry in entries:
        key_path = entry["key_path"]
        hive = entry["hive"]
        subkey = entry["key"]
        name = None if entry["name"] == "(默认)" else entry["name"]
        vtype = entry["type"]
        try:
            # ML/DW are read-only; open with write rights
            for access in (winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
                           winreg.KEY_SET_VALUE | winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(HIVES[hive], subkey, 0, access) as k:
                        current = None
                        try:
                            current = winreg.QueryValueEx(k, entry["name"] if name is not None else "")[0]
                        except OSError:
                            try:
                                current = winreg.QueryValueEx(k, name or "")[0]
                            except OSError:
                                current = entry["value"]
                        new_value = replace_in_value(current, old, new)
                        winreg.SetValueEx(k, name or "", 0, vtype, new_value)
                        changed += 1
                        break
                except OSError:
                    continue
            else:
                errors.append(f"{key_path}\\{entry['name']}: 无写入权限")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key_path}\\{entry['name']}: {exc}")
    return changed, errors


def set_install_location(key_path: str, new_path: str) -> bool:
    for access in (winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
                   winreg.KEY_SET_VALUE | winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(HIVES[key_path.split("\\")[0]],
                                key_path.split("\\", 1)[1], 0, access) as k:
                winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, new_path)
                return True
        except OSError:
            continue
    return False


def delete_key_if_empty(key_path: str) -> None:
    try:
        winreg.DeleteKey(HIVES[key_path.split("\\")[0]], key_path.split("\\", 1)[1])
    except OSError:
        pass
