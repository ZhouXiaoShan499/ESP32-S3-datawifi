# -*- coding: utf-8 -*-
"""采集工具 —— 必须有设备真实完成证据才返回成功。"""
import time, uuid
from typing import Dict, Any
from .base import BaseTool, ToolResult, ToolResultStatus, make_evidence


class StartCaptureTool(BaseTool):
    name = "start_capture"
    description = "发起一次新的 IMU 传感器数据采集任务"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        did = params.get("device_id", "").strip() if params.get("device_id") else ""
        if not did:
            return False, "device_id 是必填参数"
        if not self.check_permission(did):
            return False, f"设备 {did} 不在授权范围内"
        try:
            sc = int(params.get("sample_count", 100))
        except (TypeError, ValueError):
            sc = 100
        if sc < 1 or sc > 2000:
            return False, "sample_count 范围 1-2000"
        try:
            sr = int(params.get("sample_rate_hz", 100))
        except (TypeError, ValueError):
            sr = 100
        if sr < 10 or sr > 200:
            return False, "sample_rate_hz 范围 10-200"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        from ..config import CAPTURE_POLL_TIMEOUT_S, CAPTURE_POLL_INTERVAL_S
        main = _import_main()
        device_id = params.get("device_id", "").strip()
        sample_count = int(params.get("sample_count", 100))
        sample_rate_hz = int(params.get("sample_rate_hz", 100))
        payload = {"device_id": device_id, "kind": "capture",
                   "sample_count": sample_count, "sample_rate_hz": sample_rate_hz}
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
                # 「已有待执行任务」只是**任务被接受**，设备还没产出任何数据 ——
                # 这不算成功（否则用户点两下采集，第二次就会得到一个假的「采集完成」）。
                # 任务摘要放 data，evidence 只留给设备真实返回的数据。
                return ToolResult(
                    status=ToolResultStatus.NO_EVIDENCE,
                    data={"task": task, "duplicate": True,
                          "task_accepted": True},
                    message=(f"设备 {device_id} 已有待执行的采集任务"
                             f"（状态 {task.get('status')}），本次未重复创建；"
                             f"该任务尚未回传数据，还不能算采集完成。"))
            request_id = uuid.uuid4().hex
            now = time.time()
            conn.execute(
                "INSERT INTO tasks (request_id, device_id, source, unit,"
                " sample_rate_hz, sample_count, trigger, kind, duration_s,"
                " status, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, result["device_id"], result["source"],
                 result["unit"], result["sample_rate_hz"],
                 result["sample_count"], "manual", result["kind"],
                 result["duration_s"], "submitted", now,
                 now + result["timeout_s"]))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM tasks WHERE request_id=?",
                (request_id,)).fetchone()
            task = main._task_view(row)
        except Exception as e:
            conn.rollback()
            return ToolResult(status=ToolResultStatus.FAILURE,
                              error=str(e), message=f"任务创建失败: {e}")
        finally:
            try: conn.close()
            except Exception: pass

        # 轮询等待设备完成（防幻觉核心）
        poll_start = time.time()
        last_status = task.get("status", "submitted")
        while (time.time() - poll_start) < CAPTURE_POLL_TIMEOUT_S:
            time.sleep(CAPTURE_POLL_INTERVAL_S)
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
                    up = conn.execute(
                        "SELECT id, device_id, request_id, trigger,"
                        " sample_count, ts_ms, received_at, source"
                        " FROM uploads WHERE request_id=?"
                        " ORDER BY id DESC LIMIT 1",
                        (request_id,)).fetchone()
                    upload = dict(up) if up else None
                    conn.close()
                    sc = upload.get("sample_count", "?") if upload else "?"
                    ts_str = (main._fmt_epoch_ms(upload.get("ts_ms"))
                              if upload else "未知")
                    elapsed = round(time.time() - poll_start, 1)
                    evidence = make_evidence(
                        device_id=device_id,
                        task_request_id=request_id,
                        status="completed",
                        upload_id=upload.get("id") if upload else None,
                        sample_count=(upload.get("sample_count")
                                      if upload else None),
                        ts_ms=upload.get("ts_ms") if upload else None,
                        received_at=(upload.get("received_at")
                                     if upload else None),
                        source=upload.get("source") if upload else None)
                    return ToolResult(
                        status=ToolResultStatus.SUCCESS,
                        data={"task": task, "upload": upload,
                              "wait_s": elapsed},
                        evidence=evidence,
                        message=(f"采集完成！\n- 设备: {device_id}\n"
                                 f"- 样本数: {sc}\n"
                                 f"- 采样时间: {ts_str}\n"
                                 f"- 任务耗时: {elapsed}s"))
                elif last_status in ("failed", "timeout"):
                    conn.close()
                    err = task.get("error") or last_status
                    return ToolResult(
                        status=ToolResultStatus.FAILURE,
                        error=err,
                        message=f"采集{last_status}: {err}")
            except Exception:
                try: conn.close()
                except Exception: pass

        # 超时 -> NO_EVIDENCE
        # 关键：这里**不能**塞 evidence。evidence 的语义是「设备真实返回的数据」，
        # 而此刻只有一条我们自己刚创建的、还没被设备领取的任务记录。历史实现把
        # {status:'submitted', poll_timeout_s:3} 放进 evidence，于是 no_evidence
        # 的结果也带着一个非空 evidence —— 消费方（UI/LLM）极易读成「有关系/有证据」，
        # 恰好是防幻觉要防的那种假阳性。任务摘要改放 data，并显式标记 task_accepted。
        return ToolResult(
            status=ToolResultStatus.NO_EVIDENCE,
            error="timeout",
            data={"task": task, "task_accepted": True,
                  "last_status": last_status,
                  "poll_timeout_s": CAPTURE_POLL_TIMEOUT_S},
            message=(f"采集任务已提交，但设备 {device_id} 在 "
                     f"{CAPTURE_POLL_TIMEOUT_S}s 内未返回完成证据"
                     f"（最后状态: {last_status}）。"
                     f"任务可能仍在进行中。"))