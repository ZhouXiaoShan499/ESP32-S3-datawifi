# -*- coding: utf-8 -*-
"""
实时拍照链路自测：验证「建 camera 任务 → 板端上传 JPEG → 落盘入库 → 列表/取图/删除」。

用独立的临时 SQLite 与临时照片根目录（server/data/test_photos.db +
server/data/test_photos/），不污染正式库与 server/photos/。
运行：python server/test_photos.py
"""

import os
import shutil
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

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_DB = os.path.join(_HERE, "data", "test_photos.db")
_TEST_PHOTO_DIR = os.path.join(_HERE, "data", "test_photos")


def _remove_db_files(path):
    """删库时连 WAL 副产品（-wal/-shm/-journal）一起删（理由见 test_receive.py 同名函数）。"""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


_remove_db_files(_TEST_DB)
if os.path.isdir(_TEST_PHOTO_DIR):
    shutil.rmtree(_TEST_PHOTO_DIR)

# 先指到临时库/临时照片目录，再导入 main（main 在 import 时读取这两个变量）
os.environ["SENSOR_DB"] = _TEST_DB
os.environ["SENSOR_PHOTO_DIR"] = _TEST_PHOTO_DIR
os.environ["SENSOR_HOST"] = "127.0.0.1"
os.environ.pop("SENSOR_TOKEN", None)     # 鉴权链路由 test_receive.py 覆盖

sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

# 每类检查各用一个设备号：/api/v1/tasks/next 按 device 过滤，互不干扰
DIRECT_DEV = "esp32s3-eye-photo"          # 不经任务、直接上传的照片
CAM_DEV = "esp32s3-eye-cam"               # camera 任务正常链路
CAP_DEV = "esp32s3-eye-cap"               # capture 任务不能被照片收尾
MM_DEV = "esp32s3-eye-mm"                 # 设备号不匹配

# 由 main_run() 初始化；下面的辅助函数都要用它，所以放模块级
client = None


def jpeg_bytes(n=64, marker=0x11):
    """最小合法 JPEG：SOI + APP0 头 + n 字节负载 + EOI（服务端只校验 SOI）。"""
    return b"\xff\xd8\xff\xe0" + bytes([marker]) * n + b"\xff\xd9"


def post_photo(device, body=None, request_id=None, **params):
    query = {"device_id": device}
    if request_id:
        query["request_id"] = request_id
    query.update(params)
    payload = jpeg_bytes() if body is None else body
    return client.post("/api/v1/photos", params=query, content=payload,
                       headers={"Content-Type": "image/jpeg"})


def create_task(device, kind="camera"):
    body = {"device_id": device, "kind": kind}
    if kind == "capture":
        body.update({"sample_count": 10, "sample_rate_hz": 100})
    return client.post("/api/v1/tasks", json=body)


def claim_task(device):
    """板端视角领取任务：GET /api/v1/tasks/next?device_id=…"""
    return client.get("/api/v1/tasks/next", params={"device_id": device})


def photo_abs_path(device, photo_id):
    return os.path.join(main.PHOTO_DIR, main._photo_rel_path(device, photo_id))


