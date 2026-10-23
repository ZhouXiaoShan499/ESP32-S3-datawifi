# -*- coding: utf-8 -*-
"""Agent API —— 暴露 /agent/chat、/agent/clarify 和 /agent/cancel 端点。"""
import asyncio, json, logging, re, uuid, threading, time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

logger = logging.getLogger("agent.api")

router = APIRouter(prefix="/api/v1/agent", tags=["AI Agent"])

# 简单会话存储（内存 dict，进程重启丢失；生产可换 Redis）
_sessions: Dict[str, Dict[str, Any]] = {}
# 请求级取消状态：request_id -> {event, created_at, done, cancelled}。
# 取消失败/晚到的 HTTP 响应由前端再按 request_id 隔离；这里的 Event 让仍在轮询
# 设备的工具尽快退出，避免用户点了「停止」后线程继续等 30-60 秒。
_requests: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
SESSION_TTL_S = 900  # 15 分钟
REQUEST_TTL_S = 900
AGENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")


def _clean_expired():
    """惰性清理过期会话。"""
    now = time.time()
    with _lock:
        expired = [k for k, v in _sessions.items()
                   if now - v.get("_ts", now) > SESSION_TTL_S]
        for k in expired:
            _sessions.pop(k, None)
        stale = [k for k, v in _requests.items()
                 if now - v.get("created_at", now) > REQUEST_TTL_S]
        for k in stale:
            _requests.pop(k, None)


def _register_request(request_id: str) -> Optional[threading.Event]:
    """登记一个正在执行的 Agent 请求；同一 request_id 不允许并发重入。"""
    with _lock:
        current = _requests.get(request_id)
        if current and not current.get("done"):
            return None
        event = threading.Event()
        _requests[request_id] = {
            "event": event,
            "created_at": time.time(),
            "done": False,
            "cancelled": False,
        }
        return event


def _finish_request(request_id: str):
    with _lock:
        rec = _requests.get(request_id)
        if rec:
            rec["done"] = True


def _cancel_request(request_id: str) -> bool:
    """把正在执行的请求标记为取消；已完成/未知请求返回 False。"""
    with _lock:
        rec = _requests.get(request_id)
        if not rec or rec.get("done"):
            return False
        rec["cancelled"] = True
        rec["event"].set()
        return True


def _is_cancelled(request_id: str) -> bool:
    with _lock:
        rec = _requests.get(request_id)
        return bool(rec and rec["event"].is_set())


# ---------------------------------------------------------------------------
# Pydantic models（最少依赖，手动校验）
# ---------------------------------------------------------------------------
class AgentRequest:
    def __init__(self, message: str, session_id: str = "",
                 device_id: str = "", request_id: str = ""):
        self.message = message.strip()
        self.session_id = session_id.strip() or uuid.uuid4().hex[:12]
        self.device_id = device_id.strip() or ""
        self.request_id = request_id.strip() or ("agent-" + uuid.uuid4().hex)


class ClarifyRequest:
    def __init__(self, session_id: str, choice: str, request_id: str = ""):
        self.session_id = session_id.strip()
        self.choice = choice.strip()
        self.request_id = request_id.strip() or ("agent-" + uuid.uuid4().hex)


def _parse_agent_request(body: dict) -> Optional[AgentRequest]:
    msg = body.get("message", "")
    # 纯空白等同于没给 —— 否则会被当成一句「听不懂的话」回一轮无意义的追问
    if not msg or not isinstance(msg, str) or not msg.strip():
        return None
    return AgentRequest(
        message=msg,
        session_id=body.get("session_id", ""),
        device_id=body.get("device_id", ""),
        request_id=body.get("request_id", ""))


def _parse_clarify_request(body: dict) -> Optional[ClarifyRequest]:
    sid = body.get("session_id", "")
    c = body.get("choice", "")
    if not sid or not c:
        return None
    return ClarifyRequest(session_id=sid, choice=c,
                          request_id=body.get("request_id", ""))


