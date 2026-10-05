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
├── sdkconfig.defaults             # 默认 SDK 配置（目标芯片 esp32s3、单应用大分区、FATFS 长文件名、PSRAM、相机 JPEG、I2C）
├── .gitignore                     # 忽略 build/、sdkconfig、managed_components/、server/data/ 等
│
├── main/                          # 板端固件（ESP-IDF 组件）
│   ├── main.c                     # 主固件（约 5000 行）
│   ├── CMakeLists.txt             # 组件注册 + 依赖声明（含 esp_video / esp_cam_sensor）
│   ├── Kconfig.projbuild          # 菜单配置（WiFi / 设备 ID / 上传 URL）
│   └── idf_component.yml          # 组件依赖（esp32_s3_eye；QMA6100P 驱动已内置在主固件里）
│
├── docs/                          # 项目文档
│   ├── crash_analysis_and_fix.md       # 崩溃分析与修复记录（LVGL 栈溢出）
│   ├── psram_upload_fix.md             # 上传链路失效分析与修复（PSRAM 未启用）
│   ├── manual_capture_task.md          # 按需采集任务（Web 触发、request_id 贯穿、三维视图）
│   ├── manual_capture_task_acceptance.md # 验收手册：自检脚本 + 真机验收 + 逐条观测点/边界
│   ├── manual_capture_task_work_log.md # 按需采集任务的需求 · 交付 · 验证过程记录
│   ├── realtime_photo_capture.md       # 实时拍照（camera 任务 + JPEG 落盘 + 画廊/删除）
│   ├── camera_live_stream.md           # 摄像头实时直播（长按 Button A → 内存最新帧 → 页面实时画面）
│   ├── wifi_network_troubleshooting.md # WiFi 关联成功却拿不到 IP：DHCP 排查与静态兜底验证
│   ├── static_ip_fallback_fix.md       # 静态 IP 兜底复盘：条件编译缺陷 + sdkconfig 回写陷阱
│   ├── loop_trigger_callback.md        # 闭环事件：按键触发 → 本地反馈 → 远端显示 → 回应/取消
│   ├── camera_bad_frame_fix_work_log.md # 相机坏帧/拍照失败治理：一轮基线 → 定位改写 → 修复 → 一轮验证
│   └── camera_frame_truncation_fix_work_log.md # 直播/预览帧尾部截断（下半幅灰带/中段撕裂）：根因 → 修复 → 验证
│
└── server/                        # 服务端（PC 上运行，Python/FastAPI）
    ├── main.py                    # FastAPI 服务（约 2600 行：接收 + 存储 + 查询 + 任务 + 照片 + 闭环事件）
    ├── requirements.txt           # 依赖（fastapi、uvicorn、httpx）
    ├── test_receive.py            # 本地联调自测脚本（含按需采集任务全流程）
    ├── test_photos.py             # 拍照链路自测脚本（临时库 + 临时照片目录）
    ├── test_events.py             # 闭环事件自测脚本（临时库，23 项断言）
    ├── e2e_server_check.py        # 真实 HTTP 端到端自检脚本
    ├── e2e_ui_check.py            # 无头浏览器界面自检（可选，需本机 Edge/Chrome）
    ├── e2e_live_check.py          # 摄像头直播自检（HTTP + 可选无头页面断言）
    ├── photos/                    # 照片落盘目录（运行时生成，已 gitignore）
    └── static/                    # 实时监控页面（静态资源）
        ├── index.html             # 页面结构 + 样式（含任务卡片、闭环事件卡片、拍照画廊与三维视图画布）
        └── app.js                 # 轮询 + 渲染 + 任务跟踪 + 事件卡片 + 照片画廊 + Canvas 2D 三维姿态视图
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
| `main.c` | 主固件，整合多项功能：QMA6100P 加速度计读取、WiFi STA 连接、SNTP 时间同步、SD 卡 CSV 落盘、LVGL 实时显示、六面标定，以及 **真实传感数据定时上传**（100 Hz 采样，每 1s 打包 100 个样本点 POST 上传）。另含 **任务链路**：`task_poll_task` 每 3s 轮询 `/api/v1/tasks/next`；`kind=capture` 由 `sampler_task` 按任务节拍采一批数据（期间暂停周期上报）并带 `request_id` 回传；`kind=pause/resume`（Web「暂停周期 / 恢复周期」）由 `task_apply_control()` 切换周期上报闸门（双时钟惰性到点恢复）并 `POST /applied` 确认生效。另含 **摄像头实时直播**：长按 Button A（2 s）开关 `live_stream_task`，每 ~500 ms 拍一帧 JPEG 并 `POST /api/v1/live`（服务端只在内存里留最新一帧），与按需单帧拍照共用相机并用 `s_camera_mutex` 串行化。另含 **闭环事件**（按键触发 → 本地反馈 → 远端显示 → 回应/取消）：`loop_module_init()` 把 GPIO3 配成**开漏**输出并起 `loop_led`（闪烁反馈，队列驱动、非阻塞）与 `loop_task`（1 s 一轮：发 trigger / 发 respond / 轮询远端状态）；按键 `BSP_BUTTON_3` = 触发、`BSP_BUTTON_4` = 单击回应（accept）/ 长按 2 s 取消（cancel），回调只置标志 + 闪灯 + 改屏，HTTP 全部交给 `loop_task`，与 `sampler_task`/`uploader_task`/`live_stream_task` 完全旁路。 |
| `CMakeLists.txt` | 通过 `idf_component_register` 注册组件，声明 `SRCS "main.c"`，并 `REQUIRES` 大量依赖（`json`、`esp_http_client`、`esp_netif`、`esp_wifi`、`sdmmc`、`fatfs`、`esp_timer`、`esp_lcd` 等）。 |
| `Kconfig.projbuild` | 定义 `menuconfig` 菜单：WiFi SSID/密码、设备 ID（`SENSOR_DEVICE_ID`）、上传 URL（`SENSOR_SERVER_URL`）、可选上传鉴权 Token（`SENSOR_TOKEN`）。真实值通过本地 `sdkconfig` 配置，**不入库**。 |
| `idf_component.yml` | ESP 组件注册表依赖：`espressif/esp32_s3_eye`（BSP）、`espressif/esp_video`（相机/视频，BSP 传递依赖）、`idf >= 5.4`。 |

