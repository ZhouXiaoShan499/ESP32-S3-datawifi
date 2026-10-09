# -*- coding: utf-8 -*-
"""AI 工具集合 —— 将 ESP32-S3-EYE 硬件接口封装为受限工具。

每个工具都必须：
  1. 校验传入参数
  2. 限定可操作设备范围（权限）
  3. 处理歧义场景
  4. 返回结果必须带数据来源、采集时间、设备状态
  5. 没有设备真实完成证据，不能返回"成功"
"""

from .base import BaseTool, ToolResult, ToolResultStatus
from .query_history import QueryHistoryTool, QueryLatestTool, QueryDevicesTool, QueryHealthTool
from .capture import StartCaptureTool
from .photo import TakePhotoTool
from .task import QueryTasksTool, PausePeriodicTool, ResumePeriodicTool
from .events import QueryEventsTool

__all__ = [
    "BaseTool",
    "ToolResult",
    "ToolResultStatus",
    "QueryHistoryTool",
    "QueryLatestTool",
    "QueryDevicesTool",
    "QueryHealthTool",
    "StartCaptureTool",
    "TakePhotoTool",
    "QueryTasksTool",
    "PausePeriodicTool",
    "ResumePeriodicTool",
    "QueryEventsTool",
]