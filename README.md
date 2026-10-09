# data_capture_sim

基于 **ESP32-S3-EYE** 的 IMU 人体动作数据采集系统：板端以 100 Hz 读取三轴加速度计，
通过 WiFi 实时上传到电脑上的 FastAPI 服务端，服务端存储到 SQLite 并提供 Web 监控页面；
支持 5 种动作采集协议、按需采集、实时拍照、摄像头直播、闭环事件，
以及可选的 **AI Agent**（中文自然语言指挥设备，见第七节）。

---

## 一、硬件

| 硬件 | 用途 |
|------|------|
| ESP32-S3-EYE 开发板 | 运行固件、采集与上传 |
| QMA6100P 三轴加速度计 | 板载，采集加速度（100 Hz，I2C） |
| OV2640 摄像头 | 板载，按需拍照（640×480 JPEG） |
| LCD 屏幕 | 实时显示状态 |
| 按钮 A / B / C / D | 启动采集、切换协议、闭环事件 |
| MicroSD 卡 | 本地 CSV 数据落盘 |
| 电脑（PC） | 运行服务端 `server/main.py` |

> 板端与服务端通过**同一局域网**通信，服务端 IP 用电脑的局域网地址（不能用 `127.0.0.1`）。

---

## 二、代码结构

```
data_capture_sim/
├── CMakeLists.txt              # ESP-IDF 顶层工程
├── sdkconfig.defaults          # 默认 SDK 配置
├── .gitignore
├── PROJECT_STRUCTURE.md        # 详细开发文档（代码结构、Git 流程、CI 说明）
│
├── main/                       # 板端固件（ESP-IDF）
│   ├── main.c                  # 主固件
│   ├── CMakeLists.txt
│   ├── Kconfig.projbuild       # 菜单配置（WiFi / 设备 ID / 上传地址）
│   └── idf_component.yml
│
├── server/                     # 服务端（PC，Python/FastAPI）
│   ├── main.py                 # FastAPI 服务
│   ├── requirements.txt
│   ├── agent/                  # AI Agent：自然语言 → 意图 → 受限工具
│   │   ├── api.py              # /api/v1/agent/chat · /clarify
│   │   ├── nlu.py              # LLM 解析 + 本地关键词回退
│   │   ├── router.py           # 意图 → 工具分发
│   │   ├── config.py           # 模型档 / 设备白名单 / 等待回执超时
│   │   ├── prompt.py           # 系统提示词（意图与参数约束）
│   │   └── tools/              # 10 个受限工具（查询 / 采集 / 拍照 / 控制）
│   ├── test_receive.py         # 单元自测
│   ├── test_agent.py           # AI Agent 自测（不需要 openai、不联网）
│   ├── test_photos.py
│   ├── test_events.py
│   ├── e2e_server_check.py     # 端到端自检
│   ├── e2e_ui_check.py
│   ├── e2e_device_check.py
│   ├── e2e_live_check.py
│   ├── photos/                 # 照片目录（gitignore）
│   └── static/                 # Web 页面
│       ├── index.html
│       └── app.js
│
└── docs/                       # 详细设计文档
    ├── ai_agent_work_log.md
    ├── manual_capture_task.md
    ├── realtime_photo_capture.md
    ├── camera_live_stream.md
    ├── loop_trigger_callback.md
    └── …（崩溃修复、上传修复等工作记录）
```

### 数据链路

