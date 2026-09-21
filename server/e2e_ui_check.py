# -*- coding: utf-8 -*-
"""
Web 界面端到端自检（可选，需要本机已安装 Edge 或 Chrome 的无头模式）。

流程：临时库 + 真实 uvicorn 服务 → 通过 HTTP 造出 periodic / manual 两类数据 →
无头浏览器打开 /ui/?autocapture=1&samples=250&rate=50 → dump DOM → 断言页面 JS 真的执行到底：
  * 轮询填好了设备下拉与数据卡片（trigger / request_id / 波形点数）
  * 三维视图完成绘制（#sceneInfo 被 JS 写入了 |a|）
  * 参数表单带板端上限（样本数 ≤600、采样率 ≤100 Hz），且页面建出的任务真的带着表单里的参数
  * 任务卡片渲染了 upload_id 与 dispatched_at/acked_at/completed_at，任务历史表按倒序列出任务
  * 「手动 vs 周期」对照区两个来源各一行（走 /api/v1/latest?trigger=manual|periodic）
  * 任务被下发但迟迟没有 acked_at 时，页面给出「未收到设备回执」提示（第二次 dump）；
    板端回执后提示消失、acked_at 有值、状态文本含 acked（第三次 dump）
  * 周期上报控制：「暂停周期」按钮代码路径（/ui/?autopause=1&pause=90）建出 kind=pause 任务、
    控制卡片/任务卡片/历史表按控制任务渲染；模拟板端领取并 POST /applied 后，
    控制卡片转为「停止中」+ 倒计时 + request_id 可追溯；「恢复周期」（?autoresume=1）
    再把它恢复成「上报中」（第四/五/六次 dump）

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


def any_text(dom, elem_id):
    """取出任意标签（span / p / td …）且带指定 id 的元素文本。"""
    m = re.search(r'<(\w+)[^>]*id="%s"[^>]*>(.*?)</\1>' % elem_id, dom, re.S)
    return m.group(2).strip() if m else ""


def tbody_rows(dom, elem_id):
    """返回某个 <tbody id="x"> 内 <tr> 的行数（0 表示表体不存在）。"""
    m = re.search(r'<tbody[^>]*id="%s"[^>]*>(.*?)</tbody>' % elem_id, dom, re.S)
    return m.group(1).count("<tr") if m else 0


def tbody_html(dom, elem_id):
    """返回某个 <tbody id="x"> 的原始 HTML（用于检查单元格里是否出现某个 request_id）。"""
    m = re.search(r'<tbody[^>]*id="%s"[^>]*>(.*?)</tbody>' % elem_id, dom, re.S)
    return m.group(1) if m else ""


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
    # URL 里的 samples/rate 会先覆盖参数表单初值，页面随后就用这组参数建任务（D3 验证）
    dom = dump_dom(browser, BASE + "/ui/?autocapture=1&samples=250&rate=50")
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

    # 8) 参数表单：板端上限写进 DOM，且页面建出的任务真的用了表单里的参数
    check("samples input clamped to board limit",
          bool(re.search(r'id="countInput"[^>]*max="600"[^>]*value="250"', dom)),
          "countInput max=600 value=250")
    check("rate input clamped to board limit",
          bool(re.search(r'id="rateInput"[^>]*max="100"[^>]*value="50"', dom)),
          "rateInput max=100 value=50")
    check("source fixed to board value",
          bool(re.search(r'id="sourceSel".*?qma6100p', dom, re.S)))
    st, body = get_json("/api/v1/tasks?device_id=%s&limit=20" % DEV)
    page_task = [t for t in body["tasks"] if t["request_id"] == rid_page][0]
    check("page task uses form params",
          page_task["sample_count"] == 250 and page_task["sample_rate_hz"] == 50
          and page_task["source"] == "qma6100p",
          "count=%s rate=%s source=%s" % (page_task["sample_count"],
                                          page_task["sample_rate_hz"],
                                          page_task["source"]))

    # 9) 任务卡片：三个状态时间戳与 upload_id 都有渲染（此刻任务还没被板端领取）
    check("task timestamp fields rendered",
          field(dom, "taskDispatched") and field(dom, "taskAcked")
          and field(dom, "taskCompleted"),
          field(dom, "taskDispatched"))
    check("task upload_id placeholder when unfinished",
          "任务未完成" in field(dom, "taskUploadId"), field(dom, "taskUploadId"))

    # 10) 对照区：manual / periodic 各一行，manual 行能追到 seed 的 request_id
    cmp_html = tbody_html(dom, "cmpBody")
    check("compare table has one row per trigger",
          tbody_rows(dom, "cmpBody") == 2 and "manual（按需采集）" in cmp_html
          and "periodic（周期上报）" in cmp_html,
          "rows=%d" % tbody_rows(dom, "cmpBody"))
    check("compare manual row traced to task",
          ("%s…%s" % (rid_seed[:6], rid_seed[-4:])) in cmp_html, rid_seed)

    # 11) 任务历史表：seed 的已完成任务与页面自建任务都在表里（按创建时间倒序）
    hist_html = tbody_html(dom, "taskHistoryBody")
    check("history table has rows", tbody_rows(dom, "taskHistoryBody") >= 2,
          "rows=%d" % tbody_rows(dom, "taskHistoryBody"))
    check("history header counts completed",
          "已完成 1 条" in field(dom, "taskHistoryInfo"),
          field(dom, "taskHistoryInfo"))
    check("history lists page task", rid_page in hist_html and
          ("%s…%s" % (rid_page[:6], rid_page[-4:])) in hist_html, rid_page)
    st, task_detail = get_json("/api/v1/tasks/%s" % rid_seed)
    check("history lists completed upload_id",
          ("><td>%s</td>" % task_detail["upload"]["id"]) in hist_html.replace(" ", ""),
          "upload_id=%s" % task_detail["upload"]["id"])

    # 12) 板端领取却迟迟不回执：第二次 dump 应出现「未收到设备回执」提示（D4）
    st, body = get_json("/api/v1/tasks/next?device_id=" + DEV)
    check("page task claimed for the hint test",
          body.get("found") is True and body["task"]["request_id"] == rid_page,
          str(body)[:120])
    conn = main._connect()
    try:
        conn.execute("UPDATE tasks SET dispatched_at=? WHERE request_id=?",
                     (time.time() - 12, rid_page))
        conn.commit()
    finally:
        conn.close()
    dom2 = dump_dom(browser, BASE + "/ui/")
    check("dispatched_at rendered",
          "—（尚未下发）" not in field(dom2, "taskDispatched"),
          field(dom2, "taskDispatched"))
    hint = any_text(dom2, "taskAckHint")
    check("no-ack hint shown", "仍未收到设备回执" in hint, hint[:90])

    # 13) 板端回执后：acked_at 有值、提示消失、状态文本含 acked（D1 + D5）
    st, body = post_json("/api/v1/tasks/%s/ack" % rid_page, {})
    check("page task acked", st == 200 and body["task"]["status"] == "acked",
          str(body)[:120])
    dom3 = dump_dom(browser, BASE + "/ui/")
    check("acked_at rendered",
          "—（尚未回执）" not in field(dom3, "taskAcked"), field(dom3, "taskAcked"))
    check("no-ack hint cleared",
          "仍未收到设备回执" not in any_text(dom3, "taskAckHint"))
    check("status text mentions acked", "状态码 acked" in field(dom3, "taskStatus"),
          field(dom3, "taskStatus"))

    # 14) 周期上报控制（页面按钮代码路径）：?autopause=1&pause=90 打开页面即建一条
    #     kind=pause 任务，控制卡片/任务卡片/历史表都要按控制任务渲染
    dom4 = dump_dom(browser, BASE + "/ui/?autopause=1&pause=90")
    check("pause input clamped to server limit",
          bool(re.search(r'id="pauseInput"[^>]*max="600"[^>]*value="90"', dom4)),
          "pauseInput max=600 value=90")
    check("pause/resume buttons rendered",
          'id="pauseBtn"' in dom4 and 'id="resumeBtn"' in dom4)
    check("control card shows reporting state",
          "上报中" in field(dom4, "ctrlBadge")
          and "未暂停" in field(dom4, "ctrlRemaining"),
          "%s / %s" % (field(dom4, "ctrlBadge"), field(dom4, "ctrlRemaining")))
    check("control rid empty before any applied",
          "还没有控制任务" in field(dom4, "ctrlRid"), field(dom4, "ctrlRid"))

    rid_pause = field(dom4, "taskRid")
    check("page created a pause task", len(rid_pause) == 32 and rid_pause != rid_page,
          "rid=%s" % rid_pause)
    st, body = get_json("/api/v1/tasks?device_id=%s&limit=20" % DEV)
    pause_task = [t for t in body["tasks"] if t["request_id"] == rid_pause]
    check("pause task stored with kind/duration from form",
          bool(pause_task) and pause_task[0]["kind"] == "pause"
          and pause_task[0]["duration_s"] == 90
          and pause_task[0]["status"] == "submitted"
          and pause_task[0]["trigger"] == "control",
          str(pause_task[:1])[:160])
    check("task card renders pause spec",
          "暂停 90" in field(dom4, "taskSpec"), field(dom4, "taskSpec"))
    check("task card explains control tasks carry no batch",
          "不产生批次" in field(dom4, "taskUploadId"), field(dom4, "taskUploadId"))
    check("history shows task kind column",
          "暂停周期" in tbody_html(dom4, "taskHistoryBody"))

    # 15) 板端领取 pause 并 POST /applied → 页面控制卡片转为「停止中」并显示倒计时
    st, nx = get_json("/api/v1/tasks/next?device_id=" + DEV)
    check("board claimed the pause task",
          nx.get("found") is True and nx["task"]["request_id"] == rid_pause
          and nx["task"]["kind"] == "pause", str(nx)[:140])
    st, body = post_json("/api/v1/tasks/%s/applied" % rid_pause,
                         {"paused_until_ms": int(time.time() * 1000) + 90000})
    check("applied closes the page pause task",
          st == 200 and body["task"]["status"] == "completed"
          and body["control"]["periodic_paused"] is True, str(body)[:140])

    dom5 = dump_dom(browser, BASE + "/ui/?autoresume=1")
    check("control badge shows paused",
          "停止中" in field(dom5, "ctrlBadge")
          and "已暂停" in field(dom5, "ctrlState"),
          "%s / %s" % (field(dom5, "ctrlBadge"), field(dom5, "ctrlState")))
    check("control countdown rendered",
          "后自动恢复" in field(dom5, "ctrlRemaining"), field(dom5, "ctrlRemaining"))
    check("paused_until rendered",
          "未暂停" not in field(dom5, "ctrlUntil") and
          len(field(dom5, "ctrlUntil")) > 10, field(dom5, "ctrlUntil"))
    check("control rid traced to the pause task",
          rid_pause in field(dom5, "ctrlRid"), field(dom5, "ctrlRid"))

    rid_resume = field(dom5, "taskRid")
    check("page created a resume task", len(rid_resume) == 32 and rid_resume != rid_pause,
          "rid=%s" % rid_resume)
    st, body = get_json("/api/v1/tasks?device_id=%s&limit=20" % DEV)
    resume_task = [t for t in body["tasks"] if t["request_id"] == rid_resume]
    check("resume task stored with kind=resume",
          bool(resume_task) and resume_task[0]["kind"] == "resume"
          and resume_task[0]["duration_s"] is None
          and resume_task[0]["status"] == "submitted", str(resume_task[:1])[:160])
    check("history shows resume kind",
          "恢复周期" in tbody_html(dom5, "taskHistoryBody"))

    # 16) 板端领取 resume 并 applied → 页面回到「上报中」，任务卡片显示控制任务无批次
    st, nx = get_json("/api/v1/tasks/next?device_id=" + DEV)
    check("board claimed the resume task",
          nx["task"]["request_id"] == rid_resume and nx["task"]["kind"] == "resume",
          str(nx)[:140])
    st, body = post_json("/api/v1/tasks/%s/applied" % rid_resume, {})
    check("applied closes the resume task",
          st == 200 and body["control"]["periodic_paused"] is False
          and body["control"]["request_id"] == rid_resume, str(body)[:140])

    dom6 = dump_dom(browser, BASE + "/ui/")
    check("control badge back to reporting",
          "上报中" in field(dom6, "ctrlBadge")
          and "未暂停" in field(dom6, "ctrlRemaining"),
          "%s / %s" % (field(dom6, "ctrlBadge"), field(dom6, "ctrlRemaining")))
    check("control rid now the resume task",
          rid_resume in field(dom6, "ctrlRid"), field(dom6, "ctrlRid"))
    check("resume task card completed without batch",
          "状态码 completed" in field(dom6, "taskStatus")
          and "不产生批次" in field(dom6, "taskUploadId"),
          "%s / %s" % (field(dom6, "taskStatus"), field(dom6, "taskUploadId")))

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