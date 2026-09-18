# 按需采集任务功能：需求与开发记录（做了什么 · 验证了什么）

> - **记录对象**：分支 `feature/manual-capture-task-web-3d`（基线 `main` @ `2ac51e0`）
> - **记录日期**：2026-09-18
> - **一句话结论**：按需采集（Web 手动触发）功能、三层自测、文档全部完成并落成 **6 个提交**（10 文件，`+2666 / −70`），本地验证全绿；`git push` / PR 因环境网络限制未完成（见第七节）。
> - **配套文档**：实现细节见 `docs/manual_capture_task.md`；本文档偏向"需求 → 交付 → 验证 → 遗留"的过程记录。

---

## 一、需求（用户提出了什么）

### 1.1 原始诉求要点（按会话顺序整理，非逐字引用）

| 轮次 | 用户诉求（要点） | 我的理解与落点 |
|------|------------------|----------------|
| 1 | 在现有 ESP32-S3-EYE 采集项目上增加"**按需采集任务**（manual capture task）"流程 | 新增任务实体 + 任务接口，与既有"周期上报"链路并存，不破坏旧行为 |
| 2 | **板端轮询任务接口**，领取到任务才采集 | 新增 `task_poll_task`，每 3 s `GET /api/v1/tasks/next`，原子领取（`submitted → dispatched`） |
| 3 | **采集期间暂停周期上传** | sampler 增加"手动采集窗口"闸门，窗口内不喂周期窗口，服务端只看到带该 `request_id` 的 manual 批次 |
| 4 | **Web UI 加按钮 + 任务跟踪**，并要一个 **3D 视图** | 「仅刷新（不采集）」「采集一次最新数据」两个按钮 + 任务卡片（`request_id`/状态/剩余有效期）+ 纯 Canvas 2D 三维加速度视图 |
| 5 | **补测试与文档** | 三层自测（单元 / 真实 HTTP / 无头浏览器）+ README、PROJECT_STRUCTURE、新增专题文档 |
| 6 | 交付方式：**按 GitHub Flow 分小步提交**，最后 **push + PR + 合并** | 分支开发、按"服务端 → 板端 → 前端 → 测试 → 文档"分层提交（实际 6 笔，见 2.1） |
| 7 | 本轮追加：**把"做了什么、问了什么"整理成 md 记录** | 即本文档 |

### 1.2 诉求拆解为可验收项

| 可验收项 | 验收方式 |
|----------|----------|
| Web 点按钮 → 板端立刻采一批（默认 100 Hz × 1 s = 100 点）并回传 | 任务卡片 `submitted → dispatched → acked → completed`，数据卡片出现新批次 |
| 批次可追溯、可幂等 | `request_id` 贯穿 任务 / 回执 / 上传 / 样本；重复上传不重复写样本 |
| 采集期间暂停周期上报 | 采集窗口内服务端只收到带该 `request_id` 的 manual 批次，周期批次计数为 0 |
| 连点不产生并行任务 | 连点 5 次后任务列表只有 1 条非终态任务，同 `request_id` 只有 1 条上传 |
| 板端掉线/超时不产生脏数据 | 过期任务变 `timeout`、无上传；重启服务端状态不变 |
| 新增可旋转三维视图 | 拖拽/滚轮/双击有效，读数与 `ax/ay/az` 一致 |
| 不破坏旧行为 | 不带 `request_id` 的周期上传与旧版完全一致（含老库能直接升级） |
| 交付规范 | 分层小提交 + 每层可跑的自测 + 文档同步 |

---

## 二、做了什么（交付物）

### 2.1 提交一览（分支 `feature/manual-capture-task-web-3d`，6 笔）

