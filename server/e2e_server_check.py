# -*- coding: utf-8 -*-
"""
真实网络端到端自检：以子进程方式真正启动服务（uvicorn），
再用标准库 urllib 通过真实 HTTP 上传并查询。

运行：python server/e2e_server_check.py   （需已安装 fastapi + uvicorn）
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

# 中文输出固定 UTF-8：中文 Windows 下控制台/重定向默认 cp936，日志会变乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_E2E_DB = os.path.join(_HERE, "data", "e2e_upload.db")
if os.path.exists(_E2E_DB):
    os.remove(_E2E_DB)

PORT = "8011"
BASE = f"http://127.0.0.1:{PORT}"


def request(method, url, data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    env = dict(os.environ)
    env["SENSOR_DB"] = _E2E_DB
    env["SENSOR_PORT"] = PORT
    env["SENSOR_HOST"] = "127.0.0.1"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_HERE, "main.py")],
        cwd=_HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # 等服务起来
        up = False
        for _ in range(50):
            if proc.poll() is not None:
                raise SystemExit("server exited early:\n"
                                 + (proc.stdout.read() if proc.stdout else ""))
            try:
                st, _ = request("GET", BASE + "/api/v1/health")
                if st == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.2)
        if not up:
            raise SystemExit("server did not become ready")
        print("[PASS] uvicorn 子进程已就绪并响应 /api/v1/health")

        now_ms = int(time.time() * 1000)
        payload = {
            "device_id": "esp32s3-eye-0001",
            "source": "qma6100p",
            "unit": "m/s^2",
            "ts_ms": now_ms,
            "samples": [{"i": i, "t_ms": i * 10,
                         "ax": 0.1 * i, "ay": -0.2 * i, "az": 9.8 + 0.01 * i}
                        for i in range(4)],
        }
        st, up_j = request("POST", BASE + "/api/v1/upload", payload)
        assert st == 201, (st, up_j)
        assert up_j["ok"] and up_j["sample_count"] == 4
        print("[PASS] POST 真实 HTTP 上传 201:", up_j["upload_id"])

        st, h = request("GET", BASE + "/api/v1/health")
        assert h["total_uploads"] == 1 and h["total_samples"] == 4
        print("[PASS] health total_uploads=%s total_samples=%s"
              % (h["total_uploads"], h["total_samples"]))

        st, lj = request("GET", BASE + "/api/v1/latest")
        assert lj["found"] and abs(lj["sample_tail"][-1]["az"] - 9.83) < 1e-6
        print("[PASS] latest 末样本 az=%s（与上传值一致）"
              % lj["sample_tail"][-1]["az"])

        # ---- 按需采集任务：真实 HTTP 全链路（建 → 连点去重 → 领 → ack → 回传 → 幂等）----
        task_body = {"device_id": payload["device_id"], "sample_count": 2}
        st, tj = request("POST", BASE + "/api/v1/tasks", task_body)
        assert st == 201 and tj["duplicate"] is False, (st, tj)
        rid = tj["task"]["request_id"]
        assert tj["task"]["status"] == "submitted"

        st, dup = request("POST", BASE + "/api/v1/tasks", task_body)
        assert st == 200 and dup["duplicate"] is True, (st, dup)
        assert dup["task"]["request_id"] == rid
        print("[PASS] 连点去重（真实 HTTP）：duplicate=true 且 request_id 不变")

        next_url = BASE + "/api/v1/tasks/next?device_id=" + payload["device_id"]
        st, nx = request("GET", next_url)
        assert nx["found"] is True and nx["task"]["request_id"] == rid, nx
        st, nx2 = request("GET", next_url)
        assert nx2["found"] is False, nx2
        st, _ = request("POST", BASE + "/api/v1/tasks/%s/ack" % rid, {})
        assert st == 200, st
        print("[PASS] /tasks/next 领取一次即 dispatched，ack 回执 200（真实 HTTP）")

        manual = dict(payload)
        manual["ts_ms"] = int(time.time() * 1000)
        manual["request_id"] = rid
        manual["trigger"] = "manual"
        manual["samples"] = [{"i": i, "t_ms": i * 10, "ax": 0.01 * i,
                              "ay": -0.02 * i, "az": 9.8} for i in range(2)]
        st, mj = request("POST", BASE + "/api/v1/upload", manual)
        assert st == 201 and mj["request_id"] == rid and mj["trigger"] == "manual", \
            (st, mj)

        st, again = request("POST", BASE + "/api/v1/upload", manual)
        assert st == 200 and again["idempotent"] is True, (st, again)
        assert again["upload_id"] == mj["upload_id"], (again, mj)

        st, tj = request("GET", BASE + "/api/v1/tasks/%s" % rid)
        assert tj["task"]["status"] == "completed", tj
        assert tj["task"]["upload_id"] == mj["upload_id"], tj
        print("[PASS] 手动批次回传 → 任务 completed（真实 HTTP，upload_id=%s）"
              % mj["upload_id"])

        st, h = request("GET", BASE + "/api/v1/health")
        assert h["tasks_by_status"].get("completed") == 1, h
        print("[PASS] health 任务统计（真实 HTTP）:", h["tasks_by_status"])

        # ---- 周期上报控制：真实 HTTP 全链路（建 pause → 领 → applied → device_control）----
        ctrl_dev = "esp32s3-eye-0001-ctrl"
        # 先让该设备出现在 /api/v1/devices 里（设备列表来自 uploads 表），
        # 这样才能验证「暂停状态」在设备列表上被正确标注。
        boot = dict(payload)
        boot["device_id"] = ctrl_dev
        boot["ts_ms"] = int(time.time() * 1000)
        st, _ = request("POST", BASE + "/api/v1/upload", boot)
        assert st == 201, st

        st, cj = request("GET", BASE + "/api/v1/control?device_id=" + ctrl_dev)
        assert st == 200 and cj["control"]["periodic_paused"] is False, (st, cj)
        st, dj = request("GET", BASE + "/api/v1/devices")
        ctrl_row = [d for d in dj["devices"] if d["device_id"] == ctrl_dev][0]
        assert ctrl_row["periodic_paused"] is False, ctrl_row
        print("[PASS] /api/v1/control 初始未暂停（真实 HTTP）")

        st, pj = request("POST", BASE + "/api/v1/tasks",
                         {"device_id": ctrl_dev, "kind": "pause", "duration_s": 20})
        assert st == 201 and pj["task"]["kind"] == "pause", (st, pj)
        assert pj["task"]["duration_s"] == 20 and pj["task"]["is_control"] is True
        pause_rid = pj["task"]["request_id"]

        next_url = BASE + "/api/v1/tasks/next?device_id=" + ctrl_dev
        st, nx = request("GET", next_url)
        assert nx["found"] is True and nx["task"]["request_id"] == pause_rid, nx
        assert nx["task"]["kind"] == "pause", nx

        until_ms = int(time.time() * 1000) + 20000
        st, aj = request("POST",
                         BASE + "/api/v1/tasks/%s/applied" % pause_rid,
                         {"paused_until_ms": until_ms})
        assert st == 200 and aj["task"]["status"] == "completed", (st, aj)
        assert aj["control"]["periodic_paused"] is True, aj
        assert 15 <= aj["control"]["remaining_s"] <= 21, aj["control"]
        print("[PASS] pause 经 /applied 生效（真实 HTTP）：periodic_paused=True 剩余 %ss"
              % aj["control"]["remaining_s"])

        st, dj = request("GET", BASE + "/api/v1/devices")
        ctrl_row = [d for d in dj["devices"] if d["device_id"] == ctrl_dev][0]
        assert ctrl_row["periodic_paused"] is True, ctrl_row
        print("[PASS] /api/v1/devices 标注 periodic_paused=True（真实 HTTP）")

        st, rj = request("POST", BASE + "/api/v1/tasks",
                         {"device_id": ctrl_dev, "kind": "resume"})
        assert st == 201 and rj["superseded"] == 0, (st, rj)
        st, nx = request("GET", next_url)
        assert nx["task"]["request_id"] == rj["task"]["request_id"], nx
        st, aj2 = request("POST",
                          BASE + "/api/v1/tasks/%s/applied" % nx["task"]["request_id"],
                          {})
        assert st == 200 and aj2["control"]["periodic_paused"] is False, (st, aj2)
        print("[PASS] resume 提前恢复（真实 HTTP）：periodic_paused=False")

        st, wj = request("POST", BASE + "/api/v1/tasks/%s/applied" % rid, {})
        assert st == 409 and wj["code"] == "WRONG_KIND", (st, wj)
        print("[PASS] 采集任务调 /applied 被拒 409 WRONG_KIND（真实 HTTP）")

        st, h = request("GET", BASE + "/api/v1/health")
        assert h["tasks_by_status"].get("completed") == 3, h["tasks_by_status"]
        assert h["tasks_by_status"].get("failed", 0) == 0, h["tasks_by_status"]
        print("[PASS] health 任务统计（含控制任务）:", h["tasks_by_status"])

        # ---------------- 闭环事件（板端按键触发，真实 HTTP） ----------------
        # 这条链路方向与 tasks 相反：板端发起、Web 接收，所以这里模拟的
        # "板端"顺序是 触发 → 回应，而 "Web" 负责看列表和点确认完成。
        ev_dev = "esp32s3-eye-loop-e2e"
        ev_rid = uuid.uuid4().hex

        st, ej = request("POST", BASE + "/api/v1/events/trigger", {
            "device_id": ev_dev, "source": "qma6100p", "kind": "alert",
            "request_id": ev_rid, "ts_ms": int(time.time() * 1000),
        })
        assert st == 201 and ej["event"]["status"] == "pending", (st, ej)
        print("[PASS] 板端触发（真实 HTTP）：201 + status=pending")

        # 板端重试同一次按键（同一 request_id）→ 幂等，不产生第二条
        st, dup = request("POST", BASE + "/api/v1/events/trigger", {
            "device_id": ev_dev, "source": "qma6100p", "kind": "alert",
            "request_id": ev_rid, "ts_ms": int(time.time() * 1000),
        })
        assert st == 200 and dup.get("idempotent") is True, (st, dup)
        st, lj = request("GET", BASE + "/api/v1/events?device_id=" + ev_dev)
        assert lj["count"] == 1, lj
        print("[PASS] 板端重试幂等（真实 HTTP）：同 request_id 仍只有 1 条事件")

        # 板端轮询远端状态
        st, sj = request(
            "GET", BASE + "/api/v1/events/status?device_id=%s&request_id=%s"
            % (ev_dev, ev_rid))
        assert st == 200 and sj["status"] == "pending", (st, sj)
        print("[PASS] 板端轮询 /events/status（真实 HTTP）：status=pending")

        # Web 远端显示：列表里能看到这条待处理事件
        st, lj = request("GET", BASE + "/api/v1/events?device_id=%s&active=1" % ev_dev)
        assert lj["pending"] == 1 and lj["events"][0]["request_id"] == ev_rid, lj
        print("[PASS] Web 远端列表可见待处理事件（active=1）")

        # 板端按「回应」→ ack
        st, rj = request("POST", BASE + "/api/v1/events/respond",
                         {"request_id": ev_rid, "action": "accept", "by": "device"})
        assert st == 200 and rj["event"]["status"] == "ack", (st, rj)
        print("[PASS] 板端回应 accept（真实 HTTP）：status=ack")

        # 板端轮询到 ack —— 等价于 LCD 上从 LOOP:WAIT 走到远端已回来
        st, sj = request(
            "GET", BASE + "/api/v1/events/status?device_id=%s&request_id=%s"
            % (ev_dev, ev_rid))
        assert sj["status"] == "ack", sj
        print("[PASS] 板端轮询到 ack（真实 HTTP）：本地可收尾")

        # Web 点「确认完成」→ completed，终态不可回退
        st, cj = request("POST", BASE + "/api/v1/events/respond",
                         {"request_id": ev_rid, "action": "confirm", "by": "web"})
        assert st == 200 and cj["event"]["status"] == "completed", (st, cj)
        st, cj2 = request("POST", BASE + "/api/v1/events/respond",
                          {"request_id": ev_rid, "action": "cancel", "by": "web"})
        assert st == 409 and cj2["code"] == "CONFLICT", (st, cj2)
        print("[PASS] Web 确认完成 → completed，且终态不可回退（409）")

        # 未知 request_id：轮询 200/found=false；响应 404
        st, uj = request("GET", BASE + "/api/v1/events/status?device_id=%s&request_id=%s"
                                % (ev_dev, uuid.uuid4().hex))
        assert st == 200 and uj["found"] is False, (st, uj)
        st, uj2 = request("POST", BASE + "/api/v1/events/respond",
                          {"request_id": uuid.uuid4().hex, "action": "accept"})
        assert st == 404, (st, uj2)
        print("[PASS] 未知 request_id：轮询 200/found=false，响应 404")

        st, h = request("GET", BASE + "/api/v1/health")
        assert h["total_events"] == 1 and h["events_by_status"].get("completed") == 1, h
        print("[PASS] health 事件统计:", h["events_by_status"])

        print("\nE2E ALL CHECKS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
