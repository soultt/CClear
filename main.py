"""CClear - C 盘空间管家

默认以独立程序窗口打开（内嵌 WebView2，不依赖外部浏览器）：

  CClear.exe                  直接双击运行，打开独立窗口
  python main.py              同上（开发模式）
  python main.py --browser    改用系统默认浏览器打开
  python main.py --no-window  只启动后台服务，不打开任何界面
  python main.py --port 9000  指定端口
  python main.py --elevate    以管理员权限重新启动
  python main.py --version    显示版本
"""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import sys
import threading
import time

APP_NAME = "CClear"
APP_TITLE = "CClear · C 盘空间管家"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# True when the process owns a real console (console build / dev run). A frozen
# --windowed build has no console at all, so messages must go elsewhere.
HAS_CONSOLE = getattr(sys, "stdout", None) is not None


# ---------------------------------------------------------------------------
# diagnostics: UTF-8 console when possible, otherwise a log file
# ---------------------------------------------------------------------------

def data_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, APP_NAME)


def log_path() -> str:
    return os.path.join(data_dir(), "CClear.log")


def _setup_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if HAS_CONSOLE:
        return
    log = None
    try:
        os.makedirs(data_dir(), exist_ok=True)
        try:
            if os.path.getsize(log_path()) > 2 * 1024 * 1024:
                open(log_path(), "w", encoding="utf-8").close()
        except OSError:
            pass
        log = open(log_path(), "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def notify(message: str, popup: bool = True) -> None:
    """Report a startup problem: console when available, otherwise a popup."""
    print(f"[{APP_NAME}] {message}", flush=True)
    if HAS_CONSOLE or not popup:
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
    except Exception:
        pass


_setup_streams()


# ---------------------------------------------------------------------------
# server helpers
# ---------------------------------------------------------------------------

def port_available(port: int, host: str = "127.0.0.1") -> bool:
    """True when ``port`` can be bound and nothing is listening on it.

    A bare ``bind()`` test is not enough on Windows: SO_REUSEADDR lets a socket
    bind a port that another process is already listening on, and uvicorn would
    then fail its own bind (or, worse, this instance would talk to the other
    one). Probe with ``connect`` first, then try a plain bind for TIME_WAIT.
    """
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        try:
            if probe.connect_ex((probe_host, port)) == 0:
                return False
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host or "127.0.0.1", port))
        except OSError:
            return False
    return True


def find_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 40)):
        if port_available(port, host):
            return port
    raise SystemExit("找不到可用端口")


def wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def run_service(thread: threading.Thread) -> int:
    """Keep the process alive around the background HTTP server."""
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="C 盘空间管家")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--elevate", action="store_true", help="请求管理员权限后启动")
    parser.add_argument("--browser", action="store_true", help="使用系统默认浏览器打开界面")
    parser.add_argument("--no-window", "--no-browser", "--service", dest="no_window",
                        action="store_true", help="只启动后台服务，不打开界面")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()

    from app import __version__

    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0

    from app.core import winutil

    if args.elevate and not winutil.is_admin():
        extra = [a for a in sys.argv[1:] if a != "--elevate"]
        if winutil.relaunch_as_admin(extra):
            print("已在管理员权限下重新启动，请在新窗口中操作。")
            return 0
        print("提升权限失败，将使用普通权限启动（部分功能不可用）。")

    import uvicorn

    from app.server import app, set_elevate_args, set_relaunch_hook, set_shutdown_hook

    port = find_port(args.port, args.host)

    # The elevated copy must open the same kind of UI this one did, on the same
    # port when it is free by then: the browser tab (or the Electron shell)
    # keeps pointing at this URL after the hand-over.
    if args.browser:
        set_elevate_args(["--browser", "--port", str(port)])
    elif args.no_window:
        set_elevate_args(["--no-window", "--port", str(port)])

    url = f"http://{args.host}:{port}/"

    try:
        os.system("")
    except Exception:
        pass
    admin = "是" if winutil.is_admin() else "否（部分功能需要管理员权限）"
    print("=" * 58)
    print(f"  {APP_NAME} {__version__}  ·  C 盘空间管家")
    print("-" * 58)
    print(f"  服务地址 : {url}")
    print(f"  管理员   : {admin}")
    print(f"  数据目录 : {data_dir()}")
    if not HAS_CONSOLE:
        print(f"  运行日志 : {log_path()}")
    print("=" * 58)
    print()

    config = uvicorn.Config(app, host=args.host, port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="cclear-http", daemon=True)
    thread.start()

    if not wait_for_server(args.host, port):
        notify(f"后台服务未能启动（{url}），请检查端口是否被占用后重试。",
               popup=not args.no_window)
        return 1

    def stop_service() -> None:
        """Elevation hand-over for the windowless/browser modes."""
        server.should_exit = True

    set_shutdown_hook(stop_service)

    if args.no_window:
        set_relaunch_hook(stop_service)
        print("  后台服务已就绪（未打开界面）。按 Ctrl+C 退出")
        return run_service(thread)

    # 界面由 Electron 壳负责：本进程始终以纯后台服务模式运行
    set_relaunch_hook(stop_service)
    print("  后台服务已就绪。按 Ctrl+C 退出")
    return run_service(thread)


if __name__ == "__main__":
    raise SystemExit(main())
