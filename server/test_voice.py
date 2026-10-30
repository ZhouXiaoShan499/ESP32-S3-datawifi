# -*- coding: utf-8 -*-
"""
板端语音链路自测：验证「上传 WAV/PCM → 落盘 → 识别结果入库 → 页面轮询 → 回放」。

不依赖真实麦克风，也不要求安装 vosk：
  * 用 wave 模块合成 16 kHz 单声道测试音频；
  * 把 voice_asr.transcribe 换成固定返回，验证接口/入库/幂等/游标；
  * 再换成一个抛 AsrUnavailable 的假实现，验证「缺库/缺模型」时的降级路径。

使用独立临时库与临时音频目录，不污染正式库与 server/data/voice/。
运行：python server/test_voice.py   （需 fastapi / httpx）
"""

import array
import io
import math
import os
import shutil
import sys
import time
import uuid
import wave

# 中文输出固定 UTF-8：中文 Windows 下控制台/重定向默认 cp936，日志会变乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_DB = os.path.join(_HERE, "data", "test_voice.db")
_TEST_VOICE_DIR = os.path.join(_HERE, "data", "test_voice")
# 指向不存在的模型目录：即使本机装了模型，测试也只走 mock（状态接口除外）
_TEST_MODEL_DIR = os.path.join(_HERE, "data", "test_voice_no_model")


def _remove_db_files(path):
    """删库时连 WAL 副产品（-wal/-shm/-journal）一起删（理由见 test_receive.py）。"""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


_remove_db_files(_TEST_DB)
if os.path.isdir(_TEST_VOICE_DIR):
    shutil.rmtree(_TEST_VOICE_DIR)

# 先指到临时库/临时音频目录，再导入 main（main 在 import 时读取这些变量）
os.environ["SENSOR_DB"] = _TEST_DB
os.environ["SENSOR_VOICE_DIR"] = _TEST_VOICE_DIR
os.environ["SENSOR_HOST"] = "127.0.0.1"
os.environ["VOICE_MODEL_DIR"] = _TEST_MODEL_DIR
os.environ.pop("SENSOR_TOKEN", None)

sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

DEV = "esp32s3-eye-voice"
N = 0


def ok(msg):
    global N
    N += 1
    print("[PASS] %s" % msg)


def rid():
    return "voice-" + uuid.uuid4().hex[:16]


def make_wav(seconds=1.2, rate=16000, freq=440.0, channels=1):
    """合成一段正弦波 WAV（16 bit），避免测试依赖外部音频文件。"""
    total = int(rate * seconds)
    samples = array.array("h")
    for i in range(total):
        value = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        for _ in range(channels):
            samples.append(value)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


def db_rows(request_id):
    conn = main._connect()
    try:
        return conn.execute(
            "SELECT * FROM voice_notes WHERE request_id=?", (request_id,)
        ).fetchall()
    finally:
        conn.close()


