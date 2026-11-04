# -*- coding: utf-8 -*-
"""视觉推理包：照片 → 结构化结果 → 可理解反馈。

模块分工：
  engine.py    模型加载（TorchScript / ONNX）+ 预处理 + 推理
  decision.py  分数 → ok/low/rejected 三档 + 面向用户的一句话
"""
from .engine import (  # noqa: F401
    MODEL_DIR, VisionUnavailable, VISION_ENABLED, predict, preprocess,
    reset_for_tests, status,
)
from .decision import DECISIONS, decide, describe  # noqa: F401

__all__ = [
    "MODEL_DIR", "VISION_ENABLED", "VisionUnavailable", "predict", "preprocess",
    "status", "reset_for_tests", "DECISIONS", "decide", "describe",
]
