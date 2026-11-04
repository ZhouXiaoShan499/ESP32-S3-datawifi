# -*- coding: utf-8 -*-
"""画面可用性 + 场景分档：把「这张照片能不能用」变成结构化结果。

模型是 `train/train_vision.py` 训出来的 MobileNetV3-Small 双头网络：
  · head A（保底任务）：clear / blocked / degraded 三分类
  · head B（拓展任务）：indoor / outdoor 二分类（blocked 样本无场景语义）

推理后端按可用性降级：
  1. ONNX（onnxruntime）—— 若同目录有 .onnx 且能加载
  2. TorchScript（torch.jit）—— 训练的默认交付形态
  3. 都不可用 → `VisionUnavailable`，调用方把 reason 写进 vision_results.error，
     照片本身照常入库（推理绝不能拖垮采集主链路）

**懒加载**：模块导入时不碰 torch / onnxruntime，第一次真要推理才建 session。
torch 的 import 要 2 秒左右，放在服务启动路径上会拖慢首次可用时间。
"""
import importlib.util
import io
import json
import os
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER_DIR = os.path.dirname(_HERE)

MODEL_DIR = os.environ.get("VISION_MODEL_DIR", os.path.join(_SERVER_DIR, "models", "vision"))
VISION_ENABLED = os.environ.get("VISION_ENABLED", "1") not in ("0", "false", "False", "")

# 与训练脚本保持一致的预处理参数；labels.json 里的值是唯一口径，这里只做兜底。
_DEFAULT = {
    "labels": ["clear", "blocked", "degraded"],
    "scene_labels": ["indoor", "outdoor"],
    "input_size": 224,
    "mean": [0.485, 0.456, 0.406],
    "std": [0.229, 0.224, 0.225],
    "thresholds": {"ok": 0.75, "low": 0.55},
    "model_version": "unversioned",
    "runtime": {},
}

_lock = threading.Lock()
_state = {"loaded": False, "backend": None, "model": None, "spec": dict(_DEFAULT),
          "error": None}


class VisionUnavailable(RuntimeError):
    """模型不可用（文件缺失 / 依赖没装 / 加载失败）。"""


