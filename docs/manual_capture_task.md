# 按需采集任务（Web 手动触发）与三维姿态视图

## 一、需求与目标

原有链路里板端只做**周期性上报**（每 1 s 打包 100 点 POST），Web 页面只能"看"历史与实时数据，
无法"点一下就要一批最新数据"。本次补齐按需采集（manual capture）能力：

| 目标 | 验收方式 |
|------|----------|
| Web 点按钮 → 板端立刻采一批（默认 100 Hz × 1 s = 100 点）并回传 | 任务卡片从 `submitted` 走到 `completed`，数据卡片出现新批次 |
| 该批次可追溯、可幂等 | `request_id` 贯穿任务 / 回执 / 上传 / 样本；重复上传不重复写样本 |
| 采集期间暂停周期上报 | 采集窗口内服务端只收到带该 `request_id` 的 manual 批次 |
| 连点不产生并行任务 | 5 次连点后任务列表只有 1 条非终态任务，同一 `request_id` 只有 1 条上传 |
| 板端掉线/超时不留脏数据 | 过期任务变 `timeout`，不产生上传，重启服务端状态不变 |
| 新增可旋转三维视图 | 拖拽/滚轮/双击操作有效，向量读数与 `ax/ay/az` 一致 |

---

## 二、总体设计

```
浏览器 /ui/                服务端 server/main.py                板端 ESP32-S3-EYE
    │                              │                                   │
    │ POST /api/v1/tasks           │                                   │
    │─────────────────────────────▶│ tasks: submitted                  │
    │ 201 {request_id, task}       │                                   │
    │                              │◀── GET /tasks/next?device_id=… ────│ 每 3 s 轮询
    │                              │ 原子领取 → dispatched              │
    │                              │◀── POST /tasks/{id}/ack ──────────│ 回执（可选）
    │                              │                                   │ sampler 按节拍采 N 点
    │                              │                                   │（期间不喂周期窗口）
    │                              │◀── POST /api/v1/upload ───────────│ request_id + trigger=manual
    │                              │ 同事务：uploads+samples 入库        │
    │                              │        + tasks 置 completed        │
    │ GET /tasks/{id}（每 1 s）     │                                   │
    │─────────────────────────────▶│ 状态 + 关联 upload 摘要             │
```

**状态机**

| 状态 | 含义 | 谁能推进 |
|------|------|----------|
| `submitted` | Web 已创建，等待板端领取 | `POST /tasks` |
| `dispatched` | 板端已领取（不再重复下发） | `GET /tasks/next` |
| `acked` | 板端回执已收到任务 | `POST /tasks/{id}/ack` |
| `completed` | 数据已入库并关联到该任务 | `POST /api/v1/upload`（带 `request_id`） |
| `failed` | 板端上报失败，或上传与任务不匹配 | `POST /tasks/{id}/fail` / 服务端校验 |
| `timeout` | 超过 `expires_at` 仍未完成（终态，不再下发） | 惰性判定 |

`request_id` 是任务主键（`uuid4().hex`，32 字符），并同时出现在 `uploads.request_id`
与上传 JSON 中，因此"任务 ↔ 批次 ↔ 样本"三者可以互相回溯。

---

## 三、服务端实现（`server/main.py`）

### 3.1 `tasks` 表

| 列 | 说明 |
|----|------|
| `request_id` | 主键（uuid4 hex） |
| `device_id` / `source` / `unit` | 目标设备与数据类型（默认 `qma6100p` / `m/s^2`） |
| `sample_rate_hz` / `sample_count` | 采集节拍与点数（默认 100 Hz / 100 点） |
| `trigger` | 固定 `manual`（与 `uploads.trigger` 对应） |
| `status` | 状态机取值（见上） |
| `created_at` / `dispatched_at` / `acked_at` / `completed_at` | 各状态时间戳 |
| `expires_at` | 有效期上限（默认创建时间 + 60 s） |
| `upload_id` | 完成后关联的 `uploads.id` |
| `error` | 失败原因（板端上报或服务端校验结论） |

### 3.2 接口一览

