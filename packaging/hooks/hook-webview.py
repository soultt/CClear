"""PyInstaller hook: bundle pywebview's JS runtime and native WebView2 shims."""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = collect_data_files("webview")
binaries = collect_dynamic_libs("webview")

hiddenimports = [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
]
