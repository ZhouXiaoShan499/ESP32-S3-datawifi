# -*- coding: utf-8 -*-
"""工具基类 —— 所有 AI 工具必须继承并实现抽象方法。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ToolResultStatus(Enum):
    """工具执行结果状态。

    SUCCESS      = 成功（有设备真实证据）
    FAILURE      = 失败（参数错误、权限不足等）
    AMBIGUOUS    = 歧义（需要追问用户再做决定）
    NO_EVIDENCE  = 没有设备返回的真实完成证据（防幻觉）
    NOT_FOUND    = 资源不存在（查询无数据等）
    CANCELLED    = 用户已取消（保留设备任务号便于追溯）
    """
    SUCCESS = "success"
    FAILURE = "failure"
    AMBIGUOUS = "ambiguous"
    NO_EVIDENCE = "no_evidence"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"


@dataclass
class ToolResult:
    """工具执行结果。

    核心字段：
      status: 执行状态
      data:   实际数据（查询结果、任务摘要等）
      error:  错误信息（失败时）
      evidence: 设备返回的真实证据（成功时必须带）
      message: 给用户看的自然语言消息
    """
    status: ToolResultStatus
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    message: str = ""
    ambiguous_options: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "data": self.data,
            "error": self.error,
            "evidence": self.evidence,
            "message": self.message,
            "ambiguous_options": self.ambiguous_options,
        }


class BaseTool(ABC):
    """工具抽象基类。

    子类必须实现：
      - validate_params(params) -> (bool, str|None)
      - execute(params) -> ToolResult

    可选覆盖：
      - check_permission(device_id) -> bool   —— 权限校验（默认检查白名单）
    """

    name: str = ""
    description: str = ""

    def check_permission(self, device_id: str) -> bool:
        """权限校验 —— 检查设备是否在白名单内。"""
        if not device_id:
            return False
        from ..config import ALLOWED_DEVICES
        return device_id in ALLOWED_DEVICES

    @abstractmethod
    def validate_params(self, params: Dict[str, Any]) -> tuple:
        """参数校验。

        Returns:
            (bool, str|None): (是否有效, 错误信息)
        """
        ...

    @abstractmethod
    def execute(self, params: Dict[str, Any]) -> ToolResult:
        """执行工具逻辑。

        Args:
            params: 已校验的参数

        Returns:
            ToolResult: 执行结果
        """
        ...

    def run(self, params: Dict[str, Any]) -> ToolResult:
        """完整执行流程：校验 → 执行。"""
        valid, error = self.validate_params(params)
        if not valid:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error=error,
                message=f"参数校验失败: {error}",
            )
        return self.execute(params)


# ---------------------------------------------------------------------------
# 辅助函数：构建证据对象
# ---------------------------------------------------------------------------
def make_evidence(device_id=None, source=None, ts_ms=None, received_at=None,
                  **extra) -> Dict[str, Any]:
    """构建设备证据对象。

    证据是防止 AI 幻觉的关键 —— 只有设备真实返回的数据才能放入 evidence。
    每个证据字段都描述了数据的物理来源。
    """
    import time
    evidence = {
        "device_id": device_id,
        "source": source or "qma6100p",
        "collected_at": ts_ms,
        "received_at": received_at or time.time(),
        **extra,
    }
    # 去掉 None 值
    return {k: v for k, v in evidence.items() if v is not None}


def format_time_ago(ts):
    """将时间戳转为人类可读的"X 分钟前"格式。"""
    import time
    if ts is None:
        return "未知时间"
    now = time.time()
    delta = now - ts
    if delta < 60:
        return f"{int(delta)} 秒前"
    elif delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    elif delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    else:
        return f"{int(delta // 86400)} 天前"


def cancellation_requested(params: Dict[str, Any]) -> bool:
    event = params.get("_cancel_event")
    return bool(event and event.is_set())


def cancelled_result(agent_request_id: str = "") -> ToolResult:
    """统一的取消结果：不携带 evidence，避免被当成设备执行成功。"""
    return ToolResult(
        status=ToolResultStatus.CANCELLED,
        error="cancelled",
        data={"cancelled": True,
              "agent_request_id": agent_request_id or None},
        message="任务已取消，未继续等待设备回执。",
    )


def mark_device_task_cancelled(main, task_request_id: str):
    """把尚未结束的设备任务标记为失败，阻止队列继续领取。"""
    if not task_request_id:
        return
    import time
    conn = main._connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='failed', completed_at=?,"
            " error='cancelled by user'"
            " WHERE request_id=? AND status NOT IN"
            " ('completed','failed','timeout')",
            (time.time(), task_request_id),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
