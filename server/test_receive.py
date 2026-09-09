# -*- coding: utf-8 -*-
"""
本地联调自测：验证接收服务能正确"接收-校验-存储-查询"。

用独立的临时 SQLite（server/data/test_upload.db），不污染正式库。
运行：python server/test_receive.py   （需已安装 flask）
"""

import os
import sys
import time

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

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main_run()
