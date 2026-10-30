# 无 API Key 的本地语音助手

当前实现不需要 OpenAI、其他云端模型或任何个人 API Key。没有 Key 时，自然语言
由本地安全关键词解析器处理；浏览器负责语音识别和播报。

## 1. 启动

在仓库根目录执行：

```powershell
server\start_local_nokey.bat
```

该脚本设置 `AGENT_NLU_MODE=keyword`，即使电脑安装了 `openai` 包或存在 Key
环境变量，也不会发起模型 API 请求。

服务端启动后打开：

```text
http://127.0.0.1:8000/ui/
```

页面右侧应显示：

- 语言解析：`关键词回退`
- 识别能力：`Web Speech（浏览器）`
- 麦克风权限：`已允许` / `未选择` / `已拒绝`

点击右侧「测试麦克风」可以先做一次 `getUserMedia` 预检。它会区分：

- 权限拒绝：`NotAllowedError`
- 没有可用设备：`NotFoundError`
- 设备被占用或驱动异常：`NotReadableError`
- 非安全上下文：页面自身的 HTTPS 提示

成功时还会记录设备名称、采样率、声道和轨道状态。

如果仍显示“检测中”，通常是服务端没有重启、浏览器加载了旧 `app.js`，或使用的是
旧代码。先停止旧服务，再重新运行上面的脚本，并在浏览器中按 `Ctrl+Shift+R`。

## 2. 使用文字任务

1. 打开「语音助手」。
2. 在输入框输入明确任务，例如：
   - `查看系统状态`
   - `查看最新数据`
   - `查看采集任务`
   - `采集一次数据`
3. 点击「发送任务」。
4. 页面显示用户消息、Agent 结果和 `request_id` 时序。
5. 勾选“自动播报结果”时使用浏览器 `SpeechSynthesis`。

设备动作必须有设备回执才会显示成功；没有设备证据时显示“等待设备证据”。

## 3. 使用录音

1. 点击「开始录音」。
2. 浏览器第一次会弹出麦克风权限。
3. 说一句简短任务。
4. 再点击「停止录音」。
5. 核对识别文字，点击「发送任务」。

「测试麦克风」和「开始录音」都会先请求一次麦克风；浏览器第一次弹出权限时应选择
“允许”。Edge 设置中的“访问前询问”保持开启是正常的，它不会自动授权某个站点。

浏览器识别目前不是离线模型：Edge/Chrome 可能使用浏览器厂商的语音服务，但不需要
用户配置个人 API Key。如果需要完全离线语音识别，需要另接 Whisper.cpp 或 Vosk，
这不是当前 Web Speech 方案的自动能力。

## 4. `not-allowed` 修复步骤

`not-allowed` 不是 Agent 或 API Key 错误，而是浏览器拒绝使用麦克风。

### 页面来源

不能直接双击 HTML 打开 `file://` 页面。必须通过以下来源访问：

- 本机：`http://127.0.0.1:8000/ui/` 或 `http://localhost:8000/ui/`
- 其他电脑/手机：HTTPS 地址，例如 `https://192.168.1.100:8443/ui/`

### Edge/Chrome

1. 点击地址栏左侧的锁或滑块图标。
2. 找到「麦克风」。
3. 改为「允许」。
4. 刷新页面。

也可以在 `edge://settings/content/microphone` 或
`chrome://settings/content/microphone` 中检查：

- 该站点没有被列入“不允许”。
- 浏览器全局麦克风权限没有被关闭。

### Windows

打开：

```text
设置 → 隐私和安全性 → 麦克风
```

确认：

- 麦克风访问已开启。
- “允许桌面应用访问你的麦克风”已开启。
- 当前浏览器出现在允许列表中。

修改后关闭并重新打开浏览器，再刷新页面。

可以运行只读诊断脚本查看系统实际状态：

```powershell
powershell -ExecutionPolicy Bypass -File server\check_microphone.ps1
```