| # | 提交 | 范围 | 关键内容 |
|---|------|------|----------|
| 1 | `55d0f1e` `feat(server): add on-demand capture task endpoints with request_id traceability` | 服务端 | `tasks` 表 + 6 个任务接口 + 老库 `ALTER TABLE` 补列迁移 + 上传幂等（部分唯一索引）+ 任务收尾与写库同事务 |
| 2 | `b10ef8e` `feat(esp32): poll capture tasks and pause periodic upload during manual capture` | 板端 | `task_poll_task`（prio 3 / 8192 B / 3 s）、采样闸门、周期上报暂停、批次带 `request_id`/`trigger`、失败回执 |
| 3 | `42cde19` `feat(ui): add refresh-only and capture-once buttons with task tracking` | 前端 | 两个按钮 + 任务卡片 + 「触发方式」「任务号」字段回填 |
| 4 | `7d05e29` `feat(ui): add rotatable 3D three-axis vector and trajectory view` | 前端 | 纯 Canvas 2D 正交投影三维视图（零依赖） |
| 5 | `ff14716` `test(server): cover task lifecycle, idempotency, timeout and duplicate clicks` | 测试 | 单元 + 真实 HTTP + 无头浏览器三层自检 |
| 6 | `174b7c6` `docs: document manual capture task, web buttons and 3D view` | 文档 | README / PROJECT_STRUCTURE / `docs/manual_capture_task.md` |

> 说明：计划里服务端本想拆成"建表迁移"与"接口实现"两笔，但二者同在 `server/main.py` 且互相耦合，
> 拆开会留下不能独立运行的中间态，故合并为 1 笔；其余分层与原计划一致，最终 6 笔而非 8 笔。

### 2.2 服务端（`server/main.py`）

- **数据模型**：`tasks` 表（`request_id` 主键、`device_id`、`source`、`status`、`spec_json`、`created_at`、
  `expires_at`、`claimed_at`、`acked_at`、`finished_at`、`upload_id`、`error`）；`uploads` 增加
  `request_id` / `trigger` 列；**部分唯一索引** `idx_uploads_request`（仅 `request_id IS NOT NULL` 时唯一）。
- **迁移**：`init_db()` 检测缺列并 `ALTER TABLE` 补齐；历史行 `trigger='periodic'`、`request_id=NULL`，可重复调用。
- **6 个接口**：`POST /api/v1/tasks`（创建，连点去重返回 200 + `duplicate=true`）、`GET /api/v1/tasks/next`
  （板端领取，原子 `submitted → dispatched`）、`GET /api/v1/tasks`（列表）、`GET /api/v1/tasks/{request_id}`
  （详情 + 关联上传摘要）、`POST /api/v1/tasks/{request_id}/ack`、`POST /api/v1/tasks/{request_id}/fail`。
- **路由顺序**：`/tasks/next` 必须声明在 `/tasks/{request_id}` 之前（FastAPI 按声明顺序匹配），已在代码中注释。
- **超时**：**惰性判定**（创建/查询/领取路径先跑 `_expire_stale_tasks`），无后台线程，重启自洽；
  `/tasks/next` 的 SQL 再以 `expires_at >= now` 兜底。
- **幂等/收尾**：手动批次与任务收尾在**同一事务**内完成；重复回传返回 `200 + idempotent=true` 且不写样本。

### 2.3 板端固件（`main/main.c`）

- `server_api_url()`：由 `CONFIG_SENSOR_SERVER_URL` 推导 `/api/v1/...` 基址；**若 URL 没有 `/api/v1/upload` 后缀则
  自动关闭轮询**（只做周期上报的老配置不会被影响）。
- `task_poll_task`：3 s 轮询 `GET /api/v1/tasks/next`；领取后校验服务端 `expires_at`（SNTP 校时）再开工；
  采集完成后 `POST /api/v1/upload`（`trigger=manual` + `request_id`），失败走 `/fail` 回执。
- **采样**：始终由 `sampler_task` 独占读取 QMA6100P（单一 I2C 读取者）；手动采集复用同一 10 ms 节拍，
  按 `period_ticks = 100/rate_hz` 降采样；样本交接用**非阻塞** `xQueueSend(..., 0)`，队列满则下一拍重试。
- **暂停周期上报**：`manual_capture_begin()/consumes_sample()/on_upload_done()` 组成闸门；
  本地兜底截止时间 = 2 × 期望时长 + 5 s，避免任务悬挂锁死采样。
- `upload_batch_t` 柔性数组，按任务点数分配（**单次上限 600 点** ≈ 14 KB 堆，超限直接 `fail` 回执）。
- 其他：失败回执、堆内存日志（便于观察 WiFi/HTTP 开销）。

### 2.4 Web 前端（`server/static/index.html`、`server/static/app.js`）

