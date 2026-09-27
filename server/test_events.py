# -*- coding: utf-8 -*-
"""
闭环事件（板端按键触发 → 本地反馈 → 远端显示 → 回应/取消）自测。

用独立临时库（server/data/test_events.db），不污染正式库。
运行：python server/test_events.py   （需 fastapi / httpx）

覆盖：触发校验 / 板端重试幂等 / 状态机单向性 / 终态不可回退 / 惰性过期 /
      列表过滤与分页 / 设备侧鉴权 / health 汇总。
"""

import os
import sys
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
_TEST_DB = os.path.join(_HERE, "data", "test_events.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["SENSOR_DB"] = _TEST_DB
os.environ["SENSOR_HOST"] = "127.0.0.1"

sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

DEV = "esp32s3-eye-0001"
N = 0


def ok(msg):
    global N
    N += 1
    print("[PASS] %s" % msg)


def rid():
    return uuid.uuid4().hex


def trigger_body(request_id, device=DEV, **extra):
    body = {
        "device_id": device,
        "source": "qma6100p",
        "kind": "alert",
        "request_id": request_id,
        "ts_ms": int(time.time() * 1000),
    }
    body.update(extra)
    return body


def main_run():
    main.init_db()
    client = TestClient(main.app)

    # 1) 空库：列表为空
    r = client.get("/api/v1/events")
    assert r.status_code == 200, r.status_code
    assert r.json()["count"] == 0 and r.json()["events"] == []
    ok("空库时事件列表为空")

    # 2) 触发：缺 device_id / source / request_id 都拒绝
    r = client.post("/api/v1/events/trigger", json={"source": "qma6100p"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID", r.text
    ok("触发缺字段被拒绝: %s" % r.json()["error"])

    # 3) 触发：request_id 非法（太短 / 含非法字符）
    r = client.post("/api/v1/events/trigger",
                    json=trigger_body("ab"))
    assert r.status_code == 400, r.text
    r = client.post("/api/v1/events/trigger",
                    json=trigger_body("bad id with spaces"))
    assert r.status_code == 400, r.text
    ok("非法 request_id 被拒绝")

    # 4) 触发：正常 → 201 pending
    r1 = rid()
    r = client.post("/api/v1/events/trigger", json=trigger_body(r1))
    assert r.status_code == 201, r.text
    ev = r.json()["event"]
    assert ev["status"] == "pending" and ev["status_cn"] == "待处理", ev
    assert ev["request_id"] == r1 and ev["device_id"] == DEV
    assert ev["terminal"] is False and ev["needs_attention"] is True
    assert ev["ttl_left_s"] > 0
    ok("触发成功: status=pending, ttl=%.1fs" % ev["ttl_left_s"])

    # 5) 板端重试（同 request_id 再发）→ 200 幂等，不新增行
    r = client.post("/api/v1/events/trigger", json=trigger_body(r1))
    assert r.status_code == 200, r.text
    assert r.json()["idempotent"] is True, r.text
    assert r.json()["event"]["status"] == "pending"
    r = client.get("/api/v1/events", params={"device_id": DEV})
    assert r.json()["count"] == 1, "重试不该产生第二条事件"
    ok("板端重试幂等：同 request_id 不重复建事件")

    # 6) 板端轮询状态：指定 request_id
    r = client.get("/api/v1/events/status",
                   params={"device_id": DEV, "request_id": r1})
    assert r.status_code == 200 and r.json()["found"] is True, r.text
    assert r.json()["status"] == "pending"
    ok("板端轮询 status=pending（顶层 status 便于板端极简解析）")

    # 7) 板端轮询：未知 request_id → 200 + found=false（不是 404，避免刷错误日志）
    r = client.get("/api/v1/events/status",
                   params={"device_id": DEV, "request_id": rid()})
    assert r.status_code == 200 and r.json()["found"] is False, r.text
    ok("未知 request_id 轮询返回 200/found=false")

    # 8) 板端轮询：不带 request_id → 返回该设备最近一条
    r = client.get("/api/v1/events/status", params={"device_id": DEV})
    assert r.json()["found"] is True and r.json()["event"]["request_id"] == r1
    ok("不带 request_id 时回该设备最近一条事件")

    # 9) respond 缺字段 / 非法 action
    r = client.post("/api/v1/events/respond", json={"request_id": r1})
    assert r.status_code == 400, r.text
    r = client.post("/api/v1/events/respond",
                    json={"request_id": r1, "action": "explode"})
    assert r.status_code == 400, r.text
    ok("respond 缺 action / 非法 action 被拒绝")

    # 10) respond 未知 request_id → 404
    r = client.post("/api/v1/events/respond",
                    json={"request_id": rid(), "action": "accept"})
    assert r.status_code == 404, r.text
    ok("respond 未知 request_id 返回 404")

    # 11) 板端回应 accept → ack（带 by=device）
    r = client.post("/api/v1/events/respond",
                    json={"request_id": r1, "action": "accept", "by": "device"})
    assert r.status_code == 200, r.text
    ev = r.json()["event"]
    assert ev["status"] == "ack" and ev["status_cn"] == "已回应", ev
    assert ev["responded_by"] == "device" and ev["response"] == "accept"
    assert ev["responded_at"] is not None
    ok("板端回应 accept → status=ack（responded_by=device）")

    # 12) 重复 accept → 200 幂等
    r = client.post("/api/v1/events/respond",
                    json={"request_id": r1, "action": "accept", "by": "device"})
    assert r.status_code == 200 and r.json()["idempotent"] is True, r.text
    ok("重复 accept 幂等返回")

    # 13) 板端轮询能看到 ack（这就是「远端已回来」的判据）
    r = client.get("/api/v1/events/status",
                   params={"device_id": DEV, "request_id": r1})
    assert r.json()["status"] == "ack", r.text
    ok("板端轮询到 ack")

    # 14) Web 远端确认完成 → completed，closed_at 落库
    r = client.post("/api/v1/events/respond",
                    json={"request_id": r1, "action": "confirm", "by": "web"})
    assert r.status_code == 200, r.text
    ev = r.json()["event"]
    assert ev["status"] == "completed" and ev["terminal"] is True, ev
    assert ev["closed_at"] is not None
    ok("远端 confirm → status=completed 且 closed_at 落库")

    # 15) 终态不可回退：再 cancel → 409
    r = client.post("/api/v1/events/respond",
                    json={"request_id": r1, "action": "cancel"})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT", r.text
    ok("终态事件不可回退（409 CONFLICT）")

    # 16) 第二条事件：板端直接取消
    r2 = rid()
    client.post("/api/v1/events/trigger", json=trigger_body(r2))
    r = client.post("/api/v1/events/respond",
                    json={"request_id": r2, "action": "cancel", "by": "device"})
    assert r.status_code == 200 and r.json()["event"]["status"] == "cancelled", r.text
    ok("板端长按取消 → status=cancelled")

    # 17) 第三、四条：留在 pending / ack，用于过滤测试
    r3, r4 = rid(), rid()
    client.post("/api/v1/events/trigger", json=trigger_body(r3))
    client.post("/api/v1/events/trigger", json=trigger_body(r4))
    client.post("/api/v1/events/respond",
                json={"request_id": r4, "action": "accept", "by": "device"})

    # 18) active=1 只返回未终态（r3 pending + r4 ack）
    r = client.get("/api/v1/events", params={"device_id": DEV, "active": "1"})
    ids = [e["request_id"] for e in r.json()["events"]]
    assert set(ids) == {r3, r4}, ids
    assert r.json()["pending"] == 1, r.json()["pending"]
    ok("active=1 只返回未终态事件（pending=1, ack=1）")

    # 19) 列表新的在前
    r = client.get("/api/v1/events", params={"device_id": DEV})
    assert r.json()["count"] == 4, r.json()["count"]
    order = [e["request_id"] for e in r.json()["events"]]
    assert order[0] == r4, order
    ok("事件列表按创建时间倒序（共 4 条）")

    # 20) limit 边界（0 → 1，超上限 → 上限）
    r = client.get("/api/v1/events", params={"device_id": DEV, "limit": 0})
    assert len(r.json()["events"]) == 1, r.text
    r = client.get("/api/v1/events", params={"device_id": DEV, "limit": 99999})
    assert len(r.json()["events"]) == 4, r.text
    ok("limit 边界被夹紧（0→1，99999→上限）")

    # 21) 惰性过期：把 r3 的 expires_at 拨到过去，读列表时应判为 expired
    conn = main._connect()
    conn.execute("UPDATE events SET expires_at=? WHERE request_id=?",
                 (time.time() - 5, r3))
    conn.commit()
    conn.close()
    r = client.get("/api/v1/events", params={"device_id": DEV})
    byid = {e["request_id"]: e for e in r.json()["events"]}
    assert byid[r3]["status"] == "expired", byid[r3]
    assert byid[r3]["status_cn"] == "已过期"
    assert byid[r3]["terminal"] is True
    ok("超 TTL 的事件惰性判为 expired（无需后台线程）")

    # 22) 过期后也不允许被 accept 改写
    r = client.post("/api/v1/events/respond",
                    json={"request_id": r3, "action": "accept"})
    assert r.status_code == 409, r.text
    ok("过期事件不可被 accept 改写")

    # 23) 设备侧鉴权：设了 SENSOR_TOKEN 后无 token 的触发被拒
    main.SENSOR_TOKEN = "s3cret"
    try:
        r = client.post("/api/v1/events/trigger", json=trigger_body(rid()))
        assert r.status_code == 401, r.text
        r = client.get("/api/v1/events/status", params={"device_id": DEV})
        assert r.status_code == 401, r.text
        # 带正确 token 就通过
        r = client.post("/api/v1/events/trigger", json=trigger_body(rid()),
                        headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 201, r.text
        # Web 侧列表不校验 token
        r = client.get("/api/v1/events")
        assert r.status_code == 200, r.text
    finally:
        main.SENSOR_TOKEN = ""
    ok("设备侧接口鉴权生效，Web 侧列表不受影响")

    # 24) health 汇总里带上事件统计
    r = client.get("/api/v1/health")
    h = r.json()
    assert h["total_events"] == 5, h["total_events"]
    assert h["events_by_status"].get("completed") == 1
    assert h["latest_event"] is not None
    ok("health 汇总事件统计: %s" % h["events_by_status"])

    print("\n全部通过：%d 项断言" % N)


if __name__ == "__main__":
    main_run()
