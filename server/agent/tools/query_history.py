# -*- coding: utf-8 -*-
"""历史数据查询工具"""
import time
from typing import Dict, Any
from .base import BaseTool, ToolResult, ToolResultStatus, make_evidence, format_time_ago


class QueryHistoryTool(BaseTool):
    name = "query_history"
    description = "查询历史 IMU 传感器采集数据"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        device_id = params.get("device_id", "").strip() if params.get("device_id") else ""
        if device_id and not self.check_permission(device_id):
            return False, f"设备 {device_id} 不在授权范围内"
        try:
            limit = int(params.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        if limit < 1 or limit > 200:
            return False, "limit 范围 1-200"
        trigger = params.get("trigger", "").strip() or None
        if trigger and trigger not in ("manual", "periodic"):
            return False, "trigger 必须是 manual 或 periodic"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        conn = main._connect()
        try:
            device_id = params.get("device_id", "").strip() or None
            limit = int(params.get("limit", 10))
            trigger = params.get("trigger", "").strip() or None
            sql = "SELECT * FROM uploads"
            conds, args = [], []
            if device_id:
                conds.append("device_id=?")
                args.append(device_id)
            if trigger:
                conds.append("trigger=?")
                args.append(trigger)
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY received_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        if not rows:
            return ToolResult(
                status=ToolResultStatus.NOT_FOUND,
                message="暂无历史采集数据。设备可能还没有上传过数据。",
            )
        records = []
        for row in rows:
            r = dict(row)
            r.pop("payload", None)
            r["received_at_str"] = main._fmt_time(r.get("received_at"))
            r["ts_ms_str"] = main._fmt_epoch_ms(r.get("ts_ms")) if r.get("ts_ms") else None
            r["ago"] = format_time_ago(r.get("received_at"))
            records.append(r)
        evidence = make_evidence(
            device_id=device_id,
            source=records[0].get("source") if records else None,
            received_at=records[0].get("received_at") if records else None,
            record_count=len(records),
        )
        if len(records) == 1:
            r = records[0]
            msg = (f"找到 1 条记录:\n"
                   f"- 设备: {r['device_id']}\n"
                   f"- 采样时间: {r['ts_ms_str']}\n"
                   f"- 收到: {r['received_at_str']} ({r['ago']})\n"
                   f"- 样本数: {r.get('sample_count','N/A')} · 来源: {r.get('source','N/A')}")
        else:
            lines = [f"找到 {len(records)} 条记录:"]
            for r in records[:10]:
                lines.append(f"- [{r['device_id']}] {r['ts_ms_str']} · 样本 {r.get('sample_count','?')} · {r.get('ago','')}")
            msg = "\n".join(lines)
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            data={"count": len(records), "records": records},
            evidence=evidence,
            message=msg,
        )


class QueryLatestTool(BaseTool):
    name = "query_latest"
    description = "查询指定设备最近一次上报的 IMU 数据"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        device_id = params.get("device_id", "").strip() if params.get("device_id") else ""
        if device_id and not self.check_permission(device_id):
            return False, f"设备 {device_id} 不在授权范围内"
        trigger = params.get("trigger", "").strip() or None
        if trigger and trigger not in ("manual", "periodic"):
            return False, "trigger 必须是 manual 或 periodic"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        conn = main._connect()
        try:
            device_id = params.get("device_id", "").strip() or None
            trigger = params.get("trigger", "").strip() or None
            conds, args = [], []
            if device_id:
                conds.append("device_id=?")
                args.append(device_id)
            if trigger:
                conds.append("trigger=?")
                args.append(trigger)
            sql = "SELECT * FROM uploads"
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY received_at DESC LIMIT 1"
            row = conn.execute(sql, args).fetchone()
            if row is None:
                return ToolResult(
                    status=ToolResultStatus.NOT_FOUND,
                    message="没有找到该设备的数据。",
                )
            up = dict(row)
            up.pop("payload", None)
            up["received_at_str"] = main._fmt_time(up.get("received_at"))
            up["ts_ms_str"] = main._fmt_epoch_ms(up.get("ts_ms")) if up.get("ts_ms") else None
            head = conn.execute(
                "SELECT seq, t_ms, ax, ay, az FROM samples WHERE upload_id=? ORDER BY seq ASC LIMIT 5",
                (up["id"],),
            ).fetchall()
            tail = conn.execute(
                "SELECT seq, t_ms, ax, ay, az FROM samples WHERE upload_id=? ORDER BY seq DESC LIMIT 5",
                (up["id"],),
            ).fetchall()
            up["head_samples"] = [dict(r) for r in head]
            up["tail_samples"] = [dict(r) for r in tail]
            up["ago"] = format_time_ago(up.get("received_at"))
        finally:
            conn.close()
        evidence = make_evidence(
            device_id=up.get("device_id"), source=up.get("source"),
            ts_ms=up.get("ts_ms"), received_at=up.get("received_at"),
            sample_count=up.get("sample_count"), trigger=up.get("trigger"),
        )
        all_s = head + tail
        az_vals = [s["az"] for s in all_s if s["az"] is not None]
        az_avg = sum(az_vals) / len(az_vals) if az_vals else None
        msg = (
            f"设备 {up['device_id']} 最新数据:\n"
            f"- 采样时间: {up['ts_ms_str']}\n"
            f"- 收到: {up['received_at_str']} ({up.get('ago','')})\n"
            f"- 样本数: {up.get('sample_count','N/A')} · 来源: {up.get('source','N/A')} · {up.get('trigger','N/A')}"
        )
        if az_avg is not None:
            msg += f"\n- az 均值: {az_avg:.3f} m/s²"
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            data=up, evidence=evidence, message=msg,
        )


