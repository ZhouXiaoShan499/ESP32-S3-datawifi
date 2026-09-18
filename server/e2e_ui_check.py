# -*- coding: utf-8 -*-
"""
Web 界面端到端自检（可选，需要本机已安装 Edge 或 Chrome 的无头模式）。

流程：临时库 + 真实 uvicorn 服务 → 通过 HTTP 造出 periodic / manual 两类数据 →
无头浏览器打开 /ui/?autocapture=1 → dump DOM → 断言页面 JS 真的执行到底：
  * 轮询填好了设备下拉与数据卡片（trigger / request_id / 波形点数）
  * 三维视图完成绘制（#sceneInfo 被 JS 写入了 |a|）
  * 页面自身通过「采集一次最新数据」按钮的代码路径创建任务并渲染状态

运行：python server/e2e_ui_check.py
  未找到浏览器时打印 [SKIP] 并以 0 退出；可用 EDGE_PATH 指定浏览器可执行文件。
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

# 输出固定 UTF-8：中文 Windows 下控制台/重定向默认 cp936，遇到 m/s² 会编码失败
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(_HERE, "data", "e2e_ui.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SENSOR_DB"] = _DB
os.environ["SENSOR_HOST"] = "127.0.0.1"
os.environ["SENSOR_PORT"] = "8012"

sys.path.insert(0, _HERE)

BASE = "http://127.0.0.1:8012"
DEV = "esp32s3-eye-ui"
PROFILE = os.path.join(os.environ.get("TEMP", "."), "edge-e2e-ui-check")

PASSED = []


def find_browser():
    """返回可用的浏览器可执行文件；找不到返回 None（调用方 SKIP）。"""
    for key in ("EDGE_PATH", "CHROME_PATH"):
        path = os.environ.get(key)
        if path and os.path.exists(path):
            return path
    for path in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if os.path.exists(path):
            return path
    return None


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("[FAIL] %s %s" % (name, detail))
    PASSED.append(name)
    print("[PASS] %s %s" % (name, detail))


def post_json(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def make_samples(n):
    return [{"i": i, "t_ms": i * 10, "ax": 0.01 * i,
             "ay": -0.1 * i, "az": 9.8 + 0.05 * i} for i in range(n)]


def seed():
    """用真实 HTTP 造数据：一批周期上报 + 一次完整的按需采集任务。"""
    ts = int(time.time() * 1000)

    st, body = post_json("/api/v1/upload", {
        "device_id": DEV, "source": "qma6100p", "unit": "m/s^2", "ts_ms": ts,
        "samples": make_samples(3)})
    check("seed periodic upload",
          st == 201 and body["trigger"] == "periodic"
          and body["request_id"] is None, str(body)[:120])

    st, body = post_json("/api/v1/tasks", {"device_id": DEV, "sample_count": 5})
    check("seed task create", st == 201 and body["task"]["status"] == "submitted",
          str(body)[:120])
    rid = body["task"]["request_id"]

    st, body = get_json("/api/v1/tasks/next?device_id=" + DEV)
    check("seed task claim",
          body.get("found") is True and body["task"]["request_id"] == rid,
          str(body)[:120])

    st, _ = post_json("/api/v1/tasks/%s/ack" % rid, {})
    check("seed task ack", st == 200)

    st, body = post_json("/api/v1/upload", {
        "device_id": DEV, "source": "qma6100p", "unit": "m/s^2", "ts_ms": ts,
        "request_id": rid, "trigger": "manual", "samples": make_samples(5)})
    check("seed manual upload", st == 201 and body["trigger"] == "manual",
          str(body)[:120])

    st, body = get_json("/api/v1/tasks/%s" % rid)
    check("seed task completed",
          body["task"]["status"] == "completed"
          and body["upload"]["request_id"] == rid, str(body)[:120])
    return rid


def wait_server(deadline_s=20):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            st, _ = get_json("/api/v1/health")
            if st == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def dump_dom(browser, url):
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--user-data-dir=" + PROFILE,
        "--virtual-time-budget=9000", "--dump-dom", url,
    ]
    out = subprocess.run(cmd, capture_output=True, timeout=180)
    # --dump-dom 输出是 UTF-8；不能用 text=True（Windows 默认 GBK 会解码失败）
    return (out.stdout or b"").decode("utf-8", "replace")


def field(dom, elem_id):
    """取出 <span id="x" …>text</span> 里的文本。"""
    m = re.search(r'<span[^>]*id="%s"[^>]*>(.*?)</span>' % elem_id, dom, re.S)
    return m.group(1).strip() if m else ""


def main_run():
    import main           # 先导入 main：它在 import 时读取 SENSOR_DB
    import uvicorn

    browser = find_browser()
    if not browser:
        print("[SKIP] 未找到 Edge/Chrome，跳过浏览器端自检"
              "（可用环境变量 EDGE_PATH 指定浏览器路径）")
        return

    main.init_db()
    config = uvicorn.Config(main.app, host="127.0.0.1", port=8012,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    check("server up", wait_server(), BASE)

    rid_seed = seed()
    dom = dump_dom(browser, BASE + "/ui/?autocapture=1")
    check("browser returned DOM", len(dom) > 2000, "%d B" % len(dom))

    # 1) 轮询主流程跑完（poll() 的 finally 会写 lastPoll）
    check("poll ran", "最近刷新" in dom, field(dom, "lastPoll"))
    # 2) /api/v1/latest 的 trigger / request_id 被回填
    check("trigger rendered", "manual" in field(dom, "vTrigger"),
          field(dom, "vTrigger"))
    check("request_id rendered", rid_seed in field(dom, "vRequestId"),
          field(dom, "vRequestId"))
    # 3) 设备下拉被填充（/devices 轮询成功）
    check("device select filled", DEV in dom)
    # 4) 波形图有点数（drawChart 执行）
    m = re.search(r'id="chartCount"[^>]*>(\d+)\s*点', dom)
    check("chart drawn", bool(m) and int(m.group(1)) > 0,
          m.group(1) if m else "no match")
    # 5) 三维视图：sceneInit 执行（窗口秒数标签由 JS 写成常量）且向量已算出
    check("scene init ran", bool(re.search(r'id="sceneWin"[^>]*>\s*30\s*<', dom)))
    scene_info = field(dom, "sceneInfo")
    check("scene drew vector", "|a| =" in scene_info and "轨迹" in scene_info,
          scene_info)
    # 6) 页面自身通过「采集一次」按钮代码路径建任务并渲染状态
    status = field(dom, "taskStatus")
    rid_page = field(dom, "taskRid")
    check("page created a task", len(rid_page) == 32 and rid_page != rid_seed,
          "rid=%s" % rid_page)
    check("task status rendered", "状态码" in status, status)
    st, body = get_json("/api/v1/tasks?device_id=%s&limit=5" % DEV)
    rids = [t["request_id"] for t in body["tasks"]]
    check("server has the page-created task",
          body["tasks"][0]["request_id"] == rid_page,
          body["tasks"][0]["status"])
    check("both tasks stored", rid_seed in rids and len(rids) >= 2,
          "count=%s" % body["count"])

    # 7) 汇总接口在真实 HTTP 上一致
    st, health = get_json("/api/v1/health")
    check("health counts tasks", health["total_tasks"] >= 2,
          str(health["tasks_by_status"]))
    st, devices = get_json("/api/v1/devices")
    dev = [d for d in devices["devices"] if d["device_id"] == DEV][0]
    check("devices exposes pending_tasks", dev["pending_tasks"] >= 1, str(dev))
    check("device uploads counted", dev["uploads"] >= 2, str(dev["uploads"]))

    print("\nALL %d UI E2E CHECKS PASSED" % len(PASSED))


if __name__ == "__main__":
    code = 0
    try:
        main_run()
    except Exception as exc:                      # noqa: BLE001 - 自检脚本要打印原因
        print("\n[FAIL] %s: %s" % (type(exc).__name__, exc))
        code = 1
    finally:
        sys.stdout.flush()      # os._exit 不会刷新缓冲，必须手动 flush
        sys.stderr.flush()
        os._exit(code)          # 直接退出，避免 uvicorn 线程阻塞进程