# data_capture_sim

基于 **ESP32-S3-EYE** 开发板的 IMU 人体动作数据采集系统：板端以 100 Hz 读取三轴加速度计，
通过 WiFi 实时上传到本地电脑上的 FastAPI 服务端，服务端存储到 SQLite 并提供实时监控 Web 页面；
同时支持 SD 卡本地 CSV 落盘与多种动作采集协议（站立/上下楼/弯腰/跳跃/跌倒）。

---

## 一、使用的硬件

| 硬件 | 说明 | 用途 |
|------|------|------|
| ESP32-S3-EYE 开发板 | Espressif 官方开发板（主控 ESP32-S3） | 运行固件、采集与上传 |
| QMA6100P 三轴加速度计 | 开发板板载，I2C 地址 `0x12`/`0x13`（新驱动 `driver/i2c_master.h`） | 采集加速度数据（100 Hz） |
| OV2640 摄像头 | 开发板板载，DVP 接口 + SCCB（与 IMU 共用 I2C 总线），由 `esp_video` 驱动 | 按需拍摄 640×480 JPEG（Web「拍一张照片」） |
| LCD 屏幕 | 开发板板载，通过 LVGL 显示 | 实时显示状态与采样信息 |
| 板载按钮 A / B | 开发板按键 | A：启动/停止采集；B：切换动作协议 |
| MicroSD 卡 | 插入开发板 SD 卡槽（FATFS） | 本地 CSV 数据落盘 |
| 本地电脑（PC） | 运行服务端 `server/main.py` | 接收、存储、展示数据 |
| WiFi 局域网 | 板端与 PC 处于同一网段 | 数据上传通道 |

> 说明：板端与服务端通过局域网通信，服务端需使用 **PC 的局域网 IP**（不能用 `127.0.0.1`）。

---

## 二、代码结构

```
data_capture_sim/
├── CMakeLists.txt                 # ESP-IDF 顶层工程入口
├── sdkconfig.defaults             # 默认 SDK 配置（esp32s3 / 大分区 / FATFS 长文件名 / PSRAM / 相机 JPEG / I2C）
├── .gitignore
├── PROJECT_STRUCTURE.md           # 代码结构与开发流程详细说明
│
├── main/                          # 板端固件（ESP-IDF 组件）
│   ├── main.c                     # 主固件：采样 + WiFi + SNTP + SD 落盘 + LVGL + 上传 + 任务 + 拍照
│   ├── CMakeLists.txt             # 组件注册与依赖声明（含 esp_video / esp_cam_sensor）
│   ├── Kconfig.projbuild          # 菜单配置（WiFi / 设备 ID / 上传 URL）
│   └── idf_component.yml          # 组件依赖（esp32_s3_eye；IMU 驱动已内置在主固件，见 docs/realtime_photo_capture.md）
│
├── docs/
│   ├── crash_analysis_and_fix.md       # 崩溃分析与修复记录（LVGL 栈溢出）
│   ├── psram_upload_fix.md             # 上传链路失效分析与修复（PSRAM 未启用）
│   ├── manual_capture_task.md          # 按需采集任务（request_id 贯穿）+ Web/三维视图说明
│   ├── manual_capture_task_acceptance.md # 验收手册：3 层自检 + 真机脚本 + 逐条观测点
│   ├── manual_capture_task_work_log.md # 按需采集任务的需求 · 交付 · 验证过程记录
│   ├── realtime_photo_capture.md       # 实时拍照（camera 任务 + JPEG 落盘 + 画廊/删除）设计说明
│   ├── camera_live_stream.md           # 摄像头实时直播（长按 Button A → 内存最新帧 → 页面实时画面）
│   └── loop_trigger_callback.md        # 闭环事件：按键触发 → 本地反馈 → 远端显示 → 回应/取消
│
└── server/                        # 服务端（PC 运行，Python/FastAPI）
    ├── main.py                    # FastAPI 服务（校验 + SQLite + 查询 + 任务 + 照片 + 闭环事件接口）
    ├── requirements.txt           # Python 依赖
    ├── test_receive.py            # 单元自测（临时库）
    ├── test_photos.py             # 单元自测：拍照链路（临时库 + 临时照片目录）
    ├── test_events.py             # 单元自测：闭环事件状态机（临时库）
    ├── e2e_server_check.py        # 真实 HTTP 端到端自检
    ├── e2e_ui_check.py            # 无头浏览器界面自检（可选，需本机 Edge/Chrome）
    ├── e2e_device_check.py        # 真机验收（可选，需板端在线上报；无板则 SKIP）
    ├── e2e_live_check.py          # 摄像头实时直播自检（HTTP + 可选无头浏览器）
    ├── photos/                    # 照片落盘目录（gitignore，见 docs/realtime_photo_capture.md）
    └── static/                    # 实时监控页（静态资源）
        ├── index.html             # 页面结构 + 样式（含闭环事件卡片）
        └── app.js                 # 轮询 + 渲染 + 任务跟踪 + 事件卡片 + 照片画廊 + 三维姿态视图
```

### 数据链路