class QueryDevicesTool(BaseTool):
    name = "query_devices"
    description = "查询当前在线的设备列表及状态"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        conn = main._connect()
        try:
            rows = conn.execute(
                "SELECT d.device_id,"
                " (SELECT COUNT(*) FROM uploads u WHERE u.device_id=d.device_id) AS uploads,"
                " (SELECT MAX(u.received_at) FROM uploads u WHERE u.device_id=d.device_id) AS last_seen,"
                " (SELECT COUNT(*) FROM photos p WHERE p.device_id=d.device_id) AS photos,"
                " (SELECT COUNT(*) FROM tasks t WHERE t.device_id=d.device_id"
                "  AND t.status NOT IN ('completed','failed','timeout')) AS pending_tasks"
                " FROM (SELECT device_id FROM uploads UNION SELECT device_id FROM photos) d"
                " ORDER BY d.device_id"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return ToolResult(status=ToolResultStatus.NOT_FOUND, message="暂无设备记录。")
        devices = []
        for r in rows:
            dev = dict(r)
            dev["last_seen_str"] = main._fmt_time(r["last_seen"])
            dev["ago"] = format_time_ago(r["last_seen"])
            devices.append(dev)
        evidence = make_evidence(device_count=len(devices))
        lines = [f"共 {len(devices)} 台设备:"]
        for d in devices:
            lines.append(f"- {d['device_id']}: 上传 {d['uploads']} 批 · 照片 {d['photos']} 张 · 待处理 {d['pending_tasks']} · {d.get('ago','N/A')}")
        return ToolResult(status=ToolResultStatus.SUCCESS, data={"devices": devices}, evidence=evidence, message="\n".join(lines))


class QueryHealthTool(BaseTool):
    name = "query_health"
    description = "查询系统健康状态（上传/任务/照片/事件汇总）"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        conn = main._connect()
        try:
            # 统计口径要求：所有 COUNT / GROUP BY 之前先做惰性过期并提交。
            # 否则 health 汇总会说「待处理 2 条」而事件列表只返回 1 条
            # （见 server/main.py health() 与 MEMORY.md 的注意事项）。
            main._expire_stale_tasks(conn)
            main._expire_stale_events(conn)
            conn.commit()
            n_uploads = conn.execute(
                "SELECT COUNT(*) FROM uploads").fetchone()[0]
            n_samples = conn.execute(
                "SELECT COUNT(*) FROM samples").fetchone()[0]
            dev_rows = conn.execute(
                "SELECT DISTINCT device_id FROM uploads").fetchall()
            task_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            n_photos = conn.execute(
                "SELECT COUNT(*) FROM photos").fetchone()[0]
            event_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
            ).fetchall()
        finally:
            conn.close()

        devices = [r["device_id"] for r in dev_rows]
        tasks_by_status = {r["status"]: r["n"] for r in task_rows}
        events_by_status = {r["status"]: r["n"] for r in event_rows}
        total_tasks = sum(tasks_by_status.values())
        total_events = sum(events_by_status.values())
        pending_tasks = total_tasks - sum(
            tasks_by_status.get(s, 0)
            for s in ("completed", "failed", "timeout"))
        pending_events = sum(
            events_by_status.get(s, 0) for s in ("pending", "ack"))

        data = {
            "total_uploads": n_uploads,
            "total_samples": n_samples,
            "devices": devices,
            "total_tasks": total_tasks,
            "tasks_by_status": tasks_by_status,
            "pending_tasks": pending_tasks,
            "total_photos": n_photos,
            "total_events": total_events,
            "events_by_status": events_by_status,
            "pending_events": pending_events,
        }
        evidence = make_evidence(device_count=len(devices))
        msg = (
            f"系统状态正常。\n"
            f"- 累计上传: {n_uploads} 批 / {n_samples} 个样本\n"
            f"- 设备: {len(devices)} 台\n"
            f"- 任务: {total_tasks} 个（待处理 {pending_tasks}）\n"
            f"- 照片: {n_photos} 张\n"
            f"- 事件: {total_events} 个（待处理 {pending_events}）"
        )
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            data=data, evidence=evidence, message=msg,
        )