def _valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and bool(AGENT_REQUEST_ID_RE.match(value.strip()))


def _cancelled_response(session_id: str, request_id: str, started_at: float):
    return JSONResponse({
        "session_id": session_id,
        "request_id": request_id,
        "type": "result",
        "message": "任务已取消，已停止等待设备回执。迟到的结果不会再覆盖当前界面。",
        "status": "cancelled",
        "data": {"cancelled": True},
        "options": None,
        "evidence": None,
        "elapsed_ms": round((time.time() - started_at) * 1000),
    })


def _validate_body(body: Any, fields: Dict[str, int]) -> Optional[str]:
    """请求体必须是 JSON 对象，指定字段必须为有长度上限的字符串。"""
    if not isinstance(body, dict):
        return "request body must be a JSON object"
    for name, max_len in fields.items():
        value = body.get(name, "")
        if not isinstance(value, str):
            return f"'{name}' must be a string"
        if len(value) > max_len:
            return f"'{name}' is too long (max {max_len})"
    return None


async def _run_nlu_cancellable(parser, message: str, session: Dict[str, Any],
                               cancel_event: threading.Event,
                               request_id: str):
    """等待 NLU 时每 50 ms 查看取消事件。

    同步 OpenAI 客户端无法从外部强制中断，因此取消后立即结束 HTTP 等待；
    被放弃的线程完成/超时后由回调清理请求登记，不会再执行设备工具。
    """
    task = asyncio.create_task(asyncio.to_thread(parser.parse, message, session))
    while not task.done():
        if cancel_event.is_set():
            def _finish_late(completed):
                try:
                    completed.result()
                except BaseException as exc:          # noqa: BLE001
                    logger.warning("late NLU request_id=%s failed after cancel: %s",
                                   request_id, exc)
                finally:
                    _finish_request(request_id)
            task.add_done_callback(_finish_late)
            return None
        await asyncio.wait({task}, timeout=0.05)
    return task.result()


