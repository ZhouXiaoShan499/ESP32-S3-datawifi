# -*- coding: utf-8 -*-
"""
本地联调自测：验证接收服务能正确"接收-校验-存储-查询"。

用独立的临时 SQLite（server/data/test_upload.db），不污染正式库。
运行：python server/test_receive.py   （需已安装 flask）
"""

import os
import sys
import threading
import time
import uuid

# 中文输出固定 UTF-8：中文 Windows 下控制台/重定向默认 cp936，日志会变乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 先指到临时库，再导入 main（main 在 import 时读取 SENSOR_DB）
_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_DB = os.path.join(_HERE, "data", "test_upload.db")


def _remove_db_files(path):
    """删库时连 WAL 副产品（-wal/-shm/-journal）一起删。

    服务端现在跑 WAL 模式，只删 .db 会留下孤立的 -wal/-shm：它们不影响 sqlite 打开
    新库（WAL 头对不上会被丢弃，实测连跑两次仍全绿），但目录里会多出几个「看起来像
    数据」的文件，排查时容易误判。
    """
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


_remove_db_files(_TEST_DB)
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

    # 36) 惰性超时是**写操作**，必须落库：只调 /api/v1/health（不碰任何会做惰性
    #     超时的任务接口），再用**另一个连接**直读 tasks.status。
    #     漏 conn.commit() 时 sqlite 会在 conn.close() 时回滚这次 UPDATE ——
    #     health 的 tasks_by_status 已经算作 timeout（同连接可见），库里却仍是
    #     submitted，且这个连接下次还会把它当待办（见 README 注意事项 14）。
    def task_status_from_new_conn(request_id):
        c = main._connect()
        try:
            return c.execute("SELECT status FROM tasks WHERE request_id=?",
                             (request_id,)).fetchone()["status"]
        finally:
            c.close()

    stale_rid = client.post(
        "/api/v1/tasks", json={"device_id": task_dev, "timeout_s": 5}
    ).json()["task"]["request_id"]
    conn = main._connect()
    try:
        conn.execute("UPDATE tasks SET expires_at=? WHERE request_id=?",
                     (time.time() - 1, stale_rid))
        conn.commit()
    finally:
        conn.close()

    h = client.get("/api/v1/health").json()
    assert h["tasks_by_status"].get("timeout", 0) >= 2, h["tasks_by_status"]
    assert task_status_from_new_conn(stale_rid) == "timeout", \
        "health 的惰性超时没有落库（漏 conn.commit()）"
    # /devices 的 pending_tasks 是同一口径：先判超时再统计，否则会把已过期的任务
    # 继续算成待办（对应 README「注意事项 14」里 events 那类口径矛盾）
    devs2 = {d["device_id"]: d
             for d in client.get("/api/v1/devices").json()["devices"]}
    assert devs2[task_dev]["pending_tasks"] == 0, devs2[task_dev]
    print("[PASS] health/devices 的惰性超时已落库（另开连接读到 timeout，"
          "pending_tasks=%s）" % devs2[task_dev]["pending_tasks"])

    # 37) 加固回归：device_id 字符白名单。device_id 是首页 HTML（GET /）与照片
    #     目录名的输入源，过去任意字符都能入库 —— 存储型 XSS / 路径穿越的共同入口。
    for bad_dev in ("<script>alert(1)</script>", "esp32/../../etc", "bad dev",
                    "a\"b", "a'b", "设备一"):
        bad = make_payload(device=bad_dev, n=2)
        r = client.post("/api/v1/upload", json=bad)
        assert r.status_code == 400 and r.json()["code"] == "INVALID", (bad_dev, r.text)
        assert "device_id" in r.json()["error"], r.text
    ok_dev = "esp32s3-eye_0001.2"
    r = client.post("/api/v1/upload", json=make_payload(device=ok_dev, n=2))
    assert r.status_code == 201, r.text
    print("[PASS] device_id 白名单：脚本标签/路径穿越/空格/引号/中文 全部 400，"
          "合法 id（含 . _ -）201")

    # 38) samples[i].i 必须是整数。过去直接透传，非数值会写进 samples.seq，而
    #     /api/v1/window 与首页的 ORDER BY seq 依赖它排序（SQLite 的类型亲和性
    #     会把它们当字符串比较，排出来的顺序就错了）。
    for bad_seq in ("1", 1.5, None, [1], {"a": 1}):
        bad = make_payload(n=3)
        bad["samples"][0]["i"] = bad_seq
        r = client.post("/api/v1/upload", json=bad)
        assert r.status_code == 400, (bad_seq, r.text)
        assert "samples[0].i must be an integer" in r.json()["error"], r.text
    ok_float = make_payload(n=3)
    ok_float["samples"][0]["i"] = 1.0      # 整数值 float 与 ts_ms 同口径，仍接受
    assert client.post("/api/v1/upload", json=ok_float).status_code == 201
    print("[PASS] samples[i].i 非整数（str/小数 float/None/list/object）→ 400；"
          "整数值 float → 201")

    # 39) store_upload 并发竞态：两个线程同时写同一 request_id，必须一个写库、
    #     另一个幂等返回同一个 upload_id，绝不能抛 IntegrityError —— 旧代码的
    #     check-then-insert 在并发下会 500，板端重试反而雪上加霜。
    conc_raw = make_payload(device="esp32s3-eye-conc", n=5)
    conc_raw["request_id"] = uuid.uuid4().hex
    conc_raw["trigger"] = "manual"
    ok_conc, conc_data = main.validate_payload(conc_raw)
    assert ok_conc, conc_data
    conc_results, conc_errors = [], []

    def _store_once():
        try:
            conc_results.append(main.store_upload(conc_data, ip="127.0.0.1"))
        except Exception as exc:            # noqa: BLE001 - 测试就是要抓住任何异常
            conc_errors.append(repr(exc))

    conc_threads = [threading.Thread(target=_store_once) for _ in range(2)]
    for t in conc_threads:
        t.start()
    for t in conc_threads:
        t.join()
    assert not conc_errors, conc_errors
    assert len(conc_results) == 2, conc_results
    assert sorted(flag for _, flag in conc_results) == [False, True], conc_results
    assert conc_results[0][0] == conc_results[1][0], "并发重试应回同一个 upload_id"
    conn = main._connect()
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM uploads WHERE request_id=?",
                            (conc_raw["request_id"],)).fetchone()["n"]
    finally:
        conn.close()
    assert rows == 1, "同一 request_id 只允许 1 条 uploads 行，实际 %s" % rows
    print("[PASS] store_upload 并发同 request_id：一个写库一个幂等，uploads 仍 1 行")

    # 40) 板端 SNTP 未同步时，ts_ms 只是「开机毫秒数」而不是真实 epoch（固件
    #     main.c:5820 的会话锚点取自 gettimeofday）。过去它会被 _check_task_match
    #     的新鲜度判据判成「上传了陈旧数据」→ 400 + 任务 failed，而板端收到 4xx
    #     就不再重试 ⇒ 「网段里没有可用 NTP」的现场按需采集彻底不可用。
    #     现在只有「看起来像真实时间」的 ts_ms 才参与该判据（EPOCH_SANE_MS）。
    unsync_dev = "esp32s3-eye-unsync"
    r = client.post("/api/v1/tasks", json={"device_id": unsync_dev, "sample_count": 3})
    assert r.status_code == 201, r.text
    unsync_rid = r.json()["task"]["request_id"]
    assert client.get("/api/v1/tasks/next",
                      params={"device_id": unsync_dev}).json()["found"] is True
    unsynced = make_payload(device=unsync_dev, ts=123456, n=3)   # 开机 123 s
    unsynced["request_id"] = unsync_rid
    r = client.post("/api/v1/upload", json=unsynced)
    assert r.status_code == 201, r.text
    assert r.json()["clock_synced"] is False, r.text
    assert r.json()["idempotent"] is False, r.text
    t = client.get("/api/v1/tasks/" + unsync_rid).json()["task"]
    assert t["status"] == "completed", t
    assert t["upload_id"] == r.json()["upload_id"], t
    print("[PASS] 板端未对时（ts_ms=123456）不再被误判为陈旧：201 + 任务 completed、"
          "clock_synced=False")

    # 41) 防回放判据没有因此降级：ts_ms 看起来是真实时间、但早于任务创建时间 60 s
    #     → 仍然 400 且任务置 failed（板端不重试）。这一条是 40) 的安全网。
    stale_dev = "esp32s3-eye-stale"
    r = client.post("/api/v1/tasks", json={"device_id": stale_dev, "sample_count": 3})
    assert r.status_code == 201, r.text
    stale_rid = r.json()["task"]["request_id"]
    assert client.get("/api/v1/tasks/next",
                      params={"device_id": stale_dev}).json()["found"] is True
    created_at = client.get("/api/v1/tasks/" + stale_rid).json()["task"]["created_at"]
    stale = make_payload(device=stale_dev, ts=int(created_at * 1000) - 60000, n=3)
    stale["request_id"] = stale_rid
    r = client.post("/api/v1/upload", json=stale)
    assert r.status_code == 400, r.text
    assert "ts_ms is older than the task creation time" in r.json()["error"], r.text
    assert client.get("/api/v1/tasks/" + stale_rid).json()["task"]["status"] == "failed"
    print("[PASS] 真实时钟但陈旧（早于任务创建 60 s）仍 400 且任务 failed"
          "（防回放未降级）")

    # 42) clock_synced 口径：已对时（ts_ms 是真实 epoch）的上传为 True，
    #     与 40) 的 False 可区分，Web 页面据此提示「板端未对时」。
    r = client.post("/api/v1/upload", json=make_payload(device="esp32s3-eye-clock", n=2))
    assert r.status_code == 201, r.text
    assert r.json()["clock_synced"] is True, r.text
    print("[PASS] clock_synced=True（ts_ms 为真实 epoch），与未对时的 False 可区分")

    # 43) 通用 JSON 接口的 body 上限。这几个接口过去直接 await request.json()，
    #     **完全没有上限** —— 几百 MB 的 body 会先被读进内存才轮到字段校验，
    #     等价于一个免鉴权的内存放大点。现在超过 MAX_JSON_BODY_BYTES 直接 413。
    big = (b'{"device_id":"esp32s3-eye-big","pad":"'
           + b"x" * main.MAX_JSON_BODY_BYTES + b'"}')
    r = client.post("/api/v1/tasks", content=big,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "TOO_LARGE", r.text
    # 上限之内的正常请求不受影响（同一个 device 稍后还要被 44) 用到）
    r = client.post("/api/v1/tasks",
                    json={"device_id": "esp32s3-eye-big", "sample_count": 3})
    assert r.status_code == 201, r.text
    print("[PASS] JSON 接口 body 上限：>%d B 的 /api/v1/tasks 请求 413 TOO_LARGE，"
          "上限内正常 201" % main.MAX_JSON_BODY_BYTES)

    # 44) 存储层跑 WAL：默认的 rollback-journal 模式下写事务提交期是 EXCLUSIVE 锁，
    #     会挡住读方（Web 每 800 ms 轮询 / window 画波形），实测表现为间歇 500。
    #     这里直接读 PRAGMA，确认「每个连接都设上了」而不只是写在注释里。
    conn = main._connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert mode == "wal", mode
    assert busy == main.DB_BUSY_TIMEOUT_MS, busy
    print("[PASS] sqlite 跑 WAL（journal_mode=%s）且 busy_timeout=%d ms 生效"
          % (mode, busy))

    # 45) 老库升级路径：uploads 里若已存在重复 request_id（建唯一索引之前的历史数据），
    #     迁移必须「告警跳过」而不是抛 IntegrityError —— 后者会让 init_db() 直接失败、
    #     服务起不来，而这条路径恰恰只在老库上才走到。photos 侧早有这套防御，
    #     这里补的是对称性（两边都由同一类历史 bug 造成）。
    dup_sql = ("INSERT INTO uploads (device_id, source, unit, ts_ms, received_at,"
               " sample_count, ip, payload, request_id, trigger)"
               " VALUES ('esp32s3-eye-dup','qma6100p','m/s^2',1,1,0,'127.0.0.1','',"
               " 'dup-rid-0001','manual')")
    conn = main._connect()
    try:
        conn.execute("DROP INDEX IF EXISTS idx_uploads_request")
        conn.execute(dup_sql)
        conn.execute(dup_sql)
        conn.commit()
        main._migrate_uploads_columns(conn)      # 不抛异常 = 服务起得来
        conn.commit()
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_uploads_request'").fetchone()
    finally:
        conn.close()
    assert idx is None, "有重复行时不该建唯一索引（会整段失败）"

    conn = main._connect()
    try:
        conn.execute("DELETE FROM uploads WHERE request_id='dup-rid-0001'")
        conn.commit()
        main._migrate_uploads_columns(conn)      # 去重后重跑：索引应被补上
        conn.commit()
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_uploads_request'").fetchone()
    finally:
        conn.close()
    assert idx is not None, "去重后应能建出唯一索引"
    print("[PASS] uploads 老库迁移：重复 request_id 只告警不炸，去重后自动补上唯一索引")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main_run()