### 2.3 `server/` —— 服务端（FastAPI）

| 文件 | 说明 |
|------|------|
| `main.py` | FastAPI 应用：接收上传、校验字段、写入 SQLite、提供查询接口、任务接口（含控制任务 `/applied` 与 `/api/v1/control`）、**摄像头直播内存接口（`/api/v1/live`）**、托管监控页面。 |
| `requirements.txt` | Python 依赖：`fastapi`、`uvicorn[standard]`、`httpx`（自测用）。 |
| `test_receive.py` | 本地联调自测：用独立临时库验证「接收 → 校验 → 存储 → 查询」全流程，含可选鉴权分支与任务全流程（创建/领取/回执/回传/幂等/去重/超时/失败/控制任务 applied/暂停状态惰性归零），43 项断言。 |
| `test_photos.py` | 拍照链路自测（临时库 + 临时照片目录）：JPEG 上传校验/落盘/取图/列表/404+410/删除/camera 任务收尾/幂等，16 项断言。 |
| `test_events.py` | 闭环事件自测（临时库）：触发校验/板端重试幂等/状态机单向性（accept→ack、cancel→cancelled、confirm→completed）/终态不可回退/超 TTL 惰性过期/列表过滤与分页/设备侧鉴权/health 汇总，23 项断言。 |
| `e2e_server_check.py` | 以子进程真实启动 uvicorn，用标准库 urllib 走真实 HTTP 完成端到端自检（含任务创建-领取-ack-回传-幂等，pause/resume 的 `/applied` 收尾与 `/devices` 暂停标注，以及闭环事件「触发 → 幂等重试 → 轮询 → 回应 → 确认完成 → 终态不可回退」9 项），23 项断言。 |
| `e2e_ui_check.py` | 可选：用无头 Edge/Chrome 打开页面并 dump DOM（11 次），断言页面 JS 真正执行（设备下拉、trigger/request_id、波形点数、三维视图 `|a|`、参数表单带板端上限且任务真用该参数、任务卡片 `upload_id` 与时间戳、任务历史倒序、manual/periodic 对照区、无回执提示出现与消失、控制卡片「停止中/上报中」与控制任务渲染、闭环事件卡片的 request_id/待处理状态/徽章/流水行与「板端回应后转已回应」），90 项断言；未装浏览器时打印 SKIP。 |
| `e2e_live_check.py` | 摄像头直播自检（临时库 + 真实 uvicorn，`SENSOR_LIVE_TIMEOUT_S=1`）：推帧 201/单调 `seq`、状态 `active/尺寸/帧龄`、`/live/frame` 原样取回、只保留最新一帧、错误路径（400/413/404）、**不落盘不入库**（`photos` 总数 0 + 照片目录为空）、超时后 `active=false` 但仍可取最后一帧；有 Edge/Chrome 时再 dump 两次 DOM，断言实时卡片 `<img>`、「推流中 · 640×480」徽章与超时后的「已停止（保留最后一帧）」，28 项断言。 |
| `e2e_device_check.py` | 真机（上板）脚本化验收：起真实服务（默认 `0.0.0.0:8000`），等板端周期上报出现后跑「建任务 → 领取 → ack → 采集窗口内周期上报为 0 → completed + upload_id → 周期恢复 → 历史可见」+「暂停周期（板端 applied → device_control 停止中 → 观察窗内 0 新周期批次 → 暂停中手动采集仍可用）→ 恢复周期」28 项断言；板端不在场时打印 `[SKIP]` 并以 0 退出。 |
| `static/index.html` | 监控页面结构（设备下拉、**采集参数表单**、**暂停时长 + 暂停周期/恢复周期按钮**、数据卡片、样本表、波形画布、任务卡片（含无回执提示）、**周期上报控制卡片**、**手动/周期对照表**、**任务历史表**、**摄像头实时画面卡片（大图 + 徽章 + 元信息）**、拍照画廊、三维视图画布）。 |
| `static/app.js` | 轮询逻辑（设备列表 / 最新数据 / 波形 / 三维视图 / 暂停状态 / **直播状态与最新一帧** / **闭环事件**）、`createTask(kind)` 统一建任务入口（采集 / 暂停 / 恢复）、状态跟踪（时间戳 / upload_id / 无回执推断）、控制卡片渲染、对照区与任务历史渲染、`refreshLive()` 按 `seq` 刷新直播 `<img>`、`refreshEvents()` + `respondEvent()` 渲染闭环事件卡片与流水、照片画廊渲染、Canvas 2D 手写正交投影渲染。 |

