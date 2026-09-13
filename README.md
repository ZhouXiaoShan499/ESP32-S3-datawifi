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
│   └── crash_analysis_and_fix.md  # 崩溃分析与修复记录
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

## 三、使用说明

### 3.1 服务端（PC，先启动）

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

**自测**（可选，验证服务端正常）：

```bash
python test_receive.py        # 单元自测（临时库，不污染正式数据）
python e2e_server_check.py    # 真实 HTTP 端到端自检
```

启动后浏览器访问：
- 监控面板：`http://<PC_IP>:8000/ui/`
- 极简首页：`http://<PC_IP>:8000/`

### 3.2 板端（ESP32-S3-EYE）

**环境要求**：ESP-IDF ≥ 5.4（本工程使用 ESP Component Registry 托管依赖）。

**① 配置**（二选一）：

- 方式 A：`idf.py menuconfig`，在 `Project WiFi Configuration` 与 `Real-Sensor Upload Configuration` 菜单中设置。
- 方式 B：直接编辑本地 `sdkconfig`（已 gitignore，不入库）。

| 配置项 | 说明 |
|--------|------|
| `WIFI_SSID` / `WIFI_PASSWORD` | 连接的 WiFi 名称与密码 |
| `SENSOR_DEVICE_ID` | 设备唯一标识（默认 `esp32s3-eye-0001`） |
| `SENSOR_SERVER_URL` | 服务端上传地址，如 `http://192.168.1.100:8000/api/v1/upload` |

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

## 四、服务端 HTTP 接口

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

## 五、注意事项

1. **网络**：板端与 PC 须处于同一局域网；`SENSOR_SERVER_URL` 使用 PC 的局域网 IP，不能填 `127.0.0.1`。
2. **凭据安全**：真实 WiFi 密码只写入 `sdkconfig`（已 gitignore），`Kconfig.projbuild` 只保留占位符。
3. **上传策略**：板端上传为「尽力而为」，断网或慢网时会丢弃批次（无重试），这是为保证 100 Hz 采样不被网络阻塞的刻意取舍。
4. **鉴权**：服务端上传接口暂无鉴权/限流，面向局域网联调；如需公网部署请自行补充。
