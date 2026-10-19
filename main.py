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
import webbrowser

APP_NAME = "CClear"
APP_TITLE = "CClear · C 盘空间管家"
APP_ICON = "CClear.ico"

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

def find_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 40)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# frameless window: the page draws the title bar, so a few native details that
# normally come from the frame have to be restored by hand
# ---------------------------------------------------------------------------

GWL_STYLE = -16
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000         # taskbar/Alt+Space system menu
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0004, 0x0020

# Deliberately *not* WS_THICKFRAME: it would give native edge resizing, but it
# also makes Windows reserve a non-client frame, and on Win10 that frame is
# painted in the (light) system theme colour - a white border around a dark app
# that DWMWA_BORDER_COLOR cannot recolour. The page draws resize grips instead.
WINDOW_MIN_SIZE = (1040, 700)


def _winforms():
    """(System.Windows.Forms, System.Action) or (None, None) if unavailable."""
    try:
        import System.Windows.Forms as WinForms
        from System import Action
        return WinForms, Action
    except Exception:
        return None, None


def _on_ui_thread(form, fn) -> bool:
    """WinForms state must be touched from its own thread."""
    _, Action = _winforms()
    if Action is None:
        return False
    try:
        form.Invoke(Action(fn))
        return True
    except Exception as exc:
        print(f"{APP_NAME}: 界面线程调用失败：{exc}")
        return False


def _user32():
    """user32 with 64-bit safe signatures for the calls used here."""
    from ctypes import wintypes

    fn = ctypes.WinDLL("user32", use_last_error=True)
    fn.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    fn.GetWindowLongW.restype = ctypes.c_long
    fn.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
    fn.SetWindowLongW.restype = ctypes.c_long
    fn.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint)
    fn.SetWindowPos.restype = ctypes.c_bool
    return fn


def _hwnd(form):
    """Native handle of a WinForms form as a plain int."""
    handle = form.Handle
    try:
        return int(handle.ToInt64())
    except AttributeError:
        return int(handle)


def frameless_window_setup(window) -> None:
    """Give the borderless window back what the removed frame would have owned:
    the taskbar / Alt+Space system menu and the min-max buttons. Resizing is
    handled by the page (see the grip handlers in app.js) because a native
    resize border would paint a light system frame around the dark UI."""
    if sys.platform != "win32":
        return
    WinForms, _ = _winforms()
    form = getattr(window, "native", None)
    if WinForms is None or form is None:
        return

    def _apply() -> None:
        try:
            user32 = _user32()
            hwnd = _hwnd(form)
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(
                hwnd, GWL_STYLE,
                style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU,
            )
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
        except Exception as exc:  # pragma: no cover - cosmetic only
            print(f"{APP_NAME}: 窗口样式调整失败：{exc}")

    _on_ui_thread(form, _apply)