def _remember_context(session: Dict[str, Any], message: str, intent: str,
                      result) -> None:
    """保存最小化的多轮上下文，不保存完整聊天记录或敏感参数。"""
    session["last_intent"] = intent or ""
    session["last_message"] = (message or "")[:300]
    data = result.data if isinstance(result.data, dict) else {}
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    task_rid = task.get("request_id") or data.get("task_request_id")
    if isinstance(task_rid, str) and task_rid:
        session["last_task_request_id"] = task_rid


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.post("/chat")
async def agent_chat(request: Request):
    """接收用户消息，返回结果或追问。"""
    started_at = time.time()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    body_error = _validate_body(
        body, {"message": 4000, "session_id": 128,
               "device_id": 128, "request_id": 80})
    if body_error:
        return JSONResponse({"error": body_error}, status_code=400)
    raw_request_id = body.get("request_id", "")
    if raw_request_id and not _valid_request_id(raw_request_id):
        return JSONResponse(
            {"error": "invalid request_id (expect 6-80 chars of [A-Za-z0-9_-])"},
            status_code=400)
    req = _parse_agent_request(body)
    if req is None:
        return JSONResponse({"error": "缺少 message 参数"}, status_code=400)
    logger.info("agent start request_id=%s session_id=%s device_id=%s",
                req.request_id, req.session_id, req.device_id or "-")

    # 用单点相对导入（.nlu/.router），而不是 ..agent.nlu ——
    # 运行期 agent 有两种落点：`python server/main.py` 时是**顶层包 agent**
    # （与 test_*.py 的 `import main` 同一 sys.path 约定，此时 `..` 会越出
    # 顶层包而报 "attempted relative import beyond top-level package"）；
    # 仓库根目录布局下则是 server.agent。`.` 在两种情形下都指向 agent 包本身。
    from .nlu import get_nlu_parser
    from .router import get_router

    _clean_expired()
    cancel_event = _register_request(req.request_id)
    if cancel_event is None:
        return JSONResponse(
            {"error": "request_id is already running", "request_id": req.request_id},
            status_code=409)

    # 读取/创建会话上下文
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
    try:
        nlu = get_nlu_parser()
        parsed = await _run_nlu_cancellable(
            nlu, req.message, sess, cancel_event, req.request_id)
        if parsed is None:
            logger.info("agent cancelled during NLU request_id=%s", req.request_id)
            return _cancelled_response(req.session_id, req.request_id, started_at)
        if cancel_event.is_set():
            _finish_request(req.request_id)
            logger.info("agent cancelled after NLU request_id=%s", req.request_id)
            return _cancelled_response(req.session_id, req.request_id, started_at)

        # 路由执行
        router_ = get_router()
        result = await run_in_threadpool(
            router_.route, parsed, sess,
            cancel_event=cancel_event, agent_request_id=req.request_id)
    except Exception:
        logger.exception("agent failed request_id=%s", req.request_id)
        _finish_request(req.request_id)
        raise

    # 工具可能在取消动作与设备回执之间完成；此时仍按取消返回，避免迟到结果
    # 覆盖用户已经切走/重新输入的界面。
    if cancel_event.is_set():
        with _lock:
            sess.pop("_pending", None)
            _sessions[req.session_id] = sess
        _finish_request(req.request_id)
        logger.info("agent cancelled during tool request_id=%s", req.request_id)
        return _cancelled_response(req.session_id, req.request_id, started_at)

    # 歧义时暂存意图，以便 clarify 继续
    if result.status.value == "ambiguous":
        with _lock:
            sess["_pending"] = {
                "intent": parsed.get("intent"),
                "parameters": parsed.get("parameters", {}),
                "options": result.ambiguous_options}
            _remember_context(sess, req.message, parsed.get("intent", ""), result)
            _sessions[req.session_id] = sess
    else:
        with _lock:
            sess.pop("_pending", None)
            _remember_context(sess, req.message, parsed.get("intent", ""), result)
            _sessions[req.session_id] = sess

    _finish_request(req.request_id)
    logger.info("agent done request_id=%s status=%s elapsed_ms=%d",
                req.request_id, result.status.value,
                round((time.time() - started_at) * 1000))
    return JSONResponse({
        "session_id": req.session_id,
        "request_id": req.request_id,
        "type": ("clarification"
                 if result.status.value == "ambiguous" else "result"),
        "message": result.message,
        "status": result.status.value,
        "data": result.data,
        "options": (result.ambiguous_options
                    if result.status.value == "ambiguous" else None),
        "evidence": result.evidence,
        "elapsed_ms": round((time.time() - started_at) * 1000),
    })


