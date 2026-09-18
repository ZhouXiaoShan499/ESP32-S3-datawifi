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

        print("\nE2E ALL CHECKS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
