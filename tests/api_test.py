"""Exercise the HTTP API surface end to end (assumes the server is running)."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Windows consoles default to GBK; make sure Chinese output and check marks never
# crash the run when stdout is piped or redirected.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8791"
FAIL = []


def call(path, body=None, method=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if body is not None else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def check(name, cond, extra=""):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAIL.append(name)


def wait_job(job_id, timeout=600, quiet=True):
    t0 = time.time()
    offset = 0
    last = ""
    while time.time() - t0 < timeout:
        j = call(f"/api/jobs/{job_id}?offset={offset}")
        offset = j["log_count"]
        if not quiet and j["message"] != last:
            print(f"    · {j['percent']:5.1f}%  {j['message']}")
            last = j["message"]
        if j["status"] not in ("running", "pending"):
            return j
        time.sleep(0.4)
    raise TimeoutError(job_id)


print("=" * 62)
print("CClear HTTP API 测试")
print("=" * 62)

print("\n[1] 总览 / 静态资源")
ov = call("/api/overview")
check("GET /api/overview", "drives" in ov and "categories" in ov)
for asset in ("/", "/static/style.css", "/static/app.js"):
    with urllib.request.urlopen(BASE + asset, timeout=20) as r:
        check(f"GET {asset}", r.status == 200 and len(r.read()) > 100)

print("\n[1b] 网页内目录选择器")
root = call("/api/fs/dirs?path=")
check("GET /api/fs/dirs 根节点", len(root["drives"]) >= 1 and root["path"] == "")
drive = root["drives"][0]["letter"] + "\\"
listing = call("/api/fs/dirs?path=" + urllib.parse.quote(drive))
check(f"列出 {drive}", listing["path"].upper().startswith(drive.upper()) and len(listing["dirs"]) > 0,
      f"{len(listing['dirs'])} 个子目录")
check("盘符根目录无上级", listing["parent"] is None)
check("目录项含隐藏标记", all("hidden" in d and "path" in d for d in listing["dirs"]))
profile = call("/api/fs/dirs?path=" + urllib.parse.quote(os.environ["USERPROFILE"]))
appdata = next((d for d in profile["dirs"] if d["name"].lower() == "appdata"), None)
check("AppData 可见且标记为隐藏", appdata is not None and appdata["hidden"] is True)
bad = call("/api/fs/dirs?path=" + urllib.parse.quote("Q:\\definitely-missing-xyz"))
check("无效路径返回错误", bad["error"] != "" and bad["dirs"] == [])

print("\n[2] 垃圾扫描与清理任务")
j = wait_job(call("/api/junk/scan", {"ids": ["user_temp", "thumbs"]})["job_id"])
check("垃圾扫描任务完成", j["status"] == "done", f"{j['result']['total_human']}")
check("扫描结果含分类", len(j["result"]["categories"]) == 2)
check("扫描只覆盖所选项目",
      {c["id"] for c in j["result"]["categories"]} == {"user_temp", "thumbs"})
jmeta = call("/api/junk/categories")
_jr = [c["risk"] for c in jmeta["categories"]]
check("垃圾项目按安全等级排序",
      _jr == sorted(_jr, key=lambda r: {"safe": 0, "caution": 1, "advanced": 2}[r]), _jr)

print("\n[2b] 注册表清理分类与扫描")
rc = call("/api/regclean/categories")
check("GET /api/regclean/categories", len(rc["categories"]) >= 6)
risks = [c["risk"] for c in rc["categories"]]
check("分类按安全等级排序", risks == sorted(risks, key=lambda r: {"safe": 0, "caution": 1, "advanced": 2}[r]), risks)
check("每个分类都有等级与说明",
      all(c["risk"] in ("safe", "caution", "advanced") and c["desc"] for c in rc["categories"]))
rj = wait_job(call("/api/regclean/scan", {"ids": ["reg_mru", "reg_startup_user"]})["job_id"])
check("注册表扫描任务完成", rj["status"] == "done", f"{rj['result']['total']} 项")
check("扫描结果按等级排序",
      [c["risk"] for c in rj["result"]["categories"]] ==
      sorted([c["risk"] for c in rj["result"]["categories"]],
             key=lambda r: {"safe": 0, "caution": 1, "advanced": 2}[r]))
check("注册表扫描只覆盖所选项目",
      {c["id"] for c in rj["result"]["categories"]} == {"reg_mru", "reg_startup_user"},
      [c["id"] for c in rj["result"]["categories"]])
check("扫描项含路径与原因",
      all({"key", "issue", "label"} <= set(i) for c in rj["result"]["categories"] for i in c["items"]))
try:
    call("/api/regclean/clean", {"ids": []})
    check("拒绝空注册表清理请求", False)
except urllib.error.HTTPError as e:
    check("拒绝空注册表清理请求", e.code == 400)

print("\n[3] 大文件扫描任务")
j = wait_job(call("/api/files/scan", {"min_mb": 500})["job_id"], timeout=900,
             quiet=False)
check("大文件扫描完成", j["status"] == "done", f"{j['result']['count']} 个 / {j['result']['total_human']}")

print("\n[4] 目录迁移：计划 → 执行 → 记录 → 还原")
targets = ov["targets"]
target = targets[0]["letter"]
src = os.path.join(os.environ["TEMP"], "cclear-api-test")
if os.path.lexists(src):
    try:
        os.rmdir(src)
    except OSError:
        import shutil
        shutil.rmtree(src, ignore_errors=True)
os.makedirs(os.path.join(src, "data"), exist_ok=True)
for i in range(4):
    with open(os.path.join(src, "data", f"f{i}.dat"), "wb") as fh:
        fh.write(os.urandom(250_000))

plan_job = wait_job(call("/api/move/plan", {"src": src, "target": target})["job_id"], timeout=300)
check("迁移计划任务完成", plan_job["status"] == "done")
plan = plan_job["result"]
print(f"    src={plan['src']}")
print(f"    dst={plan['dst']}   refs={plan['registry_count']}")
check("计划可用", plan["ok"], str(plan.get("errors")))

mv = wait_job(call("/api/move/execute", {
    "src": plan["src"], "dst": plan["dst"], "link": True,
    "update_registry": True, "verify_hash": True,
})["job_id"], timeout=600)
check("迁移任务执行成功", mv["status"] == "done", str(mv.get("error")))
res = mv["result"]
check("迁移后创建了联接", res["link"])
check("迁移校验通过", res["verified"])

jlist = call("/api/journal")
rec = next((r for r in jlist["records"] if r.get("src") == src and not r.get("reverted")), None)
check("迁移记录已写入", rec is not None)
check("记录统计更新", jlist["stats"]["migrated"] >= 1, str(jlist["stats"]["freed_bytes"]))

rv = wait_job(call(f"/api/journal/{rec['id']}/revert", {})["job_id"], timeout=600)
check("还原任务成功", rv["status"] == "done", str(rv.get("error")))
check("原路径恢复为目录", os.path.isdir(src) and not os.path.islink(src))
check("还原后文件可读", os.path.isfile(os.path.join(src, "data", "f0.dat")))

print("\n[5] 应用枚举 / 分析")
apps = call("/api/apps?refresh=true")
check("应用列表", apps["total"] > 0, f"共 {apps['total']} 个，可迁移 {apps['migratable']}")
cand = next((a for a in apps["apps"] if a["migratable"] and a["folder"]), None)
if cand:
    a = wait_job(call("/api/apps/analyse", {
        "folder": cand["folder"], "target": target, "name": cand["name"],
    })["job_id"], timeout=300)
    check("应用分析完成", a["status"] == "done")
    ar = a["result"]
    print(f"    {cand['name']}: {ar['summary']['size_human']} · 注册表引用 {ar['registry_count']} · 服务 {len(ar.get('services') or [])}")
    check("应用分析返回结构", "summary" in ar and "dst" in ar)

print("\n[6] 设置")
st = call("/api/settings")
check("读取设置", "settings" in st)
st2 = call("/api/settings", {"target_drive": target, "large_file_min_mb": 300})
check("写入设置", st2["settings"]["large_file_min_mb"] == 300)
call("/api/settings", {"large_file_min_mb": 200})

print("\n[7] 错误处理 / 安全校验")
j7 = wait_job(call("/api/move/plan", {"src": "C:\\Windows", "target": target})["job_id"], timeout=200)
check("计划标记为不可行", j7["status"] == "done" and j7["result"]["ok"] is False,
      str(j7["result"]["errors"]))
try:
    call("/api/junk/clean", {"ids": []})
    check("拒绝空清理请求", False)
except urllib.error.HTTPError as e:
    check("拒绝空清理请求", e.code == 400)

print("\n" + "=" * 62)
if FAIL:
    print(f"失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("HTTP API 全部通过 ✓")
