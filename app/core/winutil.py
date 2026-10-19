"""Windows platform layer: privileges, drives, junctions, processes, shelling out."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Iterable, Optional

from .util import human_size, long_path

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Privileges
# ---------------------------------------------------------------------------


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(extra_args: Optional[list[str]] = None) -> bool:
    """Relaunch this process elevated (UAC prompt). Returns True if launched."""
    if not IS_WINDOWS:
        return False
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = subprocess.list2cmdline(list(extra_args or []))
    else:
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        params = subprocess.list2cmdline([script, *(extra_args or [])])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, os.path.dirname(os.path.abspath(sys.argv[0])), 1
        )
        return int(rc) > 32
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Drives
# ---------------------------------------------------------------------------


def system_drive() -> str:
    return (os.environ.get("SystemDrive") or "C:").upper()


def list_drives() -> list[dict]:
    """Fixed + removable drives with capacity info."""
    drives: list[dict] = []
    if not IS_WINDOWS:
        for mount in ("/",):
            try:
                usage = shutil.disk_usage(mount)
                drives.append(
                    {
                        "letter": mount,
                        "path": mount,
                        "label": "Root",
                        "fs": "",
                        "total": usage.total,
                        "used": usage.used,
                        "free": usage.free,
                        "is_system": True,
                        "removable": False,
                    }
                )
            except OSError:
                pass
        return drives

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        letter = f"{chr(65 + i)}:"
        root = letter + "\\"
        kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        # 2 removable, 3 fixed, 4 remote, 5 cdrom, 6 ramdisk
        if kind not in (2, 3):
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        label, fs = _volume_label(root)
        drives.append(
            {
                "letter": letter,
                "path": root,
                "label": label,
                "fs": fs,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "is_system": letter.upper() == system_drive(),
                "removable": kind == 2,
            }
        )
    drives.sort(key=lambda d: d["letter"])
    return drives


def _volume_label(root: str) -> tuple[str, str]:
    buf = ctypes.create_unicode_buffer(261)
    fsbuf = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    maxlen = wintypes.DWORD()
    flags = wintypes.DWORD()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), buf, 261, ctypes.byref(serial), ctypes.byref(maxlen),
        ctypes.byref(flags), fsbuf, 261,
    )
    if not ok:
        return ("", "")
    return (buf.value or "本地磁盘", fsbuf.value or "")


def relocation_targets(exclude_drive: Optional[str] = None) -> list[dict]:
    """Drives that can receive data (non-system, not the excluded one)."""
    exclude = (exclude_drive or "").upper()
    out = []
    for d in list_drives():
        if d["is_system"] or d["letter"].upper() == exclude:
            continue
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Reparse points / junctions
# ---------------------------------------------------------------------------


def is_reparse_point(path: str) -> bool:
    try:
        st = os.lstat(long_path(path))
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def read_link(path: str) -> Optional[str]:
    """Target of a junction/symlink, with the Win32 long-path prefix removed."""
    try:
        target = os.readlink(long_path(path))
    except OSError:
        return None
    if not target:
        return target
    if target.startswith("\\\\?\\UNC\\"):
        return "\\\\" + target[8:]
    if target.startswith("\\\\?\\"):
        return target[4:]
    return target


def create_junction(link: str, target: str) -> tuple[bool, str]:
    """Create an NTFS directory junction. No admin rights required."""
    link = os.path.abspath(link)
    target = os.path.abspath(target)
    if os.path.exists(link):
            return False, f"链接位置已存在: {link}"
    if not os.path.isdir(target):
            return False, f"目标目录不存在: {target}"
    try:
        os.makedirs(os.path.dirname(link), exist_ok=True)
    except OSError:
        pass
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target],
        capture_output=True, text=True, encoding="mbcs", errors="replace",
        creationflags=_NO_WINDOW,
    )
    ok = proc.returncode == 0 and os.path.exists(link)
    if not ok:
        msg = (proc.stdout or "") + (proc.stderr or "")
        return False, f"创建联接失败: {msg.strip() or 'mklink 返回 ' + str(proc.returncode)}"
    return True, ""


def create_symlink(link: str, target: str) -> tuple[bool, str]:
    """Create a file symbolic link (needs admin rights or Developer Mode)."""
    link = os.path.abspath(link)
    target = os.path.abspath(target)
    if os.path.exists(link):
            return False, f"链接位置已存在: {link}"
    if not os.path.exists(target):
            return False, f"目标不存在: {target}"
    try:
        os.makedirs(os.path.dirname(link), exist_ok=True)
    except OSError:
        pass
    proc = subprocess.run(
        ["cmd", "/c", "mklink", link, target], capture_output=True, text=True,
        encoding="mbcs", errors="replace", creationflags=_NO_WINDOW,
    )
    if proc.returncode == 0 and os.path.exists(link):
        return True, ""
    msg = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "privilege" in msg.lower() or "权限" in msg:
        return False, "创建符号链接需要管理员权限（或开启开发者模式）"
    return False, f"创建符号链接失败：{msg or proc.returncode}"


def remove_reparse_point(path: str) -> tuple[bool, str]:
    try:
        os.rmdir(long_path(path))
        return True, ""
    except OSError as exc:
        # fall back to cmd /c rmdir which never follows the link
        proc = subprocess.run(
            ["cmd", "/c", "rmdir", path], capture_output=True, text=True,
            encoding="mbcs", errors="replace", creationflags=_NO_WINDOW,
        )
        if proc.returncode == 0:
            return True, ""
        return False, f"删除联接失败: {exc}"


_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


# ---------------------------------------------------------------------------
# Processes / locks
# ---------------------------------------------------------------------------


def running_processes() -> set[str]:
    """Lower-cased image names of running processes."""
    names: set[str] = set()
    try:
        import psutil

        for p in psutil.process_iter(["name"]):
            n = p.info.get("name")
            if n:
                names.add(n.lower())
    except Exception:
        pass
    return names


def processes_using(folder: str, limit: int = 40) -> list[dict]:
    """Best-effort: processes that appear to use a path (needs admin for accuracy)."""
    hits: list[dict] = []
    root = os.path.normcase(os.path.abspath(folder)).rstrip("\\")
    try:
        import psutil

        for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                info = p.info
                candidates = [info.get("exe") or ""]
                candidates.extend(info.get("cmdline") or [])
                for c in candidates:
                    if c and root in os.path.normcase(os.path.abspath(c)):
                        hits.append({"pid": info["pid"], "name": info.get("name") or "?"})
                        break
            except Exception:
                continue
            if len(hits) >= limit:
                break
    except Exception:
        pass
    return hits


def watch_processes(path: str, names: Iterable[str]) -> list[str]:
    """Return which of ``names`` are currently running."""
    have = running_processes()
    return [n for n in names if n.lower() in have]


# ---------------------------------------------------------------------------
# Copy engine
# ---------------------------------------------------------------------------


class CopyProgress:
    """Progress reported during a copy."""

    def __init__(self, path: str, message: str = "", percent: float = 0.0,
                 done_bytes: int = 0, total_bytes: int = 0, speed_bps: float = 0.0):
        self.path = path
        self.message = message
        self.percent = percent
        self.done_bytes = done_bytes
        self.total_bytes = total_bytes
        self.speed_bps = speed_bps

    @property
    def speed(self) -> str:
        return (human_size(self.speed_bps) + "/s") if self.speed_bps else ""

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "message": self.message,
            "percent": round(self.percent, 2),
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "done_human": human_size(self.done_bytes),
            "total_human": human_size(self.total_bytes),
            "speed": human_size(self.speed_bps) + "/s" if self.speed_bps else "",
        }


def _iter_files_with_size(src: str) -> tuple[list[tuple[str, int, int]], int]:
    """[(src_file, rel, size)] and total size."""
    from .util import dir_size, safe_iterdir

    items: list[tuple[str, int, int]] = []
    total = 0
    stack = [src]
    while stack:
        cur = stack.pop()
        for entry in safe_iterdir(cur):
            try:
                if entry.is_dir(follow_symlinks=False):
                    if is_reparse_point(entry.path):
                        continue
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    rel = os.path.relpath(entry.path, src)
                    items.append((entry.path, rel, size))
                    total += size
            except OSError:
                continue
    return items, total


def copy_tree(
    src: str,
    dst: str,
    *,
    report: Optional[Callable[[CopyProgress], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    min_free_bytes: int = 0,
) -> tuple[bool, str]:
    """Copy a directory tree with byte-accurate progress.

    Uses robocopy when available (better ACL/long-path handling), reporting
    progress by polling the destination size.
    """
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if not os.path.isdir(src):
        return False, f"源目录不存在: {src}"
    if os.path.exists(dst):
            return False, f"目标已存在: {dst}"
    from .util import free_space

    if min_free_bytes and free_space(dst) < min_free_bytes:
        return False, "目标磁盘剩余空间不足"

    os.makedirs(long_path(dst), exist_ok=True)
    robocopy = shutil.which("robocopy") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "robocopy.exe"
    )

    if os.path.isfile(robocopy):
        return _robocopy_tree(src, dst, robocopy, report, cancel)
    return _python_copy_tree(src, dst, report, cancel)


def _robocopy_tree(src, dst, robocopy, report, cancel) -> tuple[bool, str]:
    items, total = _iter_files_with_size(src)
    args = [
        robocopy, src, dst, "/E", "/COPY:DAT", "/DCOPY:DAT",
        "/R:1", "/W:1", "/XJ", "/NFL", "/NDL", "/NP", "/NJH", "/NJS", "/MT:8",
    ]
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    start = time.monotonic()
    last = 0
    try:
        while proc.poll() is None:
            if cancel and cancel():
                proc.kill()
                return False, "已取消"
            done = _dir_bytes_fast(dst, total)
            now = time.monotonic()
            if report:
                elapsed = max(0.001, now - start)
                speed = done / elapsed
                remaining = max(0, total - done)
                eta = remaining / speed if speed > 0 else 0
                report(
                    CopyProgress(
                        path=dst,
                        message=f"正在复制 {human_size(done)} / {human_size(total)}"
                        + (f"，预计剩余 {int(eta)} 秒" if eta and eta < 86400 else ""),
                        percent=(done / total * 100) if total else 100.0,
                        done_bytes=done,
                        total_bytes=total,
                        speed_bps=speed,
                    )
                )
            last = done
            time.sleep(0.6)
    except Exception as exc:  # pragma: no cover
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"复制失败: {exc}"

    err = (proc.stderr.read() or b"").decode("mbcs", "replace").strip()
    if proc.returncode is not None and proc.returncode >= 8:
        return False, f"复制失败 (robocopy {proc.returncode}): {err[:400]}"
    dst_total, dst_count = _count_tree(dst)
    if dst_count < len(items):
            return False, f"复制不完整: {dst_count}/{len(items)} 个文件"
    if report:
        report(CopyProgress(path=dst, message="复制完成", percent=100.0,
                            done_bytes=dst_total, total_bytes=total))
    return True, ""


def _count_tree(root: str) -> tuple[int, int]:
    from .util import dir_size

    return dir_size(root)


def _dir_bytes_fast(root: str, cap: int) -> int:
    """Cheap running total of a directory (capped early for speed)."""
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(long_path(root)):
            dirnames[:] = [d for d in dirnames if not is_reparse_point(os.path.join(dirpath, d))]
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
            if total > cap * 2:
                break
    except OSError:
        pass
    return total


def _python_copy_tree(src, dst, report, cancel) -> tuple[bool, str]:
    items, total = _iter_files_with_size(src)
    done = 0
    start = time.monotonic()
    for src_file, rel, size in items:
        if cancel and cancel():
                return False, "已取消"
        out = os.path.join(dst, rel)
        try:
            os.makedirs(long_path(os.path.dirname(out)), exist_ok=True)
            shutil.copy2(long_path(src_file), long_path(out))
        except OSError as exc:
            return False, f"复制失败 {rel}: {exc}"
        done += size
        if report:
            elapsed = max(0.001, time.monotonic() - start)
            report(
                CopyProgress(
                    path=out,
                    message=f"正在复制 {rel}",
                    percent=(done / total * 100) if total else 100.0,
                    done_bytes=done,
                    total_bytes=total,
                    speed_bps=done / elapsed,
                )
            )
    return True, ""


def delete_tree(path: str) -> tuple[bool, str]:
    """Delete a directory tree (reparse points removed, not followed)."""
    path = os.path.abspath(path)

    def onerror(func, p, exc_info):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass

    try:
        if is_reparse_point(path):
            return remove_reparse_point(path)
        shutil.rmtree(long_path(path), onerror=onerror)
        return True, ""
    except OSError as exc:
        proc = subprocess.run(
            ["cmd", "/c", "rd", "/s", "/q", path], capture_output=True, text=True,
            encoding="mbcs", errors="replace", creationflags=_NO_WINDOW,
        )
        if not os.path.exists(path):
            return True, ""
        return False, f"删除失败: {exc}"


# ---------------------------------------------------------------------------
# Shell / notifications / shortcuts
# ---------------------------------------------------------------------------


def open_in_explorer(path: str) -> None:
    try:
        if os.path.isdir(path):
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["explorer", "/select,", os.path.abspath(path)],
                             creationflags=_NO_WINDOW)
    except Exception:
        pass


def run_powershell(script: str, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "PowerShell 执行超时"
    except Exception as exc:
        return 1, str(exc)


# ---------------------------------------------------------------------------
# Native pickers
# ---------------------------------------------------------------------------
#
# The dialog is hosted by a PowerShell child process. That process is not the
# foreground window owner, so Windows opens the dialog *behind* the browser and
# the user appears to get no popup at all. Once the dialog exists we flip it to
# topmost so it is always visible.

# The marker lives in the script so orphaned dialogs can be found later.
_PICK_MARK = "CCLEAR_PICKER_V1"
_pick_lock = threading.Lock()

_PICK_PS = r"""
# CCLEAR_PICKER_V1
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class CClearWin {
  private delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr h);
  [DllImport("kernel32.dll")] private static extern uint GetCurrentProcessId();
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
  public const uint SWP_NOMOVE = 0x2, SWP_NOSIZE = 0x1, SWP_SHOWWINDOW = 0x40;

  public static IntPtr FindDialog() {
    IntPtr found = IntPtr.Zero;
    uint me = GetCurrentProcessId();
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      uint pid;
      GetWindowThreadProcessId(h, out pid);
      if (pid == me && IsWindowVisible(h)) {
        StringBuilder sb = new StringBuilder(64);
        GetClassName(h, sb, 64);
        if (sb.ToString() == "#32770") { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
"""

_FOLDER_PS = _PICK_PS + r"""
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = '选择要迁移的目录'
$dlg.ShowNewFolderButton = $false
if ('__INITIAL__' -and (Test-Path -LiteralPath '__INITIAL__')) { $dlg.SelectedPath = '__INITIAL__' }
$tmr = New-Object System.Windows.Forms.Timer
$tmr.Interval = 150
$tmr.Add_Tick({
  $h = [CClearWin]::FindDialog()
  if ($h -ne [IntPtr]::Zero) {
    $tmr.Stop()
    [void][CClearWin]::SetWindowPos($h, [CClearWin]::HWND_TOPMOST, 0, 0, 0, 0,
      [CClearWin]::SWP_NOMOVE -bor [CClearWin]::SWP_NOSIZE -bor [CClearWin]::SWP_SHOWWINDOW)
    [void][CClearWin]::SetForegroundWindow($h)
  }
})
$tmr.Start()
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  Write-Output ('CCLEAR_PATH:' + $dlg.SelectedPath)
}
$tmr.Stop()
"""

_FILE_PS = _PICK_PS + r"""
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Filter = '__FILTER__'
$dlg.CheckFileExists = $true
if ('__INITIAL__' -and (Test-Path -LiteralPath '__INITIAL__')) { $dlg.InitialDirectory = '__INITIAL__' }
$tmr = New-Object System.Windows.Forms.Timer
$tmr.Interval = 150
$tmr.Add_Tick({
  $h = [CClearWin]::FindDialog()
  if ($h -ne [IntPtr]::Zero) {
    $tmr.Stop()
    [void][CClearWin]::SetWindowPos($h, [CClearWin]::HWND_TOPMOST, 0, 0, 0, 0,
      [CClearWin]::SWP_NOMOVE -bor [CClearWin]::SWP_NOSIZE -bor [CClearWin]::SWP_SHOWWINDOW)
    [void][CClearWin]::SetForegroundWindow($h)
  }
})
$tmr.Start()
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  Write-Output ('CCLEAR_PATH:' + $dlg.FileName)
}
$tmr.Stop()
"""


def _ps_quote(value: str) -> str:
    return (value or "").replace("'", "''")


def pick_path(kind: str, initial: str = "", keyword: str = "所有文件|*.*",
              timeout: int = 300) -> dict:
    """Show a native picker.

    Returns ``{"path": str, "error": str, "busy": bool}``; an empty ``path``
    without ``error`` means the user cancelled.
    """
    if not IS_WINDOWS:
        return {"path": "", "error": "当前系统不支持原生选择窗口", "busy": False}
    if not _pick_lock.acquire(blocking=False):
        return {"path": "", "error": "", "busy": True}
    try:
        template = _FOLDER_PS if kind == "folder" else _FILE_PS
        script = (template.replace("__INITIAL__", _ps_quote(initial))
                          .replace("__FILTER__", _ps_quote(keyword)))
        rc, out = run_powershell(script, timeout=timeout)
        prefix = "CCLEAR_PATH:"
        for line in (out or "").splitlines():
            if line.startswith(prefix):
                path = line[len(prefix):].strip()
                if path and os.path.exists(path):
                    return {"path": path, "error": "", "busy": False}
                return {"path": "", "error": "所选路径不存在", "busy": False}
        if rc != 0:
            msg = " ".join((out or "").split())[:300] or "无法打开选择窗口"
            return {"path": "", "error": msg, "busy": False}
        return {"path": "", "error": "", "busy": False}
    finally:
        _pick_lock.release()


def cleanup_stale_pickers() -> int:
    """Close picker processes left behind by an earlier crashed/closed run."""
    if not IS_WINDOWS:
        return 0
    script = r"""
$n = 0
$markers = @(('CCLEAR_' + 'PICKER_V1'), ('选择要迁移的' + '目录'))
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine } |
  ForEach-Object {
    $p = $_
    foreach ($m in $markers) {
      if ($p.CommandLine -like "*$m*") {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $n++ } catch { }
        break
      }
    }
  }