@router.post("/clarify")
async def agent_clarify(request: Request):
    """用户选择澄清选项后继续执行。"""
    started_at = time.time()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    body_error = _validate_body(
        body, {"session_id": 128, "choice": 1000, "request_id": 80})
    if body_error:
        return JSONResponse({"error": body_error}, status_code=400)
    raw_request_id = body.get("request_id", "")
    if raw_request_id and not _valid_request_id(raw_request_id):
        return JSONResponse(
            {"error": "invalid request_id (expect 6-80 chars of [A-Za-z0-9_-])"},
            status_code=400)
    req = _parse_clarify_request(body)
    if req is None:
        return JSONResponse({"error": "缺少 session_id/choice"}, status_code=400)
    logger.info("agent clarify start request_id=%s session_id=%s",
                req.request_id, req.session_id)

    from .router import get_router

    _clean_expired()
    cancel_event = _register_request(req.request_id)
    if cancel_event is None:
        return JSONResponse(
            {"error": "request_id is already running", "request_id": req.request_id},
            status_code=409)

    no_pending = False
    with _lock:
        sess = _sessions.get(req.session_id)
        if not sess or "_pending" not in sess:
            no_pending = True
            pending = None
        else:
            pending = sess.pop("_pending")
            _sessions[req.session_id] = sess
    if no_pending:
        _finish_request(req.request_id)
        return JSONResponse(
            {"type": "result", "message": "没有待澄清的上下文，请重新输入。",
             "session_id": req.session_id, "request_id": req.request_id,
             "status": "failure"}, status_code=200)

    # 用用户选择**重新解析**意图 —— 历史实现直接复用 pending["intent"]，
    # 于是不管在追问里选哪一项，跑的都是第一句「听不懂」的兜底意图（选择被静默丢弃）。
    # options 本身是自然语言短语（如「查看设备列表」），重新走一遍 NLU 就能拿到对应
    # 的 intent/parameters，本地关键词回退同样认这些句子；参数与暂存的合并，
    # 保证「选择即执行」，不把用户卡在追问里。
    from .nlu import get_nlu_parser

    try:
        chosen = await _run_nlu_cancellable(
            get_nlu_parser(), req.choice, sess, cancel_event, req.request_id)
        if chosen is None:
            logger.info("agent clarify cancelled during NLU request_id=%s",
                        req.request_id)
            return _cancelled_response(req.session_id, req.request_id, started_at)
        if cancel_event.is_set():
            _finish_request(req.request_id)
            logger.info("agent clarify cancelled after NLU request_id=%s",
                        req.request_id)
            return _cancelled_response(req.session_id, req.request_id, started_at)
        params = dict(pending.get("parameters") or {})
        params.update(chosen.get("parameters") or {})
        new_parsed = {
            "intent": chosen.get("intent") or pending["intent"],
            "parameters": params,
            "confidence": 0.9,
            "ambiguous": False}
        # 与 /chat 同理：route() 里是同步阻塞的工具轮询，不能占着事件循环。
        router_ = get_router()
        result = await run_in_threadpool(
            router_.route, new_parsed, sess,
            cancel_event=cancel_event, agent_request_id=req.request_id)
    except Exception:
        logger.exception("agent clarify failed request_id=%s", req.request_id)
        _finish_request(req.request_id)
        raise

    if cancel_event.is_set():
        _finish_request(req.request_id)
        logger.info("agent clarify cancelled during tool request_id=%s",
                    req.request_id)
        return _cancelled_response(req.session_id, req.request_id, started_at)

    with _lock:
        _remember_context(sess, req.choice, new_parsed.get("intent", ""), result)
        _sessions[req.session_id] = sess
    _finish_request(req.request_id)
    logger.info("agent clarify done request_id=%s status=%s elapsed_ms=%d",
                req.request_id, result.status.value,
                round((time.time() - started_at) * 1000))
    return JSONResponse({
        "session_id": req.session_id,
        "request_id": req.request_id,
        "type": "result",
        "message": result.message,
        "status": result.status.value,
        "data": result.data,
        "options": None,
        "evidence": result.evidence,
        "elapsed_ms": round((time.time() - started_at) * 1000),
    })


@router.post("/cancel")
async def agent_cancel(request: Request):
    """取消正在等待设备回执的 Agent 请求。

    取消只终止本次 Agent 请求的等待链；如果请求在取消前已经完成，接口会如实
    返回 cancelled=false，由前端继续按 request_id 隔离迟到结果。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    body_error = _validate_body(body, {"request_id": 80})
    if body_error:
        return JSONResponse({"error": body_error}, status_code=400)
    request_id = body.get("request_id", "")
    if not _valid_request_id(request_id):
        return JSONResponse(
            {"error": "invalid request_id (expect 6-80 chars of [A-Za-z0-9_-])"},
            status_code=400)
    request_id = request_id.strip()
    cancelled = _cancel_request(request_id)
    logger.info("agent cancel request_id=%s cancelled=%s", request_id, cancelled)
    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "cancelled": cancelled,
        "message": ("已取消正在执行的请求" if cancelled
                    else "请求已完成或不存在，迟到结果会由界面隔离"),
    })
