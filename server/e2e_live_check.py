# -*- coding: utf-8 -*-
"""
摄像头实时直播自检（板端长按 Button A → POST /api/v1/live → Web「摄像头实时画面」）。

流程：临时库 + 真实 uvicorn 服务（SENSOR_LIVE_TIMEOUT_S 压到 1 s，便于验证「停止推流」判定）
  * POST /api/v1/live 推一帧 JPEG → 201 + 全局单调 seq；GET /api/v1/live 返回
    active / seq / 分辨率 / 字节数 / 帧龄；GET /api/v1/live/frame 原样取回同一帧字节
  * 再推一帧 → seq 递增，frame 换成新内容（内存里只保留最新一帧）
  * 错误路径：缺 device_id / 非 JPEG body / ts_ms 非整数 → 400；超大 body → 413；
    未知设备取帧 → 404
  * 「不落盘、不入库」：直播帧不产生 photos 行、也不写照片目录（总数仍为 0）
  * 超时判定：等过 LIVE_TIMEOUT_S 后 active=false，但 frame 仍可取回最后一帧
  * 有 Edge/Chrome 时无头 dump /ui/ DOM：实时卡片渲染 <img src=/api/v1/live/frame…>
    且徽章为「推流中」；超时后再 dump 一次，徽章变「已停止（保留最后一帧）」而画面不消失

运行：python server/e2e_live_check.py
  未找到浏览器时只跑 HTTP 部分并打印 [SKIP]（可用 EDGE_PATH 指定浏览器路径）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# 输出固定 UTF-8：中文 Windows 下控制台/重定向默认 cp936，中文日志会乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(_HERE, "data", "e2e_live.db")
if os.path.exists(_DB):
    os.remove(_DB)
# 直播不该写磁盘：目录留着，最后断言它是空的
_PHOTOS = os.path.join(_HERE, "data", "e2e_live_photos")
shutil.rmtree(_PHOTOS, ignore_errors=True)
os.environ["SENSOR_DB"] = _DB
os.environ["SENSOR_PHOTO_DIR"] = _PHOTOS
os.environ["SENSOR_HOST"] = "127.0.0.1"
os.environ["SENSOR_PORT"] = "8013"
os.environ["SENSOR_LIVE_TIMEOUT_S"] = "1"      # 1 s 无新帧即视为停止推流（真实默认 5 s）

sys.path.insert(0, _HERE)

BASE = "http://127.0.0.1:8013"
DEV = "esp32s3-eye-live"
OTHER_DEV = "esp32s3-eye-live-2"
PROFILE = os.path.join(os.environ.get("TEMP", "."), "edge-e2e-live-check")

PASSED = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("[FAIL] %s %s" % (name, detail))
    PASSED.append(name)
    print("[PASS] %s %s" % (name, detail))


def _request(method, path, data=None, content_type=None):
    """发请求并返回 (状态码, Content-Type, body 字节)；4xx/5xx 也解析 body。"""
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def get_json(path):
    st, _, raw = _request("GET", path)
    return st, json.loads(raw.decode("utf-8"))


def post_frame(device_id, jpeg, ts_ms=None, w=640, h=480, extra=""):
    """POST 一帧 JPEG（body 即图片字节，与板端 live_post_frame 一致）。"""
    path = "/api/v1/live?device_id=%s&w=%d&h=%d" % (device_id, w, h)
    if ts_ms is not None:
        path += "&ts_ms=%d" % ts_ms
    path += extra
    return _request("POST", path, jpeg, "image/jpeg")


def jpeg_bytes(n, marker=0x66):
    """最小合法 JPEG：SOI + APP0 + n 字节负载 + EOI（服务端只校验 SOI）。"""
    return b"\xff\xd8\xff\xe0" + bytes([marker]) * n + b"\xff\xd9"


def wait_server(deadline_s=20):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            st, _ = get_json("/api/v1/health")
            if st == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def http_checks():
    """HTTP 层：推帧 → 状态 → 取图 → 错误路径 → 不落盘 → 超时判定。"""
    import main

    frame1 = jpeg_bytes(2048, 0x11)
    st, _, raw = post_frame(DEV, frame1, ts_ms=int(time.time() * 1000))
    body = json.loads(raw.decode("utf-8"))
    check("推帧 201 + seq", st == 201 and body["ok"] is True and body["seq"] == 1,
          str(body)[:140])

    st, st_body = get_json("/api/v1/live?device_id=" + DEV)
    check("状态 active/seq/尺寸", st == 200 and st_body["active"] is True
          and st_body["seq"] == 1 and st_body["width"] == 640
          and st_body["height"] == 480 and st_body["bytes"] == len(frame1),
          str(st_body)[:200])

    st, ctype, raw = _request("GET", "/api/v1/live/frame?device_id=" + DEV + "&seq=1")
    check("取帧原样返回", st == 200 and ctype.startswith("image/jpeg")
          and raw == frame1, "http=%d ctype=%s %dB" % (st, ctype, len(raw)))

    st, all_body = get_json("/api/v1/live")
    devs = {d["device_id"]: d for d in all_body.get("devices", [])}
    check("设备列表含推流设备",
          st == 200 and DEV in devs and devs[DEV]["active"] is True,
          str(all_body.get("devices"))[:200])

    # 只保留最新一帧：再推一帧 → seq 递增、frame 内容替换
    frame2 = jpeg_bytes(4096, 0x22)
    st, _, raw = post_frame(DEV, frame2)
    body = json.loads(raw.decode("utf-8"))
    check("第二帧 seq 递增", st == 201 and body["seq"] == 2, str(body)[:140])
    st, st_body = get_json("/api/v1/live?device_id=" + DEV)
    check("状态跟随最新帧", st_body["seq"] == 2 and st_body["bytes"] == len(frame2)
          and st_body["age_ms"] < 1500, str(st_body)[:200])
    st, _, raw = _request("GET", "/api/v1/live/frame?device_id=" + DEV)
    check("frame 换成最新一帧", st == 200 and raw == frame2, "%dB" % len(raw))

    # 错误路径
    st, _, raw = _request("POST", "/api/v1/live?w=640&h=480", frame1, "image/jpeg")
    check("缺 device_id → 400", st == 400, raw.decode("utf-8")[:120])
    st, _, raw = post_frame(DEV, b"\x00\x01\x02\x03not-a-jpeg")
    check("非 JPEG body → 400", st == 400, raw.decode("utf-8")[:120])
    st, _, raw = post_frame(DEV, b"")
    check("空 body → 400", st == 400, raw.decode("utf-8")[:120])
    st, _, raw = post_frame(DEV, frame1, extra="&ts_ms=abc")
    check("ts_ms 非整数 → 400", st == 400, raw.decode("utf-8")[:120])
    st, _, raw = post_frame(DEV, jpeg_bytes(main.LIVE_MAX_FRAME_BYTES + 1))
    check("超大帧 → 413", st == 413, raw.decode("utf-8")[:140])
    st, _, raw = _request("GET", "/api/v1/live/frame?device_id=" + OTHER_DEV)
    check("未知设备取帧 → 404", st == 404, raw.decode("utf-8")[:140])

    # 不落盘、不入库
    st, photos = get_json("/api/v1/photos?device_id=" + DEV)
    written = []
    for _root, _dirs, files in os.walk(_PHOTOS):
        written.extend(files)
    check("直播不入 photos 表", st == 200 and photos["total"] == 0,
          "total=%s" % photos.get("total"))
    check("直播不写磁盘", not written, str(written)[:120])

    # 超时判定：超过 LIVE_TIMEOUT_S 无新帧 → active=false，但最后一帧仍可取
    time.sleep(main.LIVE_TIMEOUT_S + 0.6)
    st, st_body = get_json("/api/v1/live?device_id=" + DEV)
    check("无新帧 → active=false", st_body["active"] is False
          and st_body["seq"] == 2 and st_body["age_ms"] > 1000,
          str(st_body)[:200])
    st, _, raw = _request("GET", "/api/v1/live/frame?device_id=" + DEV)
    check("停止后仍保留最后一帧", st == 200 and raw == frame2, "%dB" % len(raw))


def find_browser():
    """返回可用的浏览器可执行文件；找不到返回 None（调用方 SKIP）。"""
    for key in ("EDGE_PATH", "CHROME_PATH"):
        path = os.environ.get(key)
        if path and os.path.exists(path):
            return path
    for path in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if os.path.exists(path):
            return path
    return None


def dump_dom(browser, url):
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--user-data-dir=" + PROFILE,
        "--virtual-time-budget=9000", "--dump-dom", url,
    ]
    out = subprocess.run(cmd, capture_output=True, timeout=180)
    # --dump-dom 输出是 UTF-8；不能用 text=True（Windows 默认 GBK 会解码失败）
    return (out.stdout or b"").decode("utf-8", "replace")


def any_text(dom, elem_id):
    """取出任意标签且带指定 id 的元素文本。"""
    m = re.search(r'<(\w+)[^>]*id="%s"[^>]*>(.*?)</\1>' % elem_id, dom, re.S)
    return m.group(2).strip() if m else ""


def tag_of(dom, name, elem_id):
    m = re.search(r'<%s[^>]*id="%s"[^>]*>' % (name, elem_id), dom)
    return m.group(0) if m else ""


def browser_checks(browser):
    """页面断言：推流中显示 <img>，超时后保留画面并标记已停止。

    页面里设备下拉来自 /api/v1/devices（uploads 表）：先造一批周期数据，
    选中设备才会是推流的那台 —— 正好也验证「直播卡片跟着下拉设备走」。
    """
    import main

    st, _ = get_json("/api/v1/health")
    now_ms = int(time.time() * 1000)
    req = urllib.request.Request(
        BASE + "/api/v1/upload",
        data=json.dumps({
            "device_id": DEV, "source": "qma6100p", "unit": "m/s^2",
            "ts_ms": now_ms,
            "samples": [{"i": i, "t_ms": i * 10, "ax": 0, "ay": 0, "az": 9.8}
                        for i in range(3)],
        }).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        check("seed 周期上报", resp.status == 201)

    # 页面首轮 poll 必须看到 active=true。但本脚本把 LIVE_TIMEOUT_S 压到 1 s，
    # 而"启动无头浏览器 → 加载页面 → 首轮轮询"这一段本身就要 1 s 以上，
    # 所以**只推一帧必然过期**（这条断言以前是碰运气过的，机器一慢就红）。
    # 改成后台线程按 0.4 s 持续推帧，等本段浏览器断言做完再停；
    # 后面"超时判定"那段仍然靠真正停推来触发 active=false，断言强度不变。
    stop_push = threading.Event()

    def keep_pushing():
        while not stop_push.is_set():
            try:
                post_frame(DEV, jpeg_bytes(3000, 0x33),
                           ts_ms=int(time.time() * 1000))
            except Exception:
                pass        # 服务端在重启窗口内时忽略，下一轮补上
            stop_push.wait(0.4)

    pusher = threading.Thread(target=keep_pushing, daemon=True)
    pusher.start()

    # 先单独推一帧并断言接收成功，再开浏览器（这条同时确认推帧路径本身可用）
    frame = jpeg_bytes(3000, 0x33)
    st, _, raw = post_frame(DEV, frame, ts_ms=int(time.time() * 1000))
    check("页面断言前重新推帧", st == 201, raw.decode("utf-8")[:120])

    dom = dump_dom(browser, BASE + "/ui/")
    check("browser returned DOM", len(dom) > 2000, "%dB" % len(dom))
    img = tag_of(dom, "img", "liveImg")
    check("实时卡片 <img> 指向 /api/v1/live/frame",
          "/api/v1/live/frame" in img and DEV in img, img[:160])
    check("推流中不再隐藏 <img>", "hidden" not in img, img[:160])
    badge = any_text(dom, "liveBadge")
    check("徽章显示推流中", "推流中" in badge and "640×480" in badge, badge)
    meta = any_text(dom, "liveMeta")
    check("元信息带 seq / 尺寸 / 帧龄",
          "seq" in meta and "640×480" in meta and "s 前" in meta, meta[:160])
    off = tag_of(dom, "span", "liveOff")
    check("占位提示已隐藏", "hidden" in off, off[:80])

    stop_push.set()         # 停推：下面开始验证"无新帧 → 已停止"

    # 等过阈值再 dump：active=false → 徽章变「已停止（保留最后一帧）」，画面不消失
    time.sleep(main.LIVE_TIMEOUT_S + 0.6)
    dom2 = dump_dom(browser, BASE + "/ui/")
    badge2 = any_text(dom2, "liveBadge")
    check("超时后徽章标记已停止", "已停止" in badge2, badge2)
    check("超时后画面不消失",
          "/api/v1/live/frame" in tag_of(dom2, "img", "liveImg"), badge2)


def main_run():
    import main           # 先导入 main：它在 import 时读取 SENSOR_DB / SENSOR_LIVE_TIMEOUT_S
    import uvicorn

    main.init_db()
    config = uvicorn.Config(main.app, host="127.0.0.1", port=8013,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    check("server up", wait_server(), BASE)

    http_checks()

    browser = find_browser()
    if not browser:
        print("[SKIP] 未找到 Edge/Chrome，跳过页面断言"
              "（可用环境变量 EDGE_PATH 指定浏览器路径）")
    else:
        browser_checks(browser)

    print("\n[OK] 摄像头实时直播自检通过：%d 项" % len(PASSED))


if __name__ == "__main__":
    try:
        main_run()
    except AssertionError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except Exception as exc:                      # noqa: BLE001 - 自检脚本要打印原因
        print("[ERROR] 自检异常：%r" % (exc,), file=sys.stderr)
        sys.exit(2)