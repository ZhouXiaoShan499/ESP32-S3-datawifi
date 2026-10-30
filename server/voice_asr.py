# -*- coding: utf-8 -*-
"""离线语音识别（Vosk）封装：给板端麦克风上传的 WAV/PCM 做转写。

设计目标
--------
* **零 API Key、纯本地**：模型只从本机目录加载，不联网、不调用云端。
* **可选依赖**：没装 ``vosk`` 或没下载模型时，服务端其余功能照常工作；
  上传的音频仍然落盘，接口返回 ``asr_unavailable`` + 可执行的修复提示。
* **不阻塞事件循环**：调用方（server/main.py）用 ``run_in_threadpool`` 把它
  扔到工作线程；本模块自身只用一把加载锁，识别时每次新建 recognizer，
  多请求并发不会串音。

音频约定
--------
板端按 **16 kHz / 16-bit / 单声道** 录音，上传体既可以是裸 PCM
（``application/octet-stream``，采样率由查询参数给出），也可以是标准 WAV
（``audio/wav``，从 RIFF 头读取采样率）。其它采样率会先重采样到 16 kHz。

模型目录
--------
优先读环境变量 ``VOICE_MODEL_DIR``；否则在 ``server/models/`` 下找任意一个
包含 ``am/`` 子目录的 Vosk 模型（默认期望 ``vosk-model-small-cn-0.22``）。
"""

from __future__ import annotations

import array
import io
import json
import os
import re
import sys
import threading
import wave
from typing import Any, Dict, List, Optional, Tuple

# 识别目标采样率：Vosk 中文模型固定 16 kHz
TARGET_RATE = 16000
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000
# 小于 0.3 s 的音频基本不可能是一句话；大于 30 s 的请求由接口层的 2 MB 上限兜住
MIN_AUDIO_MS = 300
MAX_AUDIO_MS = 30000
MIN_RAW_BYTES = 320          # 0.01 s @16k/16bit，再小就是空包

_HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_ENV = "VOICE_MODEL_DIR"
DEFAULT_MODEL_DIR = os.path.join(_HERE, "models", "vosk-model-small-cn-0.22")

# 模型只加载一次；识别时每次新建 KaldiRecognizer（Vosk 要求 recognizer 不共享）
_lock = threading.Lock()
_model = None                # type: Any
_model_dir = ""              # type: str
_model_error = ""            # type: str


class AsrUnavailable(RuntimeError):
    """识别引擎不可用：没装 vosk，或本机没有可用的中文模型。"""


class BadAudio(ValueError):
    """上传体不是可解析的 16-bit PCM / WAV。"""


# ---------------------------------------------------------------------------
# 模型发现与加载
# ---------------------------------------------------------------------------
def _is_model_dir(path: str) -> bool:
    """Vosk 模型目录的判据：至少要有 ``am/``（声学模型）。"""
    try:
        return os.path.isdir(os.path.join(path, "am"))
    except OSError:
        return False


def _candidate_dirs() -> List[str]:
    """按优先级列出候选模型目录（环境变量 > 默认目录 > models 下任意模型）。"""
    out = []  # type: List[str]
    env = os.environ.get(MODEL_ENV, "").strip()
    if env:
        out.append(env)
    out.append(DEFAULT_MODEL_DIR)
    models_root = os.path.join(_HERE, "models")
    try:
        for name in sorted(os.listdir(models_root)):
            path = os.path.join(models_root, name)
            if os.path.isdir(path):
                out.append(path)
    except OSError:
        pass

    seen = set()             # type: set
    uniq = []                # type: List[str]
    for path in out:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            uniq.append(path)
    return uniq


def _model_candidates() -> List[str]:
    return [p for p in _candidate_dirs() if _is_model_dir(p)]


def _vosk_installed() -> bool:
    try:
        import vosk  # noqa: F401
        return True
    except Exception:        # noqa: BLE001 - 缺库/二进制不匹配都算不可用
        return False