```
ESP32-S3-EYE                          电脑（PC）                         浏览器
QMA6100P (100Hz)       ──WiFi──▶       FastAPI (server/main.py) ──HTTP──▶ 监控页 /ui/
每 1s 打包 100 点 POST                   校验 → 存 SQLite                  每 0.8s 轮询刷新

按需采集：板端每 3s 轮询 /tasks/next  ◀── tasks 表 ◀────────────────────── 「采集一次最新数据」
每批 N 点 POST（manual，带 request_id）  写库与任务收尾同事务                任务状态 1s 刷新

拍照：建 camera 任务                  ──▶ 板端拍一帧 JPEG ──▶ 落盘 + photos 表   画廊缩略图
直播：长按按钮 A 开/关                 ──▶ 每 ~250ms 一帧（内存态，不落盘）      实时画面卡片
闭环：按钮 C 触发 ──▶ POST /events/trigger ──▶ events 表                  事件卡片
AI Agent：一句中文 ──▶ 意图 ──▶ 受限工具（复用上面同一条任务/查询链路）    /api/v1/agent/chat
```

---

## 三、快速开始

> 前置：板端与电脑同一 WiFi；电脑装好 Python 3.9+；ESP-IDF ≥ 5.4。

### ① 电脑端 —— 启动服务端

```bash
cd server
pip install -r requirements.txt   # 首次
python main.py                    # 监听 http://0.0.0.0:8000
```

记下电脑的局域网 IP：Windows 用 `ipconfig`，找 IPv4 地址（如 `192.168.1.100`）。

### ② 板端 —— 配置并烧录

```bash
idf.py set-target esp32s3        # 首次
idf.py menuconfig                # 配置 WiFi 和上传地址
idf.py build
idf.py -p COMx flash monitor     # COMx = 实际串口
```

`menuconfig` 里需要改的：

| 配置项 | 值 |
|--------|-----|
| `WIFI_SSID` / `WIFI_PASSWORD` | 你的 WiFi |
| `SENSOR_SERVER_URL` | `http://<电脑IP>:8000/api/v1/upload` |
| `SENSOR_DEVICE_ID` | 随便起一个，如 `esp32s3-eye-0001` |

### ③ 浏览器 —— 查看数据

- 监控面板：`http://<电脑IP>:8000/ui/`
- 健康检查：`http://<电脑IP>:8000/api/v1/health`

> Windows 防火墙可能会拦截 `8000/TCP`，放行「专用网络」即可。

---

## 四、板端操作

### 按键

| 操作 | 功能 |
|------|------|
| 短按 A | 启动 / 停止 IMU 采集（SD 卡 CSV 落盘） |
| 长按 A（2s） | 开 / 关摄像头直播 |
| 长按 B（2s） | 循环切换动作协议 |
| 短按 C | 触发闭环事件 |
| 短按 D | 回应闭环事件 |
| 长按 D（2s） | 取消闭环事件 |

### 动作采集协议

长按 B 循环切换，共 5 种：

| 协议 | 动作 | 每组时序 |
|------|------|----------|
| Stand | 站立 | 3×（预备 3s + 20–30s + 间隔 5s） |
| Stairs | 上下楼 | 3×（预备 3s + 20–30s + 间隔 60s） |
| Bend | 弯腰 | 3×（预备 3s + 20–30s + 间隔 60–90s） |
| Jump | 跳跃 | 3×（预备 3s + 5–10s + 间隔 5–10s） |
| Fall | 跌倒 | 3×（预备 3s + 5–10s + 间隔 15–30s） |

另有 6 面标定模式：+X / -X / +Y / -Y / +Z / -Z，每面 10s。

### CSV 数据格式

```csv
label,timestamp_ms,accel_x,accel_y,accel_z
stand,1767123456789,0.02,-0.15,9.81
```

- `label`：动作标签（`stand` / `stairs` / `bend` / `jump` / `fall` / `idle`）
- `timestamp_ms`：epoch 毫秒时间戳
- `accel_x/y/z`：三轴加速度，单位 m/s²

---

## 五、Web 监控页

打开 `http://<IP>:8000/ui/`，主要功能：

