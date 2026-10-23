# 语音任务助手（Web UI）

## 1. 入口与链路

页面顶部新增「语音助手」分区。文字输入与录音识别结果共用同一个输入框和同一条
Agent 业务链路：

```text
浏览器麦克风 / 键盘
        │
        ├─ SpeechRecognition（zh-CN）→ 输入框
        └─ 键盘输入
        │
        ▼
POST /api/v1/agent/chat 或 /clarify
        │
        ▼
既有受限工具：查询 / 采集 / 拍照 / 暂停 / 恢复
        │
        ▼
页面显示结果 → SpeechSynthesis 播报
```

语音识别只在浏览器中完成，识别文字不会作为音频文件上传到服务端。页面会记录：

- 音频来源：当前浏览器麦克风。
- 采样格式：由浏览器与操作系统音频栈处理，页面不额外转码、不落盘。
- 识别与合成运行位置：当前页面所在浏览器。
- 浏览器不提供 `SpeechRecognition` 时，文字输入与完整 Agent 任务链路仍可用。

页面右侧的「语言解析」来自 `GET /api/v1/agent/status`：

- `LLM · 模型名`：已安装 `openai` 且配置了模型凭据，真实语言模型路径生效。
- `关键词回退`：未安装依赖或未配置凭据，使用本地安全关键词解析。

状态接口不会返回 API Key。安装真实 LLM 依赖：

```powershell
pip install -r server/requirements-agent.txt
```

完全不使用 API Key 时运行 `server\start_local_nokey.bat`；它把
`AGENT_NLU_MODE` 固定为 `keyword`，不会调用任何模型端点。麦克风
`not-allowed` 的排查步骤见 `docs/local_no_key_voice.md`。

## 2. 交互状态

| 状态 | 页面表现 | 可执行动作 |
|------|----------|------------|
| 空闲 | 灰色「空闲」徽章，输入框可编辑 | 输入、录音、发送 |
| 录音 / 识别中 | 录音按钮红色脉冲，显示识别中间结果 | 停止录音、发送已识别文字 |
| 任务执行中 | 黄色徽章，显示 Agent `request_id` | 停止当前任务 |
| 需要澄清 | 展示追问文本和候选按钮 | 选择候选、重新输入、清空 |
| 已完成 / 未证实 / 失败 | 绿 / 黄 / 红状态徽章与结果气泡 | 播报、继续下一轮、停止播报 |
| 已取消 | 回到空闲状态，迟到响应被 generation 丢弃 | 重新输入 |

多轮上下文由 Agent 会话保存，包括最近设备、上一次意图/输入、设备任务号与待澄清
意图；会话 TTL 为 15 分钟。安全的只读短语“再查一次”会复用上一次查询意图。
「清空并重输」会清除浏览器保存的会话号、聊天内容与时序记录。

## 3. 停止与迟到结果隔离

页面在每个请求上生成独立的 Agent `request_id`。用户停止时依次执行：

1. 中止浏览器语音识别与语音合成。
2. 对进行中的 `fetch` 调用 `AbortController.abort()`，并等待最长 3 秒的服务端确认。
3. 递增页面 generation，使旧请求即使已经返回也不会更新当前界面。
4. 调用 `POST /api/v1/agent/cancel`，设置服务端对应的取消 Event。
5. 慢速 NLU 也能立即结束 HTTP 等待；采集 / 拍照 / 控制工具在轮询等待时被
   Event 唤醒，并把尚未结束的设备任务标记为 `failed: cancelled by user`。
6. 已取消采集任务后续再上传时返回 `409 TASK_CANCELLED`，避免迟到数据入库。

如果取消接口失败，页面会明确显示“服务端取消未确认，设备任务可能继续”，不会伪装成
设备已经停止。Agent 服务端会回显 `request_id` 和 `elapsed_ms`，并在服务端日志中记录
请求开始、工具结果、取消和异常。浏览器时序表按请求号记录输入完成、本地反馈、服务端
响应、播报开始/结束、取消与异常；耗时分开展示“本地反馈”和“Agent 完成”。

## 4. 验收检查

```powershell
node --check server/static/app.js
python server/test_agent.py
python server/test_receive.py
python server/test_photos.py
python server/test_events.py
python server/e2e_ui_check.py
```

当前验证结果：

- `test_agent.py`：24 项断言通过，含强制 keyword 模式、安全意图回退、异常输入、多轮上下文、慢 NLU
  取消、真实在飞采集请求取消与设备任务取消落库。
- `test_receive.py`、`test_photos.py`、`test_events.py`：全部通过。
- `e2e_ui_check.py`：110 项 UI 断言通过；除静态控件外，`?autovoice=1` 实际执行一次
  “文字输入 → Agent → 解析模式/结果气泡/耗时日志”链路。
- 无头 Edge 截图检查：1440×1200 下语音助手分区布局完整，无横向溢出。

真实麦克风识别与扬声器播报仍依赖浏览器权限、设备音频硬件和浏览器对 Web Speech
能力的实现，因此自动化测试验证控件与降级状态，不伪造真实录音内容。`SpeechRecognition`
需要安全上下文：本机 `localhost` 可用，其他电脑/手机通过局域网 IP 访问时需要 HTTPS。

## 5. 本机 HTTPS（无 VPS）

板端仍使用 `http://<电脑IP>:8000/api/v1/upload`。浏览器改用本地反向代理：

```powershell
powershell -ExecutionPolicy Bypass -File server/make_dev_https.ps1 -LanIp 192.168.1.100
server\start_https_proxy.bat
```

然后访问 `https://<电脑IP>:8443/ui/`，并在受控客户端信任
`server/certs/dev-ca-cert.pem`。代理与 Uvicorn 共用同一个进程内状态，不会出现两个
独立应用实例导致直播帧或 Agent 会话不一致的问题。
