# -*- coding: utf-8 -*-
"""拍照工具 —— 创建 camera 任务并等待照片上传。"""
import time, uuid
from typing import Dict, Any
from .base import BaseTool, ToolResult, ToolResultStatus, make_evidence


class TakePhotoTool(BaseTool):
    name = "take_photo"
    description = "拍摄一张照片"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        did = params.get("device_id", "").strip() if params.get("device_id") else ""
        if not did:
            return False, "device_id 是必填参数"
        if not self.check_permission(did):
            return False, f"设备 {did} 不在授权范围内"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        from ..config import PHOTO_POLL_TIMEOUT_S, PHOTO_POLL_INTERVAL_S
        main = _import_main()
        did = params.get("device_id", "").strip()

        payload = {"device_id": did, "kind": "camera"}
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
            if existing:
                conn.commit()
                task = main._task_view(existing)
                # 同 capture.py：任务被接受 ≠ 拍到了照片，不能算成功
                return ToolResult(
                    status=ToolResultStatus.NO_EVIDENCE,
                    data={"task": task, "duplicate": True,
                          "task_accepted": True},
                    message=(f"设备 {did} 已有待执行的拍照任务"
                             f"（状态 {task.get('status')}），本次未重复创建；"
                             f"该任务尚未回传照片，还不能算拍照完成。"))

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
        except Exception as e:
            conn.rollback()
            return ToolResult(status=ToolResultStatus.FAILURE,
                              error=str(e), message=f"创建失败: {e}")
        finally:
            try: conn.close()
            except Exception: pass

        # 轮询等待照片上传
        poll_start = time.time()
        last_status = "submitted"
        while (time.time() - poll_start) < PHOTO_POLL_TIMEOUT_S:
            time.sleep(PHOTO_POLL_INTERVAL_S)
            conn = main._connect()
            try:
                main._expire_stale_tasks(conn)
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM tasks WHERE request_id=?",
                    (request_id,)).fetchone()
                if not row:
                    continue
                task = main._task_view(row)
                last_status = task.get("status", last_status)

                if last_status == "completed":
                    ph = conn.execute(
                        "SELECT id, ts_ms, received_at, bytes, width,"
                        " height, note FROM photos WHERE request_id=?"
                        " ORDER BY id DESC LIMIT 1",
                        (request_id,)).fetchone()
                    photo = main._photo_view(ph) if ph else None
                    conn.close()
                    elapsed = round(time.time() - poll_start, 1)
                    return ToolResult(
                        status=ToolResultStatus.SUCCESS,
                        data={"task": task, "photo": photo, "wait_s": elapsed},
                        evidence=make_evidence(
                            device_id=did, status="completed",
                            photo_id=photo.get("id") if photo else None),
                        message=(f"拍照完成！设备: {did} · "
                                 f"{photo.get('size_kb','?')}KB · {elapsed}s"))
                elif last_status in ("failed", "timeout"):
                    conn.close()
                    return ToolResult(
                        status=ToolResultStatus.FAILURE,
                        error=last_status,
                        message=f"拍照{last_status}")
            except Exception:
                try: conn.close()
                except Exception: pass

        # 超时：同 capture.py —— 只在 data 里说明「任务已被接受」，evidence 保持为空，
        # 因为此刻设备还没有回传任何照片。
        return ToolResult(
            status=ToolResultStatus.NO_EVIDENCE,
            error="timeout",
            data={"task_request_id": request_id, "task_accepted": True,
                  "last_status": last_status,
                  "poll_timeout_s": PHOTO_POLL_TIMEOUT_S},
            message=(f"拍照任务已提交，但设备 {did} 在 "
                     f"{PHOTO_POLL_TIMEOUT_S}s 内未返回照片"
                     f"（最后状态: {last_status}）。"))