| 功能 | 怎么用 |
|------|--------|
| 实时数据面板 | 自动刷新（0.8s），显示最新一批加速度 + 设备状态 |
| 采集一次最新数据 | 填样本数 / 采样率 → 点按钮 → 板端按需采集一批 → 结果实时显示 |
| 暂停 / 恢复周期上报 | 暂停后板端停止 1s 周期上传（采样和落盘照旧），到点自动恢复 |
| 拍一张照片 | 点按钮 → 板端拍 640×480 JPEG → 画廊显示缩略图，可看原图 / 删除 |
| 摄像头实时直播 | 长按板端按钮 A 开启 → 页面显示实时画面（2–4 fps） |
| 三维姿态视图 | 纯 Canvas 手写，拖拽旋转 / 滚轮缩放 / 双击复位 |
| 闭环事件 | 板端按钮 C 触发 → 页面显示「待处理」→ 可回应 / 取消 / 确认完成 |

---

## 六、HTTP 接口（常用）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/upload` | 板端上传传感批次（成功 201） |
| `GET` | `/api/v1/health` | 健康状态 + 汇总 |
| `GET` | `/api/v1/devices` | 设备列表 |
| `GET` | `/api/v1/latest?device_id=…` | 最近一次上报 |
| `POST` | `/api/v1/tasks` | 创建采集/拍照/暂停任务 |
| `GET` | `/api/v1/tasks/next?device_id=…` | 板端领取任务 |
| `POST` | `/api/v1/photos` | 板端上传照片（body = JPEG 字节） |
| `GET` | `/api/v1/photos/{id}` | 取照片 |
| `DELETE` | `/api/v1/photos/{id}` | 删除照片（库 + 文件同时删） |
| `POST` | `/api/v1/agent/chat` | AI Agent：一句中文 → 意图 → 受限工具 |
| `POST` | `/api/v1/agent/clarify` | 回答 AI 的反问（选完候选意图继续执行） |
| `GET` | `/ui/` | 监控面板 |

> 完整接口文档（含闭环事件、直播推流、控制状态等）见 `PROJECT_STRUCTURE.md`。

---

## 七、AI Agent（可选：中文自然语言控制）

服务端内置一个**受限**智能体：把一句中文解析成**意图**，再分发给**受控工具**调用既有接口。
AI 不直接碰硬件，也不能凭空说「成功」——只有设备真实回传的数据才算证据。

支持 10 个意图：`query_history` / `query_latest` / `query_devices` / `query_health` /
`query_tasks` / `query_events` / `start_capture` / `take_photo` /
`pause_periodic` / `resume_periodic`。

| 说一句 | 实际动作 |
|--------|----------|
| 「看看最新的数据」「有哪些设备」「系统状态」 | 只读查询 SQLite，不动设备 |
| 「帮我采集一次数据」「采集 200 个点」 | 建 `kind=capture` 任务 → 等板端回传 → 有证据才报成功 |
| 「拍一张照片」 | 建 `kind=camera` 任务 → 等 JPEG 落盘 |
| 「暂停周期上报」「恢复上报」 | 建 `kind=pause`/`resume` 任务 → 等板端 `/applied` 确认 |

- **防幻觉**：没有设备完成证据时状态是 `no_evidence`（`evidence` 恒为 `null`），绝不返回 `success`；
  连点第二次同样是 `no_evidence`（复用同一条任务，不重复建）。