def main_run():
    global client
    main.init_db()
    client = TestClient(main.app)

    # 1) 空库：health 里照片计数为 0，列表为空
    h = client.get("/api/v1/health").json()
    assert h["total_photos"] == 0 and h["latest_photo"] is None, h
    r = client.get("/api/v1/photos")
    assert r.status_code == 200 and r.json()["count"] == 0, r.text
    print("[PASS] 空库：total_photos=0，GET /api/v1/photos 返回空列表")

    # 2) 参数校验：缺 device_id / 非 JPEG / 空 body / 非法 ts_ms / 非法宽高
    r = client.post("/api/v1/photos", content=jpeg_bytes())
    assert r.status_code == 400 and r.json()["code"] == "INVALID", r.text
    r = post_photo(DIRECT_DEV, body=b"not-a-jpeg-at-all")
    assert r.status_code == 400 and "JPEG" in r.json()["error"], r.text
    r = post_photo(DIRECT_DEV, body=b"")
    assert r.status_code == 400, r.text
    r = post_photo(DIRECT_DEV, ts_ms="abc")
    assert r.status_code == 400 and "ts_ms" in r.json()["error"], r.text
    r = post_photo(DIRECT_DEV, w=0)
    assert r.status_code == 400 and "width" in r.json()["error"], r.text
    r = post_photo(DIRECT_DEV, h=99999)
    assert r.status_code == 400 and "height" in r.json()["error"], r.text
    print("[PASS] 参数校验：缺 device_id/非 JPEG/空 body/非法 ts_ms/非法宽高 全部 400")

    # 3) 超过 MAX_PHOTO_BYTES 直接 413（在读 body 之后就拒绝，不进校验分支）
    big = b"\xff\xd8\xff\xe0" + b"\x22" * main.MAX_PHOTO_BYTES
    r = post_photo(DIRECT_DEV, body=big)
    assert r.status_code == 413 and r.json()["code"] == "TOO_LARGE", r.text
    print("[PASS] 超大 JPEG 413 TOO_LARGE（上限 %s B）" % main.MAX_PHOTO_BYTES)

    # 4) 直接上传（不带 request_id）：201 + 文件落盘 + 元数据可读
    body = jpeg_bytes(128, marker=0x33)
    r = post_photo(DIRECT_DEV, body=body, w=640, h=480, ts_ms=1767123456789,
                   note="self-test")
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["ok"] is True and j["idempotent"] is False and j["bytes"] == len(body)
    pid = j["photo_id"]
    photo = j["photo"]
    assert photo["device_id"] == DIRECT_DEV and photo["bytes"] == len(body)
    assert photo["width"] == 640 and photo["height"] == 480
    assert photo["note"] == "self-test" and photo["size_kb"] > 0
    assert photo["url"] == "/api/v1/photos/%s" % pid
    path = photo_abs_path(DIRECT_DEV, pid)
    assert os.path.isfile(path), path
    with open(path, "rb") as fh:
        assert fh.read() == body, "落盘内容与上传字节不一致"
    print("[PASS] 直接上传 → photo_id=%s，%s B 落盘 %s"
          % (pid, photo["bytes"], os.path.relpath(path, _HERE)))

    # 5) 取图接口：字节原样回吐，Content-Type=image/jpeg
    r = client.get(photo["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", r.headers
    assert r.content == body, "取回的 JPEG 与上传的不一致"
    print("[PASS] GET %s 原样返回 %s B image/jpeg" % (photo["url"], len(r.content)))

    # 6) 列表：按设备过滤、limit 生效、新的在前、total 一致
    r = post_photo(DIRECT_DEV, body=jpeg_bytes(16, marker=0x44))
    assert r.status_code == 201, r.text
    second_id = r.json()["photo_id"]
    lst = client.get("/api/v1/photos", params={"device_id": DIRECT_DEV}).json()
    assert lst["count"] == 2 and lst["total"] == 2, lst
    assert [p["id"] for p in lst["photos"]] == [second_id, pid], "应按 id 倒序"
    r = client.get("/api/v1/photos", params={"device_id": DIRECT_DEV, "limit": 1})
    assert r.status_code == 200 and r.json()["count"] == 1, r.text
    assert r.json()["total"] == 2, "total 应是不受 limit 影响的设备内总数"
    for bad in ("0", "201", "abc"):
        assert client.get("/api/v1/photos", params={"limit": bad}).status_code == 400, bad
    print("[PASS] 列表：device 过滤/limit 1..200/倒序/total=%s 全部正确" % lst["total"])

    # 6b) 只有照片、没有任何样本上传的设备也必须出现在设备下拉里（/devices 是
    #     uploads ∪ photos 的并集），否则画廊按设备过滤就看不到自己的照片
    devs = {d["device_id"]: d for d in client.get("/api/v1/devices").json()["devices"]}
    assert DIRECT_DEV in devs, devs.keys()
    assert devs[DIRECT_DEV]["uploads"] == 0 and devs[DIRECT_DEV]["photos"] == 2, devs[DIRECT_DEV]
    print("[PASS] /devices 含「只有照片」的设备：%s uploads=0 photos=2"
          % DIRECT_DEV)

    # 7) 非法或未知 id：404（GET 与 DELETE 口径一致）
    for bad_id in ("abc", "-1", "0x1f"):
        assert client.get("/api/v1/photos/%s" % bad_id).status_code == 404, bad_id
        assert client.delete("/api/v1/photos/%s" % bad_id).status_code == 404, bad_id
    r = client.get("/api/v1/photos/999999")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND", r.text
    print("[PASS] 非法/未知 photo_id：GET 与 DELETE 都是 404 NOT_FOUND")

    # 8) camera 任务正常链路：建任务 → 板端领取 → 上传 JPEG → 任务 completed
    r = create_task(CAM_DEV, kind="camera")
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    assert task["kind"] == "camera" and task["kind_cn"] == "实时拍照", task
    assert task["is_control"] is False and task["status"] == "submitted", task
    cam_rid = task["request_id"]

    got = claim_task(CAM_DEV).json()
    assert got["found"] is True and got["task"]["request_id"] == cam_rid, got
    assert got["task"]["status"] == "dispatched", got["task"]["status"]

    cam_body = jpeg_bytes(256, marker=0x55)
    r = post_photo(CAM_DEV, body=cam_body, request_id=cam_rid, w=640, h=480,
                   ts_ms=int(time.time() * 1000))
    assert r.status_code == 201, r.text
    cam_pid = r.json()["photo_id"]
    assert r.json()["request_id"] == cam_rid, r.text

    r = client.get("/api/v1/tasks/%s" % cam_rid)
    detail = r.json()
    assert detail["task"]["status"] == "completed", detail["task"]
    assert detail["task"]["upload_id"] is None, "拍照任务不产生样本批次"
    assert detail["photo"]["id"] == cam_pid, "任务详情应带上关联照片"
    assert detail["photo"]["bytes"] == len(cam_body)
    assert detail["photo"]["width"] == 640 and detail["photo"]["height"] == 480
    print("[PASS] camera 任务链路：%s → 照片 %s 上传后任务 completed（详情带 photo）"
          % (cam_rid[:8], cam_pid))

    # 9) 幂等：同一 request_id 重复上传 → 200 idempotent，不再写第二行/第二个文件
    before = client.get("/api/v1/photos", params={"device_id": CAM_DEV}).json()["total"]
    r = post_photo(CAM_DEV, body=cam_body, request_id=cam_rid)
    assert r.status_code == 200 and r.json()["idempotent"] is True, r.text
    assert r.json()["photo_id"] == cam_pid, r.text
    after = client.get("/api/v1/photos", params={"device_id": CAM_DEV}).json()["total"]
    assert before == after == 1, (before, after)
    assert client.get("/api/v1/tasks/%s" % cam_rid).json()["task"]["status"] == "completed"
    print("[PASS] 幂等：同一 request_id 重传 200 idempotent=true，照片仍只有 1 张")

    # 9b) 幂等重放**换了不同长度的字节**（板端重试时真会发生）：服务端应当忽略新
    #     字节、沿用老 photo_id，因此顶层 bytes 必须与 photo.bytes、磁盘文件一致。
    #     若顶层用本次请求体长度，就会出现「顶层 500 B、photo 26x B」的自相矛盾。
    other = jpeg_bytes(512, marker=0x66)
    assert len(other) != len(cam_body)
    r = post_photo(CAM_DEV, body=other, request_id=cam_rid, w=320, h=240)
    assert r.status_code == 200 and r.json()["idempotent"] is True, r.text
    assert r.json()["photo_id"] == cam_pid, r.text
    assert r.json()["bytes"] == len(cam_body) == r.json()["photo"]["bytes"], r.json()
    with open(photo_abs_path(CAM_DEV, cam_pid), "rb") as fh:
        assert fh.read() == cam_body, "幂等重放不该覆盖磁盘上的老图片"
    assert client.get("/api/v1/photos",
                      params={"device_id": CAM_DEV}).json()["total"] == 1
    print("[PASS] 幂等重放换不同字节：忽略新字节，顶层 bytes / photo.bytes / 文件 三者一致")

    # 10) 归属校验：capture 任务不能被照片收尾（否则会出现「完成但没有样本」）
    r = create_task(CAP_DEV, kind="capture")
    assert r.status_code == 201, r.text
    cap_rid = r.json()["task"]["request_id"]
    got = claim_task(CAP_DEV).json()
    assert got["found"] is True and got["task"]["request_id"] == cap_rid, got
    r = post_photo(CAP_DEV, request_id=cap_rid)
    assert r.status_code == 409 and r.json()["code"] == "WRONG_KIND", r.text
    cap_status = client.get("/api/v1/tasks/%s" % cap_rid).json()["task"]["status"]
    assert cap_status in ("submitted", "dispatched", "acked"), cap_status
    print("[PASS] capture 任务收到照片 → 409 WRONG_KIND，任务状态保持 %s（未被照片收尾）"
          % cap_status)

    # 11) 设备号不匹配：400 + 任务被判 failed（错误信息写进 task.error）
    r = create_task(MM_DEV, kind="camera")
    assert r.status_code == 201, r.text
    mm_rid = r.json()["task"]["request_id"]
    got = claim_task(MM_DEV).json()
    assert got["found"] is True and got["task"]["request_id"] == mm_rid, got
    r = post_photo(DIRECT_DEV, request_id=mm_rid)
    assert r.status_code == 400 and r.json()["code"] == "INVALID", r.text
    assert "device_id mismatch" in r.json()["error"], r.text
    mm_task = client.get("/api/v1/tasks/%s" % mm_rid).json()["task"]
    assert mm_task["status"] == "failed" and "device_id mismatch" in mm_task["error"], mm_task
    print("[PASS] 照片设备号不匹配 → 400 且任务 failed：%s" % mm_task["error"])

    # 12) 元数据在、文件被手工删掉 → 410 FILE_MISSING（不是伪装成 404）
    missing_path = photo_abs_path(DIRECT_DEV, second_id)
    assert os.path.isfile(missing_path), missing_path
    os.remove(missing_path)
    r = client.get("/api/v1/photos/%s" % second_id)
    assert r.status_code == 410 and r.json()["code"] == "FILE_MISSING", r.text
    assert client.delete("/api/v1/photos/%s" % second_id).status_code == 200, "删库行应成功"
    print("[PASS] 文件丢失 → GET 410 FILE_MISSING；DELETE 仍能删掉库行")

    # 13) 删除：200 + 文件真的消失 + 列表/计数同步；重复删除 404
    del_path = photo_abs_path(DIRECT_DEV, pid)
    assert os.path.isfile(del_path), del_path
    r = client.delete("/api/v1/photos/%s" % pid)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["deleted"] is True and j["file_deleted"] is True and j["photo_id"] == pid, j
    assert not os.path.exists(del_path), "删除接口必须同时删掉磁盘文件"
    assert client.get("/api/v1/photos/%s" % pid).status_code == 404
    assert client.delete("/api/v1/photos/%s" % pid).status_code == 404
    left = client.get("/api/v1/photos", params={"device_id": DIRECT_DEV}).json()
    assert left["count"] == 0 and left["total"] == 0, left
    print("[PASS] DELETE photo %s：库行+文件同删，重复删除 404，设备内照片归零"
          % pid)

    # 14) 汇总接口：health 计数/最新照片，devices 带上 photos 计数
    h = client.get("/api/v1/health").json()
    assert h["total_photos"] == 1, h["total_photos"]          # 只剩 CAM_DEV 那张
    assert h["latest_photo"]["id"] == cam_pid, h["latest_photo"]
    assert h["latest_photo"]["device_id"] == CAM_DEV
    assert h["latest_photo"]["url"] == "/api/v1/photos/%s" % cam_pid
    devs = {d["device_id"]: d for d in client.get("/api/v1/devices").json()["devices"]}
    assert devs[CAM_DEV]["photos"] == 1, devs[CAM_DEV]
    print("[PASS] health.total_photos=%s、latest_photo=%s；devices 带 photos 计数"
          % (h["total_photos"], cam_pid))

    # 15) 加固回归：photos 的 device_id 也走同一份白名单（_device_id_ok）
    for bad_dev in ("<img src=x onerror=alert(1)>", "../escape", "bad dev"):
        r = post_photo(bad_dev)
        assert r.status_code == 400 and r.json()["code"] == "INVALID", (bad_dev, r.text)
        assert "device_id" in r.json()["error"], r.text
    print("[PASS] 照片 device_id 白名单：脚本标签/路径穿越/空格 → 400")

    # 16) photos.request_id 的部分唯一索引已由 init_db() 建好，且冗余的普通索引
    #     被清掉（全新临时库不会走 _migrate_photos_index 的「有重复行就跳过」分支）
    conn = main._connect()
    try:
        uniq = conn.execute("SELECT sql FROM sqlite_master WHERE type='index'"
                            " AND name='idx_photos_request_uq'").fetchone()
        legacy = conn.execute("SELECT name FROM sqlite_master WHERE type='index'"
                              " AND name='idx_photos_request'").fetchone()
    finally:
        conn.close()
    assert uniq is not None and "UNIQUE" in (uniq["sql"] or "").upper(), uniq
    assert legacy is None, "普通索引应被部分唯一索引取代，避免 (request_id) 上重复建索引"
    print("[PASS] photos.request_id 部分唯一索引存在，冗余普通索引已清理")

    # 17) store_photo 并发竞态：两个线程同 request_id，必须只落 1 行/1 个文件，
    #     且不抛 IntegrityError（旧代码会 500，并可能多出一张重复照片）
    conc_dev = "esp32s3-eye-concpht"
    conc_body = jpeg_bytes(64, marker=0x77)
    ok_conc, conc_data = main.validate_photo_payload({
        "device_id": conc_dev,
        "request_id": uuid.uuid4().hex,
        "ts_ms": int(time.time() * 1000),
        "width": 640,
        "height": 480,
        "note": None,
        "jpeg": conc_body,
        "ip": "127.0.0.1",
    })
    assert ok_conc, conc_data
    conc_results, conc_errors = [], []

    def _store_photo_once():
        try:
            conc_results.append(main.store_photo(conc_data))
        except Exception as exc:            # noqa: BLE001 - 测试就是要抓住任何异常
            conc_errors.append(repr(exc))

    conc_threads = [threading.Thread(target=_store_photo_once) for _ in range(2)]
    for t in conc_threads:
        t.start()
    for t in conc_threads:
        t.join()
    assert not conc_errors, conc_errors
    assert len(conc_results) == 2, conc_results
    assert sorted(flag for _, flag in conc_results) == [False, True], conc_results
    assert conc_results[0][0] == conc_results[1][0], "并发重试应回同一个 photo_id"
    total = client.get("/api/v1/photos", params={"device_id": conc_dev}).json()["total"]
    assert total == 1, total
    assert os.path.isfile(photo_abs_path(conc_dev, conc_results[0][0]))
    print("[PASS] store_photo 并发同 request_id：只落 1 张图，无异常")

    # 18) 首页 HTML 转义（纵深防御）：即便库里已存在历史上写入的恶意 device_id，
    #     GET / 也必须把它转义显示，而不是当标记执行
    conn = main._connect()
    try:
        conn.execute(
            "INSERT INTO uploads (device_id, source, unit, ts_ms, received_at,"
            " sample_count, ip, payload, request_id, trigger)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("<img src=x onerror=alert(1)>", "qma6100p", "m/s^2",
             int(time.time() * 1000), time.time(), 1, "127.0.0.1", "{}", None,
             "periodic"),
        )
        conn.commit()
    finally:
        conn.close()
    r = client.get("/")
    assert r.status_code == 200, r.status_code
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text, "device_id 未被转义"
    assert "<img src=x onerror=alert(1)>" not in r.text, "device_id 被当成 HTML 插进了页面"
    print("[PASS] 首页 GET / 对 device_id 做 HTML 转义（历史脏数据也不会执行脚本）")

    # 19) 实时直播（POST/GET /api/v1/live）：内存态，不落盘、不入库。
    #     device_id 现在也走与 upload/photos/tasks/events 同一套白名单 —— 它会被原样
    #     回吐给 Web（devices[]），过去只校验长度，任意字符都能进内存态。
    def post_live(dev, body=None, **params):
        query = {"device_id": dev}
        query.update(params)
        payload = jpeg_bytes() if body is None else body
        return client.post("/api/v1/live", params=query, content=payload,
                           headers={"Content-Type": "image/jpeg"})

    r = client.get("/api/v1/live", params={"device_id": DIRECT_DEV})
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    for bad_dev in ("live/dev\\..", "<img src=x onerror=alert(1)>", "a b", "设备"):
        r = post_live(bad_dev)
        assert r.status_code == 400 and r.json()["code"] == "INVALID", (bad_dev, r.text)
    assert client.get("/api/v1/live", params={"device_id": "a b"}).status_code == 400
    assert client.get("/api/v1/live/frame", params={"device_id": "a b"}).status_code == 400
    print("[PASS] 直播 device_id 白名单：路径穿越/脚本标签/空格/中文 → 400"
          "（POST 与两个 GET 口径一致）")

    # 20) 直播查询参数的范围校验（与 /api/v1/photos 对齐：ts_ms>=0、宽高 1..8192）
    for bad_params in ({"ts_ms": -1}, {"w": 0}, {"w": 99999}, {"h": -5}, {"ts_ms": "abc"}):
        r = post_live(DIRECT_DEV, **bad_params)
        assert r.status_code == 400, (bad_params, r.text)
    r = post_live(DIRECT_DEV, ts_ms=1767123456789, w=640, h=480)
    assert r.status_code == 201 and r.json()["bytes"] > 0, r.text
    print("[PASS] 直播参数校验：ts_ms<0 / w,h 越界 / 非整数 → 400；合法 640x480 → 201")

    # 21) 内存上界：超过 LIVE_MAX_DEVICES 台设备再推流，会挤掉最久未推流的那台。
    #     没有这条上界时 _live_frames 只增不减（每台最多 512 KiB），而 device_id
    #     来自未鉴权的 POST —— 任何能访问本端口的人都用它灌内存。
    for i in range(main.LIVE_MAX_DEVICES + 2):
        assert post_live("live-dev-%02d" % i).status_code == 201
    devices = client.get("/api/v1/live").json()["devices"]
    assert len(devices) <= main.LIVE_MAX_DEVICES, len(devices)
    assert len(main._live_frames) <= main.LIVE_MAX_DEVICES, list(main._live_frames)
    # 最近推流的设备必须在（淘汰的是最久没动静的，不能反过来把新帧丢掉）
    assert "live-dev-%02d" % (main.LIVE_MAX_DEVICES + 1) in main._live_frames
    print("[PASS] 直播内存上界：设备数夹在 LIVE_MAX_DEVICES=%d 以内（当前内存态 %d 条）"
          % (main.LIVE_MAX_DEVICES, len(main._live_frames)))

    # 22) 单帧上限：Content-Length 超限直接 413（在读 body 之前就拒绝）
    huge = b"\xff\xd8\xff" + b"\x33" * (main.LIVE_MAX_FRAME_BYTES + 1024)
    r = post_live(DIRECT_DEV, body=huge)
    assert r.status_code == 413 and r.json()["code"] == "TOO_LARGE", r.text
    print("[PASS] 直播单帧超限 413 TOO_LARGE（上限 %s B）" % main.LIVE_MAX_FRAME_BYTES)

    # 23) 清理临时库与临时照片目录（正式库/正式 photos 目录从未被写入）
    shutil.rmtree(_TEST_PHOTO_DIR, ignore_errors=True)
    _remove_db_files(_TEST_DB)
    print("[PASS] 临时库/临时照片目录已清理")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main_run()