#### 服务端数据库（SQLite，六张表）

| 表 | 说明 |
|----|------|
| `uploads` | 每个上传批次一行，记录设备、来源、单位、起始时间戳、接收时间、样本数、来源 IP、原始 JSON（`payload`）、任务号（`request_id`）与触发方式（`trigger`）。 |
| `samples` | 每批内的每个采样点一行，记录 `ax/ay/az` 三轴加速度与相对时间偏移。 |
| `tasks` | 任务：`request_id`（主键，uuid4 hex）、设备、来源、单位、采样率/样本数、**类型 `kind`（`capture`/`pause`/`resume`）与暂停时长 `duration_s`**、状态、各状态时间戳、`expires_at`、关联 `upload_id` 与失败原因。 |
| `device_control` | 周期上报暂停状态（`device_id + source` 主键）：`periodic_paused`、`paused_until`、`request_id`、`updated_at`——「是否真的暂停」的真相源，由板端 `POST /tasks/{id}/applied` 写入。 |
| `photos` | 照片数据（`id` 主键、`device_id`、`request_id`、`ts_ms`、`received_at`、`bytes`、`width`、`height`、`ip`、`path`、`note`）：图片字节不入库，文件落在 `server/photos/<device_id>/<id>.jpg`。 |
| `events` | 闭环事件（板端按键发起）：`request_id` 主键（**由板端生成**）、`device_id`、`source`、`kind`、`status`（`pending`/`ack`/`cancelled`/`completed`/`expired`）、`created_at`、`device_ts_ms`、`responded_at`/`response`/`responded_by`、`closed_at`、`expires_at`、`ip`、`note`。与 `tasks` **刻意分表**：`tasks` 的语义是「服务端派发、板端执行」，事件塞进去会被 `/tasks/next` 领回来形成回环。 |