重点看 `ACTIVE` 输入端点。只有 `NOT_PRESENT` 或 `UNPLUGGED` 时，浏览器没有可录音
的输入设备；需要在 `设置 → 系统 → 声音 → 输入` 中选择或启用麦克风。

### 其他检查

- 确认电脑有可用输入设备，系统录音测试正常。
- 确认没有其他程序独占麦克风。
- 确认不是由组策略统一关闭了浏览器麦克风。
- 如果仍然失败，先使用文字输入；文字路径与录音识别后的路径完全相同。

## 5. 板端麦克风：让 ESP32-S3-EYE 自己录（推荐）

浏览器拿不到板子的麦克风 —— Web Speech API 只认「浏览器所在系统」的采集设备。
所以板端语音换成下面这条链路，全程不需要任何 API Key：

```text
BOOT 键 → 板端录 5 s（16 kHz / 16 bit / 单声道）
        → POST /api/v1/voice（WAV）
        → 电脑本地 Vosk 识别（离线）
        → 网页轮询 /api/v1/voice/latest
        → 文字填进输入框 → 同一条 Agent 链路
```

### 一次性准备

```powershell
pip install -r server\requirements-voice.txt
python server\download_voice_model.py       # 约 42 MB；网络慢可手下载 zip 再 --zip
```

固件也要更新到带 `voice_module_init()` 的版本（BOOT 键录音是这次新加的）：

```powershell
idf.py -p COMx flash
```

> 板端语音让固件超过了 IDF 自带 1.5 MB 的 app 分区，工程改用根目录的
> `partitions.csv`（8 MB flash，app 6 MB）。第一次刷机会一并写入新分区表，
> `nvs` 偏移不变，WiFi 配网数据不受影响。

重启服务端，打开 `http://127.0.0.1:8000/ui/` → 「语音助手」→「板端麦克风」卡片
应显示 **就绪**（识别引擎 `vosk`、模型已就位）。

### 使用

1. 板子上电并连上 WiFi。
2. 按一下 **BOOT** 键（GPIO0），LCD 状态变为 `Voice: rec 5s`。
3. 对着板载麦克风说一句，例如“查看最新数据”，5 秒后自动上传。
4. 页面卡片出现 request_id、识别文字和「▶ 回放」。
5. 勾选“自动发送任务”时直接执行；否则文字只填进输入框，确认后手动点“发送任务”。

### 接口与落盘

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/voice?device_id=&request_id=&sample_rate=` | body 为 WAV 或裸 PCM16LE；同一 `request_id` 幂等 |
| `GET` | `/api/v1/voice/latest?device_id=&after_id=` | 网页游标轮询新记录 |
| `GET` | `/api/v1/voice/status` | 识别引擎 / 模型是否就绪、最近一条 |
| `GET` | `/api/v1/voice/{id}/audio` | 回放 wav |

音频落在 `server/data/voice/<request_id>.wav`（`SENSOR_VOICE_DIR` 可改），
识别结果入 `voice_notes` 表；模型目录用 `VOICE_MODEL_DIR` 覆盖。

### 没装 vosk / 没模型会怎样

不会 500：接口返回 `status=asr_unavailable` + 一句修复提示，音频仍然保存，
页面卡片显示“未就绪”和缺什么。IMU / 任务 / 拍照 / 事件等接口完全不受影响。

### 不接板子也能自测

页面「上传 WAV 自测」选一个 16 kHz 单声道 wav，走的是同一个接口和同一条 Agent。
命令行：`python server/test_voice.py`（用合成音频 + mock 识别，不需要 vosk 和麦克风）。

## 6. 不需要 API Key 的原因

```text
录音 → 浏览器 SpeechRecognition → 输入框
                                  │
键盘输入 ─────────────────────────┤
                                  ▼
                  本地关键词解析 / 受限工具
                                  ▼
                 浏览器 SpeechSynthesis 播报
```

只有在主动设置 `AGENT_NLU_MODE=auto` 或 `llm`，同时安装 `openai` 并配置可用凭据时，
服务端才会尝试调用语言模型。`keyword` 模式不会联网请求模型 API。
