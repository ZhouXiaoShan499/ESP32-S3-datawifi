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
        # 客户端不可用（未装 openai 库 / key 为空 / base_url 配错）也算「LLM 不可达」，
        # 必须走本地关键词回退，而不是把 500 抛给调用方。
        try:
            self._ensure_client()
        except Exception as e:                       # noqa: BLE001
            _warn_nlu_unavailable(e)
            return self._fallback(user_input)
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
            return self._fallback(user_input)
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
                return self._fallback(user_input)
        return {
            "intent": parsed.get("intent", ""),
            "parameters": parsed.get("parameters", {}),
            "confidence": float(parsed.get("confidence", 0.5)),
            "ambiguous": bool(parsed.get("ambiguous", False)),
            "clarification_message": parsed.get("clarification_message", ""),
            "clarification_options": parsed.get("clarification_options", []),
            "raw": raw}

    def _fallback(self, text: str) -> Dict[str, Any]:
        """LLM 不可用时的本地关键词回退。"""
        t = text.lower()
        if any(w in t for w in ("采集", "采", "获取")):
            return {"intent": "start_capture", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        if any(w in t for w in ("拍照", "照片", "拍一张")):
            return {"intent": "take_photo", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        if any(w in t for w in ("暂停", "停止", "停一下")):
            return {"intent": "pause_periodic", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        if any(w in t for w in ("恢复", "继续", "重启")):
            return {"intent": "resume_periodic", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        # 放在查询类之前：「查看系统状态」同时含「查」（query_latest 的关键词），
        # 但用户的真实意图是健康汇总，先命中这里更准。
        if any(w in t for w in ("系统状态", "健康", "汇总", "概览", "状态")):
            return {"intent": "query_health", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        if any(w in t for w in ("事件", "告警")):
            return {"intent": "query_events", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        if any(w in t for w in ("任务", "进度")):
            return {"intent": "query_tasks", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        if any(w in t for w in ("设备", "列表")):
            return {"intent": "query_devices", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        if any(w in t for w in ("数据", "结果", "看看", "查", "显示",
                                  "最新", "最近", "刚才", "上次")):
            return {"intent": "query_latest", "parameters": {},
                    "confidence": 0.4, "ambiguous": False,
                    "clarification_message": "", "clarification_options": []}
        return {"intent": "query_health", "parameters": {},
                "confidence": 0.2, "ambiguous": True,
                "clarification_message": "我不太确定您想做什么。",
                "clarification_options": ["查看最新数据", "发起采集",
                                          "查看设备列表", "查看系统状态"]}


_nlu_parser: Optional[NLUParser] = None


def get_nlu_parser() -> NLUParser:
    global _nlu_parser
    if _nlu_parser is None:
        _nlu_parser = NLUParser()
    return _nlu_parser