```
ESP32-S3-EYE 板端                   本地电脑 (PC)                    浏览器
QMA6100P (100Hz)          ──WiFi──▶  FastAPI (server/main.py)  ──HTTP──▶  监控页 /ui/
每 1s 打包 100 点 POST（periodic）      校验 → 存 SQLite                     每 0.8s 轮询刷新
按需采集：板端每 3s 轮询 /tasks/next ◀── tasks 表（request_id） ◀──────── 「采集一次最新数据」
每批 N 点 POST（manual，带 request_id） 写库与任务收尾在同一事务            任务状态 1s 刷新 + 三维视图
暂停周期：同上一条任务链（kind=pause）    写 device_control（暂停真相源）  ◀──────── 「暂停周期 / 恢复周期」
板端 POST /tasks/{id}/applied 确认生效    /api/v1/control 惰性归零到点记录       控制卡片显示停止中/倒计时
实时拍照：板端每 3s 轮询 /tasks/next ◀── tasks 表（kind=camera） ◀──────────── 「拍一张照片」
拍一帧 640×480 JPEG POST /api/v1/photos?request_id=…   落盘 + photos 表 + 任务收尾（同一事务）  画廊缩略图/原图/逐张删除
摄像头直播：板端长按 Button A(2s) 开/关  ──每 ~500ms 一帧 JPEG──▶  POST /api/v1/live?device_id=…（内存态，不落盘不入库）  ◀──── 页面「摄像头实时画面」卡片
闭环事件：按钮 C 触发（灯闪2下/屏变 TRIG）──▶ POST /api/v1/events/trigger（request_id 由板端生成）──▶ events 表  ◀──── 页面「闭环事件」卡片（待处理）
         按钮 D 单击回应 / 长按取消        ──▶ POST /api/v1/events/respond（accept/cancel）                        ◀──── 页面点「回应/取消/确认完成」
         板端每 1s 轮询 /events/status ◀── events 表 终态 ──▶ 灯闪3下 + LOOP:DONE ──▶ 3s 后回 IDLE
```

> 四条上传通道互不干扰：周期上报不带 `request_id`（行为与旧版一致）；按需采集带 `request_id`
> 并由服务端联动任务状态；拍照走 `POST /api/v1/photos`（body 是 JPEG 字节，不是 JSON），
> 同样带 `request_id` 并在同一事务里收尾任务；**摄像头直播**走 `POST /api/v1/live`
> （板端按键开启，服务端只在内存里留最新一帧，不落盘也不入库），`pause`/`resume`
> 不产生任何上传，由板端 `/applied` 收尾。详见 `docs/manual_capture_task.md`、
> `docs/realtime_photo_capture.md` 与 `docs/camera_live_stream.md`。

---

## 三、快速运行（端到端跑通）

> 前置条件：板端与电脑处于**同一 WiFi**；电脑已装 Python 3.9+；ESP-IDF ≥ 5.4。

**① 电脑端 —— 启动服务端**

```bash
cd server
pip install -r requirements.txt     # 首次
python main.py                      # 监听 http://0.0.0.0:8000
```

记下电脑的局域网 IP：Windows `ipconfig`，找 IPv4（如 `192.168.1.100`）。

**② 板端 —— 配置并烧录**

```bash
idf.py set-target esp32s3           # 首次
idf.py menuconfig                   # 或直接改本地 sdkconfig
idf.py build
idf.py -p COMx flash monitor        # COMx = 实际串口
```

`menuconfig` → `Real-Sensor Upload Configuration` 需确认：

| 配置项 | 值 |
|--------|-----|
| `SENSOR_SERVER_URL` | `http://<电脑IP>:8000/api/v1/upload`（不能填 `127.0.0.1`） |
| `SENSOR_DEVICE_ID` | 本组唯一标识，如 `esp32s3-eye-0001` |
| `SENSOR_TOKEN` | 留空（除非服务端也设了同样的值） |

**③ 浏览器 —— 查看实时数据**

- 实时监控面板：`http://<电脑IP>:8000/ui/`（每 0.8 s 自动刷新）
- 极简首页：`http://<电脑IP>:8000/`
- 健康汇总：`http://<电脑IP>:8000/api/v1/health`

板端连上 WiFi 后每 1 s 上传 1 批（100 个采样点 @ 100 Hz）。

**常见问题**

- 页面打不开 → 放行 Windows 防火墙 `8000/TCP`（专用网络）。
- 板端日志 `[upload] attempt x/3 failed ...` → 重试机制在跑；检查 `SENSOR_SERVER_URL` 是否为电脑局域网 IP、是否同网段。
- 板端日志 `[upload] skip: WiFi not connected` → 板端 WiFi 未连上。

> 详细说明见「四、使用说明」；无 VPS 时的部署步骤与课程验证对照见「七、本机替代 VPS 部署（备用路径）」。

---

## 四、使用说明

### 4.1 服务端（PC，先启动）

**环境要求**：Python 3.9+。

```bash
cd server
pip install -r requirements.txt
python main.py
# 默认监听 http://0.0.0.0:8000
```

可用环境变量覆盖默认配置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SENSOR_HOST` | `0.0.0.0` | 监听地址 |
| `SENSOR_PORT` | `8000` | 监听端口 |
| `SENSOR_DB` | `server/data/upload.db` | SQLite 数据库路径 |
| `SENSOR_TOKEN` | 空（不鉴权） | 可选上传鉴权；非空则要求板端发送 `Authorization: Bearer <token>` |
| `SENSOR_PHOTO_DIR` | `server/photos` | 照片落盘根目录（图片路径 `photos/<device_id>/<id>.jpg`） |
| `SENSOR_LIVE_TIMEOUT_S` | `5` | 摄像头直播：超过该秒数没有新帧即视为「已停止推流」（内存态，不落盘） |

**自测**（可选，验证服务端正常）：

```bash
python test_receive.py        # 单元自测（临时库，不污染正式数据）
python test_photos.py         # 拍照链路自测（临时库 + 临时照片目录）
python e2e_server_check.py    # 真实 HTTP 端到端自检
python e2e_ui_check.py        # 界面自检（无浏览器则 SKIP）
python e2e_live_check.py      # 摄像头直播自检（HTTP + 页面断言；无浏览器则只跑 HTTP）
python e2e_device_check.py    # 真机验收（板端不在线则 SKIP）
```

启动后浏览器访问：
- 监控面板：`http://<PC_IP>:8000/ui/`
- 极简首页：`http://<PC_IP>:8000/`

