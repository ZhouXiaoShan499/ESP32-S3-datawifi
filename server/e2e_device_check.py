# -*- coding: utf-8 -*-
"""
真机（上板）脚本化验收：需要一块正在周期上报的 ESP32-S3 开发板。

与另外两个自检不同，本脚本不造临时库/临时端口：它按板端固件里配置的地址
（默认 0.0.0.0:8000）起真实服务，等板端自己的周期上报出现，然后完整跑一遍

    建任务 → 板端领取(dispatched) → 回执(acked) → 采集窗口内暂停周期上报
    → 带 request_id 回传(completed，upload_id 关联) → 恢复周期上报 → 任务历史可见
    → 「暂停周期」控制任务（板端 applied → device_control 记为停止中 → 观察窗口内确实没有
    新周期批次 → 暂停期间手动采集仍可用）→ 「恢复周期」控制任务 → 周期批次重新出现

前置条件：
  1) 板端固件已烧录、连上同一 WiFi，且服务端地址指向本机（固件里的
     `CONFIG_SENSOR_SERVER_URL` / `http://<PC_IP>:8000`）；
  2) 板端正在周期上报：`/api/v1/devices` 里能看到它，且最近一批周期数据不超过
     `DEVICE_FRESH_S`（默认 15 s）。

运行：python server/e2e_device_check.py
  没有正在上报的板端时打印 [SKIP] 并以 0 退出，不会让流水线变红。
  可用环境变量覆盖：SENSOR_PORT / SENSOR_DB / DEVICE_ID / DEVICE_FRESH_S /
  TASK_SAMPLES / TASK_RATE_HZ / TASK_TIMEOUT_S / TASK_WAIT_S / RESUME_WAIT_S /
  PAUSE_S / PAUSE_OBSERVE_S。
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 输出固定 UTF-8：中文 Windows 下控制台默认 cp936，遇到 m/s² 会编码失败
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

PORT = os.environ.get("SENSOR_PORT", "8000")
os.environ.setdefault("SENSOR_DB", os.path.join(_HERE, "data", "sensor.db"))
os.environ.setdefault("SENSOR_HOST", "0.0.0.0")   # 板端要从局域网访问，不能只听 127.0.0.1
os.environ["SENSOR_PORT"] = PORT

BASE = "http://127.0.0.1:%s" % PORT
DEVICE_FRESH_S = float(os.environ.get("DEVICE_FRESH_S", "15"))
TASK_SAMPLES = int(os.environ.get("TASK_SAMPLES", "100"))
TASK_RATE_HZ = int(os.environ.get("TASK_RATE_HZ", "100"))
TASK_TIMEOUT_S = int(os.environ.get("TASK_TIMEOUT_S", "60"))
TASK_WAIT_S = float(os.environ.get("TASK_WAIT_S", "90"))
RESUME_WAIT_S = float(os.environ.get("RESUME_WAIT_S", "15"))
PAUSE_S = float(os.environ.get("PAUSE_S", "120"))          # 暂停时长（服务端/板端上限 600 s）
PAUSE_OBSERVE_S = float(os.environ.get("PAUSE_OBSERVE_S", "8"))   # 暂停窗口内观察多久无周期批次

PASSED = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("[FAIL] %s %s" % (name, detail))
    PASSED.append(name)
    print("[PASS] %s %s" % (name, detail))


def request(method, url, data=None, timeout=5):
    """标准库发 HTTP 请求；4xx/5xx 也返回 (状态码, 解析后的 JSON)。"""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def get_json(path, timeout=5):
    return request("GET", BASE + path, None, timeout)


def post_json(path, body):
    return request("POST", BASE + path, body or {})


def wait_server(deadline_s=20):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            st, _ = get_json("/api/v1/health")
            if st == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def periodic_age(device_id):
    """该设备最近一批周期数据的“年龄”（秒）；没有则 None。"""
    st, lj = get_json("/api/v1/latest?trigger=periodic&device_id="
                      + urllib.parse.quote(device_id))
    if st != 200 or not lj.get("found"):
        return None
    return time.time() - float(lj["upload"]["received_at"])


def pick_device():
    """挑出正在周期上报的设备：返回 (device_id, age_s)；没有则 (None, None)。"""
    want = (os.environ.get("DEVICE_ID") or "").strip()
    st, body = get_json("/api/v1/devices")
    if st != 200:
        return None, None
    for d in body.get("devices", []):
        if want and d["device_id"] != want:
            continue
        age = periodic_age(d["device_id"])
        if age is not None and age <= DEVICE_FRESH_S:
            return d["device_id"], age
    return None, None


def upload_counts(device_id, since, until, trigger):
    """统计采集窗口内某类批次数（直接读服务端同一个 SQLite）。"""
    import main                       # 与 uvicorn 线程同进程，共用 SENSOR_DB
    conn = main._connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM uploads WHERE device_id=? AND trigger=?"
            " AND received_at BETWEEN ? AND ?",
            (device_id, trigger, since, until),
        ).fetchone()[0]
    finally:
        conn.close()


def control_of(device_id):
    """读服务端记录的暂停状态（device_control 真相源）。"""
    st, cj = get_json("/api/v1/control?device_id=" + urllib.parse.quote(device_id))
    return cj.get("control", {}) if st == 200 else {}


def wait_status(rid, until_time, want="terminal"):
    """轮询任务；返回最后一次拿到的 (task, upload)。"""
    task, upload = {}, None
    while time.time() < until_time:
        st, dj = get_json("/api/v1/tasks/%s" % rid)
        task, upload = dj.get("task", {}), dj.get("upload")
        if want == "terminal" and task.get("terminal"):
            break
        if want == "leaving_submitted" and task.get("status") != "submitted":
            break
        time.sleep(1)
    return task, upload


def main_run():
    import main                       # 先导入 main：它在 import 时读取 SENSOR_DB
    import uvicorn

    main.init_db()
    config = uvicorn.Config(main.app, host=os.environ["SENSOR_HOST"],
                            port=int(PORT), log_level="warning")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()
    check("server up", wait_server(),
          "%s（板端需指向本机局域网 IP:%s）" % (BASE, PORT))

    device_id, age = pick_device()
    if device_id is None:
        print("[SKIP] 没有正在周期上报的板端（%.0f s 内无 periodic 批次）。"
              % DEVICE_FRESH_S)
        print("       前置：烧录固件并把服务端地址设为本机 IP:%s，"
              "板端开始周期上报后重跑。" % PORT)
        return
    print("[INFO] 目标设备 %s（最近一批周期数据 %.1f s 前）" % (device_id, age))

    # 0) 真实库是长期使用的：上一次运行/页面点击可能留下未终态任务，先清掉，
    #    否则下面建任务会命中 duplicate（复用旧任务），断言就落不到新任务上。
    st, h0 = get_json("/api/v1/tasks?limit=20&device_id="
                      + urllib.parse.quote(device_id))
    leftovers = [t for t in h0.get("tasks", []) if not t["terminal"]]
    for t in leftovers:
        post_json("/api/v1/tasks/%s/fail" % t["request_id"],
                  {"error": "cleanup before device acceptance run"})
    if leftovers:
        print("[INFO] 已清理上一次遗留的未终态任务 %d 条" % len(leftovers))

    # 1) 建任务（参数取服务端与板端都允许的范围）
    st, tj = post_json("/api/v1/tasks", {
        "device_id": device_id, "source": "qma6100p",
        "sample_count": TASK_SAMPLES, "sample_rate_hz": TASK_RATE_HZ,
        "timeout_s": TASK_TIMEOUT_S,
    })
    check("task created", st == 201 and tj["task"]["status"] == "submitted",
          str(tj)[:120])
    rid = tj["task"]["request_id"]

    # 2) 板端每 3 s 轮询一次 → 领取后状态离开 submitted
    task, upload = wait_status(rid, time.time() + 30, "leaving_submitted")
    check("board claimed the task",
          task.get("status") in ("dispatched", "acked", "completed"),
          "status=%s pid=%s" % (task.get("status"), rid))
    check("board acked the task",
          bool(task.get("acked_at")) or task.get("status") == "completed",
          "acked_at=%s" % task.get("acked_at_str"))

    # 3) 采集完成：任务 completed 且带上关联批次
    task, upload = wait_status(rid, time.time() + TASK_WAIT_S)
    check("task completed", task.get("status") == "completed",
          "status=%s error=%s" % (task.get("status"), task.get("error")))
    check("upload linked to task",
          bool(task.get("upload_id")) and (upload or {}).get("request_id") == rid,
          "upload_id=%s" % task.get("upload_id"))
    check("upload is manual and requested size",
          (upload or {}).get("trigger") == "manual"
          and (upload or {}).get("sample_count") == TASK_SAMPLES,
          str({k: (upload or {}).get(k) for k in ("trigger", "sample_count")}))

    # 4) 页面对照区/数据卡片看到的就是这次任务
    st, lj = get_json("/api/v1/latest?trigger=manual&device_id="
                      + urllib.parse.quote(device_id))
    check("latest?trigger=manual is this task",
          st == 200 and lj.get("found") and lj["upload"]["request_id"] == rid,
          str(lj.get("upload", {}).get("id")))

    # 5) 采集窗口内周期上报被暂停（板端 manual_capture 期间不喂周期窗口）
    w0 = float(task.get("dispatched_at") or task.get("created_at"))
    w1 = float(task.get("completed_at") or time.time())
    n_periodic = upload_counts(device_id, w0, w1, "periodic")
    n_manual = upload_counts(device_id, w0, w1, "manual")
    check("periodic uploads paused during capture", n_periodic == 0,
          "窗口 %.1f s：periodic=%d manual=%d" % (w1 - w0, n_periodic, n_manual))

    # 6) 任务结束后周期上报恢复
    resumed = False
    end = time.time() + RESUME_WAIT_S
    while time.time() < end:
        st, pj = get_json("/api/v1/latest?trigger=periodic&device_id="
                          + urllib.parse.quote(device_id))
        if pj.get("found") and float(pj["upload"]["received_at"]) > w1:
            resumed = True
            break
        time.sleep(1)
    check("periodic uploads resumed", resumed,
          "%.0f s 内出现新周期批次" % RESUME_WAIT_S)

    # 7) Web 任务历史的数据源里能看到这次任务
    st, hj = get_json("/api/v1/tasks?limit=20&device_id="
                      + urllib.parse.quote(device_id))
    check("task history lists it",
          any(t["request_id"] == rid and t["upload_id"] == task.get("upload_id")
              for t in hj.get("tasks", [])), "count=%s" % hj.get("count"))

    # 8) 「暂停周期」真机验收：建 pause → 板端领取并 applied → 服务端记为停止中
    ctrl0 = control_of(device_id)
    check("control endpoint reachable",
          isinstance(ctrl0.get("periodic_paused"), bool), str(ctrl0)[:120])

    st, pj = post_json("/api/v1/tasks", {
        "device_id": device_id, "source": "qma6100p",
        "kind": "pause", "duration_s": PAUSE_S, "timeout_s": TASK_TIMEOUT_S,
    })
    check("pause task created",
          st == 201 and pj["task"]["kind"] == "pause"
          and pj["task"]["duration_s"] == PAUSE_S, str(pj)[:140])
    p_rid = pj["task"]["request_id"]

    p_task, _ = wait_status(p_rid, time.time() + 30)
    check("pause task completed by device",
          p_task.get("status") == "completed",
          "status=%s error=%s（若是 failed/dispatched：板端固件很可能仍是旧版 —— "
          "控制任务需要本次新增的固件，请 idf.py build + flash 后重跑）"
          % (p_task.get("status"), p_task.get("error")))
    check("pause task carries no upload",
          not p_task.get("upload_id"),
          "upload_id=%s" % p_task.get("upload_id"))

    ctrl = control_of(device_id)
    check("device reported applied → server marks paused",
          ctrl.get("periodic_paused") is True and ctrl.get("request_id") == p_rid,
          str(ctrl)[:160])
    check("pause deadline is in the future",
          (ctrl.get("remaining_s") or 0) > 0 and ctrl.get("paused_until_str"),
          "remaining=%s until=%s" % (ctrl.get("remaining_s"),
                                     ctrl.get("paused_until_str")))
    st, dj = get_json("/api/v1/devices")
    row = [d for d in dj["devices"] if d["device_id"] == device_id]
    check("devices list marks periodic_paused",
          bool(row) and row[0]["periodic_paused"] is True, str(row[:1])[:140])

    # 9) 暂停窗口内板端确实不再传周期批次（不看设备日志，只看服务端入库记录）
    pause_w0 = time.time()
    time.sleep(PAUSE_OBSERVE_S)
    n_periodic = upload_counts(device_id, pause_w0, time.time() + 1, "periodic")
    check("no periodic batch while paused", n_periodic == 0,
          "%.0f s 观察窗内 periodic=%d" % (PAUSE_OBSERVE_S, n_periodic))

    # 10) 暂停期间按需采集仍然可用（暂停只关周期窗口）
    st, mj = post_json("/api/v1/tasks", {
        "device_id": device_id, "source": "qma6100p",
        "sample_count": TASK_SAMPLES, "sample_rate_hz": TASK_RATE_HZ,
        "timeout_s": TASK_TIMEOUT_S,
    })
    check("capture task accepted while paused",
          st == 201 and mj["task"]["kind"] == "capture", str(mj)[:140])
    m_rid = mj["task"]["request_id"]
    m_task, m_upload = wait_status(m_rid, time.time() + TASK_WAIT_S)
    check("manual capture works while paused",
          m_task.get("status") == "completed"
          and (m_upload or {}).get("trigger") == "manual",
          "status=%s upload=%s" % (m_task.get("status"),
                                   (m_upload or {}).get("id")))
    check("still paused after the manual capture",
          control_of(device_id).get("periodic_paused") is True)

    # 11) 「恢复周期」：resume 任务 applied 后立刻恢复，周期批次重新出现
    st, rj = post_json("/api/v1/tasks", {
        "device_id": device_id, "source": "qma6100p",
        "kind": "resume", "timeout_s": TASK_TIMEOUT_S,
    })
    check("resume task created",
          st == 201 and rj["task"]["kind"] == "resume", str(rj)[:140])
    r_rid = rj["task"]["request_id"]
    r_task, _ = wait_status(r_rid, time.time() + 30)
    check("resume task completed by device",
          r_task.get("status") == "completed",
          "status=%s error=%s" % (r_task.get("status"), r_task.get("error")))
    check("server cleared paused state",
          control_of(device_id).get("periodic_paused") is False,
          str(control_of(device_id))[:140])

    r_w0 = float(r_task.get("completed_at") or time.time())
    resumed2 = False
    end = time.time() + RESUME_WAIT_S
    while time.time() < end:
        st, pj2 = get_json("/api/v1/latest?trigger=periodic&device_id="
                           + urllib.parse.quote(device_id))
        if pj2.get("found") and float(pj2["upload"]["received_at"]) > r_w0:
            resumed2 = True
            break
        time.sleep(1)
    check("periodic uploads resumed after resume task", resumed2,
          "%.0f s 内出现新周期批次" % RESUME_WAIT_S)

    # 12) 页面任务历史里三条任务（采集/暂停/恢复）齐全，控制任务无 upload_id
    st, hj = get_json("/api/v1/tasks?limit=20&device_id="
                      + urllib.parse.quote(device_id))
    by_rid = {t["request_id"]: t for t in hj.get("tasks", [])}
    check("history lists all three tasks",
          all(r in by_rid for r in (rid, p_rid, m_rid, r_rid)),
          "count=%s" % hj.get("count"))
    check("history marks control tasks as control",
          by_rid[r_rid]["is_control"] is True
          and by_rid.get(m_rid, {}).get("is_control") is False,
          "%s/%s" % (by_rid[r_rid].get("kind_cn"),
                     (by_rid.get(m_rid) or {}).get("kind_cn")))

    print("\nDEVICE ACCEPTANCE PASSED (%d checks)" % len(PASSED))


if __name__ == "__main__":
    code = 0
    try:
        main_run()
    except Exception as exc:                      # noqa: BLE001 - 验收脚本要打印原因
        print("\n[FAIL] %s: %s" % (type(exc).__name__, exc))
        code = 1
    finally:
        sys.stdout.flush()      # os._exit 不会刷新缓冲，必须手动 flush
        sys.stderr.flush()
        os._exit(code)          # 直接退出，避免 uvicorn 线程阻塞进程