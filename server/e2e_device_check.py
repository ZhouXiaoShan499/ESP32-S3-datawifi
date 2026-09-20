# -*- coding: utf-8 -*-
"""
真机（上板）脚本化验收：需要一块正在周期上报的 ESP32-S3 开发板。

与另外两个自检不同，本脚本不造临时库/临时端口：它按板端固件里配置的地址
（默认 0.0.0.0:8000）起真实服务，等板端自己的周期上报出现，然后完整跑一遍

    建任务 → 板端领取(dispatched) → 回执(acked) → 采集窗口内暂停周期上报
    → 带 request_id 回传(completed，upload_id 关联) → 恢复周期上报 → 任务历史可见

前置条件：
  1) 板端固件已烧录、连上同一 WiFi，且服务端地址指向本机（固件里的
     `CONFIG_SENSOR_SERVER_URL` / `http://<PC_IP>:8000`）；
  2) 板端正在周期上报：`/api/v1/devices` 里能看到它，且最近一批周期数据不超过
     `DEVICE_FRESH_S`（默认 15 s）。

运行：python server/e2e_device_check.py
  没有正在上报的板端时打印 [SKIP] 并以 0 退出，不会让流水线变红。
  可用环境变量覆盖：SENSOR_PORT / SENSOR_DB / DEVICE_ID / DEVICE_FRESH_S /
  TASK_SAMPLES / TASK_RATE_HZ / TASK_TIMEOUT_S / TASK_WAIT_S / RESUME_WAIT_S。
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