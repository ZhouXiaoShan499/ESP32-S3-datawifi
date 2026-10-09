# -*- coding: utf-8 -*-
"""闭环事件查询工具"""
from typing import Dict, Any
from .base import BaseTool, ToolResult, ToolResultStatus, make_evidence, format_time_ago


class QueryEventsTool(BaseTool):
    name = "query_events"
    description = "查询闭环事件列表"

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        did = params.get("device_id", "").strip() if params.get("device_id") else ""
        if did and not self.check_permission(did):
            return False, f"设备 {did} 不在授权范围内"
        try: limit = int(params.get("limit", 20))
        except (TypeError, ValueError): limit = 20
        if limit < 1 or limit > 200:
            return False, "limit 范围 1-200"
        return True, None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        from .. import _import_main
        main = _import_main()
        conn = main._connect()
        did = params.get("device_id", "").strip() or None
        only_active = params.get("active_only", False)
        try: limit = int(params.get("limit", 20))
        except (TypeError, ValueError): limit = 20
        try:
            main._expire_stale_events(conn, did)
            sql = "SELECT * FROM events WHERE 1=1"
            args = []
            if did:
                sql += " AND device_id=?"
                args.append(did)
            if only_active:
                sql += " AND status IN ('pending','ack')"
            sql += " ORDER BY created_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
            conn.commit()
        finally:
            conn.close()
        if not rows:
            return ToolResult(status=ToolResultStatus.NOT_FOUND,
                              message="暂无闭环事件。")
        events = []
        for r in rows:
            ev = dict(r)
            ev["status_cn"] = main.EVENT_STATUS_CN.get(
                ev.get("status"), ev.get("status"))
            ev["ago"] = format_time_ago(ev.get("created_at"))
            events.append(ev)
        evidence = make_evidence(device_id=did, count=len(events))
        lines = [f"共 {len(events)} 个事件:"]
        for e in events[:10]:
            lines.append(
                f"- [{e['status_cn']}] "
                f"{e.get('note','') or '事件'}"
                f" · 设备 {e['device_id']} · {e.get('ago','')}")
        return ToolResult(status=ToolResultStatus.SUCCESS,
                          data={"events": events, "count": len(events)},
                          evidence=evidence, message="\n".join(lines))