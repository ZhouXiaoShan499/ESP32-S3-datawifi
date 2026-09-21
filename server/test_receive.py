# -*- coding: utf-8 -*-
"""
本地联调自测：验证接收服务能正确"接收-校验-存储-查询"。

用独立的临时 SQLite（server/data/test_upload.db），不污染正式库。
运行：python server/test_receive.py   （需已安装 flask）
"""

import os
import sys
import time

# 中文输出固定 UTF-8：中文 Windows 下控制台/重定向默认 cp936，日志会变乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 先指到临时库，再导入 main（main 在 import 时读取 SENSOR_DB）
_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_DB = os.path.join(_HERE, "data", "test_upload.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["SENSOR_DB"] = _TEST_DB
os.environ["SENSOR_HOST"] = "127.0.0.1"

sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402


def now_ms():
    return int(time.time() * 1000)


def make_payload(device="esp32s3-eye-0001", ts=None, n=3, az_base=9.8):
    ts = ts or now_ms()
    samples = [
        {"i": i, "t_ms": i * 10,
         "ax": 0.01 * i, "ay": -0.1 * i, "az": az_base + 0.05 * i}
        for i in range(n)
    ]
    return {
        "device_id": device,
        "source": "qma6100p",
        "unit": "m/s^2",
        "ts_ms": ts,
        "samples": samples,
    }


def main_run():
    main.init_db()
    client = TestClient(main.app)

    # 1) 空库：首页应显示"尚无传感数据"
    r = client.get("/")
    assert r.status_code == 200, r.status_code
    assert "尚无传感数据" in r.text, "no-data page missing"
    print("[PASS] no-data 状态正确显示")

    # 2) 非法上传：缺 device_id
    bad = make_payload()
    bad.pop("device_id")
    r = client.post("/api/v1/upload", json=bad)
    assert r.status_code == 400, r.status_code
    assert r.json()["code"] == "INVALID"
    print("[PASS] 缺少 device_id 被拒绝:", r.json()["error"])

    # 3) 非法单位
    bad2 = make_payload()
    bad2["unit"] = "miles/hour"
    r = client.post("/api/v1/upload", json=bad2)
    assert r.status_code == 400
    print("[PASS] 非法单位被拒绝:", r.json()["error"])

    # 4) 合法上传
    ok_payload = make_payload()
    r = client.post("/api/v1/upload", json=ok_payload)
    assert r.status_code == 201, r.status_code
    j = r.json()
    assert j["ok"] is True and j["sample_count"] == 3
    print("[PASS] 合法上传入库 upload_id=%s sample=%s"
          % (j["upload_id"], j["sample_count"]))

    # 5) health 统计
    r = client.get("/api/v1/health")
    h = r.json()
    assert h["total_uploads"] == 1 and h["total_samples"] == 3
    assert ok_payload["device_id"] in h["devices"]
    print("[PASS] health: uploads=%s samples=%s" % (h["total_uploads"],
                                                    h["total_samples"]))

    # 6) latest 核对数值（对照采集值，非写死）
    r = client.get("/api/v1/latest?device_id=%s" % ok_payload["device_id"])
    lj = r.json()
    assert lj["found"] is True
    assert lj["upload"]["device_id"] == ok_payload["device_id"]
    assert abs(lj["upload"]["ts_ms"] - ok_payload["ts_ms"]) == 0
    head_az = lj["sample_head"][0]["az"]
    assert abs(head_az - 9.8) < 0.001, head_az
    print("[PASS] latest: device=%s head_az=%s ts_ms=%s"
          % (lj["upload"]["device_id"], head_az, lj["upload"]["ts_ms"]))

    # 7) 上传后首页出现设备且不再是 no-data
    r = client.get("/")
    assert ok_payload["device_id"] in r.text
    assert "尚无传感数据" not in r.text
    print("[PASS] 上传后首页已展示设备真实数据（非 no-data）")

    # 8) 可选鉴权：默认关闭；临时打开 main.SENSOR_TOKEN 验证 401 / 201 两条分支
    main.SENSOR_TOKEN = "test-secret"
    try:
        no_auth = client.post("/api/v1/upload", json=make_payload(device="auth-dev"))
        assert no_auth.status_code == 401, no_auth.status_code
        assert no_auth.json()["code"] == "UNAUTHORIZED"
        print("[PASS] 开启鉴权后无 Token 上传被拒 401")

        wrong = client.post(
            "/api/v1/upload",
            json=make_payload(device="auth-dev"),
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert wrong.status_code == 401, wrong.status_code
        print("[PASS] 错误 Token 被拒 401")

        good = client.post(
            "/api/v1/upload",
            json=make_payload(device="auth-dev"),
            headers={"Authorization": "Bearer test-secret"},
        )
        assert good.status_code == 201, good.status_code
        print("[PASS] 正确 Token 上传成功 201")
    finally:
        main.SENSOR_TOKEN = ""

    # 9) 按需采集任务：Web 创建 → 板端领取 → ack（submitted→dispatched→acked）
    task_dev = "esp32s3-eye-task"
    r = client.post("/api/v1/tasks", json={"device_id": task_dev, "sample_count": 3})
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    rid = task["request_id"]
    assert task["status"] == "submitted" and task["trigger"] == "manual"
    assert task["sample_count"] == 3 and task["sample_rate_hz"] == 100
    assert task["terminal"] is False
    print("[PASS] 创建按需采集任务 request_id=%s（submitted）" % rid)

    r = client.get("/api/v1/tasks/next", params={"device_id": task_dev})
    claimed = r.json()
    assert claimed["found"] is True and claimed["task"]["status"] == "dispatched"
    assert claimed["task"]["request_id"] == rid
    r = client.get("/api/v1/tasks/next", params={"device_id": task_dev})
    assert r.json()["found"] is False, "同一任务被重复下发"
    print("[PASS] /tasks/next 领取一次即 dispatched，再次领取 found=false")

    r = client.post("/api/v1/tasks/%s/ack" % rid)
    assert r.status_code == 200 and r.json()["task"]["status"] == "acked"
    print("[PASS] /tasks/{id}/ack 回执 → acked")

    # 10) 板端带 request_id 回传 → 任务 completed，且任务与上传互相可追溯
    manual = make_payload(device=task_dev, n=3)
    manual["request_id"] = rid
    manual["trigger"] = "manual"
    r = client.post("/api/v1/upload", json=manual)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["request_id"] == rid and body["trigger"] == "manual"
    assert body["idempotent"] is False
    gj = client.get("/api/v1/tasks/%s" % rid).json()
    assert gj["task"]["status"] == "completed"
    assert gj["task"]["upload_id"] == gj["upload"]["id"]
    assert gj["upload"]["request_id"] == rid and gj["upload"]["trigger"] == "manual"
    print("[PASS] 手动批次回传 → 任务 completed，upload_id=%s 双向可追溯"
          % gj["upload"]["id"])

    # 11) 同一 request_id 重复上传 → 200 idempotent，样本不重复写
    def sample_count_of(dev):
        conn = main._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM samples WHERE device_id=?", (dev,)
            ).fetchone()[0]
        finally:
            conn.close()

    before = sample_count_of(task_dev)
    r = client.post("/api/v1/upload", json=manual)
    assert r.status_code == 200 and r.json()["idempotent"] is True, r.text
    conn = main._connect()
    try:
        uploads_for_rid = conn.execute(
            "SELECT COUNT(*) FROM uploads WHERE request_id=?", (rid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert sample_count_of(task_dev) == before and uploads_for_rid == 1
    print("[PASS] 幂等：重复上传 200 idempotent，样本仍为 %d 条、uploads=%d 条"
          % (before, uploads_for_rid))

    # 12) 连点（并行创建）只复用一个未完成任务，不产生并行采集
    r1 = client.post("/api/v1/tasks", json={"device_id": task_dev, "sample_count": 3})
    r2 = client.post("/api/v1/tasks", json={"device_id": task_dev, "sample_count": 3})
    assert r1.status_code == 201 and r2.status_code == 200, (r1.status_code,
                                                            r2.status_code)
    assert r2.json()["duplicate"] is True
    rid2 = r1.json()["task"]["request_id"]
    assert r2.json()["task"]["request_id"] == rid2
    print("[PASS] 连点去重：第二次 duplicate=true 且 request_id 不变")

    # 13) 归属/样本数不匹配 → 400 且任务置 failed（板端收到 4xx 不再重试）
    client.get("/api/v1/tasks/next", params={"device_id": task_dev})
    wrong = make_payload(device="other-device", n=3)
    wrong["request_id"] = rid2
    r = client.post("/api/v1/upload", json=wrong)
    assert r.status_code == 400 and "device_id mismatch" in r.json()["error"], r.text
    assert client.get("/api/v1/tasks/%s" % rid2).json()["task"]["status"] == "failed"
    print("[PASS] 设备号不匹配被拒 400 且任务 failed:", r.json()["error"][:40])

    short_rid = client.post(
        "/api/v1/tasks", json={"device_id": task_dev, "sample_count": 50}
    ).json()["task"]["request_id"]
    client.get("/api/v1/tasks/next", params={"device_id": task_dev})
    short = make_payload(device=task_dev, n=3)
    short["request_id"] = short_rid
    r = client.post("/api/v1/upload", json=short)
    assert r.status_code == 400 and "sample_count too small" in r.json()["error"]
    assert client.get("/api/v1/tasks/%s" % short_rid).json()["task"]["status"] == "failed"
    print("[PASS] 样本数不足被拒 400 且任务 failed:", r.json()["error"][:40])

    # 14) 超时：过期任务惰性置 timeout，且不再被下发（服务端无后台线程）
    to_rid = client.post(
        "/api/v1/tasks", json={"device_id": task_dev, "timeout_s": 5}
    ).json()["task"]["request_id"]
    conn = main._connect()
    try:
        conn.execute("UPDATE tasks SET expires_at=? WHERE request_id=?",
                     (time.time() - 1, to_rid))
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/v1/tasks/%s" % to_rid).json()["task"]["status"] == "timeout"
    assert client.get("/api/v1/tasks/next",
                      params={"device_id": task_dev}).json()["found"] is False
    print("[PASS] 过期任务惰性判为 timeout，且 /tasks/next 不再下发")

    # 15) 板端主动上报失败 + 未知 request_id → 404
    fail_rid = client.post(
        "/api/v1/tasks", json={"device_id": task_dev, "sample_count": 3}
    ).json()["task"]["request_id"]
    client.get("/api/v1/tasks/next", params={"device_id": task_dev})
    r = client.post("/api/v1/tasks/%s/fail" % fail_rid,
                    json={"error": "i2c read timeout"})
    assert r.status_code == 200 and r.json()["task"]["status"] == "failed"
    assert r.json()["task"]["error"] == "i2c read timeout"
    unknown = "deadbeefdeadbeefdeadbeefdeadbeef"
    r = client.post("/api/v1/tasks/%s/fail" % unknown, json={"error": "x"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert client.get("/api/v1/tasks/%s" % unknown).status_code == 404
    bogus = make_payload(device=task_dev, n=3)
    bogus["request_id"] = unknown
    r = client.post("/api/v1/upload", json=bogus)
    assert r.status_code == 404 and r.json()["code"] == "TASK_NOT_FOUND"
    print("[PASS] 板端 fail 上报置 failed；未知 request_id 一律 404 NOT_FOUND")

    # 16) 周期批次（无 request_id）行为不变 + 汇总接口带上任务信息
    periodic = make_payload(device="esp32s3-eye-periodic", n=3)
    r = client.post("/api/v1/upload", json=periodic)
    assert r.status_code == 201 and r.json()["trigger"] == "periodic"
    assert r.json()["request_id"] is None
    lj = client.get("/api/v1/latest?device_id=esp32s3-eye-periodic").json()
    assert lj["upload"]["trigger"] == "periodic" and lj["upload"]["request_id"] is None
    print("[PASS] 周期批次仍是 trigger=periodic 且不带 request_id")

    lj = client.get("/api/v1/latest?device_id=%s" % task_dev).json()
    assert lj["upload"]["trigger"] == "manual" and lj["upload"]["request_id"] == rid
    print("[PASS] /latest 带 trigger=manual 与 request_id（可追溯到任务）")

    h = client.get("/api/v1/health").json()
    assert h["total_tasks"] >= 5, h["total_tasks"]
    assert h["tasks_by_status"].get("completed") == 1
    assert h["tasks_by_status"].get("timeout") == 1
    assert h["tasks_by_status"].get("failed", 0) >= 3
    print("[PASS] health 任务统计:", h["tasks_by_status"])

    devs = {d["device_id"]: d
            for d in client.get("/api/v1/devices").json()["devices"]}
    assert "pending_tasks" in devs[task_dev] and devs[task_dev]["pending_tasks"] == 0
    print("[PASS] /devices 带 pending_tasks=%s（任务走到终态后归零）"
          % devs[task_dev]["pending_tasks"])

    # 17) 参数校验：越界/缺失都被 400 拒绝
    for bad_body in ({"device_id": task_dev, "sample_count": 0},
                     {"device_id": task_dev, "sample_rate_hz": 5000},
                     {"device_id": task_dev, "timeout_s": 1},
                     {}):
        r = client.post("/api/v1/tasks", json=bad_body)
        assert r.status_code == 400 and r.json()["code"] == "INVALID", (bad_body, r.text)
    print("[PASS] 任务参数越界/缺失均被 400 拒绝")

    # 18) /latest?trigger= 过滤（监控页「手动 vs 周期」对照区用）
    lj = client.get("/api/v1/latest",
                    params={"device_id": task_dev, "trigger": "manual"}).json()
    assert lj["found"] is True and lj["upload"]["trigger"] == "manual"
    assert lj["upload"]["request_id"] == rid
    pj = client.get("/api/v1/latest",
                    params={"device_id": task_dev, "trigger": "periodic"}).json()
    assert pj["found"] is False, pj      # 该设备只有手动批次
    pj2 = client.get("/api/v1/latest",
                     params={"device_id": "esp32s3-eye-periodic",
                             "trigger": "periodic"}).json()
    assert pj2["found"] is True and pj2["upload"]["trigger"] == "periodic"
    r = client.get("/api/v1/latest",
                   params={"device_id": task_dev, "trigger": "auto"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID", r.text
    print("[PASS] /latest?trigger=manual|periodic 过滤生效；非法 trigger 400")

    # 19) /tasks 历史列表：字段齐全、按创建时间倒序、limit 夹取、device_id 过滤
    tj = client.get("/api/v1/tasks",
                    params={"device_id": task_dev, "limit": 20}).json()
    ts = tj["tasks"]
    assert tj["count"] == len(ts) >= 5, tj["count"]
    assert all(t["device_id"] == task_dev for t in ts)
    assert [t["created_at"] for t in ts] == sorted(
        (t["created_at"] for t in ts), reverse=True), "任务列表未按创建时间倒序"
    assert all(t["created_at_str"] and "status_cn" in t and "terminal" in t
               for t in ts), "列表缺少 Web 需要的展示字段"
    done = [t for t in ts if t["status"] == "completed"]
    assert done and done[0]["upload_id"] == gj["upload"]["id"], \
        "历史里的 upload_id 与任务详情不一致"
    assert len(client.get("/api/v1/tasks",
                          params={"device_id": task_dev, "limit": 2}).json()["tasks"]) == 2
    assert len(client.get("/api/v1/tasks",
                          params={"device_id": task_dev, "limit": 0}).json()["tasks"]) == 1
    print("[PASS] /tasks?limit= 历史 %d 条：倒序、字段齐全、upload_id=%s 与详情一致、"
          "limit 夹取到 1..100" % (len(ts), done[0]["upload_id"]))

    # 20) 周期上报控制（kind=pause/resume）：Web 建任务 → 板端领取 → applied 确认生效
    #     → 服务端写 device_control（真相源）→ /api/v1/control 反映暂停状态。
    #     用独立 device_id，避免影响上面的统计类断言。
    ctrl_dev = "esp32s3-eye-ctrl"

    def control_of(dev=ctrl_dev):
        r = client.get("/api/v1/control", params={"device_id": dev})
        assert r.status_code == 200, r.text
        return r.json()["control"]

    def device_of(dev=ctrl_dev):
        devs2 = {d["device_id"]: d
                 for d in client.get("/api/v1/devices").json()["devices"]}
        return devs2.get(dev, {})

    def claim(dev=ctrl_dev):
        """领取该设备最早的待执行任务（/tasks/next 按 created_at 正序）。"""
        return client.get("/api/v1/tasks/next", params={"device_id": dev}).json()

    def task_of(request_id):
        return client.get("/api/v1/tasks/%s" % request_id).json()["task"]

    # 缺省 kind = capture（老客户端不带 kind 时行为与旧版一致）
    seed_ctrl = client.post("/api/v1/tasks",
                            json={"device_id": ctrl_dev, "sample_count": 3})
    assert seed_ctrl.status_code == 201, seed_ctrl.text
    cap0 = seed_ctrl.json()["task"]
    assert cap0["kind"] == "capture" and cap0["kind_cn"] == "按需采集"
    assert cap0["is_control"] is False and cap0["duration_s"] is None
    assert claim()["task"]["request_id"] == cap0["request_id"]
    drain = make_payload(device=ctrl_dev, n=3)
    drain["request_id"] = cap0["request_id"]
    assert client.post("/api/v1/upload", json=drain).status_code == 201
    assert task_of(cap0["request_id"])["status"] == "completed"
    print("[PASS] kind 缺省为 capture（老客户端兼容），字段 kind_cn/is_control 齐全")

    # 21) 创建 pause：进入同一套任务状态机，但带 duration_s、trigger=control
    r = client.post("/api/v1/tasks",
                    json={"device_id": ctrl_dev, "kind": "pause", "duration_s": 30})
    assert r.status_code == 201, r.text
    pause_task = r.json()["task"]
    pause_rid = pause_task["request_id"]
    assert pause_task["status"] == "submitted" and pause_task["kind"] == "pause"
    assert pause_task["duration_s"] == 30 and pause_task["is_control"] is True
    assert pause_task["trigger"] == "control" and pause_task["expires_in_s"] > 0
    assert control_of()["periodic_paused"] is False, "任务未 applied 就不该显示暂停"
    print("[PASS] 创建暂停任务 request_id=%s（kind=pause, duration_s=30）" % pause_rid)

    # 22) 连点「暂停周期」只复用同一条未完成任务（duplicate）
    r2 = client.post("/api/v1/tasks",
                     json={"device_id": ctrl_dev, "kind": "pause", "duration_s": 300})
    assert r2.status_code == 200 and r2.json()["duplicate"] is True, r2.text
    assert r2.json()["task"]["request_id"] == pause_rid
    assert r2.json()["task"]["duration_s"] == 30, "重复请求不该改写已有任务的时长"
    print("[PASS] 连点暂停：duplicate=true 且 request_id/duration_s 不变")

    # 23) 去重键带 kind：一条待执行的 pause 不能把后续 capture 挡住
    r = client.post("/api/v1/tasks",
                    json={"device_id": ctrl_dev, "sample_count": 3})
    assert r.status_code == 201 and r.json()["duplicate"] is False, r.text
    cap_after_pause = r.json()["task"]["request_id"]
    assert cap_after_pause != pause_rid
    print("[PASS] pause 待执行时 capture 仍能新建（去重键 = device+source+kind）")

    # 24) 方向相反的控制任务互斥：新建 resume 会把 pending pause 置 failed
    r = client.post("/api/v1/tasks",
                    json={"device_id": ctrl_dev, "kind": "resume"})
    assert r.status_code == 201, r.text
    resume_task = r.json()["task"]
    resume_rid = resume_task["request_id"]
    assert r.json()["superseded"] == 1, r.text
    st_pause = task_of(pause_rid)
    assert st_pause["status"] == "failed"
    assert st_pause["error"] == "superseded by newer control task", st_pause["error"]
    print("[PASS] resume 取代 pending pause：%s → failed（superseded by newer "
          "control task）" % pause_rid[:8])

    # 25) 控制任务不影响采集：先按 created_at 顺序领取 capture 并带 request_id 回传
    got = claim()
    assert got["found"] is True and got["task"]["request_id"] == cap_after_pause, got
    assert got["task"]["kind"] == "capture"
    upl = make_payload(device=ctrl_dev, n=3)
    upl["request_id"] = cap_after_pause
    assert client.post("/api/v1/upload", json=upl).status_code == 201
    assert task_of(cap_after_pause)["status"] == "completed"
    print("[PASS] 控制任务在队列里不影响按需采集（capture 仍能下发并完成）")

    # 26) 板端领取 resume → POST /applied → 任务 completed，且控制状态为「未暂停」
    got = claim()
    assert got["task"]["request_id"] == resume_rid, got
    assert got["task"]["kind"] == "resume" and got["task"]["duration_s"] is None
    r = client.post("/api/v1/tasks/%s/ack" % resume_rid)
    assert r.status_code == 200 and r.json()["task"]["status"] == "acked"
    r = client.post("/api/v1/tasks/%s/applied" % resume_rid, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True and body["updated"] is True and body["kind"] == "resume"
    assert body["task"]["status"] == "completed"
    assert body["control"]["periodic_paused"] is False
    assert body["control"]["request_id"] == resume_rid
    print("[PASS] resume 经 /applied 收尾 → completed，无关联 upload_id=%s"
          % body["task"]["upload_id"])

    # 27) pause + applied：写 device_control（真相源），/devices 同步标注暂停
    r = client.post("/api/v1/tasks",
                    json={"device_id": ctrl_dev, "kind": "pause", "duration_s": 45})
    assert r.status_code == 201, r.text
    pause2_rid = r.json()["task"]["request_id"]
    got = claim()
    assert got["task"]["request_id"] == pause2_rid
    until_ms = int(time.time() * 1000) + 45000
    r = client.post("/api/v1/tasks/%s/applied" % pause2_rid,
                    json={"paused_until_ms": until_ms})
    assert r.status_code == 200, r.text
    ctrl = r.json()["control"]
    assert r.json()["task"]["status"] == "completed"
    assert ctrl["periodic_paused"] is True and ctrl["request_id"] == pause2_rid
    assert 40 <= ctrl["remaining_s"] <= 46, ctrl["remaining_s"]
    assert ctrl["paused_until_str"] and ctrl["paused_until"] > time.time()
    assert control_of()["periodic_paused"] is True
    dev_row = device_of()
    assert dev_row.get("periodic_paused") is True, dev_row
    print("[PASS] pause 经 /applied 生效：periodic_paused=True 剩余 %ss（板端报告终点被采用）"
          % ctrl["remaining_s"])

    # 28) applied 幂等重放（板端 POST 成功但响应丢失时会重试）：不把暂停终点往后推
    before_until = control_of()["paused_until"]
    r = client.post("/api/v1/tasks/%s/applied" % pause2_rid, json={})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] is False and r.json()["replay"] is True, r.text
    assert control_of()["paused_until"] == before_until, "重放不该延长暂停窗口"
    print("[PASS] /applied 重放幂等：updated=false, replay=true，暂停终点不变")

    # 29) 采集任务不能走 /applied 收尾（否则会出现「completed 但 0 样本」）
    r = client.post("/api/v1/tasks/%s/applied" % cap0["request_id"], json={})
    assert r.status_code == 409 and r.json()["code"] == "WRONG_KIND", r.text
    assert task_of(cap0["request_id"])["status"] == "completed", "误调用不该改动任务状态"
    print("[PASS] capture 调 /applied → 409 WRONG_KIND（且任务状态未被改动）")

    # 30) 控制任务不该收到带它 request_id 的上传：409 WRONG_KIND，不走 _check_task_match
    bogus2 = make_payload(device=ctrl_dev, n=3)
    bogus2["request_id"] = pause2_rid
    bogus2["trigger"] = "manual"
    r = client.post("/api/v1/upload", json=bogus2)
    assert r.status_code == 409 and r.json()["code"] == "WRONG_KIND", r.text
    assert "never carries samples" in r.json()["error"]
    assert task_of(pause2_rid)["status"] == "completed"
    print("[PASS] 控制任务收到 samples 上传 → 409 WRONG_KIND:",
          r.json()["error"][:60])

    # 31) 暂停到期自动恢复：paused_until 一过就被惰性归零（服务端无后台线程）
    conn = main._connect()
    try:
        conn.execute("UPDATE device_control SET paused_until=? WHERE device_id=?",
                     (time.time() - 1, ctrl_dev))
        conn.commit()
    finally:
        conn.close()
    expired = control_of()
    assert expired["periodic_paused"] is False, expired
    assert expired["paused_until"] is None and expired["remaining_s"] is None
    assert expired["request_id"] == pause2_rid, "归零后仍应保留最近控制任务号以便追溯"
    print("[PASS] 暂停到期自动恢复（惰性归零，保留 request_id 便于追溯）")

    # 32) 控制任务的参数校验：kind / duration_s 越界都被 400 拒绝
    for bad_body in ({"device_id": ctrl_dev, "kind": "explode"},
                     {"device_id": ctrl_dev, "kind": "pause", "duration_s": 0},
                     {"device_id": ctrl_dev, "kind": "pause", "duration_s": 601},
                     {"device_id": ctrl_dev, "kind": "pause", "duration_s": "abc"},
                     {"device_id": ctrl_dev, "kind": "pause", "duration_s": True}):
        r = client.post("/api/v1/tasks", json=bad_body)
        assert r.status_code == 400 and r.json()["code"] == "INVALID", (bad_body, r.text)
    # resume 不接受 duration_s（恒为 None），pause 缺省时长 = 服务端默认值
    r = client.post("/api/v1/tasks",
                    json={"device_id": ctrl_dev, "kind": "resume", "duration_s": 99})
    assert r.status_code == 201 and r.json()["task"]["duration_s"] is None, r.text
    r = client.post("/api/v1/tasks",
                    json={"device_id": ctrl_dev, "kind": "pause"})
    assert r.status_code == 201, r.text
    assert r.json()["task"]["duration_s"] == main.PAUSE_DEFAULT_S, r.text
    assert r.json()["superseded"] == 1, "新 pause 应把刚才的 resume 取代"
    default_pause_rid = r.json()["task"]["request_id"]
    print("[PASS] 控制任务参数校验：非法 kind/duration_s 400，缺省时长 %s s，resume 忽略"
          " duration_s" % main.PAUSE_DEFAULT_S)

    # 33) /api/v1/control 的入参校验与未知设备
    assert client.get("/api/v1/control").status_code == 400
    assert client.get("/api/v1/control", params={"device_id": ""}).status_code == 400
    r = client.get("/api/v1/control",
                   params={"device_id": "x" * (main.MAX_DEVICE_ID_LEN + 1)})
    assert r.status_code == 400, r.text
    unknown_ctrl = client.get("/api/v1/control",
                              params={"device_id": "esp32s3-eye-unknown"}).json()
    assert unknown_ctrl["control"]["periodic_paused"] is False
    assert unknown_ctrl["control"]["request_id"] is None
    assert client.get("/api/v1/control",
                      params={"device_id": ctrl_dev,
                              "source": "qma6100p"}).status_code == 200
    print("[PASS] /api/v1/control 缺 device_id/超长 400；未知设备返回未暂停")

    # 34) 控制任务也能被板端主动上报失败（板端执行不了时不会卡住队列）
    got = claim()
    assert got["task"]["request_id"] == default_pause_rid, got
    r = client.post("/api/v1/tasks/%s/fail" % default_pause_rid,
                    json={"error": "pause rejected by device"})
    assert r.status_code == 200 and r.json()["task"]["status"] == "failed"
    assert control_of()["periodic_paused"] is False
    print("[PASS] 控制任务失败上报 → failed，控制状态不被改动")

    # 35) 历史列表带上 kind / duration_s / is_control，供 Web 按类型渲染
    tj2 = client.get("/api/v1/tasks",
                     params={"device_id": ctrl_dev, "limit": 20}).json()
    kinds = {t["kind"] for t in tj2["tasks"]}
    assert kinds == {"capture", "pause", "resume"}, kinds
    assert all("kind_cn" in t and "duration_s" in t and "is_control" in t
               for t in tj2["tasks"]), "历史列表缺少控制任务字段"
    assert len(tj2["tasks"]) >= 7, tj2["count"]
    print("[PASS] 任务历史含控制任务：kinds=%s，共 %d 条"
          % (sorted(kinds), tj2["count"]))

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main_run()