def _read_spec():
    """读 labels.json；缺失就用内置默认值，但不谎报版本号。"""
    spec = dict(_DEFAULT)
    path = os.path.join(MODEL_DIR, "labels.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                spec.update(json.load(f))
        except Exception as e:                      # noqa: BLE001
            spec["_spec_error"] = f"{type(e).__name__}: {e}"
    return spec


def _load():
    """建 session。调用方必须持锁。"""
    spec = _read_spec()
    runtime = spec.get("runtime") or {}
    ts_name = runtime.get("file") or spec.get("onnx") or ""
    onnx_name = spec.get("onnx") or ""

    onnx_path = os.path.join(MODEL_DIR, onnx_name) if onnx_name else ""
    ts_path = os.path.join(MODEL_DIR, ts_name) if ts_name else ""

    if onnx_path and os.path.isfile(onnx_path):
        try:
            import onnxruntime as ort
            sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
            _state.update(backend="onnx", model=sess, spec=spec)
            return
        except Exception as e:                      # noqa: BLE001 - 回退 TorchScript
            spec["_onnx_error"] = f"{type(e).__name__}: {e}"

    if ts_path and os.path.isfile(ts_path):
        try:
            import torch
            module = torch.jit.load(ts_path, map_location="cpu")
            module.eval()
            _state.update(backend="torchscript", model=module, spec=spec)
            return
        except Exception as e:                      # noqa: BLE001
            raise VisionUnavailable(f"TorchScript 加载失败: {type(e).__name__}: {e}")

    raise VisionUnavailable(
        f"模型文件缺失（找过 {os.path.basename(ts_path) or '—'} / "
        f"{os.path.basename(onnx_path) or '—'}，目录 {MODEL_DIR}）")


def _ensure_loaded():
    if _state["loaded"]:
        if _state["error"]:
            raise VisionUnavailable(_state["error"])
        return
    with _lock:
        if _state["loaded"]:
            if _state["error"]:
                raise VisionUnavailable(_state["error"])
            return
        try:
            _load()
            _state["error"] = None
        except VisionUnavailable as e:
            _state["error"] = str(e)
            raise
        except Exception as e:                      # noqa: BLE001
            _state["error"] = f"{type(e).__name__}: {e}"
            raise VisionUnavailable(_state["error"])
        finally:
            _state["loaded"] = True


def preprocess(jpeg_bytes, input_size=None, mean=None, std=None):
    """JPEG 字节 → NCHW float32。

    与训练侧 `_resize_short_side(size+32) → center_crop(size)` 完全一致 —— 训练
    和推理的预处理差一个像素的缩放比例，分数分布就会漂，阈值也就不作数了。
    """
    import numpy as np
    from PIL import Image

    size = int(input_size or _state["spec"].get("input_size") or 224)
    mean = np.array(mean or _state["spec"].get("mean"), dtype=np.float32)
    std = np.array(std or _state["spec"].get("std"), dtype=np.float32)

    with Image.open(io.BytesIO(jpeg_bytes)) as im:
        img = im.convert("RGB")
        w, h = img.size
        scale = (size + 32) / float(min(w, h))
        img = img.resize((max(size, int(round(w * scale))), max(size, int(round(h * scale)))),
                         Image.BILINEAR)
        w, h = img.size
        left, top = (w - size) // 2, (h - size) // 2
        img = img.crop((left, top, left + size, top + size))
        arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = (arr - mean) / std
    return np.transpose(arr, (2, 0, 1))[None, :, :, :].copy()


def _run(model, backend, x):
    if backend == "onnx":
        out = model.run(None, {model.get_inputs()[0].name: x})
        return out[0][0], out[1][0]
    import torch
    with torch.no_grad():
        a, s = model(torch.from_numpy(x))
    return a[0].numpy(), s[0].numpy()


def predict(jpeg_bytes):
    """推理单张照片，返回结构化结果；不可用时抛 VisionUnavailable。"""
    from .decision import decide

    _ensure_loaded()
    spec = _state["spec"]
    t0 = time.time()
    x = preprocess(jpeg_bytes)
    prob_a, prob_s = _run(_state["model"], _state["backend"], x)
    labels = spec.get("labels") or _DEFAULT["labels"]
    scene_labels = spec.get("scene_labels") or _DEFAULT["scene_labels"]
    thresholds = spec.get("thresholds") or _DEFAULT["thresholds"]

    idx = int(prob_a.argmax())
    label = labels[idx]
    score = float(prob_a[idx])
    decision = decide(score, thresholds)

    scene_idx = int(prob_s.argmax())
    return {
        "label": label if decision != "rejected" else None,
        "score": round(score, 4),
        "decision": decision,
        "scene_label": scene_labels[scene_idx] if decision != "rejected" else None,
        "scene_score": round(float(prob_s[scene_idx]), 4),
        "probabilities": {labels[i]: round(float(prob_a[i]), 4) for i in range(len(labels))},
        "model_name": spec.get("model_name", "vision"),
        "model_version": spec.get("model_version") or _DEFAULT["model_version"],
        "backend": _state["backend"],
        "latency_ms": int(round((time.time() - t0) * 1000)),
    }


def status():
    """给 /api/v1/vision/status 与前端用的自检信息（不会因此加载模型）。"""
    spec = _state["spec"] if _state["loaded"] else _read_spec()
    runtime = spec.get("runtime") or {}
    ts_name = runtime.get("file") or ""
    onnx_name = spec.get("onnx") or ""
    files = {
        "torchscript": os.path.join(MODEL_DIR, ts_name) if ts_name else None,
        "onnx": os.path.join(MODEL_DIR, onnx_name) if onnx_name else None,
    }
    present = {k: bool(v and os.path.isfile(v)) for k, v in files.items()}
    # 模型文件在 ≠ 能推理：没装 torch / onnxruntime 时 ready 必须是 false，
    # 否则页面显示「模型就绪」，然后每张照片都推理失败。find_spec 只查有没有装，
    # 不真的 import —— 和懒加载的前提一致（import torch 要 2 秒）。
    deps = {
        "torch": importlib.util.find_spec("torch") is not None,
        "onnxruntime": importlib.util.find_spec("onnxruntime") is not None,
    }
    runnable = (present["torchscript"] and deps["torch"]) or (present["onnx"] and deps["onnxruntime"])
    # 不可用时必须说清「缺什么」——页面直接显示这句，比只说 ready=false 有用得多
    if not VISION_ENABLED:
        reason = "推理已关闭（VISION_ENABLED=0）"
    elif not (present["torchscript"] or present["onnx"]):
        reason = "模型文件缺失：%s" % MODEL_DIR
    elif not runnable:
        reason = "缺少运行时依赖（torch / onnxruntime），pip install -r requirements-vision.txt"
    else:
        reason = None
    return {
        "enabled": VISION_ENABLED,
        "model_dir": MODEL_DIR,
        "loaded": _state["loaded"],
        "backend": _state["backend"],
        "model_name": spec.get("model_name"),
        "model_version": spec.get("model_version"),
        "labels": spec.get("labels"),
        "scene_labels": spec.get("scene_labels"),
        "thresholds": spec.get("thresholds"),
        "files_present": present,
        "deps": deps,
        "ready": VISION_ENABLED and runnable,
        "reason": reason,
        "error": _state["error"],
    }


def reset_for_tests():
    """自测用：把懒加载状态清回未初始化。"""
    with _lock:
        _state.update({"loaded": False, "backend": None, "model": None,
                       "spec": dict(_DEFAULT), "error": None})
