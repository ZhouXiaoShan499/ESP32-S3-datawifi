# -*- coding: utf-8 -*-
"""把连续的模型分数翻译成三档结论 + 一句人话。

分档是**产品口径**，不是模型的一部分：阈值改了不需要重训模型，但改之前要清楚
它在换什么 ——

  ok        分数 ≥ ok_threshold：模型有把握，标签直接给用户看
  low       low_threshold ≤ 分数 < ok_threshold：'可能不清晰'，建议重拍
  rejected  分数 < low_threshold：拒识。**不给标签**，只报'未知'

为什么宁可拒识：`blocked` 和 `degraded` 在画面上本来就难分（一片黑既可能是手挡住，
也可能是曝光失败），硬答一个标签会让用户按错误原因去处理。拒识 + 建议重拍，
比 51% 的置信度更有用，也正好是「未知样例」这一类的来源。
"""
from typing import Dict

DECISIONS = ("ok", "low", "rejected")

# 面向用户的一句话描述。模板里不放分数，分数由页面/接口单独给，避免两处口径。
_LABEL_TEXT = {
    "clear": "画面可用",
    "blocked": "镜头被遮挡",
    "degraded": "画面不可用（模糊/过曝/欠曝）",
}

_ACTION_TEXT = {
    "ok": "无需处理",
    "low": "建议重新采集一张",
    "rejected": "模型无法判断，请重新采集或人工标注",
}


def decide(score: float, thresholds: Dict[str, float]) -> str:
    """分数 → 档位。阈值从 labels.json 读，缺项时用调用方给的兜底值。"""
    ok = float(thresholds.get("ok", 0.75))
    low = float(thresholds.get("low", 0.55))
    if score >= ok:
        return "ok"
    if score >= low:
        return "low"
    return "rejected"


def describe(decision: str, label, score: float) -> Dict[str, str]:
    """给页面和 AI Agent 共用的一句话结论。"""
    if decision == "rejected" or not label:
        return {
            "title": "未知",
            "text": f"模型无法判断这张画面（最高置信度 {score:.0%}），"
                    f"既不能确认可用，也不能确认被遮挡",
            "action": _ACTION_TEXT["rejected"],
        }
    title = _LABEL_TEXT.get(label, label)
    if decision == "low":
        return {
            "title": title,
            "text": f"模型倾向于「{title}」，但把握只有 {score:.0%}",
            "action": _ACTION_TEXT["low"],
        }
    return {
        "title": title,
        "text": f"模型判定「{title}」（置信度 {score:.0%}）",
        "action": _ACTION_TEXT["ok"],
    }