### 4.2 板端（ESP32-S3-EYE）

**环境要求**：ESP-IDF ≥ 5.4（本工程使用 ESP Component Registry 托管依赖）。

**① 配置**（二选一）：

- 方式 A：`idf.py menuconfig`，在 `Project WiFi Configuration` 与 `Real-Sensor Upload Configuration` 菜单中设置。
- 方式 B：直接编辑本地 `sdkconfig`（已 gitignore，不入库）。

| 配置项 | 说明 |
|--------|------|
| `WIFI_SSID` / `WIFI_PASSWORD` | 连接的 WiFi 名称与密码 |
| `SENSOR_DEVICE_ID` | 设备唯一标识（默认 `esp32s3-eye-0001`） |
| `SENSOR_SERVER_URL` | 服务端上传地址，如 `http://192.168.1.100:8000/api/v1/upload` |
| `SENSOR_TOKEN` | 可选上传 Token（默认空=不鉴权）；须与服务端 `SENSOR_TOKEN` 一致 |

> ⚠️ 真实 WiFi 凭据请只写入 `sdkconfig`，勿提交到 `Kconfig.projbuild`（那里保留占位符）。

**相机（实时拍照）配置**：已写入 `sdkconfig.defaults`，正常 `idf.py build` 即可生效，无需 menuconfig：

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `CONFIG_CAMERA_OV2640` | `y` | 启用 OV2640 传感器驱动；不启用时 `esp_video` 找不到传感器，拍照任务会以 `POST /fail` 明确失败 |
| `CONFIG_CAMERA_OV2640_DVP_JPEG_640X480_25FPS` | `y` | 只开 JPEG VGA 这一种格式（板端原样直传 JPEG，不做编码/解码） |
| `CONFIG_CAMERA_OV2640_DVP_DEFAULT_FMT_JPEG_640X480_25FPS` | `y` | 把它设为传感器默认格式（DVP 设备要求 `S_FMT` 的宽高与当前格式完全一致） |
| `CONFIG_ESP_VIDEO_ENABLE_DVP_VIDEO_DEVICE` | `y` | 启用 DVP 视频设备（`/dev/video2`，BSP 的 `BSP_CAMERA_DEVICE`） |
| `CONFIG_I2C_SKIP_LEGACY_CONFLICT_CHECK` | `y` | IMU 已迁到 `driver/i2c_master.h` 与相机共用总线；跳过 legacy 驱动的启动期冲突检查（详见 `docs/realtime_photo_capture.md`） |

> IMU 与相机共用 BSP I2C 总线（端口 1 / GPIO4-5 / 400 kHz），端口不再由旧驱动占用。

**② 编译烧录**：

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p <串口> flash monitor
```

**③ 板端操作**：

| 操作 | 功能 |
|------|------|
| 短按按钮 A | 启动 / 停止普通 IMU 采集（SD 卡 CSV 落盘） |
| 长按按钮 A（2s） | 开 / 关**摄像头实时直播**：板端每 ~500 ms 推一帧 JPEG 到 `POST /api/v1/live`，页面「摄像头实时画面」卡片显示实时画面（约 1-2 fps）；LCD 状态栏显示 `Live:ON/OFF` |
| 长按按钮 B（2s） | 循环切换 5 种动作采集协议 |
| **短按按钮 C（`BSP_BUTTON_3`）** | **触发闭环事件**：绿灯闪 2 下 → LCD 显示 `LOOP:TRIG <request_id前4位>` → 上报 `POST /api/v1/events/trigger`，页面「闭环事件」卡片出现「待处理」 |
| **短按按钮 D（`BSP_BUTTON_4`）** | **回应**闭环事件（accept）：绿灯闪 1 下 → LCD 显示 `LOOP:WAIT` |
| **长按按钮 D（2s）** | **取消**闭环事件（cancel）：绿灯闪 4 下 → LCD 显示 `LOOP:CANCEL` |

**5 种动作采集协议**（长按按钮 B 切换）：

| 协议 | 动作 | 每组时序 | 输出 CSV |
|------|------|----------|----------|
| Stand | 站立 | 3×（预备 3s + 站立 20–30s + 间隔 5s） | `stand_1/2/3.csv` |
| Stairs | 上下楼 | 3×（预备 3s + 上下楼 20–30s + 间隔 60s） | `stairs_1/2/3.csv` |
| Bend | 弯腰 | 3×（预备 3s + 弯腰 20–30s + 间隔 60–90s） | `bend_1/2/3.csv` |
| Jump | 跳跃 | 3×（预备 3s + 跳跃 5–10s + 间隔 5–10s） | `jump_1/2/3.csv` |
| Fall | 跌倒 | 3×（预备 3s + 跌倒 5–10s + 间隔 15–30s） | `fall_1/2/3.csv` |

另有 **6 面标定模式**：+X / -X / +Y / -Y / +Z / -Z，每面 10s。

**CSV 数据格式**（UTF-8）：

```
label,timestamp_ms,accel_x,accel_y,accel_z
```

- `label`：动作标签（`stand` / `stairs` / `bend` / `jump` / `fall` / `idle`）
- `timestamp_ms`：采样时间戳（epoch ms）
- `accel_x/y/z`：三轴加速度，单位 m/s²

### 4.3 浏览器监控页（仅刷新 / 采集一次 / 三维视图）

打开 `http://<PC_IP>:8000/ui/`：