- **权限**：只能操作 `AGENT_ALLOWED_DEVICES` 白名单内的设备，校验发生在工具执行之前。
- **降级可用**：未装 `openai`、无 key 或断网时自动退回**本地关键词解析**，接口照常可用。
- **追问**：意图不明确或置信度低于阈值时返回 `type=clarification` + 候选 `options`，再用 `/clarify` 继续。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/agent/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"帮我采集一次数据\",\"device_id\":\"esp32s3-eye-0001\"}"
```

响应：`{session_id, type, status, message, data, options, evidence}`，其中
`status` = `success`（有设备证据）/ `no_evidence`（任务已提交但设备没回执）/
`not_found`（查无数据）/ `failure`（参数或权限错误）/ `ambiguous`（需要追问）。
会话存在服务端进程内存里（TTL 15 分钟），重启即丢。

### 接入真实 LLM（可选）

不装也能用（走关键词回退）；要让它真正「听懂」自然语言：

```bash
pip install openai          # 可选依赖，不进 CI
# 默认 AGENT_ENV=dev，读 OPENAI_API_KEY
# 本地 Ollama 离线：AGENT_ENV=local（默认 qwen2.5:7b @ localhost:11434/v1）
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `AGENT_ENV` | `dev` | `dev` 开发模型 / `prod` 低温度稳定 / `local` 走 Ollama |
| `OPENAI_API_KEY`（dev）· `AGENT_API_KEY`（prod） | 空 | 留空 ⇒ 回退关键词解析 |
| `AGENT_DEV_MODEL` / `AGENT_PROD_MODEL` / `AGENT_LOCAL_MODEL` | `gpt-4o-mini` / `gpt-4o-mini` / `qwen2.5:7b` | 模型名 |
| `AGENT_DEV_BASE_URL` / `AGENT_PROD_BASE_URL` / `AGENT_LOCAL_BASE_URL` | OpenAI 官方 / OpenAI 官方 / `http://localhost:11434/v1` | OpenAI 兼容端点 |
| `AGENT_ALLOWED_DEVICES` | `esp32s3-eye-0001` | 设备白名单，逗号分隔 |
| `AGENT_CONFIDENCE_THRESHOLD` | `0.7` | 低于此置信度触发追问 |
| `AGENT_CAPTURE_TIMEOUT_S` / `AGENT_PHOTO_TIMEOUT_S` / `AGENT_CONTROL_TIMEOUT_S` | `60` / `30` / `30` | 等设备回执的上限（秒） |

> 目前只提供 HTTP 接口，Web 页面还没有对话入口。自测 `python server/test_agent.py`
> **不需要 `openai`、不联网**（内部强制走关键词回退分支）。

---

## 八、服务端配置

可用环境变量覆盖默认值：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SENSOR_HOST` | `0.0.0.0` | 监听地址 |
| `SENSOR_PORT` | `8000` | 端口 |
| `SENSOR_DB` | `server/data/upload.db` | SQLite 路径 |
| `SENSOR_TOKEN` | 空（不鉴权） | 可选上传鉴权 |
| `SENSOR_PHOTO_DIR` | `server/photos` | 照片目录 |

数据库共 6 张表：`uploads`（上传批次）、`samples`（采样点）、`tasks`（任务）、`device_control`（暂停状态）、`photos`（照片记录）、`events`（闭环事件）。

---

## 九、自测命令

```bash
python server/test_receive.py      # 单元自测（校验/入库/查询/任务全流程）
python server/test_photos.py       # 拍照链路自测
python server/test_events.py       # 闭环事件自测（23 项）
python server/test_agent.py        # AI Agent 自测（19 项；不需要 openai、不联网）
python server/e2e_server_check.py  # 真实 HTTP 端到端自检
python server/e2e_ui_check.py      # 界面自检（可选；无浏览器则 SKIP）
python server/e2e_device_check.py  # 真机验收（可选；无板则 SKIP）
python server/e2e_live_check.py    # 直播自检
```

> 板端编译验证：`idf.py build`（根目录 `build_idf.bat` 会写 `build_log.txt`，末尾 `BUILD_EXIT=0` = 成功）。

---

## 详细文档

- **代码结构、Git 流程、CI 说明** → `PROJECT_STRUCTURE.md`
- **按需采集 + Web/三维视图** → `docs/manual_capture_task.md`
- **实时拍照** → `docs/realtime_photo_capture.md`
- **摄像头直播** → `docs/camera_live_stream.md`
- **闭环事件** → `docs/loop_trigger_callback.md`
- **AI Agent（需求 · 修复 · 验证记录）** → `docs/ai_agent_work_log.md`
- **崩溃修复、上传修复等工作记录** → `docs/`