[Console]::Out.Write($n)
"""
    rc, out = run_powershell(script, timeout=60)
    try:
        return int((out or "0").strip() or 0)
    except ValueError:
        return 0


def update_shortcuts(old_path: str, new_path: str, roots: Optional[list[str]] = None) -> list[dict]:
    """Rewrite .lnk targets that point into ``old_path`` so they use ``new_path``."""
    roots = roots or [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu"),
        os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Internet Explorer\Quick Launch"),
    ]
    script = _SHORTCUT_PS.replace("__OLD__", old_path.replace("'", "''")) \
                        .replace("__NEW__", new_path.replace("'", "''")) \
                        .replace("__ROOTS__", ",".join(
                            "'" + r.replace("'", "''") + "'" for r in roots if r))
    rc, out = run_powershell(script, timeout=180)
    changed = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("CHANGED:"):
            changed.append({"path": line[8:].strip()})
    if rc != 0 and not changed:
        return []
    return changed


_SHORTCUT_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$old = '__OLD__'
$new = '__NEW__'
$roots = @(__ROOTS__)
$shell = New-Object -ComObject WScript.Shell
foreach ($root in $roots) {
  if (-not (Test-Path -LiteralPath $root)) { continue }
  Get-ChildItem -LiteralPath $root -Filter *.lnk -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $f = $_.FullName
    try {
      $sc = $shell.CreateShortcut($f)
      $dirty = $false
      foreach ($prop in @('TargetPath','WorkingDirectory','IconLocation','Arguments')) {
        $v = $sc.$prop
        if ($v -and ($v.ToLower().StartsWith($old.ToLower()))) {
          $sc.$prop = $new + $v.Substring($old.Length)
          $dirty = $true
        }
      }
      if ($dirty) { $sc.Save(); Write-Output "CHANGED:$f" }
    } catch { }
  }
}
"""