| 控件 | 行为 |
|------|------|
| 设备下拉 | 来自 `/api/v1/devices`（板端至少上传过一次才会出现） |
| **仅刷新（不采集）** | 只重新拉取查询接口，不打扰板端 |
| **采集参数表单** | 样本数（默认 100，上限 **600**）、采样率（默认 100 Hz，上限 **100**）、有效期（默认 60 s，5–600）；越界在本地就拒绝，不发请求 |
| **采集一次最新数据** | 按表单参数创建任务 → 板端轮询领取 → 暂停周期上报并按参数采集一批 → 带 `request_id` 回传；任务卡片实时显示状态与时间戳 |
| **暂停时长 s** | 「暂停周期」的持续时间（默认 120，服务端允许 **5–600**；越界在本地就拒绝，不发请求） |
| **暂停周期** | 建一条 `kind=pause` 任务 → 板端领取后**停喂 1 s 周期上传窗口**（采样/落盘/刷屏照旧，手动采集仍可用）→ 板端 `POST /applied` 确认生效；到点自动恢复 |
| **恢复周期** | 建一条 `kind=resume` 任务，提前结束暂停窗口（方向相反的控制任务不会同时排队：新的会把旧的置 `failed: superseded by newer control task`） |
| **拍一张照片** | 建一条 `kind=camera` 任务 → 板端领取后用 esp_video/DVP 拍一帧 **640×480 JPEG** → `POST /api/v1/photos?device_id=…&request_id=…` 原样上传 → 服务端落盘 + 入库 + 同一事务把任务置 `completed`；完成后画廊立刻刷新 |
| **摄像头实时画面** | 不是按钮，而是**板端长按按钮 A（2 s）**开启的直播：板端每 ~500 ms 推一帧 JPEG 到 `POST /api/v1/live?device_id=…&ts_ms=…&w=…&h=…`，服务端只在内存里保留该设备**最新一帧**（不落盘、不进 `photos` 表、不建任务）；本卡片按 `GET /api/v1/live` 的 `seq` 轮询 `GET /api/v1/live/frame` 刷新 `<img>`，约 **1-2 fps**；再长按一次停止 |
| **闭环事件**（板端发起，方向相反） | **不是 Web 下发的按钮，触发只来自板子上的按钮 C**：按 C → 板端本地先闪灯/改屏 → `POST /api/v1/events/trigger`（`request_id` 由**板端生成**）→ 卡片出现「待处理」；按 D 单击回应 / 长按取消 → `POST /api/v1/events/respond`；本卡片可点「回应 / 取消 / 确认完成」→ 板端 1 s 轮询 `GET /api/v1/events/status` 读到终态即闪灯收尾。表尾列最近 8 条事件流水 |

- 任务状态机：`submitted → dispatched → acked → completed`，失败为 `failed`，超期未完成变 `timeout`
  （默认有效期 60 s，页面显示剩余时间）。
- **连点不会并行采集**：同一设备已有未完成任务时，服务端直接复用并返回 `duplicate=true`。
- 数据卡片新增「触发方式」与「任务号」，可判断最近一批是 `periodic` 还是 `manual`、属于哪个任务。
- 任务卡片补齐 `dispatched_at` / `acked_at` / `completed_at` 与关联批次 `upload_id`（可一路追到具体批次）；
  任务停在 `dispatched` 超过 5 s 仍无回执时给出黄色提示（板端 ack 失败只写设备串口日志，服务端状态不会变）。
- 页面下方两张表：「手动批次 vs 周期批次」对照（走 `/api/v1/latest?trigger=manual|periodic`，
  用来确认手动采集没有污染周期链路）与「任务历史（最近 20 条）」（走 `/api/v1/tasks?device_id=…&limit=20`，
  多一列「类型」区分 按需采集 / 暂停周期 / 恢复周期）。
- **「周期上报控制」卡片**（暂停 / 恢复）：状态来自 `GET /api/v1/control?device_id=…`，
  真相源是服务端 `device_control` 表（由板端 `POST /api/v1/tasks/{id}/applied` 写入，见 §五），
  不靠「周期批次数为 0」反推；显示 `periodic_paused` / 剩余秒数 / 自动恢复时刻 `paused_until` /
  最近控制任务 `request_id`。暂停到期由**板端自愈 + 服务端惰性归零**双向保证（忘点「恢复周期」
  也不会永久静默），板端状态只存 RAM，重启即恢复正常上报。
- 页面底部「三维姿态视图」：纯 Canvas 2D 手写正交投影（无外部库、无 CDN，局域网离线可用），
  画三轴箭头、当前加速度向量与其分量/地面投影、重力参考 `g=9.81`、最近 30 s 轨迹；
  支持**拖拽旋转、滚轮缩放、双击或按钮复位、自动旋转开关**。
- **「实时拍照」卡片**：点「拍一张照片」→ 任务卡片显示 `kind=实时拍照` 与关联照片；
  下方画廊显示该设备最近 12 张缩略图（`GET /api/v1/photos?device_id=…&limit=12`），
  每张显示 `#id / 大小 KB / 宽×高 / 接收时间 / 任务号`，点图或「原图」在新标签页看原尺寸，
  点「删除」= `DELETE /api/v1/photos/{id}`（**同时删除数据库记录与磁盘 JPEG**，二次确认后执行），
  删除后画廊与计数立即刷新。板端没有相机/未启用 `CONFIG_CAMERA_OV2640` 时任务会明确
  `failed`（错误信息来自板端 `POST /fail`），不会静默等到超时。