- 按钮：「仅刷新（不采集）」只刷新数据/任务卡片；「采集一次最新数据」走 `POST /tasks` → 轮询状态直至终态。
- 任务卡片：`request_id`、状态、规格（频率/点数）、剩余有效期；连点提示"已复用未完成任务"。
- 数据卡片：补「触发方式」（`periodic（周期上报）` / `manual（按需采集）`）与「任务号」回填。
- 三维视图：正交投影（偏航 + 俯仰）三轴箭头、加速度向量 + 三分量虚线 + 地面投影 + 重力参考 `g`、
  最近 30 s 轨迹（抽样 600 点）、读数 `a=(…)` / `|a|` / 视角参数；拖拽旋转、滚轮缩放、双击复位、
  自动旋转开关（限 30 fps）；零第三方库、零 CDN（局域网离线可用），完全复用 `/api/v1/window`。
- 自动化钩子：`/ui/?autocapture=1` 在加载 1.2 s 后调用与按钮**完全相同**的 `captureOnce()`，便于无头验证。

### 2.5 测试（`server/`）

| 文件 | 类型 | 覆盖 |
|------|------|------|
| `test_receive.py` | 单元（临时库，不起服务） | 接收/校验/入库/查询/鉴权 + **任务全流程 8 组新用例**：创建 → 领取 → ack → 回传 → 双向可追溯 → 幂等不重复写样本 → 连点去重 → 不匹配返回 400 且任务置 `failed` → 惰性 `timeout` 且不再下发 → `/fail` 上报 → 404 → 参数越界 |
| `e2e_server_check.py` | 真实 HTTP 端到端 | 启动真实 uvicorn 子进程，走真实 HTTP 跑任务循环：上传、查询、任务创建/连点/领取/ack/回传/幂等/`completed`/`health` |
| `e2e_ui_check.py` | 无头浏览器 UI 自检（新增） | 用本机 Edge/Chrome `--headless --dump-dom` 打开 `/ui/?autocapture=1`：设备下拉、`trigger`/`request_id` 回填、波形点数、三维视图 `|a|` 读数、**页面自身通过按钮代码路径建任务且服务端可见**；无浏览器时打印 SKIP 而不是失败 |

> 过程中曾有一个临时脚本 `server/_e2e_ui_check.py`，被正式版 `server/e2e_ui_check.py` 取代后删除，避免仓库里留两份。

### 2.6 文档

| 文件 | 变更 |
|------|------|
| `README.md` | 代码结构树补 `e2e_ui_check.py`；数据链路图加入"按需采集"通道；§2/§4.3/§5/§7 补按钮用法、任务接口、三维视图、验收口径 |
| `PROJECT_STRUCTURE.md` | §2.2/§2.3/§3.3 补任务接口清单、板端任务模块说明、本功能的提交示例表 |
| `docs/manual_capture_task.md`（新增） | 需求与目标、总体设计与状态机、接口字段、板端实现要点、前端实现要点、验证（三层自测 + 手工验收场景 + 编译验证）、已知限制 |
| `docs/manual_capture_task_work_log.md`（本文档） | 需求 → 交付 → 验证 → 遗留 的过程记录 |

---

## 三、接口与状态机（速览）

**状态机**

| 状态 | 含义 | 谁推进 |
|------|------|--------|
| `submitted` | Web 已创建，等待板端领取 | `POST /tasks` |
| `dispatched` | 板端已领取（不再重复下发） | `GET /tasks/next` |
| `acked` | 板端回执已收到任务（可选步骤） | `POST /tasks/{id}/ack` |
| `completed` | 上传成功、与上传同事务收尾 | 手动批次 `POST /api/v1/upload` |
| `failed` | 采集/上传/参数不匹配导致失败 | `/fail` 或上传校验失败 |
| `timeout` | 超期未领取或未完成（惰性判定） | 任意任务接口触发 `_expire_stale_tasks` |

**端到端时序**

```
浏览器 /ui/                服务端 server/main.py                板端 ESP32-S3-EYE
    │ POST /api/v1/tasks           │                                   │
    │─────────────────────────────▶│ tasks: submitted                  │
    │ 200/201 {request_id, task}    │                                   │
    │                               │◀── GET /tasks/next?device_id=… ───│ 每 3 s 轮询
    │                               │ 原子领取 → dispatched              │
    │                               │◀── POST /tasks/{id}/ack ──────────│ 回执（可选）
    │                               │                                   │ sampler 按节拍采 N 点
    │                               │                                   │（期间不喂周期窗口）
    │                               │◀── POST /api/v1/upload ───────────│ request_id + trigger=manual
    │                               │ 同事务：uploads+samples 入库        │
    │                               │        + tasks 置 completed        │
    │ GET /tasks/{id}（每 1 s）      │                                   │
    │─────────────────────────────▶│ 状态 + 关联 upload 摘要             │
```