def _ensure_model():
    """加载并缓存 Vosk 模型；不可用时抛 AsrUnavailable（带中文修复提示）。"""
    global _model, _model_dir, _model_error
    with _lock:
        if _model is not None:
            return _model
        if not _vosk_installed():
            _model_error = (
                "未安装 vosk。请在电脑上执行："
                "pip install -r server/requirements-voice.txt"
            )
            raise AsrUnavailable(_model_error)

        candidates = _model_candidates()
        if not candidates:
            _model_error = (
                "未找到中文语音模型（server/models/ 下没有含 am/ 的目录）。"
                "请运行：python server/download_voice_model.py"
            )
            raise AsrUnavailable(_model_error)

        import vosk

        try:
            vosk.SetLogLevel(-1)      # 关掉解码器的逐帧日志，避免刷屏
        except Exception:             # noqa: BLE001 - 老版本没有这个函数
            pass

        path = candidates[0]
        try:
            model = vosk.Model(path)
        except Exception as exc:      # noqa: BLE001 - 模型损坏/版本不匹配
            _model_error = "Vosk 模型加载失败（%s）：%s" % (path, exc)
            raise AsrUnavailable(_model_error) from exc

        _model = model
        _model_dir = path
        _model_error = ""
        return model


# ---------------------------------------------------------------------------
# 对外状态
# ---------------------------------------------------------------------------
def status() -> Dict[str, Any]:
    """给 Web 页面看的引擎状态：是否可用、模型在哪里、缺什么。"""
    installed = _vosk_installed()
    candidates = _model_candidates()
    loaded = _model is not None
    info = {
        "engine": "vosk",
        "target_rate": TARGET_RATE,
        "vosk_installed": installed,
        "model_present": bool(candidates),
        "model_dir": (_model_dir or (candidates[0] if candidates else "")),
        "loaded": loaded,
        "ready": bool(installed and candidates),
        "detail": "",
    }
    if loaded:
        info["detail"] = "模型已加载，可直接识别"
    elif not installed:
        info["detail"] = ("未安装 vosk：pip install -r server/requirements-voice.txt"
                          "（无 Key、纯离线）")
    elif not candidates:
        info["detail"] = ("缺少中文模型：python server/download_voice_model.py"
                          "（约 42 MB，或手动解压到 server/models/）")
    else:
        info["detail"] = "模型就绪，首次识别时加载（约 1–2 秒）"
    return info


def engine_name() -> str:
    return "vosk" if _vosk_installed() else "none"


# ---------------------------------------------------------------------------
# 音频解码 / 重采样
# ---------------------------------------------------------------------------
def _downmix_stereo(frames: bytes) -> bytes:
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    mono = array.array("h")
    for i in range(0, len(samples) - 1, 2):
        mono.append(int((samples[i] + samples[i + 1]) / 2))
    if sys.byteorder == "big":
        mono.byteswap()
    return mono.tobytes()


def _decode_wav(raw: bytes) -> Tuple[bytes, int]:
    try:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError, ValueError) as exc:
        raise BadAudio("WAV 解析失败：%s" % exc)

    if width != 2:
        raise BadAudio("只支持 16-bit PCM，当前位宽 %d bit" % (width * 8))
    if channels == 2:
        frames = _downmix_stereo(frames)
    elif channels != 1:
        raise BadAudio("只支持单声道或双声道，当前 %d 声道" % channels)
    return frames, int(rate)


def decode(raw: bytes, sample_rate: int = TARGET_RATE) -> Tuple[bytes, int]:
    """把上传体归一成 (PCM16LE 单声道字节, 采样率)。

    * 以 ``RIFF....WAVE`` 开头的按 WAV 解析，采样率取文件头；
    * 其余按裸 PCM 处理，采样率取参数（默认 16 kHz）。
    """
    if not raw or len(raw) < MIN_RAW_BYTES:
        raise BadAudio("音频太短（%d 字节）" % len(raw or b""))
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        pcm, rate = _decode_wav(raw)
        if rate < MIN_SAMPLE_RATE or rate > MAX_SAMPLE_RATE:
            raise BadAudio("不支持的采样率 %d Hz" % rate)
        return pcm, rate

    try:
        rate = int(sample_rate or TARGET_RATE)
    except (TypeError, ValueError):
        raise BadAudio("sample_rate 不是整数")
    if rate < MIN_SAMPLE_RATE or rate > MAX_SAMPLE_RATE:
        raise BadAudio("不支持的采样率 %d Hz（允许 %d–%d）"
                       % (rate, MIN_SAMPLE_RATE, MAX_SAMPLE_RATE))
    if len(raw) % 2:
        raw = raw[:-1]                     # 丢掉半截采样，避免 array 解析报错
    return raw, rate