| 方法 | 路径 | 调用方 | 说明 |
|------|------|--------|------|
| `POST` | `/api/v1/tasks` | Web | 创建任务；同设备已有未完成任务时复用（`200 + duplicate=true`） |
| `GET` | `/api/v1/tasks` | Web | 任务列表（`?device_id=…&limit=n`） |
| `GET` | `/api/v1/tasks/next?device_id=…` | 板端 | 领取任务（原子 `UPDATE … WHERE status='submitted'`） |
| `GET` | `/api/v1/tasks/{request_id}` | Web | 任务详情 + 关联上传摘要 |
| `POST` | `/api/v1/tasks/{request_id}/ack` | 板端 | 回执 |
| `POST` | `/api/v1/tasks/{request_id}/fail` | 板端 | 上报失败原因 |

创建（Web）：

```json
// POST /api/v1/tasks
{"device_id": "esp32s3-eye-0001", "sample_count": 100, "sample_rate_hz": 100, "timeout_s": 60}

// 201 Created
{"code": "OK", "ok": true, "duplicate": false,
 "task": {"request_id": "8f3c…", "status": "submitted", "status_cn": "已提交",
          "terminal": false, "expires_in_s": 59.9, "sample_count": 100, "sample_rate_hz": 100}}
```

领取（板端）：

```json
// GET /api/v1/tasks/next?device_id=esp32s3-eye-0001
{"ok": true, "found": true,
 "task": {"request_id": "8f3c…", "status": "dispatched", "sample_count": 100,
          "sample_rate_hz": 100, "expires_at": 1767123516.78}}

// 无任务（或任务已被其它请求领走 / 已过期）
{"ok": true, "found": false}
```

回传（板端）——与周期批次共用同一个上传接口，只是多带两个字段：

```json
// POST /api/v1/upload
{"device_id": "esp32s3-eye-0001", "source": "qma6100p", "unit": "m/s^2",
 "ts_ms": 1767123456789, "request_id": "8f3c…", "trigger": "manual",
 "samples": [{"i": 0, "t_ms": 0, "ax": 0.02, "ay": -0.15, "az": 9.81}, "…"]}

// 201 Created（首次） / 200 OK（同一 request_id 重放，idempotent=true 且不写样本）
{"code": "OK", "ok": true, "upload_id": 42, "request_id": "8f3c…",
 "trigger": "manual", "idempotent": false, "sample_count": 100}
```

### 3.3 四个关键设计点

1. **去重（连点）**：`POST /tasks` 先 `_expire_stale_tasks()` 清理过期任务，再查
   `device_id + source` 上是否已有非终态任务；有则直接返回该任务且 `duplicate=true`，
   因而不可能因为连点产生并行采集。
2. **幂等（重放）**：`uploads.request_id` 上建了**部分唯一索引**
   （`CREATE UNIQUE INDEX idx_uploads_request ON uploads(request_id) WHERE request_id IS NOT NULL`）。
   上传时先按 `request_id` 查已有行，命中即 `200 + idempotent=true`，不写 `uploads` 也不写 `samples`。
3. **惰性超时（无后台线程）**：`_expire_stale_tasks()` 把 `expires_at < now` 且未终态的任务批量置
   `timeout`；创建 / 查询 / 领取路径都会先调用它。因此不需要定时线程，服务重启后状态依然自洽，
   且 `/tasks/next` 的 SQL 里还有 `expires_at >= now` 兜底——过期任务绝不会被下发。
4. **路由顺序**：FastAPI 按声明顺序匹配，`/api/v1/tasks/next` 必须写在 `/api/v1/tasks/{request_id}`
   之前，否则 `next` 会被当成 `request_id`。

另外两点工程细节：

- **任务收尾与数据入库在同一事务**：`store_upload()` 内先查幂等、再插 `uploads` / `samples`、
  最后 `UPDATE tasks SET status='completed', upload_id=?`，一起 `commit`。不会出现
  "任务完成但没有数据"或反之。
- **带 `request_id` 的上传先校验归属**（`_check_task_match()`）：`device_id` / `source` / `unit` /
  样本数 / `ts_ms` 逐项比对，不匹配则把任务置 `failed` 并返回 **400**（板端收到 4xx 不重试），
  避免任务卡在 `dispatched` 直到超时。

