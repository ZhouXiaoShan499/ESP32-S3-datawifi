# 项目结构与开发流程说明

> 项目：`data_capture_sim` —— ESP32-S3-EYE 板端 IMU 传感数据采集 + WiFi 上传 + 服务端存储与实时监控。

---

## 一、项目概览

本项目实现一条完整的 **"板端 → 服务端 → 浏览器"** 数据链路：

```
ESP32-S3-EYE 开发板           本地电脑 (PC)                浏览器
┌─────────────────────┐     ┌──────────────────────┐    ┌─────────────┐
│ QMA6100P 加速度计     │     │ FastAPI 接收服务        │    │ 实时监控页面   │
│ (100 Hz 采样)        │     │ (server/main.py)      │    │ (server/static)│
│  → 每 1s 打包 100 点  │──WiFi→│ → 校验 → 存 SQLite     │──HTTP→│ /ui/ 轮询展示  │
│  → JSON POST 上传     │     │ → 提供查询接口          │    │               │
└─────────────────────┘     └──────────────────────┘    └─────────────┘
```

- **板端**：ESP32-S3-EYE（ESP-IDF 框架），读取 QMA6100P 三轴加速度计，通过 WiFi 定时批量上传。
- **服务端**：FastAPI（Python），校验并存储数据到 SQLite，提供查询接口与监控页面。
- **前端**：纯静态 HTML/JS 监控面板，定时轮询服务端接口实时刷新。

---

## 二、代码结构

```
data_capture_sim/                  # 仓库根目录
├── CMakeLists.txt                 # ESP-IDF 顶层工程入口（project: data_capture_sim）
├── sdkconfig.defaults             # 默认 SDK 配置（目标芯片 esp32s3、单应用大分区、FATFS 长文件名）
├── .gitignore                     # 忽略 build/、sdkconfig、managed_components/、server/data/ 等
│
├── main/                          # 板端固件（ESP-IDF 组件）
│   ├── main.c                     # 主固件（约 2800 行）
│   ├── CMakeLists.txt             # 组件注册 + 依赖声明
│   ├── Kconfig.projbuild          # 菜单配置（WiFi / 设备 ID / 上传 URL）
│   └── idf_component.yml          # 组件依赖（esp32_s3_eye、qma6100p）
│
├── docs/                          # 项目文档
│   └── crash_analysis_and_fix.md  # 崩溃分析与修复记录
│
└── server/                        # 服务端（PC 上运行，Python/FastAPI）
    ├── main.py                    # FastAPI 接收服务（约 530 行）
    ├── requirements.txt           # 依赖（fastapi、uvicorn、httpx）
    ├── test_receive.py            # 本地联调自测脚本
    └── static/                    # 实时监控页面（静态资源）
        ├── index.html             # 页面结构 + 样式
        └── app.js                 # 轮询 + 渲染逻辑
```

### 2.1 根目录

| 文件 | 说明 |
|------|------|
| `CMakeLists.txt` | ESP-IDF 工程的顶层入口，引入 `$IDF_PATH/tools/cmake/project.cmake`，声明项目名 `data_capture_sim`。 |
| `sdkconfig.defaults` | 提交到仓库的默认配置：目标芯片 `esp32s3`、`single app large` 分区方案、启用 FATFS 长文件名。 |
| `.gitignore` | 忽略构建产物（`build/`）、本地配置（`sdkconfig`）、托管组件（`managed_components/`）以及服务端运行时数据（`server/data/`、`*.db`）。 |

### 2.2 `main/` —— 板端固件（ESP-IDF 组件）

| 文件 | 说明 |
|------|------|
| `main.c` | 主固件，整合多项功能：QMA6100P 加速度计读取、WiFi STA 连接、SNTP 时间同步、SD 卡 CSV 落盘、LVGL 实时显示、六面标定，以及 **真实传感数据定时上传**（100 Hz 采样，每 1s 打包 100 个样本点 POST 上传）。 |
| `CMakeLists.txt` | 通过 `idf_component_register` 注册组件，声明 `SRCS "main.c"`，并 `REQUIRES` 大量依赖（`json`、`esp_http_client`、`esp_netif`、`esp_wifi`、`sdmmc`、`fatfs`、`esp_timer`、`esp_lcd` 等）。 |
| `Kconfig.projbuild` | 定义 `menuconfig` 菜单：WiFi SSID/密码、设备 ID（`SENSOR_DEVICE_ID`）、上传 URL（`SENSOR_SERVER_URL`）。真实值通过本地 `sdkconfig` 配置，**不入库**。 |
| `idf_component.yml` | ESP 组件注册表依赖：`espressif/esp32_s3_eye`（BSP）、`espressif/qma6100p`（传感器驱动）、`idf >= 5.4`。 |

### 2.3 `server/` —— 服务端（FastAPI）