- **「摄像头实时画面」卡片**：板端长按按钮 A 后自动出现实时画面（`<img src="/api/v1/live/frame?device_id=…&seq=…">`，
  服务端带 `Cache-Control: no-store`），徽章显示「推流中 · 640×480」，并在右侧列出
  `seq` / 分辨率 / 单帧大小 / 帧龄 / 板端拍摄时刻 / 来源 IP；超过 5 s 没有新帧
  （板端停止推流 / 断网 / 掉电）徽章变「已停止（保留最后一帧）」，画面停在最后一帧而不是报错；
  从未推流的设备显示「未推流」。直播与「拍一张照片」共用相机，板端用互斥锁串行化，
  不占用 IMU 采样器、不暂停周期上报。
- 自动化核对：打开 `/ui/?autocapture=1` 会触发与按钮完全相同的代码路径，可再加
  `&samples=250&rate=50&timeout=45` 指定参数（便于无人值守验证表单参数真的传到了任务里）；
  `/ui/?autopause=1&pause=90` 触发「暂停周期」、`/ui/?autoresume=1` 触发「恢复周期」、
  `/ui/?autophoto=1` 触发「拍一张照片」，同样走的不是测试专用分支，而是按钮的事件处理函数。
- 自检脚本：`test_receive.py`（43 项）、`test_photos.py`（16 项）、`test_events.py`（23 项）、
  `e2e_server_check.py`（23 项，含 9 项闭环事件）、
  `e2e_ui_check.py`（90 项，含 9 项事件卡片渲染；无浏览器则 SKIP）、`e2e_live_check.py`（28 项，含无头页面断言；无浏览器则只跑 HTTP 部分）、
  `e2e_device_check.py`（真机 28 项，无板则 SKIP）。

> 完整说明（接口字段、状态机、板端实现、验证场景）见 `docs/manual_capture_task.md`；
> 需求来源、交付物清单、实测结果与遗留事项见 `docs/manual_capture_task_work_log.md`；
> 逐条验收清单（含真机脚本与明确不验收的边界）见 `docs/manual_capture_task_acceptance.md`。

---

## 五、服务端 HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/upload` | 接收板端上传的传感批次（校验后入库，成功返回 201） |
| `GET` | `/api/v1/health` | 健康状态 + 汇总（总批次 / 总样本 / 设备列表 / 最近上报） |
| `GET` | `/api/v1/devices` | 设备列表 |
| `GET` | `/api/v1/latest?device_id=…` | 某设备最近一次上报（含首末样本、`trigger`、`request_id`）；可选 `&trigger=manual\|periodic` 只看该来源的最近一批（页面「手动 vs 周期」对照区用） |
| `POST` | `/api/v1/tasks` | 创建任务（Web 用；`kind=capture`（缺省）/`pause`/`resume`，`pause` 另带 `duration_s`）；同 `device+source+kind` 已有未完成任务时复用并返回 `duplicate=true` |
| `GET` | `/api/v1/tasks` | 任务列表（`?device_id=…&limit=n`，默认最近 20 条；含 `kind`/`kind_cn`/`duration_s`/`is_control`） |
| `GET` | `/api/v1/tasks/next?device_id=…` | 板端轮询领取任务（原子领取，只返回 `submitted` 且未过期的任务） |
| `GET` | `/api/v1/tasks/{request_id}` | 按任务号查询状态与关联上传摘要 |
| `POST` | `/api/v1/tasks/{request_id}/ack` | 板端回执「已收到任务」 |
| `POST` | `/api/v1/tasks/{request_id}/fail` | 板端上报任务失败（原因写入 `error`） |
| `POST` | `/api/v1/tasks/{request_id}/applied` | 板端回执「控制任务已生效」（仅 `pause`/`resume`）：置 `completed` 并写 `device_control`；采集任务调用返回 `409 WRONG_KIND`（它必须靠带 `request_id` 的上传收尾）。可选 body `{"paused_until_ms": …}` 上报板端自己的定时器终点 |
| `GET` | `/api/v1/control?device_id=…&source=…` | 板端周期上报的暂停状态（`device_control` 真相源，到点惰性归零）：`periodic_paused` / `remaining_s` / `paused_until_str` / `request_id` |
| `POST` | `/api/v1/photos?device_id=…&request_id=…&ts_ms=…&w=…&h=…&note=…` | 板端上传一帧 JPEG（**body 即图片字节**，`Content-Type: image/jpeg`）；校验 SOI 魔数与大小上限（512 KiB），落盘 `server/photos/<device_id>/<id>.jpg`，带 `request_id` 时在同一事务里把 `kind=camera` 任务置 `completed`（同 `request_id` 重传幂等，返回 200 + `idempotent=true`） |
| `GET` | `/api/v1/photos?device_id=…&limit=12` | 照片列表（新的在前；`limit` 1–200，默认 20；含 `bytes`/`width`/`height`/`size_kb`/`received_at_str`/`url`） |
| `GET` | `/api/v1/photos/{id}` | 取一张照片的 JPEG 字节（画廊 `<img src>` 直接用它）；不存在 404，库里有行但文件被删 410 |
| `DELETE` | `/api/v1/photos/{id}` | 删除一张照片（先删库行再删文件，返回 `file_deleted`）；重复删除 404 |
| `POST` | `/api/v1/live?device_id=…&ts_ms=…&w=…&h=…` | 板端推一帧直播画面（**body 即 JPEG 字节**，`Content-Type: image/jpeg`）；校验 `device_id` / SOI 魔数 / 大小上限（512 KiB），成功 201 + 全局单调 `seq`；**只写内存、不落盘不入库、不建任务** |
| `GET` | `/api/v1/live?device_id=…` | 直播状态：`active`（超过 `SENSOR_LIVE_TIMEOUT_S`，默认 5 s，没有新帧即 `false`）/ `seq` / `age_ms` / 分辨率 / 字节数 / 时间戳 / `devices[]`；未知设备返回 200 + `active=false` |
| `GET` | `/api/v1/live/frame?device_id=…` | 最新一帧 JPEG 字节（页面 `<img src>` 直接用；`Cache-Control: no-store`）；从未推流 404 |
| `POST` | `/api/v1/events/trigger` | **闭环事件：板端按键触发**。body 为 JSON（`device_id`/`source`/`kind`/`request_id`/`ts_ms`）；`request_id` 由**板端生成**并作主键，同号重传返回 200 + `idempotent=true`（同一次按键只算一条事件） |
| `POST` | `/api/v1/events/respond` | **闭环事件：回应 / 取消 / 确认完成**（板端与 Web 共用）。body `{"request_id":…,"action":"accept\|cancel\|confirm","by":"device\|web"}`；重复同动作幂等 200，终态不可回退 409，未知 id 404 |
| `GET` | `/api/v1/events/status?device_id=…&request_id=…` | **板端轮询事件状态**。返回 `found` / 顶层 `status` / 完整 `event`；未知 `request_id` 返回 **200 + found=false**（轮询过渡态，不算错误）；不带 `request_id` 时回该设备最近一条 |
| `GET` | `/api/v1/events?device_id=…&limit=8&active=1` | 事件列表（新的在前，`limit` 1–200），附 `pending` 计数供页面红色徽章；`active=1` 只看未终态 |
| `GET` | `/` | 极简 HTML 首页 |
| `GET` | `/ui/` | 实时监控面板 |