class WindowBridge:
    """Exposed to the page as ``window.pywebview.api`` for the custom title bar.

    Only the public methods below become JS calls: pywebview walks every public
    attribute of this object, and a pywebview Window on a public attribute would
    drag it through the whole WinForms object graph. Hence ``_window``.
    """

    def __init__(self) -> None:
        self._window = None
        self._maximized = False

    # -- internals ---------------------------------------------------------
    def _form(self):
        """The underlying WinForms form (pywebview's ``native``)."""
        return getattr(self._window, "native", None)

    def _sync(self) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                "window.cclearSetMaximized && window.cclearSetMaximized(%s)"
                % ("true" if self._maximized else "false")
            )
        except Exception:
            pass

    def _set_maximized(self, value: bool) -> None:
        value = bool(value)
        if value == self._maximized:
            return
        self._maximized = value
        self._sync()

    def _keep_inside_work_area(self) -> None:
        """Without this a maximized borderless window would cover the taskbar."""
        WinForms, _ = _winforms()
        form = self._form()
        if WinForms is None or form is None:
            return

        def _apply() -> None:
            try:
                form.MaximizedBounds = WinForms.Screen.FromControl(form).WorkingArea
            except Exception as exc:  # pragma: no cover - cosmetic only
                print(f"{APP_NAME}: 设置最大化边界失败：{exc}")

        _on_ui_thread(form, _apply)

    def on_restored(self) -> None:
        """The window came back from maximized *or* minimized.

        Both routes end in the same place, so this only has to put the title bar
        button back in sync. The window's own rect is left untouched: there is no
        resize frame any more, so WinForms has nothing to lose on the way back.
        """
        self._set_maximized(False)

    # -- called from the page ---------------------------------------------
    def minimize(self) -> bool:
        self._window.minimize()
        return True

    def toggle_maximize(self) -> bool:
        target = not self._maximized
        if target:
            self._keep_inside_work_area()
            self._window.maximize()
        else:
            self._window.restore()
        self._set_maximized(target)
        return target

    def close(self) -> bool:
        self._window.destroy()
        return True

    def is_maximized(self) -> bool:
        return self._maximized

    def get_bounds(self) -> list:
        """[x, y, width, height, min_width, min_height] in logical pixels."""
        w = self._window
        return [w.x, w.y, w.width, w.height, WINDOW_MIN_SIZE[0], WINDOW_MIN_SIZE[1]]

    def set_bounds(self, x, y, width, height) -> bool:
        """Move/resize in one step - used by the page's resize grips."""
        x, y = int(round(x)), int(round(y))
        width, height = int(round(width)), int(round(height))
        self._window.move(x, y)
        self._window.resize(width, height)
        return True


def open_window(url: str, icon: str) -> bool:
    """Show the UI inside an embedded WebView2 window. False when unavailable."""
    try:
        import webview
    except Exception as exc:  # pragma: no cover - depends on the environment
        print(f"{APP_NAME}: 未安装 pywebview（{exc}）")
        return False

    from app.server import set_relaunch_hook

    bridge = WindowBridge()
    try:
        window = webview.create_window(
            APP_TITLE,
            url,
            width=1360,
            height=880,
            min_size=WINDOW_MIN_SIZE,
            background_color="#0b111c",
            text_select=True,
            frameless=True,     # no native title bar: the page draws its own
            easy_drag=False,    # only .pywebview-drag-region elements move it
            js_api=bridge,      # window.pywebview.api -> the custom title bar
        )
        bridge._window = window
        # Elevating starts a second (elevated) copy: close this window once it is
        # up, so only one CClear is left. Cancelling the UAC prompt keeps it open.
        set_relaunch_hook(window.destroy)
        window.events.shown += lambda: frameless_window_setup(window)
        window.events.maximized += lambda: bridge._set_maximized(True)
        window.events.restored += bridge.on_restored
        kwargs: dict = {"gui": "edgechromium", "debug": bool(os.environ.get("CCLEAR_DEBUG"))}
        if os.path.isfile(icon):
            kwargs["icon"] = icon
        webview.start(**kwargs)          # blocks until the window is closed
        return True
    except Exception as exc:  # pragma: no cover - depends on the environment
        print(f"{APP_NAME}: 独立窗口启动失败：{type(exc).__name__}: {exc}")
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

    from app.server import WEB_DIR, app, set_elevate_args, set_relaunch_hook

    # The elevated copy must open the same kind of UI this one did.
    if args.browser:
        set_elevate_args(["--browser"])
    elif args.no_window:
        set_elevate_args(["--no-window"])

    port = find_port(args.port)
    url = f"http://{args.host}:{port}/"
    icon = os.path.join(WEB_DIR, APP_ICON)

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

    if args.no_window:
        set_relaunch_hook(stop_service)
        print("  后台服务已就绪（未打开界面）。按 Ctrl+C 退出")
        return run_service(thread)

    if not args.browser and open_window(url, icon):
        server.should_exit = True
        thread.join(5)
        return 0

    if not args.browser:
        notify("独立窗口无法启动（需要 Microsoft Edge WebView2 运行时），"
               "已改用系统默认浏览器打开。")
    set_relaunch_hook(stop_service)
    webbrowser.open(url)
    print("  按 Ctrl+C 退出")
    return run_service(thread)


if __name__ == "__main__":
    raise SystemExit(main())