### 3.4 老库迁移与向后兼容

- `CREATE TABLE IF NOT EXISTS` **不会**给已存在的表补列。`init_db()` 在 `executescript` 之后用
  `PRAGMA table_info(uploads)` 判断，缺列时 `ALTER TABLE uploads ADD COLUMN request_id TEXT` /
  `ADD COLUMN trigger TEXT NOT NULL DEFAULT 'periodic'`，再创建上述部分唯一索引。
  老库升级后历史行 `trigger` 自动填 `periodic`、`request_id` 为 `NULL`。
- 不带 `request_id` 的上传走的是同一条代码路径的旧分支：`trigger` 归一为 `periodic`，
  返回体保持在原字段基础上新增 `request_id: null` / `trigger` / `idempotent`，旧调用方不受影响。
- `GET /api/v1/latest` 与 `/api/v1/health`、`/api/v1/devices` 顺势多返回任务相关信息
  （`request_id` / `trigger` / `total_tasks` / `tasks_by_status` / `pending_tasks`）。
- 鉴权范围不变：设备侧接口在配置 `SENSOR_TOKEN` 时校验 Bearer Token，Web 侧查询/建任务接口
  与既有查询接口一致不校验。

---

## 四、板端实现（`main/main.c`）

### 4.1 新增任务线程

| 项 | 值 | 说明 |
|----|----|------|
| 任务名 | `task_poll` | 优先级 3、栈 8192 B（HTTP + cJSON 需要） |
| 轮询周期 | `TASK_POLL_INTERVAL_MS` = 3000 ms | 仅在 WiFi 已连接时轮询 |
| 地址推导 | `server_api_url()` | 把 `CONFIG_SENSOR_SERVER_URL` 结尾的 `/api/v1/upload` 换成目标路径；后缀不匹配则**禁用轮询**，避免往错误地址发请求 |

`task_poll_task` 的处理顺序：`MANUAL_ERROR` → 上报失败回执；非 `IDLE` → 采集/上传进行中，不领新任务；
`IDLE` → `GET /tasks/next`（`?device_id=` 用 `CONFIG_SENSOR_DEVICE_ID`）解析 JSON，校验
`sample_count ≤ TASK_MAX_SAMPLES(600)`、`sample_rate_hz ≤ 100`，超限直接 `POST fail`；
通过则分配批次 → `manual_capture_begin()` → `POST ack`。

### 4.2 采样闸门与"暂停周期上报"

QMA6100P 的 I2C 只由 `sampler_task` 读取（新增线程绝不碰传感器），手动采集复用同一个 10 ms 采样节拍：

```c
/* 采样闸门：原来只有 s_collecting */
if (s_collecting || manual_capture_is_active()) { … }

/* 每个采样点：手动采集优先；返回 true 表示这一拍属于任务，周期窗口不喂数据 */
bool manual_tick = manual_capture_consumes_sample(timestamp_ms, ax_ms2, ay_ms2, az_ms2);
if (!manual_tick && s_wifi_connected) {
    upload_accumulate(timestamp_ms, ax_ms2, ay_ms2, az_ms2);   /* 周期批次 */
}
```

- 接手任务的第一拍会 `s_upload_count = 0`，丢弃未满的周期窗口，避免周期数据混进 manual 批次。
- `rate_hz < 100` 时按 `period_ticks = 100 / rate_hz` 抽点（如 10 Hz 即每 10 拍取一个点）。
- 批次填满后**非阻塞**入队（`xQueueSend(..., 0)`）：队列满就在下一拍重试，绝不阻塞 10 ms 采样循环；
  直到本地 deadline（理论采集时长 ×2 + 5 s）仍失败才判失败。
- 进入采集前还会用 SNTP 时间比对服务端 `expires_at`，已过期则直接失败，不做无用的采集。

### 4.3 批次结构与内存

```c
typedef struct {
    uint64_t ts_ms;
    uint32_t count;
    uint32_t cap;                              /* samples[] 容量 */
    char     request_id[TASK_REQUEST_ID_LEN];  /* 空 = 周期批次 */
    char     trigger[TASK_TRIGGER_LEN];        /* "periodic" / "manual" */
    upload_sample_t samples[];                 /* 柔性数组，按 cap 分配 */
} upload_batch_t;
```