**上传数据格式**（`POST /api/v1/upload`）：

```json
{
  "device_id": "esp32s3-eye-0001",
  "source": "qma6100p",
  "unit": "m/s^2",
  "ts_ms": 1767123456789,
  "request_id": "8f3c…",             // 可选：按需采集任务号（manual 批次才有）
  "trigger": "manual",               // 可选：periodic（周期上报）/ manual（按需采集）
  "samples": [
    {"i": 0, "t_ms": 0,  "ax": 0.02, "ay": -0.15, "az": 9.81},
    {"i": 1, "t_ms": 10, "ax": 0.03, "ay": -0.14, "az": 9.80}
  ]
}
```

**数据库**（SQLite，六张表）：

| 表 | 说明 |
|----|------|
| `uploads` | 每个上传批次一行（设备、来源、单位、起始时间、样本数、来源 IP、原始 JSON、`request_id`、`trigger`） |
| `samples` | 每批内每个采样点一行（`ax`/`ay`/`az` + 相对时间偏移） |
| `tasks` | 任务（`request_id` 主键、设备、`kind`（`capture`/`camera`/`pause`/`resume`）、`duration_s`、状态、状态时间戳、有效期、关联 `upload_id`、错误原因） |
| `device_control` | 每个 `device+source` 一行的暂停状态（`periodic_paused`、`paused_until`、`request_id`、`updated_at`）——「是否真的暂停」的真相源 |
| `photos` | 每张照片一行（`device_id`、`request_id`、`ts_ms`、`received_at`、`bytes`、`width`、`height`、`ip`、`path`、`note`）；**图片字节不入库**，只存路径，文件在 `server/photos/<device_id>/<id>.jpg` |
| `events` | 闭环事件（板端发起）：`request_id` 主键、`device_id`、`source`、`kind`、`status`（`pending`/`ack`/`cancelled`/`completed`/`expired`）、`created_at`、`device_ts_ms`、`responded_at`/`response`/`responded_by`、`closed_at`、`expires_at`、`ip`、`note`。**与 `tasks` 分表**：`tasks` 是「服务端派发、板端执行」，把板端发起的事件塞进去会被 `/tasks/next` 领回来形成回环 |

- 老库升级：`CREATE TABLE IF NOT EXISTS` 不会给已有表加列，服务端启动时用 `PRAGMA table_info`
  检查并 `ALTER TABLE` 补 `uploads.request_id` / `uploads.trigger`（再建 **部分唯一索引**
  `idx_uploads_request`：同 `request_id` 只允许一条上传 → 重复上传幂等且不重复写样本）
  与 `tasks.kind` / `tasks.duration_s`（历史任务自动按 `capture` 看待，Web 渲染不受影响）。
- 任务超时是**惰性判定**：创建/查询/领取路径先把过期的非终态任务置 `timeout`（无后台线程，
  服务重启后状态依然自洽），过期任务也不会被 `/tasks/next` 下发。
- 暂停到期同样是**惰性归零**：任何读控制状态的路径（`/api/v1/control`、`/api/v1/devices`、
  `/api/v1/tasks/{id}/applied`）先把 `paused_until` 已过的行置回「未暂停」，同样不需要后台线程。

---

## 六、注意事项

1. **网络**：板端与 PC 须处于同一局域网；`SENSOR_SERVER_URL` 使用 PC 的局域网 IP，不能填 `127.0.0.1`。
2. **凭据安全**：真实 WiFi 密码只写入 `sdkconfig`（已 gitignore），`Kconfig.projbuild` 只保留占位符。
3. **上传策略**：板端上传带**有界重试**（最多 3 次，500 ms / 1 s 退避；4xx 表示服务端拒绝载荷、不再重试），RAM 队列 8 批（约 8 s）作为慢网缓冲。重试耗尽或队列满时仍会丢弃批次——这是为保证 100 Hz 采样不被网络阻塞的刻意取舍；**批量落盘重放 / 掉电续传未实现**。
4. **鉴权**：默认不鉴权，面向局域网联调。如需公网部署：给服务端设 `SENSOR_TOKEN`，并同步配置板端 `SENSOR_TOKEN`，板端会带 `Authorization: Bearer <token>`，不匹配返回 `401`。注意本机直连仅为「备用路径」，正式公网部署仍需在 VPS 上补 TLS/反向代理与进程守护。
5. **CORS**：监控页 `/ui/` 与接口同源，浏览器不会触发跨域；板端上传是 HTTP 客户端而非浏览器，与 CORS 无关，故未配置 CORS 中间件。
6. **控制任务需新版固件**：`pause`/`resume` 依赖板端解析 `kind` 字段。若板端仍是本次改动前的固件，
   它会把控制任务当成一次普通采集（`sample_count`/`sample_rate_hz` 的默认值恰好合法）——
   服务端会用 `409 WRONG_KIND` 拒绝这次上传（**不会写入任何样本**），任务最终变 `failed`。
   使用「暂停周期 / 恢复周期」前请先 `idf.py build && idf.py flash` 重新烧录，并跑
   `python server/e2e_device_check.py` 验收（该脚本会打印明确的固件过旧提示）。
