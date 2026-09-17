# data_capture_sim

基于 **ESP32-S3-EYE** 开发板的 IMU 人体动作数据采集系统：板端以 100 Hz 读取三轴加速度计，
通过 WiFi 实时上传到本地电脑上的 FastAPI 服务端，服务端存储到 SQLite 并提供实时监控 Web 页面；
同时支持 SD 卡本地 CSV 落盘与多种动作采集协议（站立/上下楼/弯腰/跳跃/跌倒）。

---

## 一、使用的硬件

| 硬件 | 说明 | 用途 |
|------|------|------|
| ESP32-S3-EYE 开发板 | Espressif 官方开发板（主控 ESP32-S3） | 运行固件、采集与上传 |
| QMA6100P 三轴加速度计 | 开发板板载，BSP 驱动 `espressif/qma6100p` | 采集加速度数据（100 Hz） |
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
├── sdkconfig.defaults             # 默认 SDK 配置（esp32s3 / 大分区 / FATFS 长文件名）
├── .gitignore
├── PROJECT_STRUCTURE.md           # 代码结构与开发流程详细说明
│
├── main/                          # 板端固件（ESP-IDF 组件）
│   ├── main.c                     # 主固件：采样 + WiFi + SNTP + SD 落盘 + LVGL + 上传
│   ├── CMakeLists.txt             # 组件注册与依赖声明
│   ├── Kconfig.projbuild          # 菜单配置（WiFi / 设备 ID / 上传 URL）
│   └── idf_component.yml          # 组件依赖（esp32_s3_eye、qma6100p）
│
├── docs/
│   ├── crash_analysis_and_fix.md  # 崩溃分析与修复记录（LVGL 栈溢出）
│   └── psram_upload_fix.md        # 上传链路失效分析与修复（PSRAM 未启用）
│
└── server/                        # 服务端（PC 运行，Python/FastAPI）
    ├── main.py                    # FastAPI 接收服务（校验 + SQLite 存储 + 查询接口）
    ├── requirements.txt           # Python 依赖
    ├── test_receive.py            # 单元自测（临时库）
    ├── e2e_server_check.py        # 真实 HTTP 端到端自检
    └── static/                    # 实时监控页（静态资源）
        ├── index.html             # 页面结构 + 样式
        └── app.js                 # 轮询 + 渲染逻辑
```

### 数据链路

```
ESP32-S3-EYE 板端              本地电脑 (PC)               浏览器
QMA6100P (100Hz)      ──WiFi──▶  FastAPI (server/main.py)  ──HTTP──▶  监控页 /ui/
每 1s 打包 100 点 POST            校验 → 存 SQLite                    每 2s 轮询刷新
```

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

- 实时监控面板：`http://<电脑IP>:8000/ui/`（每 2 s 自动刷新）
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

**自测**（可选，验证服务端正常）：

```bash
python test_receive.py        # 单元自测（临时库，不污染正式数据）
python e2e_server_check.py    # 真实 HTTP 端到端自检
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
| 长按按钮 B（2s） | 循环切换 5 种动作采集协议 |

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

---

## 五、服务端 HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/upload` | 接收板端上传的传感批次（校验后入库，成功返回 201） |
| `GET` | `/api/v1/health` | 健康状态 + 汇总（总批次 / 总样本 / 设备列表 / 最近上报） |
| `GET` | `/api/v1/devices` | 设备列表 |
| `GET` | `/api/v1/latest?device_id=…` | 某设备最近一次上报（含首末样本） |
| `GET` | `/` | 极简 HTML 首页 |
| `GET` | `/ui/` | 实时监控面板 |

**上传数据格式**（`POST /api/v1/upload`）：

```json
{
  "device_id": "esp32s3-eye-0001",
  "source": "qma6100p",
  "unit": "m/s^2",
  "ts_ms": 1767123456789,
  "samples": [
    {"i": 0, "t_ms": 0,  "ax": 0.02, "ay": -0.15, "az": 9.81},
    {"i": 1, "t_ms": 10, "ax": 0.03, "ay": -0.14, "az": 9.80}
  ]
}
```

**数据库**（SQLite，两张表）：

| 表 | 说明 |
|----|------|
| `uploads` | 每个上传批次一行（设备、来源、单位、起始时间、样本数、来源 IP、原始 JSON） |
| `samples` | 每批内每个采样点一行（`ax`/`ay`/`az` + 相对时间偏移） |

---

## 六、注意事项

1. **网络**：板端与 PC 须处于同一局域网；`SENSOR_SERVER_URL` 使用 PC 的局域网 IP，不能填 `127.0.0.1`。
2. **凭据安全**：真实 WiFi 密码只写入 `sdkconfig`（已 gitignore），`Kconfig.projbuild` 只保留占位符。
3. **上传策略**：板端上传带**有界重试**（最多 3 次，500 ms / 1 s 退避；4xx 表示服务端拒绝载荷、不再重试），RAM 队列 8 批（约 8 s）作为慢网缓冲。重试耗尽或队列满时仍会丢弃批次——这是为保证 100 Hz 采样不被网络阻塞的刻意取舍；**批量落盘重放 / 掉电续传未实现**。
4. **鉴权**：默认不鉴权，面向局域网联调。如需公网部署：给服务端设 `SENSOR_TOKEN`，并同步配置板端 `SENSOR_TOKEN`，板端会带 `Authorization: Bearer <token>`，不匹配返回 `401`。注意本机直连仅为「备用路径」，正式公网部署仍需在 VPS 上补 TLS/反向代理与进程守护。
5. **CORS**：监控页 `/ui/` 与接口同源，浏览器不会触发跨域；板端上传是 HTTP 客户端而非浏览器，与 CORS 无关，故未配置 CORS 中间件。

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

### 自测

```bash
python server/test_receive.py    # 单元自测（临时库）：校验/入库/查询/可选鉴权
python server/e2e_server_check.py # 真实 HTTP 端到端自检
```
