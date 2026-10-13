# -*- coding: utf-8 -*-
"""Agent API —— 暴露 /agent/chat 和 /agent/clarify 端点。"""
import json, logging, uuid, threading, time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

logger = logging.getLogger("agent.api")

router = APIRouter(prefix="/api/v1/agent", tags=["AI Agent"])

# 简单会话存储（内存 dict，进程重启丢失；生产可换 Redis）
_sessions: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
SESSION_TTL_S = 900  # 15 分钟


def _clean_expired():
    """惰性清理过期会话。"""
    now = time.time()
    expired = [k for k, v in _sessions.items()
               if now - v.get("_ts", now) > SESSION_TTL_S]
    for k in expired:
        _sessions.pop(k, None)


# ---------------------------------------------------------------------------
# Pydantic models（最少依赖，手动校验）
# ---------------------------------------------------------------------------
class AgentRequest:
    def __init__(self, message: str, session_id: str = "",
                 device_id: str = ""):
        self.message = message.strip()
        self.session_id = session_id.strip() or uuid.uuid4().hex[:12]
        self.device_id = device_id.strip() or ""


class ClarifyRequest:
    def __init__(self, session_id: str, choice: str):
        self.session_id = session_id.strip()
        self.choice = choice.strip()


def _parse_agent_request(body: dict) -> Optional[AgentRequest]:
    msg = body.get("message", "")
    # 纯空白等同于没给 —— 否则会被当成一句「听不懂的话」回一轮无意义的追问
    if not msg or not isinstance(msg, str) or not msg.strip():
        return None
    return AgentRequest(
        message=msg,
        session_id=body.get("session_id", ""),
        device_id=body.get("device_id", ""))


def _parse_clarify_request(body: dict) -> Optional[ClarifyRequest]:
    sid = body.get("session_id", "")
    c = body.get("choice", "")
    if not sid or not c:
        return None
    return ClarifyRequest(session_id=sid, choice=c)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.post("/chat")
async def agent_chat(request: Request):
    """接收用户消息，返回结果或追问。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    req = _parse_agent_request(body)
    if req is None:
        return JSONResponse({"error": "缺少 message 参数"}, status_code=400)

    # 用单点相对导入（.nlu/.router），而不是 ..agent.nlu ——
    # 运行期 agent 有两种落点：`python server/main.py` 时是**顶层包 agent**
    # （与 test_*.py 的 `import main` 同一 sys.path 约定，此时 `..` 会越出
    # 顶层包而报 "attempted relative import beyond top-level package"）；
    # 仓库根目录布局下则是 server.agent。`.` 在两种情形下都指向 agent 包本身。
    from .nlu import get_nlu_parser
    from .router import get_router

    # 读取/创建会话上下文
    _clean_expired()
    with _lock:
        sess = _sessions.get(req.session_id, {"_ts": time.time()})
        sess["_ts"] = time.time()
        if req.device_id:
            sess["last_device_id"] = req.device_id

    # NLU 解析与路由执行都改到工作线程里跑 —— 这两步都是**同步阻塞**调用：
    #   - nlu.parse() 同步调 LLM（超时 30s，见 config.AGENT_CONFIG[...]["timeout_s"]）；
    #   - router_.route() 里各工具要同步轮询设备证据，全是 time.sleep 死等
    #     （采集 60s / 拍照 30s / 控制 30s，见 tools/capture.py 等）。
    # 直接把它们放在 async def 里会独占 uvicorn 的事件循环：轮询期间**所有**请求
    # 都得不到响应，连 /api/v1/health 都会超时，设备端的 /api/v1/tasks/next 同样领
    # 不到任务 —— 于是刚创建的任务只能自己走到过期，接口最终返回
    # 「采集timeout: expired before device upload」（实测：agent 请求 60.2s 期间
    # health 连续 12 次 3s 超时，agent 一返回 0.19s 内立刻恢复 200）。
    # run_in_threadpool 把这两个阻塞调用挪出事件循环，其它请求照常被服务。
    nlu = get_nlu_parser()
    parsed = await run_in_threadpool(nlu.parse, req.message, sess)

    # 路由执行
    router_ = get_router()
    result = await run_in_threadpool(router_.route, parsed, sess)

    # 歧义时暂存意图，以便 clarify 继续
    if result.status.value == "ambiguous":
        with _lock:
            sess["_pending"] = {
                "intent": parsed.get("intent"),
                "parameters": parsed.get("parameters", {}),
                "options": result.ambiguous_options}
            _sessions[req.session_id] = sess
    else:
        with _lock:
            sess.pop("_pending", None)
            _sessions[req.session_id] = sess

    return JSONResponse({
        "session_id": req.session_id,
        "type": ("clarification"
                 if result.status.value == "ambiguous" else "result"),
        "message": result.message,
        "status": result.status.value,
        "data": result.data,
        "options": (result.ambiguous_options
                    if result.status.value == "ambiguous" else None),
        "evidence": result.evidence,
    })


@router.post("/clarify")
async def agent_clarify(request: Request):
    """用户选择澄清选项后继续执行。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    req = _parse_clarify_request(body)
    if req is None:
        return JSONResponse({"error": "缺少 session_id/choice"}, status_code=400)

    from .router import get_router

    with _lock:
        sess = _sessions.get(req.session_id)
        if not sess or "_pending" not in sess:
            return JSONResponse(
                {"type": "result", "message": "没有待澄清的上下文，请重新输入。",
                 "session_id": req.session_id, "status": "failure"}, status_code=200)
        pending = sess.pop("_pending")
        _sessions[req.session_id] = sess

    # 用用户选择**重新解析**意图 —— 历史实现直接复用 pending["intent"]，
    # 于是不管在追问里选哪一项，跑的都是第一句「听不懂」的兜底意图（选择被静默丢弃）。
    # options 本身是自然语言短语（如「查看设备列表」），重新走一遍 NLU 就能拿到对应
    # 的 intent/parameters，本地关键词回退同样认这些句子；参数与暂存的合并，
    # 保证「选择即执行」，不把用户卡在追问里。
    from .nlu import get_nlu_parser

    chosen = await run_in_threadpool(get_nlu_parser().parse, req.choice, sess)
    params = dict(pending.get("parameters") or {})
    params.update(chosen.get("parameters") or {})
    new_parsed = {
        "intent": chosen.get("intent") or pending["intent"],
        "parameters": params,
        "confidence": 0.9,
        "ambiguous": False}
    # 与 /chat 同理：route() 里是同步阻塞的工具轮询，不能占着事件循环。
    router_ = get_router()
    result = await run_in_threadpool(router_.route, new_parsed, sess)

    return JSONResponse({
        "session_id": req.session_id,
        "type": "result",
        "message": result.message,
        "status": result.status.value,
        "data": result.data,
        "options": None,
        "evidence": result.evidence,
    })