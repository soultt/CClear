"""Fast, non-destructive unit checks for the helpers touched by fixes.

Unlike ``smoke_test.py`` this never migrates real data: everything happens in a
throw-away temp directory, and the journal/settings locations are redirected so
the developer's %LOCALAPPDATA%\\CClear is left untouched.
"""
import os
import shutil
import socket
import sys
import tempfile
import types
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TMP = tempfile.mkdtemp(prefix="cclear-unit-")
os.environ["LOCALAPPDATA"] = TMP  # must be set before importing journal/server

from app.core import journal, mover, scanner, winutil  # noqa: E402
from app.core.util import fingerprint  # noqa: E402
from main import find_port, port_available  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAIL.append(name)


def rm(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def section(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


try:
    section("1. 端口探测")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy = srv.getsockname()[1]
    check("已监听端口报告为不可用", port_available(busy) is False, f"port={busy}")
    picked = find_port(busy)
    check("find_port 会跳过被占用端口", picked != busy, f"picked={picked}")
    check("find_port 返回可用端口", port_available(picked))
    srv.close()
    check("关闭后端口恢复可用", port_available(busy))

    section("2. 文件指纹采样")
    root = os.path.join(TMP, "fp")
    os.makedirs(root)
    size = 5 * 1024 * 1024
    data = bytearray(os.urandom(size))
    a = os.path.join(root, "a.bin")
    with open(a, "wb") as fh:
        fh.write(data)
    base = fingerprint(root, sample_mb=1)
    same = fingerprint(root, sample_mb=1)
    check("同一目录指纹稳定", base == same, base[2][:12])

    with open(a, "r+b") as fh:  # change one byte in the first MB
        fh.seek(1024)
        fh.write(b"\x00" if data[1024] else b"\x01")
    head = fingerprint(root, sample_mb=1)
    check("头部变化能检出", head[2] != base[2])

    with open(a, "r+b") as fh:  # change one byte in the last MB
        fh.seek(size - 1024)
        fh.write(b"\x00" if data[size - 1024] else b"\x01")
    tail = fingerprint(root, sample_mb=1)
    check("尾部变化能检出", tail[2] != head[2])

    section("3. 目录热点递归")
    hot = os.path.join(TMP, "hot")
    deep = os.path.join(hot, "alpha", "beta")
    os.makedirs(deep)
    with open(os.path.join(deep, "blob.bin"), "wb") as fh:
        fh.write(b"x" * 4096)
    with open(os.path.join(hot, "top.bin"), "wb") as fh:
        fh.write(b"y" * 1024)
    tree = scanner.scan_hotspots(hot, depth=2)["tree"]
    check("根节点大小包含子目录", tree["size"] >= 5120, tree["size_human"])
    names = {c["name"]: c for c in tree["children"]}
    check("保留一级子目录", "alpha" in names, ", ".join(names))
    if "alpha" in names:
        sub = {c["name"]: c for c in names["alpha"]["children"]}
        check("保留二级子目录", "beta" in sub, ", ".join(sub))
        check("子目录体积有效", names["alpha"]["size"] > 0, names["alpha"]["size_human"])

    section("4. 迁移保护路径随系统盘")
    drive = winutil.system_drive().lower()
    check("系统盘根目录受保护", drive in mover.FORBIDDEN)
    check("Windows 目录受保护", f"{drive}\\windows" in mover.FORBIDDEN)

    section("5. 注册表模糊检索")
    term = "CClearUnitProbe"
    base_key = r"Software\CClearUnitTest"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base_key + "\\" + term) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_SZ, "probe")
    try:
        hits = __import__("app.core.registry", fromlist=["search_keys"]).search_keys(
            term.lower(), hints=[("HKCU", base_key)])
        check("search_keys 接受三元组 hints", any(term in h["key"] for h in hits), f"{len(hits)} 项")
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base_key + "\\" + term)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base_key)
        except OSError:
            pass

    section("6. 迁移日志状态统计")
    journal._JOURNAL = os.path.join(TMP, "journal.json")
    journal._DATA_DIR = TMP
    journal.clear()
    done = journal.add({"action": "move_dir", "src": "X", "size_bytes": 100})
    journal.add({"action": "move_dir", "src": "Y", "status": "running", "size_bytes": 200})
    journal.add({"action": "move_dir", "src": "Z", "status": "failed", "size_bytes": 300})
    journal.update(done["id"], status="done")
    st = journal.stats()
    check("只有完成的迁移计入统计", st["migrated"] == 1 and st["active"] == 1, str(st))
    check("未完成记录不虚报释放空间", st["freed_bytes"] == 100, str(st["freed_bytes"]))

    section("7. /api/shutdown 本机校验")
    from app.server import _is_local_request  # noqa: E402

    def req(headers):
        return types.SimpleNamespace(headers=headers)

    good = {"host": "127.0.0.1:8765", "x-cclear-request": "local"}
    check("本机请求放行", _is_local_request(req(good)))
    check("缺少自定义头时拒绝", not _is_local_request(req({"host": "127.0.0.1:8765"})))
    check("非回环 Host 拒绝", not _is_local_request(req(dict(good, host="evil.example:8765"))))
    check("跨站 Origin 拒绝", not _is_local_request(req(dict(good, origin="https://evil.example"))))
    check("本机 Origin 放行", _is_local_request(req(dict(good, origin="http://127.0.0.1:8765"))))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 62)
if FAIL:
    print(f"失败 {len(FAIL)} 项：" + "；".join(FAIL))
    sys.exit(1)
print("全部单元检查通过")