def resample(pcm: bytes, rate: int) -> bytes:
    """重采样到 16 kHz。优先 audioop（C 实现），没有就退回线性插值。"""
    if rate == TARGET_RATE or not pcm:
        return pcm
    try:
        import audioop                   # Python 3.13 起移除，3.9–3.12 都在
        converted, _ = audioop.ratecv(pcm, 2, 1, rate, TARGET_RATE, None)
        return converted
    except Exception:                    # noqa: BLE001 - 没有 audioop 时走纯 Python
        pass

    src = array.array("h")
    src.frombytes(pcm)
    if sys.byteorder == "big":
        src.byteswap()
    if not len(src):
        return b""
    out_len = max(1, int(len(src) * TARGET_RATE / float(rate)))
    out = array.array("h", bytes(2 * out_len))
    ratio = rate / float(TARGET_RATE)
    last = len(src) - 1
    for i in range(out_len):
        pos = i * ratio
        i0 = int(pos)
        if i0 > last:
            i0 = last
        i1 = i0 + 1 if i0 < last else i0
        frac = pos - i0
        out[i] = int(src[i0] + (src[i1] - src[i0]) * frac)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def to_wav(pcm: bytes, rate: int) -> bytes:
    """把裸 PCM 包成标准 WAV（落盘/回放用）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(pcm)
    return buf.getvalue()


def duration_ms(pcm: bytes, rate: int) -> int:
    if rate <= 0:
        return 0
    return int(len(pcm) * 1000 / (rate * 2))


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
def transcribe(pcm: bytes, rate: int = TARGET_RATE) -> str:
    """把 PCM 转成文字；模型不可用时抛 AsrUnavailable。"""
    model = _ensure_model()
    import vosk

    if rate != TARGET_RATE:
        pcm = resample(pcm, rate)

    recognizer = vosk.KaldiRecognizer(model, TARGET_RATE)
    # 5 s / 8 KB 一块：既能及时消化，也不会产生过多 Python 调用
    step = 8000
    for offset in range(0, len(pcm), step):
        recognizer.AcceptWaveform(pcm[offset:offset + step])
    result = json.loads(recognizer.FinalResult() or "{}")
    text = (result.get("text") or "").strip()
    # Vosk 中文结果按词用空格分隔，去掉空格才是自然句子
    return re.sub(r"\s+", "", text)


def analyze(raw: bytes, sample_rate: int = TARGET_RATE) -> Dict[str, Any]:
    """完整流程：解码 → 校验时长 → 转写。返回可直接入库/回给页面的字典。

    不落盘、不碰数据库，方便单测（测试里 monkeypatch ``transcribe`` 即可）。
    """
    pcm, rate = decode(raw, sample_rate)
    ms = duration_ms(pcm, rate)
    wav = to_wav(pcm, rate)
    out = {
        "wav": wav,
        "sample_rate": rate,
        "duration_ms": ms,
        "text": "",
        "status": "ok",
        "engine": engine_name(),
        "error": "",
        "hint": "",
    }  # type: Dict[str, Any]

    if ms < MIN_AUDIO_MS:
        out["status"] = "too_short"
        out["error"] = "录音只有 %d ms" % ms
        out["hint"] = "按住/触发后至少说 1 秒；确认麦克风没有被静音。"
        return out
    if ms > MAX_AUDIO_MS:
        out["status"] = "too_long"
        out["error"] = "录音 %d ms，超过 %d ms 上限" % (ms, MAX_AUDIO_MS)
        out["hint"] = "缩短录音时长后重试。"
        return out

    try:
        text = transcribe(pcm, rate)
    except AsrUnavailable as exc:
        out["status"] = "asr_unavailable"
        out["error"] = str(exc)
        out["hint"] = "服务端已保存音频；按提示安装 vosk 并下载模型后重试。"
        return out
    except Exception as exc:             # noqa: BLE001 - 解码器内部异常也要落一条记录
        out["status"] = "asr_error"
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        out["hint"] = "音频已保存，便于复现；可尝试更短的录音或更换模型。"
        return out

    out["text"] = text
    if not text:
        out["status"] = "no_speech"
        out["hint"] = ("没有识别到文字：靠近板载麦克风、用普通话短句，"
                       "或在安静环境重试。")
    return out


def describe_error() -> str:
    """最近一次模型加载失败的原因（排障用）。"""
    return _model_error
