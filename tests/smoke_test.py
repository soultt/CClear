"""End-to-end smoke test for the CClear core engine (no UI)."""
import os
import shutil
import sys
import time
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows consoles default to GBK; make sure Chinese output and check marks never
# crash the run when stdout is piped or redirected.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app.core import appmover, cleaner, journal, mover, regclean, registry, scanner, winutil  # noqa: E402
from app.core.util import human_size  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAIL.append(name)


def section(t):
    print("\n" + "=" * 62)
    print(t)
    print("=" * 62)


def rm(path):
    """Remove a file/dir/junction robustly."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.exists(path)):
        try:
            os.rmdir(path)
        except OSError:
            os.remove(path)
        return
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


section("1. 平台信息")
drives = winutil.list_drives()
for d in drives:
    print(f"  {d['letter']} {d['label']!r} fs={d['fs']} free={human_size(d['free'])}/{human_size(d['total'])} sys={d['is_system']}")
check("检测到系统盘", any(d["is_system"] for d in drives))
targets = winutil.relocation_targets()
check("存在可迁移目标盘", len(targets) > 0, [t["letter"] for t in targets])
print(f"  admin={winutil.is_admin()}")

section("2. 垃圾扫描（安全类目）")
t0 = time.time()
res = cleaner.scan(["user_temp", "thumbs", "recent", "crash_dumps"],
                   report=lambda p, m: None)
print(f"  总计 {res['total_human']}，耗时 {time.time()-t0:.1f}s")
for c in res["categories"]:
    print(f"    - {c['name']:<24} {c['size_human']:>10}  ({c['count']} 个文件)")
check("垃圾扫描返回结果", len(res["categories"]) == 4)
check("垃圾扫描结果按安全等级排列",
      [c["risk"] for c in res["categories"]] ==
      sorted([c["risk"] for c in res["categories"]], key=lambda r: cleaner.RISK_ORDER[r]))
check("垃圾清理项目按安全等级排列",
      [c["risk"] for c in cleaner.category_meta()] ==
      sorted([c["risk"] for c in cleaner.category_meta()], key=lambda r: cleaner.RISK_ORDER[r]))
check("扫描只覆盖所选项目",
      {c["id"] for c in res["categories"]} == {"user_temp", "thumbs", "recent", "crash_dumps"})

section("3. 目录迁移引擎（真实迁移 + 联接 + 注册表）")
target = targets[0]["letter"]
work = os.path.join(os.environ["TEMP"], "cclear-test-src")
rm(work)
leftover = os.path.join(target + "\\", "Migrated")
rm(leftover)
os.makedirs(os.path.join(work, "sub"), exist_ok=True)
payload = []
for i in range(6):
    p = os.path.join(work, "sub", f"file{i}.bin")
    with open(p, "wb") as fh:
        fh.write(os.urandom(300_000 + i * 1000))
    payload.append((p, os.path.getsize(p)))
with open(os.path.join(work, "root.txt"), "w", encoding="utf-8") as fh:
    fh.write("CClear migration test")

src_size = sum(s for _p, s in payload)
plan = mover.plan(work, target + "\\", include_registry=True)
print(f"  src  = {plan['src']}")
print(f"  dst  = {plan['dst']}")
print(f"  ok={plan['ok']} errors={plan['errors']} warnings={plan['warnings']}")
print(f"  size={plan['summary']['size_human']} files={plan['summary']['count']}")
check("迁移计划可行", plan["ok"])

events = []
result = mover.execute(
    work, plan["dst"], link=True, update_registry=True, verify_hash=True,
    report=lambda p, m, d=None: events.append((round(p), m)),
)
print(f"  迁移完成: link={result['link']} files={result['files']} size={human_size(result['size_bytes'])}")
print(f"  校验通过: {result['verified']}  耗时 {result.get('seconds')}s")
check("复制并校验成功", result["verified"])
check("原位置已创建联接", result["link"] and winutil.is_reparse_point(work),
      winutil.read_link(work) or "")
check("联接目标正确", os.path.normcase(os.path.abspath(winutil.read_link(work) or "")) == os.path.normcase(os.path.abspath(result["dst"])))
check("通过联接可读取文件", os.path.isfile(os.path.join(work, "root.txt")))
check("数据在目标盘", os.path.isdir(result["dst"]))

section("4. 注册表引擎")
probe_key = "HKCU\\Software\\CClearTest"
with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\CClearTest") as k:
    winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, work)
    winreg.SetValueEx(k, "Legacy", 0, winreg.REG_SZ, "keep-me")
refs = registry.find_references(work, roots=[("HKCU", r"Software\CClearTest")], max_seconds=10)
print(f"  直接检索到 {len(refs)} 处引用")
check("能检索到自定义注册表项", len(refs) == 1)
backup = registry.backup_keys([r["key_path"] for r in refs], tag="test")
print(f"  备份目录: {backup}")
changed, errors = registry.rewrite_references(refs, work, result["dst"])
print(f"  改写 {changed} 处，错误 {errors}")
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\CClearTest") as k:
    val = winreg.QueryValueEx(k, "InstallLocation")[0]
print(f"  改写后值: {val}")
check("注册表路径已更新", val.startswith(result["dst"]))
ok, total, errs = registry.restore_backup(backup)
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\CClearTest") as k:
    val2 = winreg.QueryValueEx(k, "InstallLocation")[0]
check("注册表备份可还原", val2 == work, val2)

section("5. 迁移还原（回滚）")
rec = next(r for r in journal.all_records() if r.get("src") == work and not r.get("reverted"))
out = mover.revert(rec)
print(f"  还原: {out}")
check("原目录已恢复为真实目录", os.path.isdir(work) and not winutil.is_reparse_point(work))
check("目标位置已清理", not os.path.exists(result["dst"]))
check("文件内容完整", os.path.getsize(os.path.join(work, "root.txt")) == len("CClear migration test"))
rm(work)
try:
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\CClearTest")
except OSError:
    pass

section("5b. 注册表清理（分级 + 备份 + 删除）")
check("分类按安全等级排序",
      [c["risk"] for c in regclean.category_meta()] ==
      sorted([c["risk"] for c in regclean.category_meta()],
             key=lambda r: regclean.RISK_ORDER[r]))
check("只包含已知安全等级",
      {c["risk"] for c in regclean.category_meta()} <= {"safe", "caution", "advanced"})
check("命令路径解析", regclean.command_target('"C:\\a b\\x.exe" /run') == "C:\\a b\\x.exe"
      and regclean.command_target("C:\\x.exe -k") == "C:\\x.exe"
      and regclean.command_target("rundll32.exe foo.dll") == "")

reg_base = r"Software\CClearRegTest"
with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_base + r"\Run") as k:
    winreg.SetValueEx(k, "GhostApp", 0, winreg.REG_SZ, r"C:\NoSuchDir\ghost.exe")
    winreg.SetValueEx(k, "RealApp", 0, winreg.REG_SZ, r"C:\Windows\explorer.exe")


def _fake_scanner(cancel=None):
    return [regclean._finding("HKCU", reg_base + r"\Run", "GhostApp",
                              r"C:\NoSuchDir\ghost.exe", "test", value_name="GhostApp")]


fake = regclean._cat(id="zz_test", name="测试项", risk="safe", scanner=_fake_scanner)
regclean.CATEGORIES.append(fake)
regclean.CATEGORY_INDEX[fake["id"]] = fake
try:
    sres = regclean.scan(["zz_test"])
    check("扫描发现测试残留", sres["total"] == 1, sres["categories"][0]["items"])
    check("扫描只覆盖所选项目", [c["id"] for c in sres["categories"]] == ["zz_test"],
          [c["id"] for c in sres["categories"]])
    cres = regclean.clean(["zz_test"])
    check("清理删除测试值", cres["removed"] == 1 and not cres["failed"], cres["details"])
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_base + r"\Run") as k:
        left = [winreg.EnumValue(k, i)[0] for i in range(winreg.QueryInfoKey(k)[1])]
    check("保留正常启动项", left == ["RealApp"], left)
    check("已写入注册表备份", bool(cres["backups"]) and os.path.isdir(cres["backups"][0]))
    okb, totalb, errsb = registry.restore_backup(cres["backups"][0])
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_base + r"\Run") as k:
        restored = [winreg.EnumValue(k, i)[0] for i in range(winreg.QueryInfoKey(k)[1])]
    check("备份可还原被删项", "GhostApp" in restored and okb >= 1, restored)
finally:
    regclean.CATEGORIES.remove(fake)
    regclean.CATEGORY_INDEX.pop(fake["id"], None)
    for path in (reg_base + r"\Run", reg_base):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

section("6. 应用枚举")
apps = appmover.list_apps()
print(f"  共 {apps['total']} 个应用，其中可迁移 {apps['migratable']} 个")
for a in [x for x in apps["apps"] if x["migratable"]][:8]:
    print(f"    - {a['name'][:38]:<40} {a['folder']}")
check("能枚举已安装应用", apps["total"] > 0)
check("存在可迁移应用", apps["migratable"] > 0)

section("6b. 应用迁移（真实迁移 + 注册表 + 单条记录）")
app_src = os.path.join(os.environ["TEMP"], "cclear-test-app")
rm(app_src)
os.makedirs(app_src, exist_ok=True)
with open(os.path.join(app_src, "TestApp.exe"), "wb") as fh:
    fh.write(os.urandom(200_000))
with open(os.path.join(app_src, "data.txt"), "w", encoding="utf-8") as fh:
    fh.write("app payload")
app_key = r"Software\CClearTestApp"
with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app_key) as k:
    winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, app_src)

app_meta = {"folder": app_src, "name": "CClear 测试应用", "id": "smokeapp",
            "registry_key": "HKCU\\" + app_key, "uninstall": ""}
app_res = appmover.migrate(app_meta, target + "\\", link=True, update_registry=True,
                           verify_hash=False)
print(f"  迁移: link={app_res['link']} files={app_res['files']} "
      f"registry_changed={app_res['registry_changed']} warnings={app_res['warnings']}")
check("应用迁移完成且校验通过", app_res["verified"])
check("应用迁移无警告", not app_res["warnings"], app_res["warnings"])
check("应用原位置已创建联接", app_res["link"] and winutil.is_reparse_point(app_src))
check("应用注册表已改写", app_res["registry_changed"] >= 1)

app_recs = [r for r in journal.all_records()
            if r.get("src") == app_src and not r.get("reverted")]
check("应用迁移只写入一条记录", len(app_recs) == 1, [r.get("id") for r in app_recs])
check("记录含应用元数据",
      bool(app_recs) and app_recs[0].get("kind") == "app" and app_recs[0].get("strategy"),
      app_recs[0].get("strategy") if app_recs else None)

out2 = mover.revert(app_recs[0])
print(f"  还原: {out2}")
check("应用迁移可还原", os.path.isdir(app_src) and not winutil.is_reparse_point(app_src))
try:
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, app_key)
except OSError:
    pass
rm(app_src)

section("7. 安全校验（应拒绝）")
for bad in ["C:\\Windows", "C:\\Program Files", "C:\\"]:
    ok2, errs2, _w = mover.validate(bad, target + "\\")
    print(f"  {bad:<22} ok={ok2} :: {errs2[:1]}")
    check(f"拒绝迁移 {bad}", not ok2)

section("8. 大文件扫描（小阈值，限时）")
res = scanner.scan_large_files(min_bytes=60 * 1024 * 1024, limit=15, max_seconds=25)
print(f"  扫描 {res['scanned']} 个文件，找到 {res['count']} 个 >= {res['min_human']}，合计 {res['total_human']}")
for f in res["files"][:6]:
    print(f"    {f['size_human']:>10}  {f['path']}")
check("大文件扫描返回结构", "files" in res)

print("\n" + "=" * 62)
if FAIL:
    print(f"失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("全部测试通过 ✓")
