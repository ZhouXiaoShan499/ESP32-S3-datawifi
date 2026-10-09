# -*- coding: utf-8 -*-
"""任务管理工具 —— 查询任务、暂停/恢复周期上报。"""
import time, uuid
from typing import Dict, Any
from .base import BaseTool, ToolResult, ToolResultStatus, make_evidence, format_time_ago


class QueryTasksTool(BaseTool):
    name = "query_tasks"
    description = "查询任务列表及状态"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        did = params.get("device_id", "").strip() if params.get("device_id") else ""
        if did and not self.check_permission(did):
            return False, f"设备 {did} 不在授权范围内"
        try: limit = int(params.get("limit", 10))
        except (TypeError, ValueError): limit = 10
        if limit < 1 or limit > 100:
            return False, "limit 范围 1-100"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        conn = main._connect()
        did = params.get("device_id", "").strip() or None
        try: limit = int(params.get("limit", 10))
        except (TypeError, ValueError): limit = 10
        try:
            main._expire_stale_tasks(conn, did)
            if did:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE device_id=?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (did, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,)).fetchall()
            conn.commit()
        finally:
            conn.close()
        if not rows:
            return ToolResult(status=ToolResultStatus.NOT_FOUND,
                              message="暂无任务记录。")
        tasks = []
        for r in rows:
            t = dict(r)
            t["status_cn"] = main.TASK_STATUS_CN.get(
                t.get("status"), t.get("status"))
            t["ago"] = format_time_ago(t.get("created_at"))
            tasks.append(t)
        evidence = make_evidence(device_id=did, count=len(tasks))
        lines = [f"共 {len(tasks)} 个任务:"]
        for t in tasks[:10]:
            lines.append(
                f"- [{t['status_cn']}] {t.get('kind','capture')}"
                f" · 设备 {t['device_id']} · {t.get('ago','')}")
        return ToolResult(status=ToolResultStatus.SUCCESS,
                          data={"tasks": tasks, "count": len(tasks)},
                          evidence=evidence, message="\n".join(lines))


