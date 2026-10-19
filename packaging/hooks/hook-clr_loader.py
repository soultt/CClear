"""PyInstaller hook: keep clr_loader's native loader next to its package.

``clr_loader.ffi.load_netfx`` resolves ``ClrLoader.dll`` relative to the
installed package, so the DLLs must keep their original sub-directory layout
inside the bundle instead of being flattened into the top level.
"""

from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs("clr_loader")

hiddenimports = [
    "clr_loader.ffi",
    "clr_loader.ffi.hostfxr",
    "clr_loader.ffi.mono",
    "clr_loader.ffi.netfx",
    "clr_loader.util",
    "clr_loader.util.find",
    "clr_loader.util.runtime_spec",
]
