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

## 2. 交互状态

| 状态 | 页面表现 | 可执行动作 |
|------|----------|------------|
| 空闲 | 灰色「空闲」徽章，输入框可编辑 | 输入、录音、发送 |
| 录音 / 识别中 | 录音按钮红色脉冲，显示识别中间结果 | 停止录音、发送已识别文字 |
| 任务执行中 | 黄色徽章，显示 Agent `request_id` | 停止当前任务 |
| 需要澄清 | 展示追问文本和候选按钮 | 选择候选、重新输入、清空 |
| 已完成 / 未证实 / 失败 | 绿 / 黄 / 红状态徽章与结果气泡 | 播报、继续下一轮、停止播报 |
| 已取消 | 回到空闲状态，迟到响应被 generation 丢弃 | 重新输入 |

多轮上下文由 Agent 会话保存，包括最近设备与待澄清意图；会话 TTL 为 15 分钟。
「清空并重输」会清除浏览器保存的会话号、聊天内容与时序记录。

## 3. 停止与迟到结果隔离

页面在每个请求上生成独立的 Agent `request_id`。用户停止时依次执行：

1. 中止浏览器语音识别与语音合成。
2. 对进行中的 `fetch` 调用 `AbortController.abort()`。
3. 递增页面 generation，使旧请求即使已经返回也不会更新当前界面。
4. 调用 `POST /api/v1/agent/cancel`，设置服务端对应的取消 Event。
5. 采集 / 拍照 / 控制工具在轮询等待时被 Event 唤醒，并把尚未结束的设备任务
   标记为 `failed: cancelled by user`。
6. 已取消采集任务后续再上传时返回 `409 TASK_CANCELLED`，避免迟到数据入库。

Agent 服务端会回显 `request_id` 和 `elapsed_ms`。浏览器时序表按请求号记录输入完成、
首次反馈、识别、服务端响应、播报开始/结束、取消与异常，并显示相对输入完成时刻的耗时。

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

- `test_agent.py`：20 项断言通过，含真实在飞采集请求取消与设备任务取消落库。
- `test_receive.py`、`test_photos.py`、`test_events.py`：全部通过。
- `e2e_ui_check.py`：105 项 UI 断言通过；语音分区提供文字、录音、停止任务、
  停止播报、清空重输及 `request_id` 时序。
- 无头 Edge 截图检查：1440×1200 下语音助手分区布局完整，无横向溢出。

真实麦克风识别与扬声器播报仍依赖浏览器权限、设备音频硬件和浏览器对 Web Speech
能力的实现，因此自动化测试验证控件与降级状态，不伪造真实录音内容。