7. **拍照需新版固件**：`kind=camera` 同样需要重新烧录（旧固件不认这个 `kind`，会把拍照任务
   当成采集任务并回传样本 → 服务端 `409 WRONG_KIND` 把任务置 `failed`）。
   相机在开机时初始化（`bsp_camera_start()`），失败**不影响** IMU 采集与上传，
   只会让拍照任务收到 `camera not initialized on device` 的明确失败原因。
8. **IMU 与相机共用一条 I2C 总线**：QMA6100P 已从 legacy 驱动（`driver/i2c.h`）迁到
   `driver/i2c_master.h`，与相机 SCCB 共用 BSP 端口 1（GPIO4/5、400 kHz）。
   两个驱动同时被链接时 esp-idf 会在启动期 `abort()`，因此 `sdkconfig.defaults` 里
   显式设置 `CONFIG_I2C_SKIP_LEGACY_CONFLICT_CHECK=y`（已不使用 legacy 驱动，跳过检查是安全的）。
   若 IMU 读数异常，先看板端日志里的 `Detected QMA6100P at 0x12/0x13` 与 `[photo]` 行。
9. **照片占用磁盘**：每张 640×480 JPEG 约 20–60 KB，服务端不做自动清理；图片与元数据
   都可用画廊的「删除」按钮（或 `DELETE /api/v1/photos/{id}`）逐张清理，
   `server/photos/` 已随 `server/data/` 一起 gitignore。
10. **摄像头直播占用的是"带宽"，不是磁盘**：直播帧只存在服务端内存里（每台设备一帧，
    ≤ 512 KiB），服务端重启后画面回到「未推流」，等板端下一帧即恢复；板端侧
    **长按按钮 A（2 s）**开/关，WiFi 掉线或连续 5 帧上传失败会自动停止推流并写串口日志。
    约 1-2 fps × 每帧几十 KB ≈ 数十 KB/s，长时间开着会持续占用 WiFi 带宽（100 Hz
    周期上报仍然优先，直播任务的优先级最低），不用时请再长按一次关闭。
11. **板载绿灯 GPIO3 必须配成开漏输出**：闭环事件的本地灯光反馈用 GPIO3
    （`BSP_LED_1_IO`）。乐鑫官方硬件手册明确要求
    「GPIO3 must be set up in open-drain mode. **Pulling GPIO3 up may burn the LED.**」，
    因此**不能**用 BSP 的 `bsp_led_indicator_create()`/`bsp_led_set()`——那条路径走
    `led_indicator_gpio`，内部是推挽（`GPIO_MODE_OUTPUT`），正是官方警告要避免的配置；
    本功能直接 `gpio_config()` 成 `GPIO_MODE_OUTPUT_OD`。
    开漏下哪个电平算"亮"取决于 LED 接法，所以留了两个编译期开关：
    `LOOP_LED_ON_LEVEL`（默认 1，**上板若亮灭相反改成 0**）与
    `LOOP_LED_REST_ON`（默认 0=平时灭；改 1 可当电源指示灯常亮）。
12. **闭环按键用的是 `BSP_BUTTON_3` / `BSP_BUTTON_4`**：这两颗不是独立 GPIO，
    而是与 A/B 共用同一个 ADC 通道的电阻梯按键（详见 `docs/loop_trigger_callback.md`），
    `iot_button` 已把它们封装成独立设备，注册方式与 GPIO 按键一致。
    按键回调里**只置标志 + 闪灯 + 改屏**，HTTP 一律交给 `loop_task` ——
    回调运行在 `iot_button` 的任务上下文里，栈很小，既不能 `vTaskDelay` 也不能发请求。
13. **闭环事件与动作协议共用 LCD 那条黄色状态行**：优先级为
    「动作协议 > 闭环状态 > `s_status_text`」。屏幕只有 240×240，状态栏已贴底，
    硬塞第四行会被裁掉，因此协议运行时闭环文本会被临时盖住（协议结束即恢复）。
14. **事件状态的所有读路径都要先做惰性过期**：`pending`/`ack` 超 TTL 后的 `expired`
    不是后台线程改的，而是**每条读取路径自己先判**（`_expire_stale_events()`）。
    少调一处就会出现口径矛盾——实测 `/api/v1/health` 漏调时，它报「待处理 2 条」
    而 `GET /api/v1/events` 只返回 1 条，排查时极易被误导。新增任何读 `events`
    的接口（尤其统计类）时，务必在 `SELECT` **之前**补上这次调用。

---

## 七、本机替代 VPS 部署（备用路径）

没有 VPS 时，用本机 `server/main.py` 充当接收端，板端经**同一局域网**上传，浏览器访问本机页面查看：

```
ESP32-S3-EYE ──WiFi(局域网)──▶ 本机 PC:8000 (FastAPI + SQLite) ──▶ 浏览器 /ui/
```

步骤：