- **老库迁移**：`CREATE TABLE IF NOT EXISTS` 不会给已存在的表补列。`init_db()` 在 `executescript`
  之后用 `PRAGMA table_info(...)` 判断并 `ALTER TABLE` 补 `uploads.request_id` / `uploads.trigger`
  与 `tasks.kind`（默认 `'capture'`）/ `tasks.duration_s`，最后创建
  **部分唯一索引** `idx_uploads_request`（`WHERE request_id IS NOT NULL`）与
  `idx_tasks_device_kind`（支撑 `device+source+kind` 去重）。
- **幂等**：带 `request_id` 的上传先查 `uploads.request_id`，命中即返回 `200 + idempotent=true`
  且不写任何新行；`tasks` 的收尾与 `uploads/samples` 写入在同一事务内提交。
- **惰性超时 / 惰性归零**：`_expire_stale_tasks()` 在创建/查询/领取路径调用，把 `expires_at`
  已过的非终态任务置 `timeout`；`_lazy_reset_control()` 在读控制状态的路径调用，把
  `paused_until` 已过的行置回未暂停。两者都不需要后台线程，服务重启后状态自洽。

#### 服务端 HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/upload` | 接收板端上传的传感数据批次（校验后入库，成功返回 201）。 |
| `GET` | `/api/v1/health` | 服务健康状态 + 汇总统计（总批次 / 总样本 / 设备列表 / 最近上报）。 |
| `GET` | `/api/v1/devices` | 设备列表（各设备的批次数、最近上报时间与 `periodic_paused`）。 |
| `GET` | `/api/v1/latest?device_id=…` | 某设备最近一次上报（含首末样本，并带回 `trigger` / `request_id` 便于判断数据来源）。 |
| `POST` | `/api/v1/tasks` | 创建任务（`kind=capture`/`pause`/`resume`；同 `device+source+kind` 已有未完成任务时复用，返回 `duplicate=true`；控制任务返回 `superseded` 计数）。 |
| `GET` | `/api/v1/tasks` | 任务列表（`?device_id=…&limit=n`）。 |
| `GET` | `/api/v1/tasks/next?device_id=…` | 板端领取任务（原子 `UPDATE … WHERE status='submitted'`；声明在 `/tasks/{request_id}` 之前）。 |
| `GET` | `/api/v1/tasks/{request_id}` | 任务详情 + 关联上传摘要。 |
| `POST` | `/api/v1/tasks/{request_id}/ack` | 板端回执（`submitted/dispatched` → `acked`）。 |
| `POST` | `/api/v1/tasks/{request_id}/fail` | 板端上报失败原因（写入 `tasks.error`）。 |
| `POST` | `/api/v1/tasks/{request_id}/applied` | 板端回执「控制任务已生效」（仅 `pause`/`resume`）：置 `completed` 并写 `device_control`；`capture` 调用返回 `409 WRONG_KIND`。 |
| `GET` | `/api/v1/control?device_id=…&source=…` | 暂停状态真相源（惰性归零后返回 `periodic_paused` / `remaining_s` / `paused_until_str` / `request_id`）。 |
| `POST` | `/api/v1/photos?device_id=…&request_id=…` | 板端上传一帧 JPEG（body 即图片字节）：校验 SOI 魔数与 512 KiB 上限，落盘 + 写 `photos` 表，带 `request_id` 时同事务把 `kind=camera` 任务置 `completed`（同号重传幂等）。 |
| `GET` | `/api/v1/photos?device_id=…&limit=12` | 照片列表（新的在前，`limit` 1–200），供页面画廊渲染缩略图与删除按钮。 |
| `GET` | `/api/v1/photos/{id}` | 取一张照片的 JPEG 字节（画廊 `<img src>` 用它）；不存在 404，元数据在而文件丢失 410。 |
| `DELETE` | `/api/v1/photos/{id}` | 删除一张照片（先删库行再删文件，返回 `file_deleted`）；重复删除 404。 |
| `POST` | `/api/v1/live?device_id=…&ts_ms=…&w=…&h=…` | 板端直播推一帧 JPEG（body 即图片字节）：校验 `device_id` / SOI 魔数 / 512 KiB 上限，**只写内存**（不落盘、不入库、不建任务），成功 201 + 全局单调 `seq`。 |
| `GET` | `/api/v1/live?device_id=…` | 直播状态：`active`（超过 `SENSOR_LIVE_TIMEOUT_S`，默认 5 s 无新帧即 false）/ `seq` / `age_ms` / 分辨率 / 字节数 / 时间戳 / `devices[]`；从未推流的设备返回 200 + `active=false`。 |
| `GET` | `/api/v1/live/frame?device_id=…` | 最新一帧 JPEG（页面 `<img src>` 用它；`Cache-Control: no-store`）；从未推流 404。 |
| `POST` | `/api/v1/events/trigger` | 闭环事件：板端按键**触发**。`request_id` 由板端生成并作主键 → 同号重传返回 200 + `idempotent=true`（同一次按键只算一条）。 |
| `POST` | `/api/v1/events/respond` | 闭环事件：**回应 / 取消 / 确认完成**（板端与 Web 共用）。`action` ∈ `accept`/`cancel`/`confirm`；重复同动作幂等 200，终态不可回退 409，未知 id 404。 |
| `GET` | `/api/v1/events/status?device_id=…&request_id=…` | 板端**轮询事件状态**：返回 `found` / 顶层 `status` / 完整 `event`；未知 `request_id` 返回 200 + `found=false`（轮询过渡态，不算错误）。 |
| `GET` | `/api/v1/events?device_id=…&limit=8&active=1` | 事件列表（新的在前），附 `pending` 计数供页面红色徽章；`active=1` 只看未终态。 |
| `GET` | `/` | 极简 HTML 首页（内联渲染，数值不写死）。 |
| `GET` | `/ui/` | 实时监控面板（托管 `server/static/` 下的静态文件）。 |