周期批次固定 100 点（与旧版一致），手动批次按任务要求分配（上限 600 点 ≈ 14 KB 堆）。
启动日志会把队列最坏占用与单次手动采集的额外开销一起打出来：

```
[HEAP] upload_batch_t=… B x queue 8 = … B worst case (+… B for one on-demand capture)
```

### 4.4 上传与收尾

手动批次走上传队列，由 `uploader_task` 带 `request_id` / `trigger` POST；上传结束后回调
`manual_capture_on_upload_done(ok, reason)`：成功 → `IDLE`（周期上报恢复）；失败 → `ERROR`，
由下一轮 `task_poll_task` 调 `POST /tasks/{id}/fail` 上报（上报失败会保留 `ERROR` 状态下次重试）。

### 4.5 日志样例

```
I (12345) app: [task] polling http://10.1.41.14:8000/api/v1/tasks/next?device_id=esp32s3-eye-0001 every 3000 ms
I (15346) app: [task] accepted 8f3c…: 100 samples @ 100 Hz
I (15347) app: [task] capturing 8f3c…
I (16350) app: [task] captured 100 samples for 8f3c…
I (16402) app: [upload] OK ts=… n=100 http=201 attempt=1
I (16403) app: [task] done, periodic uploads resumed
```

### 4.6 板端能力上限

| 参数 | 上限 | 超限行为 |
|------|------|----------|
| `sample_count` | 600（约 6 s @100 Hz） | `POST /tasks/{id}/fail`：`sample_count or sample_rate_hz beyond device capability` |
| `sample_rate_hz` | 100（板端采样上限） | 同上 |

---

## 五、Web 界面

### 5.1 按钮与任务卡片

| 控件 | 行为 |
|------|------|
| 仅刷新（不采集） | 重新拉 `/api/v1/devices`、`/latest`、`/window`，不打扰板端 |
| 采集一次最新数据 | `POST /api/v1/tasks`（100 点 @ 100 Hz、60 s 有效期）→ 每 1 s 轮询 `/api/v1/tasks/{id}` 直到终态 |
| 任务卡片 | `request_id`、状态码 + 中文状态、样本数/采样率、剩余有效期、`error`；`duplicate=true` 时提示"已复用未完成任务" |

任务完成后页面会立刻补拉一次数据，不用等下一次 0.8 s 常规轮询。

### 5.2 三维姿态视图（纯 Canvas 2D）

- **投影**：正交投影，`project3D(x, y, z)` = 绕 Y 轴偏航（`yaw`）→ 绕屏幕水平轴俯仰（`pitch`）
  → 屏幕坐标 = 画布中心 + 缩放 × 投影坐标；不做透视除法，读数稳定、CPU 开销小。
- **图元**：`ax/ay/az` 三轴箭头（红/绿/蓝）与轴标签；当前加速度向量（粗实线 + 端点圆点）；
  三个分量虚线；向量到 `y = -L` 地面网格的投影虚线；重力参考 `g`（灰虚线，与 `az` 同向）；
  最近 30 s 轨迹折线（抽样至 600 点，起点灰、终点蓝）；左上角读数（`a=(…)`, `|a|`, 视角参数）。
- **交互**：拖拽旋转（偏航不限、俯仰限 ±83°）、滚轮缩放（0.4×–3×）、双击或「复位视角」按钮、
  「自动旋转」开关（rAF 驱动、限 30 fps）；窗口尺寸变化时按新尺寸重绘。
- **数据来源**：完全复用已取的 `/api/v1/window?seconds=30`（`state.lastPoints`），服务端零改动。
- **依赖**：零第三方库、零 CDN（局域网离线可用），与波形图共用 `fitCanvas()` 的 DPR 处理。

### 5.3 自动化钩子

`/ui/?autocapture=1` 会在页面加载 1.2 s 后调用与按钮完全相同的 `captureOnce()`，
便于用无头浏览器（见 6.1 第 3 个脚本）验证"建任务 → 渲染状态"链路而无需人工点击。