def main_run():
    main.init_db()
    client = TestClient(main.app)

    assert main.voice_asr is not None, "voice_asr 模块没有加载"
    original = main.voice_asr.transcribe

    calls = {"n": 0}

    def fake_ok(pcm, rate=main.voice_asr.TARGET_RATE):
        calls["n"] += 1
        return "查看最新数据"

    main.voice_asr.transcribe = fake_ok

    # 1) 引擎状态接口：字段齐全，缺模型时给出可执行提示
    r = client.get("/api/v1/voice/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["api"] == "/api/v1/voice", body
    assert body["target_rate"] == 16000 and "ready" in body, body
    assert body["detail"], "就绪/未就绪都必须给一句人话说明"
    ok("status 接口：ready=%s，detail=%s" % (body["ready"], body["detail"][:28]))

    # 2) 正常 WAV：识别文字入库、音频落盘
    rid1 = rid()
    audio = make_wav(1.2)
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "request_id": rid1},
                    content=audio, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status"] == "ok", body
    assert body["text"] == "查看最新数据", body
    assert body["sample_rate"] == 16000, body
    assert 1100 <= body["duration_ms"] <= 1400, body["duration_ms"]
    note_id = body["note"]["note_id"]
    assert note_id > 0 and body["note"]["audio_url"].endswith("/audio"), body["note"]
    assert calls["n"] == 1, "正常路径应调用一次识别"
    saved = os.path.join(_TEST_VOICE_DIR, rid1 + ".wav")
    assert os.path.isfile(saved), "音频没有落盘：%s" % saved
    with open(saved, "rb") as fh:
        assert fh.read(4) == b"RIFF", "落盘文件不是 WAV"
    ok("WAV 上传：text=%s，duration=%d ms，音频已落盘" % (body["text"], body["duration_ms"]))

    # 3) 同一 request_id 重试：幂等，不重复识别/入库
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "request_id": rid1},
                    content=audio, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200 and r.json()["duplicate"] is True, r.text
    assert calls["n"] == 1, "重试不该再次识别"
    assert len(db_rows(rid1)) == 1, "重试不该产生第二行"
    ok("板端重试幂等：同 request_id 只识别一次、只落一行")

    # 4) 裸 PCM（板端实际发的是 WAV，但协议允许裸 PCM，便于手工联调）
    pcm = b"\x00\x01" * 8000
    rid2 = rid()
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "request_id": rid2,
                            "sample_rate": 16000},
                    content=pcm,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert r.json()["duration_ms"] == 500, r.json()["duration_ms"]
    ok("裸 PCM 上传：按 sample_rate 解析，自动补 WAV 头落盘")

    # 4b) 非 16 kHz / 双声道：解码时下混、识别前重采样（真机只会发 16 kHz，
    #     但浏览器自测可能上传手机录音，格式不能只认一种）
    stereo = make_wav(1.0, rate=44100, channels=2)
    rid2b = rid()
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "request_id": rid2b},
                    content=stereo, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert r.json()["sample_rate"] == 44100, r.json()["sample_rate"]
    pcm, rate = main.voice_asr.decode(stereo, main.voice_asr.TARGET_RATE)
    assert rate == 44100 and len(pcm) == 44100 * 2, (rate, len(pcm))
    resampled = main.voice_asr.resample(pcm, 44100)
    assert len(resampled) == 16000 * 2, len(resampled)
    ok("44.1 kHz 双声道：下混 + 重采样到 16 kHz 的字节数正确")

    # 5) 页面轮询游标：after_id 之后没有新记录时返回空
    r = client.get("/api/v1/voice/latest", params={"device_id": DEV, "after_id": 0})
    assert r.status_code == 200, r.text
    notes = r.json()["notes"]
    assert len(notes) == 3, [n["request_id"] for n in notes]
    assert notes[0]["note_id"] < notes[1]["note_id"], "必须按 id 升序"
    assert r.json()["last_id"] == notes[-1]["note_id"], r.json()["last_id"]
    r = client.get("/api/v1/voice/latest",
                   params={"device_id": DEV, "after_id": notes[-1]["note_id"]})
    assert r.json()["notes"] == [] and r.json()["last_id"] == notes[-1]["note_id"], r.text
    ok("latest 游标：升序返回、after_id 之后为空")

    # 6) 回放：音频接口返回 WAV 字节
    r = client.get("/api/v1/voice/%d/audio" % note_id)
    assert r.status_code == 200 and r.content[:4] == b"RIFF", r.status_code
    assert r.headers.get("content-type", "").startswith("audio/wav"), r.headers
    r = client.get("/api/v1/voice/999999/audio")
    assert r.status_code == 404, r.status_code
    ok("回放接口：存在时 audio/wav，未知 id → 404")

    # 7) 参数校验
    r = client.post("/api/v1/voice", content=audio)
    assert r.status_code == 400 and "device_id" in r.json()["error"], r.text
    r = client.post("/api/v1/voice", params={"device_id": "bad dev"}, content=audio)
    assert r.status_code == 400, r.text
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "request_id": "ab"}, content=audio)
    assert r.status_code == 400 and "request_id" in r.json()["error"], r.text
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "sample_rate": 123456},
                    content=b"\x00\x01" * 8000,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400 and "采样率" in r.json()["error"], r.text
    ok("参数校验：缺/非法 device_id、非法 request_id、超范围采样率 → 400")

    # 8) 太短/太大
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "request_id": rid()},
                    content=b"\x00\x00" * 200,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and r.json()["status"] == "too_short", r.text
    r = client.post("/api/v1/voice", params={"device_id": DEV},
                    content=b"\x00" * 10,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400, r.text
    r = client.post("/api/v1/voice", params={"device_id": DEV},
                    content=b"\x00" * (main.MAX_VOICE_BYTES + 1),
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413 and r.json()["code"] == "TOO_LARGE", r.status_code
    ok("时长/体积闸门：0.2 s → too_short，10 B → 400，>2 MB → 413")

    # 9) 缺 vosk/缺模型的降级：仍 200 + 音频保存，只是没有文字
    def fake_unavailable(pcm, rate=main.voice_asr.TARGET_RATE):
        raise main.voice_asr.AsrUnavailable("模拟：未安装 vosk")

    main.voice_asr.transcribe = fake_unavailable
    rid3 = rid()
    r = client.post("/api/v1/voice",
                    params={"device_id": DEV, "request_id": rid3},
                    content=audio, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and body["status"] == "asr_unavailable", body
    assert "vosk" in body["error"] or "模型" in body["error"], body["error"]
    assert body["hint"], "降级时必须告诉用户怎么修"
    assert os.path.isfile(os.path.join(_TEST_VOICE_DIR, rid3 + ".wav")), "降级也要保存音频"
    assert db_rows(rid3)[0]["status"] == "asr_unavailable"
    ok("降级路径：缺引擎 → asr_unavailable + 修复提示，音频照样保存")

    main.voice_asr.transcribe = original
    print("\n全部通过：%d 项断言" % N)


if __name__ == "__main__":
    main_run()
