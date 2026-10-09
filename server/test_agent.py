# -*- coding: utf-8 -*-
"""
AI Agent（自然语言 → 意图 → 受限工具调用）自测。

用独立临时库（server/data/test_agent.db），不污染正式库。
运行：python server/test_agent.py   （需 fastapi / httpx；**不需要 openai**）

测试围绕本模块存在的三条硬约束展开：
  1) 防幻觉：没有设备返回的真实完成证据时，**绝不返回 success**；
  2) 降级可用：LLM 不可达（未装 openai / 无 key / 断网）时必须回退本地关键词
     解析，而不是把 500 抛给调用方；
  3) 权限：工具只能操作 AGENT_ALLOWED_DEVICES 白名单内的设备。

覆盖：路由挂载 / 关键词回退的意图映射 / 空库查询 / 歧义追问与 /clarify 闭环（含
      「所选选项必须真正改变执行的意图」）/ 入参校验 / 设备白名单 / 防幻觉（采集、
      拍照、暂停、恢复四条动作路径）/ 工具层返回值结构 / 会话 TTL / 包导入布局。
"""

import os
import subprocess
import sys
import time

# 中文输出固定 UTF-8：中文 Windows 下控制台/重定向默认 cp936，日志会变乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_TEST_DB = os.path.join(_HERE, "data", "test_agent.db")


def _remove_db_files(path):
    """删库时连 WAL 副产品（-wal/-shm/-journal）一起删（理由见 test_receive.py 同名函数）。"""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


_remove_db_files(_TEST_DB)
os.environ["SENSOR_DB"] = _TEST_DB
os.environ["SENSOR_HOST"] = "127.0.0.1"
# 轮询超时压到 3 s：没有真机时这几条路径必须尽快返回 no_evidence，
# 不能让自测干等默认的 60 s（控制类任务默认 30 s）。
os.environ["AGENT_CAPTURE_TIMEOUT_S"] = "3"
os.environ["AGENT_PHOTO_TIMEOUT_S"] = "3"
os.environ["AGENT_CONTROL_TIMEOUT_S"] = "3"

sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient        # noqa: E402
import main                                       # noqa: E402
import agent.api as agent_api                     # noqa: E402
import agent.nlu as nlu_mod                       # noqa: E402
from agent.router import get_router               # noqa: E402
from agent.tools.base import (                    # noqa: E402
    ToolResult, ToolResultStatus, make_evidence, format_time_ago)

DEV = "esp32s3-eye-0001"
N = 0


def ok(msg):
    global N
    N += 1
    print("[PASS] %s" % msg)