> 鉴权范围：设备侧接口（`/api/v1/upload`、`/api/v1/tasks/next`、`/tasks/{id}/ack`、
> `/tasks/{id}/fail`、`/tasks/{id}/applied`、`/api/v1/live`）在配置了 `SENSOR_TOKEN` 时要求
> `Authorization: Bearer <token>`；Web 侧查询/建任务接口与既有查询接口一样不做 Token 校验。

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
2. **分支命名**：用描述性前缀，如 `feature/<描述>`、`bugfix/<描述>`、`docs/<描述>`。历史上用过
   `feature/real-sensor-upload-web`、`feature/psram-upload-fix-web-ui`；本次按需采集任务功能使用
   `feature/manual-capture-task-web-3d`。
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

**「按需采集任务 + Web/三维视图」功能（分支 `feature/manual-capture-task-web-3d`）** 同样按分层递进：

| 提交 | 类型 | 说明 |
|------|------|------|
| `feat(server): add on-demand capture task endpoints with request_id traceability` | 服务端 | `tasks` 表 + 6 个任务接口 + 老库补列迁移 + 上传幂等与任务收尾同事务。 |
| `feat(esp32): poll capture tasks and pause periodic upload during manual capture` | 板端 | 任务轮询、采样闸门、周期上报暂停、批次带 `request_id` 回传、失败回执。 |
| `feat(ui): add refresh-only and capture-once buttons with task tracking` | 前端 | 两个按钮 + 任务卡片（request_id / 状态 / 剩余有效期）。 |
| `feat(ui): add rotatable 3D three-axis vector and trajectory view` | 前端 | 纯 Canvas 2D 正交投影三维视图（拖拽/滚轮/复位/自动旋转）。 |
| `test(server): cover task lifecycle, idempotency, timeout and duplicate clicks` | 测试 | 单元 + 真实 HTTP + 无头浏览器三层自检。 |
| `docs: document manual capture task, web buttons and 3D view` | 文档 | README / PROJECT_STRUCTURE / `docs/manual_capture_task.md`。 |
| `docs: add work log for manual capture task session` | 文档 | `docs/manual_capture_task_work_log.md`：需求 → 交付 → 验证 → 遗留的过程记录。 |

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