---

## 六、验证

### 6.1 三层自测（均已实测通过）

| 脚本 | 覆盖 | 实测结果 |
|------|------|----------|
| `python server/test_receive.py` | 接收/校验/入库/查询/鉴权 + 任务全流程（创建 → 领取 → ack → 回传 → 幂等 → 连点去重 → 不匹配 400+failed → 惰性超时 → fail 上报 → 404 → 参数校验） | `ALL CHECKS PASSED`（25 项） |
| `python server/e2e_server_check.py` | 真实 uvicorn 子进程 + 真实 HTTP：上传、查询、任务创建/连点/领取/ack/回传/幂等/completed/health | `E2E ALL CHECKS PASSED`（8 项） |
| `python server/e2e_ui_check.py` | 无头 Edge 打开 `/ui/?autocapture=1` 并 dump DOM：设备下拉、`trigger`/`request_id` 回填、波形点数、三维视图 `|a|`、页面自身建任务且服务端可见 | `ALL 22 UI E2E CHECKS PASSED`（无浏览器时打印 SKIP） |

**老库迁移**单独验证过：手工造一个没有 `request_id` / `trigger` 列的旧库后调用 `init_db()`，
列被补齐、历史行 `trigger='periodic'`、`request_id` 为 `NULL`、部分唯一索引存在，
随后周期上传与手动上传都正常，且 `init_db()` 可重复调用。

### 6.2 手工验收场景

| 场景 | 操作 | 期望 |
|------|------|------|
| 采集一次 | `/ui/` 点按钮 | 任务卡片 `submitted → dispatched → acked → completed`；数据卡片「触发方式」为 `manual（按需采集）`、「任务号」等于卡片里的 `request_id` |
| 暂停周期上报 | 观察采集窗口内服务端收到的批次 | 窗口内只有带该 `request_id` 的 manual 批次，**周期批次计数为 0**；板端日志只有 `[task] accepted/capturing/captured` |
| 连点 5 次 | 快速点击按钮 5 次 | 只产生 1 条非终态任务；`uploads` 中该 `request_id` 只有 1 行；页面提示"已复用未完成任务" |
| 掉电/超时 | 点按钮后给板端断电 | 任务在有效期（默认 60 s）后变 `timeout` 且无上传；重启服务端状态不变，`/tasks/next` 返回 `found:false`；重新上电后可正常创建新任务 |
| 重复回传 | 用同一 `request_id` 再 POST 一次上传 | 返回 `200 + idempotent=true`，`samples` 计数不变 |
| 三维视图 | 拖拽/滚轮/双击 | 视角可旋转缩放复位；静止时向量贴近重力参考 `g`（约 `|a| = 9.81 m/s²`） |

### 6.3 编译验证

`idf.py build` 通过（仓库根 `build_idf.bat` 写出的 `build_log.txt` 末尾为 `BUILD_EXIT=0`），
`data_capture_sim.bin` 约 0x15a200 字节、分区剩余 8%。日志中只有改造前就存在的
`-Wunused-function` / `-Wunused-variable` 告警，没有新增告警。

---

## 七、已知限制与后续可做

1. **板端单次点数上限 600**（≈14 KB 堆）：更大的任务由板端直接 `fail` 回执，不做分片采集。
2. **失败批次不落盘重放**：手动批次与周期批次共用"有界重试 + 队列满丢弃"策略，
   掉电或长时间断网时任务会走到 `timeout`（不产生半截数据）。若要做"掉电续传"，
   需要引入 SD 卡落盘队列，当前未实现。
3. **Web 侧接口未鉴权**：面向局域网联调；公网部署需补 TLS/反向代理与 Web 侧鉴权。
4. **三维视图不做姿态解算**：QMA6100P 只有三轴加速度（无陀螺/磁力计），因此展示的是
   "加速度向量 + 轨迹 + 重力参考"，不是欧拉角姿态；要得到姿态角需要额外传感器融合。
5. **每设备同时只跑一个任务**：去重范围是 `device_id + source`；多设备可各持有一个任务，
   `/tasks/next` 按 `device_id` 过滤，互不干扰。