1. 查本机局域网 IP（Windows `ipconfig` / Linux,macOS `ifconfig`），例如 `192.168.149.97`。
2. 启动服务端（`SENSOR_HOST` 默认 `0.0.0.0`，局域网可达）：
   ```bash
   cd server && python main.py
   ```
3. 板端配置 `SENSOR_SERVER_URL = http://<本机IP>:8000/api/v1/upload`，`SENSOR_DEVICE_ID` 用本组唯一标识（`idf.py menuconfig` 或改本地 `sdkconfig`）。
4. 烧录运行，浏览器打开 `http://<本机IP>:8000/ui/` 观察实时数据。

> - Windows 防火墙若拦截，需放行 `8000/TCP`（专用网络）。
> - **公网部署待补**：VPS 上的 TLS/反向代理、`SENSOR_TOKEN` 鉴权、systemd 等进程守护、限流。

### 课程验证对照

| 验证点 | 怎么看 |
|---|---|
| 真实传感源 | 板端 IMU 100 Hz 采集，`source=qma6100p` |
| 单位与时间 | `/ui/` 显示 `m/s^2` 与板端 NTP(epoch ms) 时间；`/api/v1/latest` 可逐值核对 |
| 数据来自本组设备 | 页面/查询接口显示 `device_id`，`/api/v1/devices` 只列本组标识 |
| 页面未写死数值 | 所有数值均来自 SQLite 实时读库 |
| 停采后保留旧时间、提示未更新 | 停止采集 30 s 后状态由「更新中」变「未更新」 |
| 显示无数据 | 空库时首页/面板显示「无数据」 |
| 按需采集链路 | `/ui/` 点「采集一次最新数据」→ 任务卡片从 `submitted` 走到 `completed`；板端日志有 `[task] accepted/capturing/captured/done` |
| 暂停周期上报（采集窗口内） | 采集窗口内服务端只收到带该 `request_id` 的 manual 批次，周期批次计数为 0 |
| 暂停周期上报（Web 按钮） | `/ui/` 点「暂停周期」→ 控制卡片显示「停止中」与倒计时；板端日志有 `[ctrl] pause applied`，此后服务端**没有任何新的 periodic 批次**；点「恢复周期」（或等到期）后周期批次重新出现 |
| request_id 可追溯 | `uploads.request_id` ↔ `tasks.request_id` ↔ 页面「任务号」，`/api/v1/tasks/{id}` 返回关联 `upload_id` |
| 连点不并行 | 连续点击 5 次：任务列表只有 1 条非终态任务，同一 `request_id` 只有 1 条上传 |
| 掉电/超时 | 断开板端电源后任务在有效期（默认 60 s）变为 `timeout`，不产生上传；重启服务端状态不变，`/tasks/next` 返回 `found:false` |
| 三维视图 | 拖拽/滚轮/双击可旋转缩放复位；向量指向与 `ax/ay/az` 数值一致（静止时贴近重力参考） |
| 实时拍照链路 | `/ui/` 点「拍一张照片」→ 任务卡片 `kind=实时拍照` 从 `submitted` 走到 `completed`，画廊出现该照片缩略图（含尺寸/大小）；板端日志有 `[photo] captured … for <request_id>` 与 `[photo] upload OK` |
| 照片落盘与删除 | `server/photos/<device_id>/<id>.jpg` 与 `photos` 表一一对应；点「删除」后文件与库行同时消失，再次 `GET /api/v1/photos/{id}` 返回 404 |
| 闭环触发（按键 → 本地反馈） | 按按钮 C：绿灯闪 **2** 下、状态行变 `LOOP:TRIG xxxx`（**先于网络**发生）；串口 `[loop] state=TRIGGERED` |
| 闭环远端显示 | 页面「闭环事件」卡片出现该 `request_id`、状态「待处理」、徽章变红；表尾流水新增一行 |
| 闭环回应 / 取消 | 按钮 D 单击=回应（闪 1 下 → 页面「已回应」）、长按 2 s=取消（闪 4 下 → 「已取消」）；页面也能点「回应 / 取消 / 确认完成」 |
| 闭环收口 | 页面点「确认完成」→ 按钮侧轮询到终态 → 绿灯闪 **3** 下、状态行 `LOOP:DONE`，3 s 后回 `LOOP:IDLE` |
| 闭环连点不重复 | 快速按 5 次按钮 C：只有 1 条事件（`request_id` 由板端生成，服务端主键去重），串口 `trigger ignored: already in TRIGGERED` |
| 闭环关机 / 超时 | 触发后直接断电：事件停在「待处理」，TTL（默认 300 s）后自动「已过期」；网络一直不通时板端 60 s 本地兜底 → `LOOP:TIMEOUT` |

### 自测

```bash
python server/test_receive.py     # 单元自测（临时库）：校验/入库/查询/可选鉴权/任务全流程
python server/test_photos.py      # 拍照链路自测（临时库 + 临时照片目录）：上传/取图/列表/删除/任务收尾
python server/test_events.py      # 闭环事件自测（临时库，23 项）：状态机/幂等/终态不可回退/惰性过期/鉴权
python server/e2e_server_check.py # 真实 HTTP 端到端自检（含任务创建-领取-回传-幂等 + 闭环事件全流程）
python server/e2e_ui_check.py     # 界面自检（可选，需本机 Edge/Chrome；无浏览器则 SKIP 退出）
python server/e2e_device_check.py # 真机验收（可选，需板端在线；板端不在线则 SKIP 退出）
```

> 板端固件侧的编译验证：`idf.py build`（本仓库根目录的 `build_idf.bat` 会写 `build_log.txt`，
> 末尾的 `BUILD_EXIT=0` 表示成功）。