**关键约定**

- 设备侧接口沿用既有 Token 鉴权，Web 侧接口保持局域网免鉴权（未改动）。
- 两条上传通道共用 `POST /api/v1/upload`：**不带 `request_id` = 周期上报（行为与旧版一致）**，带 `request_id` = 按需采集。
- 连点去重范围是 `device_id + source`：每设备同时只允许 1 个非终态任务，多设备互不干扰。

---

## 四、关键设计与取舍

| # | 决策 | 原因 / 被否方案 |
|---|------|----------------|
| 1 | 连点返回 `200 + duplicate=true`（复用未完成任务），而不是 409/新建 | 409 需要前端额外分支；新建会造成多条并行任务、板端重复采 |
| 2 | 幂等靠 `uploads` 上的**部分唯一索引** | 应用层"先查后写"在并发下仍可能重复；索引让数据库兜底 |
| 3 | 超时用**惰性判定**，不启后台线程 | 后台线程与 uvicorn reload/多 worker 冲突；惰性判定天然重启自洽，SQL 再兜底 |
| 4 | 板端单次上限 600 点，超限直接 `fail` | 分片采集会让"一次任务 = 一批数据"的语义变复杂；先限住并明确回执 |
| 5 | 手动采集**始终复用 `sampler_task` 的 10 ms 节拍**，不新开读取任务 | QMA6100P 是 I2C 从设备，两处并发读取会互相打断；单一读取者最稳 |
| 6 | 交接用**非阻塞** `xQueueSend(..., 0)` + 下一拍重试 | 阻塞发送会让网络抖动拖慢采样节拍 |
| 7 | 本地兜底截止时间 = 2 × 期望时长 + 5 s | 服务器不可达时避免"采集中"状态永久锁死周期上报 |
| 8 | 三维视图用**纯 Canvas 2D 正交投影**，不用 WebGL/第三方库 | 局域网离线可用、无 CDN 依赖、CPU 开销小、读数稳定 |
| 9 | 三维视图**复用** `/api/v1/window`，服务端零改动 | 避免为可视化再加一套数据接口 |
| 10 | 加 `?autocapture=1` 钩子 | 无头浏览器可验证"页面自身建任务"的完整路径，而不必模拟点击 |

---

## 五、验证记录（均为本次实测）

| 验证项 | 命令 / 方式 | 实测结果 |
|--------|-------------|----------|
| 单元自测 | `python server/test_receive.py` | `ALL CHECKS PASSED`（25 项，含任务全流程 8 组） |
| 真实 HTTP 端到端 | `python server/e2e_server_check.py` | `E2E ALL CHECKS PASSED`（8 项） |
| 无头浏览器 UI 端到端 | `python server/e2e_ui_check.py` | `ALL 22 UI E2E CHECKS PASSED`、`UI_EXIT=0`（关键断言：任务卡片渲染出 `manual（按需采集）` 与 `request_id`；页面自身建出的任务服务端可见；`health` 计数 `{completed:1, submitted:1}`；`devices` 暴露 `pending_tasks:1`） |
| 固件编译 | `idf.py build`（`build_idf.bat` → `build_log.txt`） | `BUILD_EXIT=0`；`data_capture_sim.bin` ≈ `0x15a200`；分区剩余 8%；无新增告警 |
| 老库迁移 | 手工造无 `request_id`/`trigger` 列的旧库后调 `init_db()` | 列被补齐、历史行 `trigger='periodic'`、部分唯一索引存在、周期与手动上传均正常、`init_db()` 可重复调用 |
| 代码结构复核 | 检索路由声明顺序 / `app.js` 收尾 | `/tasks/next`（721 行）声明在 `/tasks/{request_id}`（804 行）之前；`app.js` 末尾为 `sceneInit();` |
| 工作区状态 | `git status --short` | 干净（无未提交改动），`main` 未被合并 |

> 本环境的终端截图/快照不可靠（常为空或截断），因此关键结论一律以**文件落盘**为准：
> `build_log.txt` 中的 `BUILD_EXIT=0`、`server/data/_e2e_result.txt` 等。

