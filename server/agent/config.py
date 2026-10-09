# -*- coding: utf-8 -*-
"""智能体配置 —— 模型分离（开发 / 生产 / 本地离线）与设备白名单。"""

import os

# ---------------------------------------------------------------------------
# 运行环境切换：通过环境变量 AGENT_ENV 切换
#   dev   = 开发调试（用轻量模型，便宜快速）
#   prod  = 生产运行（用稳定模型，确定性高）
#   local = 完全断网（本地 Ollama）
# ---------------------------------------------------------------------------
AGENT_ENV = os.environ.get("AGENT_ENV", "dev").strip()

# ---------------------------------------------------------------------------
# 模型配置（按环境分离）
# ---------------------------------------------------------------------------
AGENT_CONFIG = {
    "dev": {
        "model": os.environ.get("AGENT_DEV_MODEL", "gpt-4o-mini"),
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "api_base_url": os.environ.get(
            "AGENT_DEV_BASE_URL", "https://api.openai.com/v1"
        ),
        "temperature": 0.3,          # 开发期稍微高一点，方便调 prompt
        "max_tokens": 1024,
        "timeout_s": 30,
    },
    "prod": {
        "model": os.environ.get("AGENT_PROD_MODEL", "gpt-4o-mini"),
        "api_key": os.environ.get("AGENT_API_KEY", ""),
        "api_base_url": os.environ.get(
            "AGENT_PROD_BASE_URL", "https://api.openai.com/v1"
        ),
        "temperature": 0.1,          # 生产环境要确定性，降低幻觉
        "max_tokens": 1024,
        "timeout_s": 30,
    },
    "local": {
        "model": os.environ.get("AGENT_LOCAL_MODEL", "qwen2.5:7b"),
        "api_key": "ollama",         # Ollama 不需要真实 key
        "api_base_url": os.environ.get(
            "AGENT_LOCAL_BASE_URL", "http://localhost:11434/v1"
        ),
        "temperature": 0.1,
        "max_tokens": 1024,
        "timeout_s": 60,             # 本地模型可能慢一些
    },
}

# 获取当前环境配置
def get_agent_config():
    """返回当前环境下的模型配置。"""
    env = AGENT_ENV
    if env not in AGENT_CONFIG:
        env = "dev"
    return AGENT_CONFIG[env]

# ---------------------------------------------------------------------------
# 设备白名单 —— 权限控制的核心
#   AI 只能控制在白名单中列出的设备。新设备必须先加入此列表。
# ---------------------------------------------------------------------------
ALLOWED_DEVICES = os.environ.get(
    "AGENT_ALLOWED_DEVICES", ""
).strip().split(",") if os.environ.get(
    "AGENT_ALLOWED_DEVICES", ""
).strip() else []

# 如果环境变量没设，使用默认设备（板端常见的 device_id）
if not ALLOWED_DEVICES:
    ALLOWED_DEVICES = [
        "esp32s3-eye-0001",
    ]

# ---------------------------------------------------------------------------
# 任务轮询超时 —— 防幻觉的关键
#   工具创建任务后，最多等待此秒数来获取设备返回的真实证据。
#   超时后返回 NO_EVIDENCE，绝不编造"采集成功"。
# ---------------------------------------------------------------------------
CAPTURE_POLL_TIMEOUT_S = int(os.environ.get("AGENT_CAPTURE_TIMEOUT_S", "60"))
CAPTURE_POLL_INTERVAL_S = float(os.environ.get("AGENT_CAPTURE_POLL_INTERVAL_S", "2"))

# 拍照轮询超时（拍照通常更快）
PHOTO_POLL_TIMEOUT_S = int(os.environ.get("AGENT_PHOTO_TIMEOUT_S", "30"))
PHOTO_POLL_INTERVAL_S = float(os.environ.get("AGENT_PHOTO_POLL_INTERVAL_S", "1.5"))

# 控制类任务（暂停 / 恢复周期上报）的轮询超时。
# 控制任务不产生观测数据，收尾靠板端 POST /api/v1/tasks/{id}/applied（同事务写
# device_control）；所以「已生效」同样必须等设备确认，工具不能自己给自己发证据。
CONTROL_POLL_TIMEOUT_S = int(os.environ.get("AGENT_CONTROL_TIMEOUT_S", "30"))
CONTROL_POLL_INTERVAL_S = float(os.environ.get("AGENT_CONTROL_POLL_INTERVAL_S", "2"))

# ---------------------------------------------------------------------------
# NLU 置信度阈值
#   低于此值的意图会被标记为 ambiguous，触发追问
# ---------------------------------------------------------------------------
CONFIDENCE_THRESHOLD = float(os.environ.get("AGENT_CONFIDENCE_THRESHOLD", "0.7"))