def chat(client, message, device_id=""):
    """POST /agent/chat，要求 200 并返回 JSON（404/500 都在这里炸出来）。"""
    body = {"message": message}
    if device_id:
        body["device_id"] = device_id
    r = client.post("/api/v1/agent/chat", json=body)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def main_run():
    main.init_db()
    client = TestClient(main.app)

    # 0) 强制「LLM 不可达」：自测不联网，也不依赖本机是否装了 openai。
    #    _HAS_OPENAI=False 时 _ensure_client() 抛错，parse() 必须吞掉并回退。
    nlu_mod._nlu_parser = None
    nlu_mod._HAS_OPENAI = False

    # 1) 路由确实挂在 app 上（没挂上时下面的请求会是 404，而不是 200）
    assert "/api/v1/agent/chat" in getattr(main, "_AGENT_PATHS", []), \
        getattr(main, "_AGENT_PATHS", None)
    assert chat(client, "查看系统状态")["status"] == "success"
    ok("main.app 已挂载 AI Agent 路由（/api/v1/agent/chat 可达）")

    # 2) LLM 不可达 → 关键词回退；逐条核对意图映射
    parser = nlu_mod.get_nlu_parser()
    expect = {
        "看看最新的数据": "query_latest",
        "查看设备列表": "query_devices",
        "有哪些任务": "query_tasks",
        "有事件吗": "query_events",
        "查看系统状态": "query_health",
        "帮我采集一次数据": "start_capture",
        "拍一张照片": "take_photo",
        "暂停周期上报": "pause_periodic",
        "恢复周期上报": "resume_periodic",
    }
    for text, intent in expect.items():
        got = parser.parse(text)
        assert got["intent"] == intent, (text, got)
        assert got["confidence"] > 0, (text, got)
    ok("LLM 不可达时回退关键词解析：%d 句话映射到正确意图" % len(expect))

    # 3) 正面用例：成功结果必须带 evidence
    body = chat(client, "查看系统状态")
    assert body["status"] == "success" and body["evidence"], body
    assert body["data"]["total_uploads"] == 0, body["data"]
    ok("成功返回必带 evidence（空库 query_health 也是 success + 证据）")

    # 4) 空库下的查询类意图：不 500，如实说“没数据”
    for text in ("看看最新的数据", "查看设备列表", "有事件吗"):
        body = chat(client, text)
        assert body["status"] in ("not_found", "success"), body
    ok("空库查询：不 500，如实返回 not_found/success")

    # 5) 歧义 → 追问 → /clarify 闭环
    body = chat(client, "嗯……那个")
    assert body["type"] == "clarification", body
    assert body["status"] == "ambiguous", body
    opts = body["options"]
    assert opts and "查看系统状态" in opts, opts
    r = client.post("/api/v1/agent/clarify",
                    json={"session_id": body["session_id"],
                          "choice": "查看系统状态"})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["type"] == "result" and done["status"] == "success", done
    ok("歧义 → clarification（带 options）→ /clarify 继续执行成功")

    # 5b) 追问的**选择必须真正改变执行的意图**。历史 bug：/clarify 直接把
    #     pending["intent"] 原样复用，于是不管在追问里选哪一项，跑的都是第一句
    #     「听不懂」的兜底意图（恰好是 query_health，所以旧用例看不出问题）。
    #     选「查看设备列表」→ 应执行 query_devices：空库返回 not_found；
    #     若仍是旧行为会跑 query_health（空库也 success），断言即可区分。
    body = chat(client, "嗯……那个")
    assert body["type"] == "clarification", body
    r = client.post("/api/v1/agent/clarify",
                    json={"session_id": body["session_id"],
                          "choice": "查看设备列表"})
    assert r.status_code == 200, r.text
    picked = r.json()
    assert picked["status"] == "not_found", picked
    ok("追问选项被真正执行：选「查看设备列表」跑的是 query_devices（not_found）")

    # 6) 没有 pending 上下文的 /clarify：友好提示，不是 500
    r = client.post("/api/v1/agent/clarify",
                    json={"session_id": "no-such-session", "choice": "x"})
    assert r.status_code == 200 and r.json()["status"] == "failure", r.text
    ok("无待澄清上下文的 /clarify 返回 failure 而非 500")

    # 7) 入参校验：缺 / 空白 message、缺 session_id/choice、非法 JSON 都 → 400
    assert client.post("/api/v1/agent/chat", json={}).status_code == 400
    assert client.post("/api/v1/agent/chat",
                       json={"message": "   "}).status_code == 400
    assert client.post("/api/v1/agent/chat",
                       json={"message": 123}).status_code == 400
    assert client.post("/api/v1/agent/clarify",
                       json={"choice": "x"}).status_code == 400
    assert client.post("/api/v1/agent/clarify",
                       json={"session_id": "s"}).status_code == 400
    r = client.post("/api/v1/agent/chat", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    ok("入参校验：缺/空白 message、缺 session_id|choice、非法 JSON → 400")

    # 8) 防幻觉（核心，两条不变量）：
    #    a) 没有真机领任务并回传时，start_capture 只能 no_evidence；
    #    b) evidence 的语义是「设备真实返回的数据」，所以**不成功时它必须是 None**。
    #       历史实现把 {status:'submitted', poll_timeout_s:3} 塞进 evidence，导致
    #       no_evidence 的结果也带着非空 evidence —— 消费方极易读成「有证据」。
    body = chat(client, "帮我采集一次数据", device_id=DEV)
    assert body["status"] in ("no_evidence", "failure"), body
    assert body["status"] != "success", body
    assert body["evidence"] is None, body["evidence"]
    assert body["data"]["task_accepted"] is True, body["data"]
    ok("防幻觉：无真机时 start_capture 返回 %s 且 evidence=None（绝不 success）"
       % body["status"])

    # 8b) 连点第二次：任务已存在（duplicate）同样不算成功 —— 设备仍未回传数据
    body = chat(client, "帮我采集一次数据", device_id=DEV)
    assert body["status"] != "success", body
    assert body["evidence"] is None, body["evidence"]
    assert body["data"]["duplicate"] is True, body["data"]
    ok("防幻觉：重复点击（已有待执行任务）也不返回 success，evidence=None")

    # 8c) 拍照同理
    body = chat(client, "拍一张照片", device_id=DEV)
    assert body["status"] != "success" and body["evidence"] is None, body
    ok("防幻觉：无真机时 take_photo 返回 %s，绝不 success" % body["status"])

    # 8d) 全响应扫一遍不变量：只要不是 success/not_found，evidence 就必须是 None。
    #     四条会真的去动设备的意图（采集 / 拍照 / 暂停 / 恢复）都在扫描范围内 ——
    #     暂停与恢复是最容易漏掉的那一条（历史实现不等板端回执就自造 evidence +
    #     success，且当时没被这条扫描覆盖）。
    for text, dev in (("帮我采集一次数据", DEV), ("拍一张照片", DEV),
                      ("暂停周期上报", DEV), ("恢复周期上报", DEV),
                      ("查看系统状态", ""), ("看看最新的数据", ""),
                      ("帮我采集一次数据", "someone-elses-board"), ("嗯……那个", "")):
        b = chat(client, text, device_id=dev)
        if b["status"] not in ("success", "not_found"):
            assert b["evidence"] is None, (text, b["status"], b["evidence"])
    ok("不变量扫描：非 success/not_found 的响应一律不带 evidence（含暂停/恢复）")

    # 8e) 控制类任务（暂停 / 恢复）单独断言：没有设备 /applied 回执时只能
    #     no_evidence（绝不 success），evidence 必须为 None。此时任务已由 8d 建好，
    #     所以走的是「连点复用」分支 —— duplicate=true 且复用同一条任务。
    for text in ("暂停周期上报", "恢复周期上报"):
        body = chat(client, text, device_id=DEV)
        assert body["status"] == "no_evidence", body
        assert body["evidence"] is None, body["evidence"]
        assert body["data"]["duplicate"] is True, body["data"]
        assert body["data"]["task_accepted"] is True, body["data"]
    ok("防幻觉：暂停/恢复无设备确认 → no_evidence + duplicate + evidence=None")

    # 9) 设备白名单：AI 不能碰未授权设备（工具校验阶段就被拦下，不走到执行）
    body = chat(client, "帮我采集一次数据", device_id="someone-elses-board")
    assert body["status"] == "failure", body
    assert "不在授权范围" in body["message"], body["message"]
    ok("设备白名单：非白名单设备被拒（failure，未进入工具执行）")

    # 10) 路由器：未知意图 → failure，而不是静默当成功
    res = get_router().route({"intent": "delete_everything", "parameters": {}})
    assert isinstance(res, ToolResult), res
    assert res.status is ToolResultStatus.FAILURE, res
    assert "unknown_intent" in (res.error or ""), res.error
    ok("路由器对未知意图返回 failure(unknown_intent)")

    # 11) 工具层返回值结构 & 证据构造约定
    ev = make_evidence(device_id=DEV, source="qma6100p", ts_ms=123,
                       received_at=1.0, record_count=2)
    assert ev["device_id"] == DEV and ev["record_count"] == 2, ev
    assert ev["received_at"] == 1.0, ev           # 显式给的 received_at 不被覆盖
    bare = make_evidence()                        # 只剔除 None 字段，保留默认值
    assert "device_id" not in bare and "collected_at" not in bare, bare
    assert bare["source"] == "qma6100p", bare
    assert isinstance(bare["received_at"], float), bare
    assert format_time_ago(None) == "未知时间"
    assert format_time_ago(time.time() - 5).endswith("秒前")
    assert format_time_ago(time.time() - 3 * 3600).endswith("小时前")
    assert format_time_ago(time.time() - 3 * 86400).endswith("天前")
    d = ToolResult(status=ToolResultStatus.SUCCESS).to_dict()
    assert set(d) == {"status", "data", "error", "evidence", "message",
                      "ambiguous_options"}, d
    assert d["status"] == "success"
    ok("工具层：make_evidence 剔 None / format_time_ago 分级 / to_dict 结构稳定")

    # 12) 会话表惰性清理：超过 TTL 的条目被摘掉，未过期的保留
    now = time.time()
    agent_api._sessions["stale-sess"] = {
        "_ts": now - agent_api.SESSION_TTL_S - 1}
    agent_api._sessions["fresh-sess"] = {"_ts": now}
    agent_api._clean_expired()
    assert "stale-sess" not in agent_api._sessions
    assert "fresh-sess" in agent_api._sessions
    agent_api._sessions.pop("fresh-sess", None)
    ok("会话表惰性清理：超 TTL 的 session 被摘掉，未过期的保留")

    # 13) 包导入布局：agent 有两种落点，api.py 的相对导入必须都成立。
    #     这正是回归点 —— 用 `..agent.nlu` 时顶层 agent 布局会在**请求期**
    #     抛 "attempted relative import beyond top-level package"（= 500）。
    for path_entry, module_name in ((_HERE, "agent.api"),
                                    (_ROOT, "server.agent.api")):
        code = (
            "import sys, importlib;"
            "sys.path.insert(0, %r);"
            "m = importlib.import_module(%r);"
            "exec('from .nlu import get_nlu_parser', m.__dict__);"
            "exec('from .router import get_router', m.__dict__);"
            "print('LAYOUT_OK')" % (path_entry, module_name))
        p = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                           capture_output=True, text=True, timeout=60)
        assert "LAYOUT_OK" in (p.stdout or ""), \
            (module_name, p.stdout, p.stderr)
    ok("包导入布局：顶层 agent 与 server.agent 两种落点下相对导入均成立")

    # 14) 收尾：清理临时库（与 test_photos.py 同一约定 —— Windows 上进程仍持有
    #     连接时删不掉，所以只清理不断言，正式库/正式 photos 目录从未被写入）
    client.close()
    _remove_db_files(_TEST_DB)
    ok("临时库已清理（正式库未被写入）")

    print("\n全部通过：%d 项断言" % N)


if __name__ == "__main__":
    try:
        main_run()
    finally:
        _remove_db_files(_TEST_DB)