---

## 六、过程中遇到的问题与处理

| 问题 | 现象 | 处理 |
|------|------|------|
| Windows 终端编码 | 无头浏览器 `--dump-dom` 输出用 `text=True` 解码会崩/乱码 | 改为读二进制后按 UTF-8 解码；脚本内 `sys.stdout.reconfigure(encoding="utf-8")` |
| 进程收尾丢输出 | 用 `os._exit(0)` 退出时最后几行打印丢失 | 退出前手动 `sys.stdout.flush()` |
| 终端快照不可靠 | 命令已成功，回显却为空/被截断 | 关键结果写入文件（`build_log.txt`、`server/data/_e2e_result.txt`）后再读取判定 |
| 并行跑测试互相干扰 | 两个脚本同时起 uvicorn / 写库，结果互相污染 | 改为**串行**执行三层测试 |
| 临时脚本残留 | 一度出现 `server/_e2e_ui_check.py` 与正式脚本并存 | 正式版落地后删除临时脚本 |
| 旧库兼容 | 既有 `data_capture.db` 缺新列 | `ALTER TABLE` 补列 + 历史行回填，并单独验证迁移与重复调用 |

---

## 七、未完成 / 受阻

### 7.1 `git push` + PR（**受环境网络限制，未完成**）

| 尝试 | 命令要点 | 结果 |
|------|----------|------|
| 直接推送 | `git push -u origin feature/manual-capture-task-web-3d`（`GIT_TERMINAL_PROMPT=0`） | `fatal: unable to access 'https://github.com/ZhouXiaoShan499/ESP32-S3-datawifi.git/': Failed to connect to github.com:443 after 21053 ms: Could not connect to server` |
| 降级协议重试 | 同上并加 `-c http.version=HTTP/1.1 -c credential.interactive=false`、`GCM_INTERACTIVE=never` | 同样失败：`Failed to connect to github.com:443 after 21090 ms` |

即：**分支只在本地，`main` 未合并**。需要你在有凭据的终端执行：

```bash
git push -u origin feature/manual-capture-task-web-3d
# 打开 PR（标题建议）：
#   feat: on-demand capture task (request_id traceable) + web buttons + Canvas 3D view
# 合并后清理分支：
git checkout main && git pull && git branch -d feature/manual-capture-task-web-3d
```

### 7.2 需要真机才能验收的场景（清单见 `docs/manual_capture_task.md` §6.2）

1. 采集窗口内**周期批次计数为 0**、板端日志只有 `[task] accepted/capturing/captured`；
2. 连点 5 次只产生 1 条非终态任务；
3. 点按钮后断电 → 60 s 后任务变 `timeout`、无上传，重启服务端状态不变；
4. 静止时三维视图向量贴近重力参考（约 `|a| = 9.81 m/s²`）。

---

## 八、复现方式（怎么再跑一遍）

```bash
# 1. 服务端三层自测（串行，避免端口/库互相干扰）
python server/test_receive.py          # 期望：ALL CHECKS PASSED
python server/e2e_server_check.py      # 期望：E2E ALL CHECKS PASSED
python server/e2e_ui_check.py          # 期望：ALL 22 UI E2E CHECKS PASSED（无浏览器则 SKIP）

# 2. 固件编译
build_idf.bat                          # 期望：build_log.txt 末尾 BUILD_EXIT=0

# 3. 手工联调
cd server && python main.py            # 浏览器打开 http://<PC 局域网 IP>:8000/ui/
#   · 点「采集一次最新数据」→ 看任务卡片状态流转与三维视图
#   · 让页面自动触发一次：http://<PC 局域网 IP>:8000/ui/?autocapture=1
```

---

## 九、后续可做（按优先级）

1. **完成 push / PR / 合并**（唯一被环境卡住的交付步骤）。
2. 上板跑完 §7.2 的 4 个手工场景，把实测日志补进 `docs/manual_capture_task.md`。
3. 若需要"掉电续传"：引入 SD 卡落盘队列，配合 `request_id` 做补传（当前失败批次不重放）。
4. 若要公网部署：Web 侧接口补鉴权 + TLS / 反向代理。
5. 若要真实姿态角：在三维视图上做加速度 + 陀螺的传感器融合（当前仅加速度向量可视化）。