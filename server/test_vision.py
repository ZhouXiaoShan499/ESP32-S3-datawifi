# -*- coding: utf-8 -*-
"""
视觉推理与反馈闭环自测（第 8 周）。

覆盖三件事，缺一不可：
  1. **推理**：照片入库自动出结构化结果（label / score / decision / model_version），
     且 JPEG 解不开时只降级、不影响照片本身入库；
  2. **反馈**：人工纠正的校验与幂等（一张照片只留最新一条），高分误判率能被统计出来；
  3. **闭环**：低分/遮挡 → 重拍建 camera 任务；删照片后结果跟着从列表里消失。

用独立临时库与临时照片目录（server/data/test_vision.db + test_vision/），
不污染正式库与 server/photos/。运行：python server/test_vision.py
"""

import os
import shutil
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_DB = os.path.join(_HERE, "data", "test_vision.db")
_TEST_PHOTO_DIR = os.path.join(_HERE, "data", "test_vision")


def _remove_db_files(path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


_remove_db_files(_TEST_DB)
if os.path.isdir(_TEST_PHOTO_DIR):
    shutil.rmtree(_TEST_PHOTO_DIR)

os.environ["SENSOR_DB"] = _TEST_DB
os.environ["SENSOR_PHOTO_DIR"] = _TEST_PHOTO_DIR
os.environ["SENSOR_HOST"] = "127.0.0.1"
os.environ.pop("SENSOR_TOKEN", None)      # 鉴权链路由 test_receive.py 覆盖

sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from vision import engine as vision_engine  # noqa: E402
from vision.decision import decide, describe  # noqa: E402

main.init_db()                            # 与其余自测脚本一致：先建表再起 TestClient
client = TestClient(main.app)
DEV = "esp32s3-eye-vision"


def jpeg_from_image(img, quality=85):
    import io

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def solid_image(color, size=(640, 480)):
    from PIL import Image

    return Image.new("RGB", size, color)


def textured_image(size=(640, 480)):
    """一张有明确结构、亮度正常的"像照片"的图（渐变 + 方块）。"""
    from PIL import Image, ImageDraw
    import numpy as np

    w, h = size
    yy = np.linspace(40, 210, h, dtype=np.float32)[:, None]
    xx = np.linspace(0, 0.35, w, dtype=np.float32)[None, :]
    arr = np.zeros((h, w, 3), dtype=np.float32)
    arr[:, :, 0] = yy + xx * 90
    arr[:, :, 1] = yy * 0.85 + 30
    arr[:, :, 2] = 200 - yy * 0.5
    img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")
    draw = ImageDraw.Draw(img)
    for i in range(6):
        x0 = 30 + i * 95
        draw.rectangle([x0, 120 + (i % 3) * 70, x0 + 70, 190 + (i % 3) * 70],
                       fill=(30 + i * 30, 200 - i * 20, 120))
    return img


def blurred_image(size=(640, 480)):
    """糊到没有可用信息的一张：高斯模糊后叠一点噪声。"""
    from PIL import ImageFilter
    import numpy as np

    img = textured_image(size).filter(ImageFilter.GaussianBlur(radius=9))
    arr = np.asarray(img, dtype=np.float32) + np.random.normal(0, 6, (size[1], size[0], 3))
    from PIL import Image

    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")


def post_photo(device, body, request_id=None):
    query = {"device_id": device}
    if request_id:
        query["request_id"] = request_id
    return client.post("/api/v1/photos", params=query, content=body,
                       headers={"Content-Type": "image/jpeg"})


def get_result(photo_id):
    r = client.get("/api/v1/vision/results/%d" % photo_id)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def main_run():
    checks = 0

    # 1) 空库：结果列表为空，但 status 必须已经能答（页面靠它决定显示"未启用"还是阈值）
    r = client.get("/api/v1/vision/results")
    assert r.status_code == 200 and r.json()["total"] == 0, r.text
    r = client.get("/api/v1/vision/status")
    st = r.json()
    assert r.status_code == 200 and st["ready"] is True, r.text
    assert st["labels"] == ["clear", "blocked", "degraded"], st
    assert st["thresholds"]["ok"] == 0.75 and st["thresholds"]["low"] == 0.55, st
    assert st["model_version"], st
    print("[PASS] 空库：results.total=0；status ready=true，标签/阈值/版本齐全")
    checks += 1

    # 2) 分档函数（纯函数，先把口径钉死，后面所有断言都按它来）
    th = {"ok": 0.75, "low": 0.55}
    assert decide(0.90, th) == "ok"
    assert decide(0.75, th) == "ok"          # 闭区间下界
    assert decide(0.74, th) == "low"
    assert decide(0.55, th) == "low"         # 闭区间下界
    assert decide(0.54, th) == "rejected"
    assert decide(0.0, th) == "rejected"
    d = describe("rejected", None, 0.42)
    assert d["title"] == "未知" and "无法判断" in d["text"], d
    d = describe("ok", "blocked", 0.91)
    assert d["title"] == "镜头被遮挡" and "91%" in d["text"], d
    print("[PASS] 分档阈值：0.75/0.55 为闭区间下界；拒识不给标签、文案是「未知」")
    checks += 1

    # 3) 黑图入库 → 自动推理出 blocked/degraded，且带模型版本与耗时
    body = jpeg_from_image(solid_image((4, 4, 6)))
    r = post_photo(DEV, body)
    assert r.status_code == 201, r.text
    black_id = r.json()["photo_id"]
    res = get_result(black_id)
    assert res["label"] in ("blocked", "degraded"), res
    assert 0.0 <= res["score"] <= 1.0 and res["decision"] in ("ok", "low"), res
    assert res["model_version"] == st["model_version"], res
    assert res["latency_ms"] is not None and res["error"] is None, res
    assert abs(sum(res["probabilities"].values()) - 1.0) < 0.02, res["probabilities"]
    assert res["feedback"] and res["feedback"]["text"], res
    print("[PASS] 纯黑图入库即推理：label=%s score=%.3f decision=%s 版本=%s" %
          (res["label"], res["score"], res["decision"], res["model_version"]))
    checks += 1

    # 4) 正常纹理图 → 结构合法（不断言具体类别：那是模型的判断，不是接口的契约）
    r = post_photo(DEV, jpeg_from_image(textured_image()))
    assert r.status_code == 201, r.text
    ok_id = r.json()["photo_id"]
    res = get_result(ok_id)
    assert res["decision"] in ("ok", "low", "rejected"), res
    assert res["label"] is None or res["label"] in st["labels"], res
    assert set(res["probabilities"]) == set(st["labels"]), res
    if res["decision"] == "rejected":
        assert res["label"] is None and res["scene_label"] is None, "拒识时不能给标签"
    else:
        assert res["label"] is not None, res
    print("[PASS] 纹理图：decision=%s label=%s（结构合法，拒识时 label 为空）" %
          (res["decision"], res["label"]))
    checks += 1

    # 5) 伪 JPEG（只有 SOI/EOI）→ 照片照样入库，只把推理失败记进 error
    bad_body = b"\xff\xd8\xff\xe0" + b"\x11" * 64 + b"\xff\xd9"
    r = post_photo(DEV, bad_body)
    assert r.status_code == 201, r.text
    bad_id = r.json()["photo_id"]
    res = get_result(bad_id)
    assert res["decision"] == "rejected" and res["label"] is None, res
    assert res["error"] and "Error" in res["error"], res
    print("[PASS] 解不开的 JPEG：照片 201 入库，推理失败只写在 error 里（不 500）")
    checks += 1

    # 6) 幂等：同一 request_id 二次上传不重复推理（结果行还是那一条，时间戳不变）
    #    带 request_id 的照片会先校验归属（未知任务号 404），所以要先建一条 camera 任务。
    task = client.post("/api/v1/tasks", json={"device_id": DEV, "kind": "camera"}).json()["task"]
    rid = task["request_id"]
    body = jpeg_from_image(textured_image())
    r1 = post_photo(DEV, body, request_id=rid)
    assert r1.status_code == 201, r1.text
    pid = r1.json()["photo_id"]
    first = get_result(pid)
    time.sleep(0.05)
    r2 = post_photo(DEV, body, request_id=rid)
    assert r2.status_code == 200 and r2.json().get("idempotent") is True, r2.text
    again = get_result(pid)
    assert again["created_at"] == first["created_at"], "幂等命中不应重跑推理"
    print("[PASS] 幂等：同 request_id 重传不重复推理（结果时间戳不变）")
    checks += 1

    # 7) 列表过滤 + 参数校验
    r = client.get("/api/v1/vision/results", params={"device_id": DEV, "limit": 50})
    data = r.json()
    assert r.status_code == 200 and data["total"] >= 4, data
    assert data["policy"]["available"] is True, data["policy"]
    r = client.get("/api/v1/vision/results", params={"decision": "rejected"})
    assert r.status_code == 200, r.text
    assert all(x["decision"] == "rejected" for x in r.json()["results"]), r.text
    assert any(x["photo_id"] == bad_id for x in r.json()["results"]), "拒识样本应能筛出来"
    assert client.get("/api/v1/vision/results",
                      params={"decision": "bogus"}).status_code == 400
    for bad in ("0", "201", "abc"):
        assert client.get("/api/v1/vision/results",
                          params={"limit": bad}).status_code == 400, bad
    print("[PASS] 列表：device/decision 过滤有效，非法 decision 与 limit 400")
    checks += 1

    # 8) 单条详情：不存在的 photo 404；删照片后结果从列表消失
    assert client.get("/api/v1/vision/results/999999").status_code == 404
    r = client.delete("/api/v1/photos/%d" % bad_id)
    assert r.status_code == 200, r.text
    ids = [x["photo_id"] for x in
           client.get("/api/v1/vision/results", params={"limit": 50}).json()["results"]]
    assert bad_id not in ids, "照片删了，结果不该还挂在列表里（JOIN photos 过滤）"
    print("[PASS] 详情 404；删除照片后结果随之从列表消失")
    checks += 1

    # 9) 人工反馈的参数校验
    url = "/api/v1/vision/results/%d/feedback" % ok_id
    assert client.post(url, json={"verdict": "bogus"}).status_code == 400
    r = client.post(url, json={"verdict": "wrong"})
    assert r.status_code == 400 and "corrected_label" in r.json()["error"], r.text
    r = client.post(url, json={"verdict": "wrong", "corrected_label": "nonsense"})
    assert r.status_code == 400, r.text
    r = client.post("/api/v1/vision/results/999999/feedback", json={"verdict": "correct"})
    assert r.status_code == 404, r.text
    print("[PASS] 反馈校验：非法 verdict / wrong 缺答案 / 非法标签 / 未知照片全部挡下")
    checks += 1

    # 10) 反馈幂等：同一张照片提交两次只留最新一条
    r = client.post(url, json={"verdict": "wrong", "corrected_label": "blocked",
                               "note": "贴桌面了"})
    assert r.status_code == 200, r.text
    first_fb = r.json()["result"]["human"]
    assert first_fb["verdict"] == "wrong" and first_fb["corrected_label"] == "blocked", first_fb
    time.sleep(0.02)
    r = client.post(url, json={"verdict": "unknown"})
    assert r.status_code == 200, r.text
    second = r.json()["result"]["human"]
    assert second["verdict"] == "unknown" and second["corrected_label"] is None, second
    assert second["updated_at"] > first_fb["updated_at"], "应覆盖成最新一条"
    print("[PASS] 反馈幂等：同一照片只保留最新一条（verdict/答案随之更新）")
    checks += 1

    # 11) 汇总口径：by_decision 合计 = total；review 计数与人工判断一致
    r = client.get("/api/v1/vision/summary", params={"device_id": DEV})
    s = r.json()
    assert r.status_code == 200, r.text
    assert sum(s["by_decision"].values()) == s["total"], s
    assert s["review"]["reviewed"] == s["review"]["correct"] + s["review"]["wrong"] + \
        s["review"]["unknown"], s["review"]
    assert s["review"]["unknown"] >= 1, s["review"]
    assert "wrong_rate" in s["high_confidence"], s
    print("[PASS] 汇总：by_decision 合计=%d 与 total 一致；复核计数可加；含高分误判率" %
          s["total"])
    checks += 1

    # 12) 高分误判率：把一张 ok 档结果人工判错，wrong_rate 必须动
    before = s["high_confidence"]
    ok_rows = [x for x in client.get("/api/v1/vision/results",
                                     params={"decision": "ok", "limit": 50}).json()["results"]]
    assert ok_rows, "至少要有一条 ok 档结果才能验证高分误判率"
    target = ok_rows[0]["photo_id"]
    r = client.post("/api/v1/vision/results/%d/feedback" % target,
                    json={"verdict": "wrong", "corrected_label": "degraded",
                          "note": "测试：高分误判"})
    assert r.status_code == 200, r.text
    after = client.get("/api/v1/vision/summary", params={"device_id": DEV}).json()[
        "high_confidence"]
    if before["total"] == 0:
        assert after["total"] == 1 and after["wrong"] == 1, after
    else:
        assert after["wrong"] >= before["wrong"], (before, after)
    assert after["wrong_rate"] is not None and 0 < after["wrong_rate"] <= 1, after
    print("[PASS] 高分误判率：ok 档样本被判错后 wrong=%d/%d rate=%.3f" %
          (after["wrong"], after["total"], after["wrong_rate"]))
    checks += 1

    # 13) 重拍：建一条 kind=camera 任务，设备对齐；非法/未知 photo 挡下
    r = client.post("/api/v1/vision/recapture", json={"photo_id": black_id})
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    assert task["kind"] == "camera" and task["device_id"] == DEV, task
    assert task["status"] == "submitted", task
    nxt = client.get("/api/v1/tasks/next", params={"device_id": DEV})
    assert nxt.status_code == 200 and nxt.json().get("task"), "重拍任务应能被板端领到"
    assert client.post("/api/v1/vision/recapture",
                       json={"photo_id": "abc"}).status_code == 400
    assert client.post("/api/v1/vision/recapture",
                       json={"photo_id": 999999}).status_code == 404
    print("[PASS] 重拍：201 → kind=camera 的 submitted 任务，板端 /tasks/next 能领到")
    checks += 1

    # 14) 模型缺失时的降级：照片照样入库，结果只记 error
    real_dir = vision_engine.MODEL_DIR
    empty_dir = os.path.join(_HERE, "data", "test_vision_model_empty")
    os.makedirs(empty_dir, exist_ok=True)
    vision_engine.MODEL_DIR = empty_dir
    vision_engine.reset_for_tests()
    try:
        r = client.get("/api/v1/vision/status")
        assert r.json()["ready"] is False, r.text
        r = post_photo(DEV, jpeg_from_image(textured_image()))
        assert r.status_code == 201, "模型缺失时照片必须照样入库"
        res = get_result(r.json()["photo_id"])
        assert res["error"] and res["decision"] == "rejected", res
        print("[PASS] 模型缺失：/status ready=false，照片仍 201，结果只记 error")
        checks += 1
    finally:
        vision_engine.MODEL_DIR = real_dir
        vision_engine.reset_for_tests()

    # 15) VISION_ENABLED=0 时 status 明确"未启用"，但接口不 500
    vision_engine.VISION_ENABLED = False
    try:
        r = client.get("/api/v1/vision/status")
        assert r.status_code == 200 and r.json()["enabled"] is False, r.text
        assert client.get("/api/v1/vision/results").status_code == 200
        print("[PASS] 关闭开关：status enabled=false，结果接口仍 200（页面显示未启用）")
        checks += 1
    finally:
        vision_engine.VISION_ENABLED = True

    print("\n全部通过：%d 项断言" % checks)


if __name__ == "__main__":
    try:
        main_run()
    finally:
        _remove_db_files(_TEST_DB)
        try:
            shutil.rmtree(_TEST_PHOTO_DIR)
        except OSError:
            pass
        try:
            shutil.rmtree(os.path.join(_HERE, "data", "test_vision_model_empty"))
        except OSError:
            pass
        print("[PASS] 临时库/临时照片目录已清理")
