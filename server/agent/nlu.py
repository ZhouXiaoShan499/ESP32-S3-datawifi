# -*- coding: utf-8 -*-
"""NLU 解析器 —— 调用 LLM 解析用户自然语言输入为结构化 JSON。"""
import json, logging, time
from typing import Dict, Any, Optional

logger = logging.getLogger("agent.nlu")

# 「LLM 客户端不可用」只告警一次：回退关键词是**正常的降级路径**，用户每问一句都刷
# 一条 WARNING 只会把日志淹掉（一次会话里能刷出几十条同样的行）。
_fallback_warned = False


def _warn_nlu_unavailable(exc):
    global _fallback_warned
    if not _fallback_warned:
        _fallback_warned = True
        logger.warning("NLU 客户端不可用，回退关键词解析（同类告警只提示一次）: %s",
                       exc)


try:
    from openai import OpenAI
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False


class NLUParser:
    """自然语言意图解析器（兼容 OpenAI 格式 API，支持 Ollama）。"""

    def __init__(self, config: Optional[Dict] = None):
        from .config import get_agent_config, ALLOWED_DEVICES
        self._cfg = config or get_agent_config()
        self._allowed = ALLOWED_DEVICES
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return
        if not _HAS_OPENAI:
            raise RuntimeError("openai 库未安装: pip install openai")
        self._client = OpenAI(
            api_key=self._cfg.get("api_key", ""),
            base_url=self._cfg.get("api_base_url"),
            timeout=self._cfg.get("timeout_s", 30))

    def parse(self, user_input: str,
              context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from .prompt import build_system_prompt, build_user_message
        from .config import AGENT_NLU_MODE
        if AGENT_NLU_MODE == "keyword":
            return self._fallback(user_input, context)
        # 客户端不可用（未装 openai 库 / key 为空 / base_url 配错）也算「LLM 不可达」，
        # 必须走本地关键词回退，而不是把 500 抛给调用方。
        try:
            self._ensure_client()
        except Exception as e:                       # noqa: BLE001
            _warn_nlu_unavailable(e)
            return self._fallback(user_input, context)
        system = build_system_prompt(self._allowed)
        user_msg = build_user_message(user_input, context)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg}]
        t0 = time.time()
        try:
            resp = self._client.chat.completions.create(
                model=self._cfg.get("model", "gpt-4o-mini"),
                temperature=self._cfg.get("temperature", 0.1),
                max_tokens=self._cfg.get("max_tokens", 1024),
                messages=messages,
                response_format={"type": "json_object"})
            elapsed = (time.time() - t0) * 1000
            raw = resp.choices[0].message.content
            logger.info("NLU parsed in %.0fms, tokens=%s",
                        elapsed, getattr(resp.usage, "total_tokens", "?"))
        except Exception as e:
            logger.error("NLU parse error: %s", e)
            return self._fallback(user_input, context)
        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            if "```" in raw:
                snippet = raw.split("```")
                for s in snippet:
                    s = s.strip()
                    if s.startswith("json"):
                        s = s[4:]
                    try:
                        parsed = json.loads(s)
                        break
                    except json.JSONDecodeError:
                        continue
            if not isinstance(parsed, dict):
                return self._fallback(user_input, context)
        return {
            "intent": parsed.get("intent", ""),
            "parameters": parsed.get("parameters", {}),
            "confidence": float(parsed.get("confidence", 0.5)),
            "ambiguous": bool(parsed.get("ambiguous", False)),
            "clarification_message": parsed.get("clarification_message", ""),
            "clarification_options": parsed.get("clarification_options", []),
            "raw": raw}

    def _fallback(self, text: str, context: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
        """LLM 不可用时的本地关键词回退。

        安全原则：查询优先于动作、否定句绝不执行、单字「采/查」不当作动作。
        设备动作只认明确短语；其余问法进入追问，避免“查看采集任务”真的发起采集。
        """
        t = "".join((text or "").lower().split())
        context = context or {}

        def out(intent, confidence=0.82, parameters=None, ambiguous=False,
                message="", options=None):
            return {
                "intent": intent,
                "parameters": parameters or {},
                "confidence": confidence,
                "ambiguous": ambiguous,
                "clarification_message": message,
                "clarification_options": options or [],
            }

        followups = ("再查一次", "再查一遍", "再看一次", "重新查询", "重复查询")
        if any(w in t for w in followups):
            last_intent = context.get("last_intent")
            if last_intent in ("query_history", "query_latest", "query_devices",
                               "query_health", "query_tasks", "query_events"):
                return out(last_intent, 0.88)
            return out(
                "", 0.3, ambiguous=True,
                message="目前没有可重复的上一次查询，请说明要查询什么。",
                options=["查看最新数据", "查看任务状态", "查看设备列表"])

        if any(w in t for w in ("不要", "别", "无需", "不用", "取消")) and any(
                w in t for w in ("采集", "拍照", "暂停", "停止", "恢复")):
            return out(
                "", 0.35, ambiguous=True,
                message="这条输入包含否定或取消含义，我没有执行设备动作。",
                options=["查看任务状态", "查看最新数据"])

        if (any(w in t for w in ("历史", "多少批", "几条记录", "采集记录")) or
                ("采集" in t and any(w in t for w in ("多少", "几次", "几批")))):
            return out("query_history", 0.86)
        if any(w in t for w in ("任务状态", "任务进度", "任务怎么样", "完成了吗",
                                  "是否完成", "查询任务", "查看任务",
                                  "暂停了吗", "恢复了吗", "正在采集吗")) or \
                "任务" in t:
            return out("query_tasks", 0.9)
        if any(w in t for w in ("系统状态", "健康", "汇总", "概览")):
            return out("query_health", 0.88)
        if any(w in t for w in ("事件", "告警")):
            return out("query_events", 0.86)
        if any(w in t for w in ("设备列表", "有哪些设备", "查看设备", "设备在线")):
            return out("query_devices", 0.88)

        capture_actions = ("发起采集", "开始采集", "重新采集", "再采集",
                           "采集一次", "采一组", "帮我采集", "请采集",
                           "采集200", "采集100", "采集50")
        if (any(w in t for w in capture_actions) or
                (t.startswith("采集") and "数据" in t) or
                ("采集" in t and "个点" in t)):
            return out("start_capture", 0.85)

        if any(w in t for w in ("拍一张", "拍个照", "拍照", "拍摄照片")):
            return out("take_photo", 0.86)
        if any(w in t for w in ("暂停周期", "停止周期", "暂停上报",
                                  "停止上报", "暂停上传")):
            return out("pause_periodic", 0.88)
        if any(w in t for w in ("恢复周期", "恢复上报", "恢复上传",
                                  "继续上报")):
            return out("resume_periodic", 0.88)

        if any(w in t for w in ("数据", "结果", "看看", "查询", "显示",
                                  "最新", "最近", "刚才", "上次")):
            return out("query_latest", 0.8)

        return out(
            "query_health", 0.2, ambiguous=True,
            message="我不太确定您想做什么。",
            options=["查看最新数据", "查看任务状态", "查看设备列表", "查看系统状态"])


_nlu_parser: Optional[NLUParser] = None


def get_nlu_parser() -> NLUParser:
    global _nlu_parser
    if _nlu_parser is None:
        _nlu_parser = NLUParser()
    return _nlu_parser


def get_nlu_status() -> Dict[str, Any]:
    """返回不包含密钥的运行配置，供页面明确显示真实 LLM / 关键词回退。"""
    from .config import AGENT_ENV, AGENT_NLU_MODE, get_agent_config
    cfg = get_agent_config()
    has_key = bool(str(cfg.get("api_key") or "").strip())
    configured = bool(
        AGENT_NLU_MODE != "keyword" and _HAS_OPENAI and has_key and cfg.get("model"))
    if AGENT_NLU_MODE == "keyword":
        fallback_reason = "AGENT_NLU_MODE=keyword（主动禁用模型调用）"
    elif not _HAS_OPENAI:
        fallback_reason = "openai 包未安装"
    elif not has_key:
        fallback_reason = "未配置模型 API Key"
    else:
        fallback_reason = ""
    return {
        "agent_env": AGENT_ENV if AGENT_ENV in ("dev", "prod", "local") else "dev",
        "requested_mode": AGENT_NLU_MODE,
        "mode": "llm" if configured else "keyword_fallback",
        "provider": ("ollama" if AGENT_ENV == "local"
                     else "openai-compatible"),
        "model": cfg.get("model") or "",
        "api_base_url": cfg.get("api_base_url") or "",
        "openai_installed": bool(_HAS_OPENAI),
        "credential_configured": has_key,
        "llm_configured": configured,
        "fallback_reason": fallback_reason,
    }