| 文件 | 说明 |
|------|------|
| `main.py` | FastAPI 应用：接收上传、校验字段、写入 SQLite、提供查询接口、托管监控页面。 |
| `requirements.txt` | Python 依赖：`fastapi`、`uvicorn[standard]`、`httpx`（自测用）。 |
| `test_receive.py` | 本地联调自测：用独立临时库验证「接收 → 校验 → 存储 → 查询」全流程。 |
| `static/index.html` | 监控页面结构（设备下拉、数据卡片、样本表）。 |
| `static/app.js` | 轮询逻辑（每 2s 拉取设备列表与最新数据并渲染）。 |

#### 服务端数据库（SQLite，两张表）

| 表 | 说明 |
|----|------|
| `uploads` | 每个上传批次一行，记录设备、来源、单位、起始时间戳、接收时间、样本数、来源 IP、原始 JSON（`payload`）。 |
| `samples` | 每批内的每个采样点一行，记录 `ax/ay/az` 三轴加速度与相对时间偏移。 |

#### 服务端 HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/upload` | 接收板端上传的传感数据批次（校验后入库，成功返回 201）。 |
| `GET` | `/api/v1/health` | 服务健康状态 + 汇总统计（总批次 / 总样本 / 设备列表 / 最近上报）。 |
| `GET` | `/api/v1/devices` | 设备列表（各设备的批次数与最近上报时间）。 |
| `GET` | `/api/v1/latest?device_id=…` | 某设备最近一次上报（含首末样本，用于核对采集值）。 |
| `GET` | `/` | 极简 HTML 首页（内联渲染，数值不写死）。 |
| `GET` | `/ui/` | 实时监控面板（托管 `server/static/` 下的静态文件）。 |

---

## 三、GitHub Flow 提交方法

本项目采用 **GitHub Flow** 工作流 + **Conventional Commits** 提交规范。

### 3.1 核心流程

```
main（始终可部署）
   │
   ├── 1. 拉出功能分支 ──▶ feature/<功能名>
   │                          │
   │                          ├── 2. 小步提交（Conventional Commits）
   │                          ├── 3. 推送到远程 (git push)
   │                          ├── 4. 发起 Pull Request
   │                          ├── 5. 评审 / 讨论 / 修改
   │                          └── 6. 合并回 main
   │
   └── 7. 删除功能分支
```

1. **从 `main` 创建分支**：`main` 分支始终保持可部署状态，任何新功能都从 `main` 拉出新分支，不直接在 `main` 上开发。
2. **分支命名**：用描述性前缀，如 `feature/<描述>`、`bugfix/<描述>`、`docs/<描述>`。本项目当前分支为 `feature/real-sensor-upload-web`。
3. **小步提交**：频繁、原子化提交，每次提交只做一件事，便于 review 与回滚。
4. **推送**：将分支推送到远程仓库（`origin`）。
5. **Pull Request**：发起 PR 请求合并到 `main`，附上改动说明。
6. **评审**：他人 review、讨论、按需修改。
7. **合并**：通过后合并到 `main`，随后删除已合并的功能分支。

### 3.2 提交信息规范（Conventional Commits）

格式：

```
<type>(<scope>): <subject>
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `type` | 提交类型 | `feat`（新功能）、`fix`（修复）、`docs`（文档）、`refactor`（重构）、`test`（测试）、`build`（构建）、`chore`（杂项） |
| `scope` | 可选，影响范围 | `server`、`esp32`、`main`、`ui` |
| `subject` | 简短描述（祈使句，约 50 字符内） | `add FastAPI receiver` |

### 3.3 本项目的实际提交示例

本功能按「服务端 → 板端 → 前端」分层递进，每次提交聚焦一个层次：

| 提交 | 类型 | 说明 |
|------|------|------|
| `feat(server): add FastAPI receiver and store service for IMU upload` | 服务端 | 先建好接收 + 存储服务，板端才有目标。 |
| `feat(esp32): upload real IMU samples to FastAPI server over WiFi` | 板端 | 板端定时打包真实采样数据并 WiFi 上传。 |
| `feat: add UI monitor page, expand build deps, expose /ui endpoint` | 前端 | 补充监控页面与静态资源托管，形成完整闭环。 |

### 3.4 常用命令

```bash
# 1. 从 main 拉出新分支
git checkout main && git pull
git checkout -b feature/<功能名>

# 2. 小步提交（按范围拆分）
git add <相关文件>
git commit -m "feat(<scope>): <描述>"

# 3. 推送到远程
git push -u origin feature/<功能名>

# 4. 在 GitHub/GitLab 上发起 Pull Request 并完成评审、合并

# 5. 合并后清理本地分支
git checkout main && git pull
git branch -d feature/<功能名>
```

### 3.5 注意事项

- **不提交敏感/本地配置**：如 `sdkconfig`（含真实 WiFi 密码、服务器 IP）、`server/data/`（运行库）。这些已由 `.gitignore` 排除，仓库中只保留占位默认值（见 `Kconfig.projbuild` 与 `sdkconfig.defaults`）。
- **提交粒度**：与业务分层对应，避免一个提交混杂多个层次/多个无关改动，便于逐条 review 与回滚。
- **提交信息**：用祈使句、英文（与本仓库历史一致），`subject` 简洁描述「做了什么」，必要时在正文补充「为什么」。
