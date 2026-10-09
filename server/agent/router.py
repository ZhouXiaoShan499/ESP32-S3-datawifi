# -*- coding: utf-8 -*-
"""意图路由器 —— 将 NLU 解析结果分发到对应工具。"""
import logging
from typing import Dict, Any, Optional

from .tools.base import ToolResult, ToolResultStatus
from .tools.query_history import (
    QueryHistoryTool, QueryLatestTool, QueryDevicesTool, QueryHealthTool)
from .tools.capture import StartCaptureTool
from .tools.photo import TakePhotoTool
from .tools.task import QueryTasksTool, PausePeriodicTool, ResumePeriodicTool
from .tools.events import QueryEventsTool

logger = logging.getLogger("agent.router")

# 意图 → 工具映射
INTENT_TOOL_MAP: Dict[str, Any] = {
    "query_history": QueryHistoryTool,
    "query_latest": QueryLatestTool,
    "query_devices": QueryDevicesTool,
    "query_health": QueryHealthTool,
    "start_capture": StartCaptureTool,
    "take_photo": TakePhotoTool,
    "query_tasks": QueryTasksTool,
    "pause_periodic": PausePeriodicTool,
    "resume_periodic": ResumePeriodicTool,
    "query_events": QueryEventsTool,
}


class IntentRouter:
    """意图路由器。"""

    def __init__(self):
        self._tools: Dict[str, Any] = {}

    def _get_tool(self, intent: str):
        """惰性创建工具实例。"""
        if intent not in self._tools:
            cls = INTENT_TOOL_MAP.get(intent)
            if cls is None:
                return None
            self._tools[intent] = cls()
        return self._tools[intent]

    def route(self, parsed: Dict[str, Any],
              context: Optional[Dict[str, Any]] = None) -> ToolResult:
        """执行意图路由并运行工具。"""
        from .config import CONFIDENCE_THRESHOLD
        intent = parsed.get("intent", "")

        # 歧义处理
        if parsed.get("ambiguous"):
            return ToolResult(
                status=ToolResultStatus.AMBIGUOUS,
                message=parsed.get("clarification_message", "请明确您的意图。"),
                ambiguous_options=parsed.get("clarification_options", []))

        # 低置信度追问
        conf = parsed.get("confidence", 1.0)
        if conf < CONFIDENCE_THRESHOLD and parsed.get("clarification_message"):
            opts = parsed.get("clarification_options", [])
            return ToolResult(
                status=ToolResultStatus.AMBIGUOUS,
                message=parsed.get("clarification_message",
                                   f"您的意图不太明确（置信度 {conf:.0%}），请确认。"),
                ambiguous_options=opts)

        # 工具分发
        tool = self._get_tool(intent)
        if tool is None:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error=f"unknown_intent: {intent}",
                message=f"暂不支持的操作: {intent}。试试说\"查看最新数据\"或\"发起采集\"。")

        # 上下文补齐 device_id
        params = dict(parsed.get("parameters", {}))
        if context:
            last_did = context.get("last_device_id")
            if last_did and not params.get("device_id"):
                params["device_id"] = last_did

        logger.info("Route intent=%s tool=%s params=%s",
                    intent, tool.name, params)
        result = tool.run(params)
        return result


# 全局单例
_router: Optional[IntentRouter] = None


def get_router() -> IntentRouter:
    global _router
    if _router is None:
        _router = IntentRouter()
    return _router