def _submit_control_task(main, did, kind, duration_s):
    """提交一条控制任务（pause / resume）并等板端确认「已生效」。

    控制任务不产生观测数据，收尾靠板端 POST /api/v1/tasks/{id}/applied
    （同一事务里把 device_control 的 request_id 写成这条任务）。所以口径与
    capture / photo 完全一致：
      * 任务只是「被接受」（含连点复用已有任务）→ NO_EVIDENCE，evidence 必须为 None；
      * 只有板端确认完成、且 device_control 的 request_id 指向本任务时才 SUCCESS，
        evidence 全部取自这次设备回执（device_id / source / updated_at / 状态）。
    历史实现不等回执就返回 SUCCESS 并自造 evidence —— 那正是本模块防幻觉约束要
    禁止的行为（工具不能给自己发证据）。
    """
    from ..config import CONTROL_POLL_TIMEOUT_S, CONTROL_POLL_INTERVAL_S
    kind_cn = "暂停" if kind == "pause" else "恢复"
    payload = {"device_id": did, "kind": kind}
    if duration_s is not None:
        payload["duration_s"] = duration_s
    ok, result = main.validate_task_request(payload)
    if not ok:
        return ToolResult(status=ToolResultStatus.FAILURE,
                          error=result, message=f"参数错误: {result}")

    conn = main._connect()
    try:
        main._expire_stale_tasks(conn, result["device_id"])
        existing = conn.execute(
            "SELECT * FROM tasks WHERE device_id=? AND source=? AND kind=?"
            " AND status NOT IN ('completed','failed','timeout')"
            " ORDER BY created_at ASC LIMIT 1",
            (result["device_id"], result["source"], result["kind"]),
        ).fetchone()
        if existing is not None:
            conn.commit()
            task = main._task_view(existing)
            # 同 capture.py：任务被接受 ≠ 设备已按它生效，不能算成功
            return ToolResult(
                status=ToolResultStatus.NO_EVIDENCE,
                data={"task": task, "duplicate": True, "task_accepted": True},
                message=(f"设备 {did} 已有待执行的{kind_cn}任务"
                         f"（状态 {task.get('status')}），本次未重复创建；"
                         f"该任务尚未被设备确认生效，还不能算{kind_cn}完成。"))
        request_id = uuid.uuid4().hex
        now = time.time()
        conn.execute(
            "INSERT INTO tasks (request_id, device_id, source, unit,"
            " sample_rate_hz, sample_count, trigger, kind, duration_s,"
            " status, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, result["device_id"], result["source"],
             result["unit"], result["sample_rate_hz"],
             result["sample_count"], "control", result["kind"],
             result["duration_s"], "submitted", now,
             now + result["timeout_s"]))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return ToolResult(status=ToolResultStatus.FAILURE,
                          error=str(e), message=f"创建失败: {e}")
    finally:
        try: conn.close()
        except Exception: pass

    # 轮询等板端回执（防幻觉核心：没有回执就没有 success）
    poll_start = time.time()
    last_status = "submitted"
    while (time.time() - poll_start) < CONTROL_POLL_TIMEOUT_S:
        time.sleep(CONTROL_POLL_INTERVAL_S)
        conn = main._connect()
        try:
            main._expire_stale_tasks(conn)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM tasks WHERE request_id=?",
                (request_id,)).fetchone()
            if row is None:
                continue
            task = main._task_view(row)
            last_status = task.get("status", last_status)

            if last_status == "completed":
                ctrl = conn.execute(
                    "SELECT * FROM device_control WHERE device_id=? AND source=?",
                    (result["device_id"], result["source"])).fetchone()
                ctrl_view = dict(ctrl) if ctrl else None
                conn.close()
                # 证据必须来自设备回执：/applied 会把 device_control 的 request_id
                # 写成这条任务。若真相源没指向本任务（已被更新的控制任务覆盖等），
                # 就不能据此断言「现在处于暂停 / 已恢复」。
                if ctrl_view is None or ctrl_view.get("request_id") != request_id:
                    return ToolResult(
                        status=ToolResultStatus.NO_EVIDENCE,
                        data={"task": task, "task_accepted": True,
                              "last_status": last_status},
                        message=(f"{kind_cn}任务已被设备 {did} 确认完成，"
                                 f"但控制状态已被更新的任务覆盖，"
                                 f"无法据此断定当前周期上报状态。"))
                elapsed = round(time.time() - poll_start, 1)
                evidence = make_evidence(
                    device_id=result["device_id"],
                    source=result["source"],
                    received_at=ctrl_view.get("updated_at"),
                    status="completed",
                    task_request_id=request_id,
                    kind=kind,
                    periodic_paused=bool(ctrl_view.get("periodic_paused")),
                    paused_until=ctrl_view.get("paused_until"))
                if kind == "pause":
                    message = (
                        f"{kind_cn}已生效（设备 {did} 已确认）。\n"
                        f"- 暂停时长: {result['duration_s']}s\n"
                        f"- 预计自动恢复: "
                        f"{main._fmt_time(ctrl_view.get('paused_until'))}\n"
                        f"- 任务耗时: {elapsed}s")
                else:
                    message = (f"{kind_cn}已生效（设备 {did} 已确认）。\n"
                               f"- 周期上报已恢复\n"
                               f"- 任务耗时: {elapsed}s")
                return ToolResult(
                    status=ToolResultStatus.SUCCESS,
                    data={"task": task, "control": ctrl_view,
                          "wait_s": elapsed},
                    evidence=evidence, message=message)
            if last_status in ("failed", "timeout"):
                err = task.get("error") or last_status
                conn.close()
                return ToolResult(
                    status=ToolResultStatus.FAILURE,
                    error=err,
                    message=f"{kind_cn}{last_status}: {err}")
        except Exception:
            try: conn.close()
            except Exception: pass

    # 超时 -> NO_EVIDENCE（同 capture/photo：只把「任务已被接受」放 data，不冒充证据）
    return ToolResult(
        status=ToolResultStatus.NO_EVIDENCE,
        error="timeout",
        data={"task_request_id": request_id, "task_accepted": True,
              "last_status": last_status,
              "poll_timeout_s": CONTROL_POLL_TIMEOUT_S},
        message=(f"{kind_cn}任务已提交，但设备 {did} 在 "
                 f"{CONTROL_POLL_TIMEOUT_S}s 内未确认生效"
                 f"（最后状态: {last_status}）。任务可能仍在进行中。"))


class PausePeriodicTool(BaseTool):
    name = "pause_periodic"
    description = "暂停周期上报"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        did = params.get("device_id", "").strip() if params.get("device_id") else ""
        if not did: return False, "device_id 是必填参数"
        if not self.check_permission(did):
            return False, f"设备 {did} 不在授权范围内"
        try: dur = int(params.get("duration_s", 120))
        except (TypeError, ValueError): dur = 120
        if dur < 5 or dur > 600:
            return False, "duration_s 范围 5-600"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        did = params.get("device_id", "").strip()
        dur = int(params.get("duration_s", 120))
        return _submit_control_task(main, did, "pause", dur)


class ResumePeriodicTool(BaseTool):
    name = "resume_periodic"
    description = "恢复周期上报"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        did = params.get("device_id", "").strip() if params.get("device_id") else ""
        if not did: return False, "device_id 是必填参数"
        if not self.check_permission(did):
            return False, f"设备 {did} 不在授权范围内"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        did = params.get("device_id", "").strip()
        return _submit_control_task(main, did, "resume", None)