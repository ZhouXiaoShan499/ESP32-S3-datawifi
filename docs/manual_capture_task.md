# 按需采集任务（Web 手动触发）与三维姿态视图

> 需求来源、交付物清单、实测结果与遗留事项见 `docs/manual_capture_task_work_log.md`。

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
| `sample_rate_hz` / `sample_count` | 采集节拍与点数（默认 100 Hz / 100 点；控制任务不采数据，仅保留合法默认值） |
| `kind` | 任务类型：`capture`（缺省，按需采集）/ `pause`（暂停周期上报）/ `resume`（恢复周期上报） |
| `duration_s` | 仅 `pause`：暂停时长（默认 120 s，允许 5–600） |
| `trigger` | `manual`（采集）/ `control`（pause、resume，不产生批次） |
| `status` | 状态机取值（见上） |
| `created_at` / `dispatched_at` / `acked_at` / `completed_at` | 各状态时间戳 |
| `expires_at` | 有效期上限（默认创建时间 + 60 s） |
| `upload_id` | 完成后关联的 `uploads.id`（控制任务恒为 `NULL`） |
| `error` | 失败原因（板端上报或服务端校验结论） |

`device_control` 表（周期上报暂停状态，**每个 `device_id + source` 一行**）：

| 列 | 说明 |
|----|------|
| `device_id` / `source` | 联合主键 |
| `periodic_paused` | 0/1，板端 `POST /applied` 时写入 |
| `paused_until` | 暂停终点（epoch s）；到点即视为恢复，读路径惰性归零 |
| `request_id` | 最近一次写入它的控制任务号（归零后保留，便于追溯） |
| `updated_at` | 最近一次状态变更时间 |

### 3.2 接口一览

| 方法 | 路径 | 调用方 | 说明 |
|------|------|--------|------|
| `POST` | `/api/v1/tasks` | Web | 创建任务（`kind=capture`/`pause`/`resume`）；同 `device+source+kind` 已有未完成任务时复用（`200 + duplicate=true`） |
| `GET` | `/api/v1/tasks` | Web | 任务列表（`?device_id=…&limit=n`） |
| `GET` | `/api/v1/tasks/next?device_id=…` | 板端 | 领取任务（原子 `UPDATE … WHERE status='submitted'`） |
| `GET` | `/api/v1/tasks/{request_id}` | Web | 任务详情 + 关联上传摘要 |
| `POST` | `/api/v1/tasks/{request_id}/ack` | 板端 | 回执 |
| `POST` | `/api/v1/tasks/{request_id}/fail` | 板端 | 上报失败原因 |
| `POST` | `/api/v1/tasks/{request_id}/applied` | 板端 | **控制任务**生效回执（置 `completed` + 写 `device_control`；`capture` 调用返回 `409 WRONG_KIND`） |
| `GET` | `/api/v1/control?device_id=…&source=…` | Web | 暂停状态真相源（惰性归零后返回） |

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
 "trigger": "manual", "idempotent": false, "sample_count": 100,
 "clock_synced": true}      // false = 板端 ts_ms 看起来未对时（见 §4.2「时间戳与新鲜度判据」）
```

控制任务（`kind=pause` / `kind=resume`）——不产生任何上传，靠板端 `/applied` 收尾：

```json
// POST /api/v1/tasks  （pause：多久以后自动恢复）
{"device_id": "esp32s3-eye-0001", "kind": "pause", "duration_s": 120, "timeout_s": 60}

// 201 Created（连点则 200 + duplicate=true；方向相反的 pending 任务会被置 failed）
{"code": "OK", "ok": true, "duplicate": false, "superseded": 1,
 "task": {"request_id": "b7a1…", "kind": "pause", "kind_cn": "暂停周期上报",
          "duration_s": 120, "trigger": "control", "is_control": true,
          "status": "submitted"}}

// POST /api/v1/tasks/{request_id}/applied  （板端确实切换了周期上报开关之后调用）
{"paused_until_ms": 1767123636789}          // 可选：板端自己的定时器终点
// 200 OK
{"code": "OK", "ok": true, "applied": true, "updated": true, "kind": "pause",
 "stale": false, "replay": false,
 "task": {"request_id": "b7a1…", "status": "completed", "upload_id": null},
 "control": {"device_id": "esp32s3-eye-0001", "source": "qma6100p",
             "periodic_paused": true, "remaining_s": 120.0,
             "paused_until_str": "2026-09-21 10:20:36", "request_id": "b7a1…"}}

// GET /api/v1/control?device_id=esp32s3-eye-0001
{"code": "OK", "ok": true,
 "control": {"periodic_paused": true, "remaining_s": 118.4, "request_id": "b7a1…"}}
```

### 3.3 五个关键设计点

1. **去重（连点）**：`POST /tasks` 先 `_expire_stale_tasks()` 清理过期任务，再查
   `device_id + source + kind` 上是否已有非终态任务；有则直接返回该任务且 `duplicate=true`，
   因而不可能因为连点产生并行采集。**去重键必须带 `kind`**：否则一条待执行的 `pause`
   会把后续的 `capture` 一直阻挡住（`test_receive.py` 第 23 项专门守着这一点）。
2. **幂等（重放）**：`uploads.request_id` 上建了**部分唯一索引**
   （`CREATE UNIQUE INDEX idx_uploads_request ON uploads(request_id) WHERE request_id IS NOT NULL`）。
   上传时先按 `request_id` 查已有行，命中即 `200 + idempotent=true`，不写 `uploads` 也不写 `samples`。
3. **惰性超时（无后台线程）**：`_expire_stale_tasks()` 把 `expires_at < now` 且未终态的任务批量置
   `timeout`；创建 / 查询 / 领取路径都会先调用它。因此不需要定时线程，服务重启后状态依然自洽，
   且 `/tasks/next` 的 SQL 里还有 `expires_at >= now` 兜底——过期任务绝不会被下发。
4. **路由顺序**：FastAPI 按声明顺序匹配，`/api/v1/tasks/next` 必须写在 `/api/v1/tasks/{request_id}`
   之前，否则 `next` 会被当成 `request_id`。
5. **控制任务与采集任务不共用收尾通道**：`pause`/`resume` 没有任何数据可上传，若沿用
   「上传即完成」，它们只会在有效期结束后变成 `timeout`。因此新增
   `POST /api/v1/tasks/{request_id}/applied`：板端**确实切换了周期上报开关**之后调用它，
   服务端同一事务里置 `completed` 并写 `device_control`。反向保护同样重要——
   `capture` 任务调 `/applied` 一律 `409 WRONG_KIND`（否则会出现「任务 completed 但 0 样本」），
   带控制任务 `request_id` 的上传同样 `409 WRONG_KIND`（`_check_task_match` 之前就拦下，
   不改任务状态、不写任何样本）。

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
- 同理由 `_migrate_tasks_columns(conn)` 给 `tasks` 补 `kind TEXT NOT NULL DEFAULT 'capture'` /
  `duration_s REAL`，并建 `idx_tasks_device_kind(device_id, source, kind, status)` 支撑新的去重键。
  历史任务因此自动被当作 `capture`，页面渲染与统计口径都不变。
- `device_control` 是新表，由 `CREATE TABLE IF NOT EXISTS` 直接建出，老库不用额外处理。
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
| 地址推导 | `server_api_url()` | 把 `CONFIG_SENSOR_SERVER_URL` 结尾的 `/api/v1/upload` 换成目标路径；后缀必须**位于 URL 末尾**（反向代理前缀会被保留，后缀之后有查询串等则判定失败），不匹配则**禁用轮询**并打 `cannot derive API base` 警告，避免往错误地址发请求 |

`task_poll_task` 的处理顺序：`MANUAL_ERROR` → 上报失败回执；非 `IDLE` → 采集/上传进行中，不领新任务；
重试未送达的 `applied` 回执（有界）；`IDLE` → `GET /tasks/next`（`?device_id=` 用
`CONFIG_SENSOR_DEVICE_ID`）解析 JSON：
控制任务（`kind=pause|resume`）**先判**并直接 `task_apply_control()` → `POST ack` → `POST applied`，
不进下面的采集分支；采集任务才校验 `sample_count ≤ TASK_MAX_SAMPLES(600)`、
`sample_rate_hz ≤ 100`，超限直接 `POST fail`，通过则分配批次 → `manual_capture_begin()` → `POST ack`。

> 领到控制任务时**绝不停掉 `task_poll_task`**：它是唯一能领到「恢复周期」任务的线程，
> 一旦停掉就再也恢复不了（这是暂停功能最容易踩的坑，`e2e_device_check.py` 第 8 步会验证）。

### 4.2 采样闸门与"暂停周期上报"

QMA6100P 的 I2C 只由 `sampler_task` 读取（新增线程绝不碰传感器），手动采集复用同一个 10 ms 采样节拍：

```c
/* 采样闸门：原来只有 s_collecting */
if (s_collecting || manual_capture_is_active()) { … }

/* 每个采样点：手动采集优先；返回 true 表示这一拍属于任务，周期窗口不喂数据 */
bool manual_tick = manual_capture_consumes_sample(timestamp_ms, ax_ms2, ay_ms2, az_ms2);

/* Web「暂停周期」窗口内：照常采样/落盘/刷 UI，只是不喂 1 s 上传窗口 */
bool periodic_paused = periodic_upload_paused();
if (!periodic_paused && periodic_pause_was_on) {
    s_upload_count = 0;            /* 恢复的第一拍丢掉未满窗口，首批仍是干净 100 点 */
}
periodic_pause_was_on = periodic_paused;

if (!manual_tick && !periodic_paused && s_wifi_connected) {
    upload_accumulate(timestamp_ms, ax_ms2, ay_ms2, az_ms2);   /* 周期批次 */
}
```

- 接手任务的第一拍会 `s_upload_count = 0`，丢弃未满的周期窗口，避免周期数据混进 manual 批次。
- 暂停窗口结束（或手动「恢复周期」）的那一拍同样丢窗口，理由相同：不把暂停前后的样本缝进同一批。
- 时间戳按**会话**锚定：`s_base_timestamp_ms` 在会话首个样本处锚定，`timestamp_ms = 锚点 + 样本序号 × 10 ms`；
  闸门"由关变开"即视为新会话（`s_sample_count = 0` → 重新锚定）。否则板子空闲时被任务唤起会沿用上一次会话的
  样本序号，时间戳落到过去，被服务端以 `ts_ms is older than the task creation time` 拒收（实测复现并已修，见
  `docs/manual_capture_task_work_log.md` §7.4）。
- **时间戳与新鲜度判据**（与上一条同源但原因不同）：服务端只在 `ts_ms` 看起来是**真实 epoch**
  （≥ `EPOCH_SANE_MS` = `1600000000000`，即 2020-09-13）时才做 `ts_ms < 任务创建时间 - 5 s` 的回放检查。
  板端 SNTP 没成功时 `gettimeofday()` 只是「开机毫秒数」，拿它跟任务创建时间比大小恒为「陈旧」，会把
  「这个现场没有可用 NTP」误判成「板端上传了旧数据」（400 + 任务 `failed`，而板端收到 4xx 就不再重试）。
  现在这种上传按正常批次入库，响应里 `clock_synced=false`，页面/脚本据此提示「板端未对时」。
  防回放能力没有降级：时钟可信但批次确实陈旧时仍然 400。
- `rate_hz < 100` 时按 `period_ticks = 100 / rate_hz` 抽点（如 10 Hz 即每 10 拍取一个点）。
- 批次填满后**非阻塞**入队（`xQueueSend(..., 0)`）：队列满就在下一拍重试，绝不阻塞 10 ms 采样循环；
  直到本地 deadline（理论采集时长 ×2 + 5 s）仍失败才判失败。
- 进入采集前还会用 SNTP 时间比对服务端 `expires_at`，已过期则直接失败，不做无用的采集。
  该字段服务端发的是 epoch **秒**，板端按 `< 1e12` 判断并归一到毫秒后再比较（见 `manual_capture_task_work_log.md` §7.3）。

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

控制任务没有数据可传，收尾走另一条路：`task_apply_control()` 切换暂停标志后立刻
`POST /api/v1/tasks/{id}/applied`（**不是** best-effort：失败会保留 `s_ctrl_pending_rid`，
由后续轮询重试 `TASK_APPLIED_RETRY_MAX` 次；服务端对同 `request_id` 的重放是幂等的，
不会把暂停终点往后推）。

### 4.5 日志样例

```
I (12345) app: [task] polling http://192.0.2.10:8000/api/v1/tasks/next?device_id=esp32s3-eye-0001 every 3000 ms
I (15346) app: [task] accepted 8f3c…: 100 samples @ 100 Hz
I (15347) app: [task] capturing 8f3c…
I (16350) app: [task] captured 100 samples for 8f3c…
I (16402) app: [upload] OK ts=… n=100 http=201 attempt=1
I (16403) app: [task] done, periodic uploads resumed
I (20011) app: [ctrl] pause applied for b7a1… (120 s window)
I (20012) app: [ctrl] applied receipt delivered for b7a1…
I (140010) app: [ctrl] pause window elapsed - periodic uploads resumed
```

> 「暂停窗口结束」这条日志只在**到点的那一拍**打印一次（标志位被清零，不会刷屏）。

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
| **参数表单** | 样本数（默认 100、上限 **600**）、采样率 Hz（默认 100、上限 **100**）、有效期 s（默认 60、5–600）；`source` 固定 `qma6100p`、`unit` 固定 `m/s^2` 故不开放编辑。范围取「服务端校验」与「板端能力」的交集，越界时本地直接拒绝（状态条提示，不发请求） |
| 采集一次最新数据 | 按表单参数 `POST /api/v1/tasks` → 每 1 s 轮询 `/api/v1/tasks/{id}` 直到终态 |
| 任务卡片 | `request_id`、状态码 + 中文状态、样本数/采样率、剩余有效期、`error`、`dispatched_at` / `acked_at` / `completed_at` 时间戳、关联批次 `upload_id`；`duplicate=true` 时提示"已复用未完成任务" |
| 回执未收到提示 | 任务停在 `dispatched` 且 `dispatched_at` 已过 5 s 仍无 `acked_at` → 黄色提示"仍未收到设备回执"，并说明板端回执失败只写设备串口日志（纯前端推断，服务端状态机不变） |
| 手动 vs 周期对照表 | 两行分别取 `/api/v1/latest?device_id=…&trigger=manual` 与 `?trigger=periodic`，列出 `upload_id` / 点数 / 批次起点 / 接收时间 / `request_id`，用来确认手动采集没有污染周期链路 |
| 任务历史表 | `/api/v1/tasks?device_id=…&limit=20`：创建时间 / **类型** / `request_id` / 点数@Hz（控制任务显示「暂停 120 s」）/ 状态 / `upload_id` / 耗时（按创建时间倒序） |
| **暂停时长 s** | 默认 120、允许 5–600（与服务端 `PAUSE_MIN_S`/`PAUSE_MAX_S` 一致），越界本地拒绝 |
| **暂停周期 / 恢复周期** | 共用 `createTask(kind)`：`pause` 带 `duration_s` → 每 1 s 轮询任务直到 `completed`（板端 `applied` 后）；`resume` 不带时长 |
| **周期上报控制卡片** | `GET /api/v1/control?device_id=…`：徽标（上报中/停止中）、`periodic_paused` 文案、剩余秒数、`paused_until`、最近控制任务 `request_id`、状态更新时间；控制任务终态时立即回读 |

任务完成后页面会立刻补拉一次数据，不用等下一次 0.8 s 常规轮询；
对照表与任务历史挂在主轮询上按节流刷新（每 5 个轮询周期一次），切换设备或建任务时立即刷新。

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
同一 URL 还支持 `&samples=250&rate=50&timeout=45&source=qma6100p`：这些值会先覆盖参数表单，
`captureOnce()` 就用它们建任务，因此可以无人值守地验证「表单参数真的传到了板端任务里」。

控制类按钮同理：`/ui/?autopause=1&pause=90` 走 `createTask('pause')`（`&pause=` 同时覆盖
「暂停时长」输入框），`/ui/?autoresume=1` 走 `createTask('resume')`。

---

## 六、验证

### 6.1 三层自测（均已实测通过）

| 脚本 | 覆盖 | 实测结果 |
|------|------|----------|
| `python server/test_receive.py` | 接收/校验/入库/查询/鉴权 + 任务全流程（创建 → 领取 → ack → 回传 → 幂等 → 连点去重 → 不匹配 400+failed → 惰性超时 → fail 上报 → 404 → 参数校验 → `/latest?trigger=` 过滤与非法值 400 → `/tasks` 历史倒序/字段齐全/limit 夹取）+ **周期上报控制**（kind 缺省兼容、pause 创建/连点、去重键带 kind、resume 取代 pending pause、applied 收尾、`device_control` 真相源、applied 幂等重放、两项 `409 WRONG_KIND`、到期惰性归零、kind/duration_s 校验、`/control` 入参校验、控制任务 fail、历史含 kind） | `ALL CHECKS PASSED`（43 项） |
| `python server/e2e_server_check.py` | 真实 uvicorn 子进程 + 真实 HTTP：上传、查询、任务创建/连点/领取/ack/回传/幂等/completed/health + pause/resume 的 `/applied` 收尾与 `/devices` 暂停标注 | `E2E ALL CHECKS PASSED`（14 项） |
| `python server/e2e_ui_check.py` | 无头 Edge 打开 `/ui/?autocapture=1&samples=250&rate=50` 并 dump DOM（共 6 次 dump）：设备下拉、`trigger`/`request_id` 回填、波形点数、三维视图 `|a|`、参数表单带上板端上限且任务真用该参数、任务卡片 `upload_id` 与三个时间戳、任务历史倒序、`manual`/`periodic` 对照区、无回执提示出现与消失、状态文本含 `acked`；**控制链路**：`?autopause=1&pause=90` 建出 `kind=pause` 任务、控制卡片/任务卡片/历史表按控制任务渲染、模拟板端 `applied` 后转为「停止中 + 倒计时 + request_id 可追溯」、`?autoresume=1` 再恢复为「上报中」 | `ALL 64 UI E2E CHECKS PASSED`（无浏览器时打印 SKIP） |
| `python server/e2e_device_check.py` | **真机验收**：起真实服务（默认 `0.0.0.0:8000`），等板端自己的周期上报出现，再跑满「建任务 → 领取 → ack → 采集窗口内周期上报为 0 → `completed` + `upload_id` → 周期恢复 → 历史可见」+「暂停周期」（板端 `applied` → `device_control` 停止中 → 观察窗内 0 新周期批次 → 暂停中手动采集仍可用）与「恢复周期」（周期批次重新出现）；板端不在场时打印 `[SKIP]` 并以 0 退出 | `DEVICE ACCEPTANCE PASSED`（28 项；**控制任务部分需先烧录新版固件**，旧固件会在该步骤明确失败并提示固件过旧） |

**老库迁移**单独验证过：手工造一个没有 `request_id` / `trigger` 列的旧库后调用 `init_db()`，
列被补齐、历史行 `trigger='periodic'`、`request_id` 为 `NULL`、部分唯一索引存在，
随后周期上传与手动上传都正常，且 `init_db()` 可重复调用。
`tasks.kind` / `tasks.duration_s` 与 `device_control` 同理走 `_migrate_tasks_columns()`：真实库里
（长期使用的 `server/data/sensor.db`）实测升级后可正常收发任务，历史任务被当作 `capture`。

### 6.2 手工验收场景

| 场景 | 操作 | 期望 |
|------|------|------|
| 采集一次 | `/ui/` 点按钮 | 任务卡片 `submitted → dispatched → acked → completed`；数据卡片「触发方式」为 `manual（按需采集）`、「任务号」等于卡片里的 `request_id` |
| 暂停周期上报 | 观察采集窗口内服务端收到的批次 | 窗口内只有带该 `request_id` 的 manual 批次，**周期批次计数为 0**；板端日志只有 `[task] accepted/capturing/captured` |
| 连点 5 次 | 快速点击按钮 5 次 | 只产生 1 条非终态任务；`uploads` 中该 `request_id` 只有 1 行；页面提示"已复用未完成任务" |
| 掉电/超时 | 点按钮后给板端断电 | 任务在有效期（默认 60 s）后变 `timeout` 且无上传；重启服务端状态不变，`/tasks/next` 返回 `found:false`；重新上电后可正常创建新任务 |
| 重复回传 | 用同一 `request_id` 再 POST 一次上传 | 返回 `200 + idempotent=true`，`samples` 计数不变 |
| 三维视图 | 拖拽/滚轮/双击 | 视角可旋转缩放复位；静止时向量贴近重力参考 `g`（约 `|a| = 9.81 m/s²`） |
| 参数表单越界 | 样本数填 700（或采样率填 200）后点按钮 | 不发请求，状态条提示"任务参数不合法：样本数 必须是 1-600 的整数"，任务卡片不变 |
| 参数表单生效 | 填 200 点 @ 50 Hz 后点按钮 | 任务卡片「样本数 / 采样率」显示 `200 点 @ 50 Hz`，板端日志 `accepted …: 200 samples @ 50 Hz` |
| 无回执提示 | 让板端不 ack（或在 `/ui/` 页面里点按钮后把板端断电，任务已 `dispatched`） | 任务卡片下出现黄色"仍未收到设备回执"提示；板端重启并回执/回传后自动消失 |
| **暂停周期（按钮）** | `/ui/` 填「暂停时长」= 60 后点「暂停周期」 | 控制卡片徽标变「停止中」、剩余秒数倒计时、显示自动恢复时刻与任务号；板端日志 `[ctrl] pause applied`；此后**服务端不再收到任何 periodic 批次**（`/api/v1/devices` 里该设备 `periodic_paused=true`） |
| **暂停期间手动采集** | 暂停状态下点「采集一次最新数据」 | 任务照常 `completed` 并产生 manual 批次（暂停只关周期窗口）；暂停状态不因此被清除 |
| **恢复周期（按钮）** | 点「恢复周期」 | 控制卡片回到「上报中」；板端日志 `[ctrl] resume applied`；15 s 内服务端重新出现 periodic 批次 |
| **到期自动恢复** | 暂停时长填 5、点「暂停周期」，然后什么都不做 | 约 5 s 后板端打印 `[ctrl] pause window elapsed`；`/api/v1/control` 的 `periodic_paused` 自动变回 `false`（惰性归零，无需点恢复） |
| **控制任务不产生批次** | 暂停任务完成后看任务卡片与任务历史 | 任务卡片「关联数据 upload_id」显示"控制任务不产生批次，靠板端 `/applied` 收尾"；历史「类型」列为 暂停周期 / 恢复周期 |
| 任务历史与对照 | 连做两次采集后看页面下方两张表 | 任务历史每次新增一行（创建时间/`request_id`/点数@Hz/状态/`upload_id`/耗时，按时间倒序）；对照表 manual 行带 `request_id`、periodic 行 `request_id` 为 `—` |

> 上表逐条的**脚本化版本**（含"无板也能跑"的三层自检、真机 11 项断言、边界与不验收项）
> 见 `docs/manual_capture_task_acceptance.md`。

### 6.3 编译验证

`idf.py build` 通过（仓库根 `build_idf.bat` 写出的 `build_log.txt` 末尾为 `BUILD_EXIT=0`），
`data_capture_sim.bin` 约 0x15a200 字节、分区剩余 8%。日志中只有改造前就存在的
`-Wunused-function` / `-Wunused-variable` 告警，没有新增告警。

> ⚠️ **控制任务（pause/resume）的固件改动需在本机重新编译验证**：本次改动新增了
> `task_apply_control()` / `periodic_upload_paused()` / `task_post_applied()` /
> `task_report_pending_control()` 与几个宏，代码已按现有风格写好并逐行复核（含加锁顺序：
> sampler 持 `s_state_mutex` 再取 `s_task_mutex`，与既有 `manual_capture_consumes_sample()`
> 完全一致，不存在反向加锁），但**尚未在本环境执行 `idf.py build`**（本机没有 ESP-IDF）。
> 请在烧录前先 `idf.py build`，确认零新增告警后再 flash；随后跑
> `python server/e2e_device_check.py` 做真机验收。

---

## 七、已知限制与后续可做

1. **板端单次点数上限 600**（≈14 KB 堆）：更大的任务由板端直接 `fail` 回执，不做分片采集。
2. **失败批次不落盘重放**：手动批次与周期批次共用"有界重试 + 队列满丢弃"策略，
   掉电或长时间断网时任务会走到 `timeout`（不产生半截数据）。若要做"掉电续传"，
   需要引入 SD 卡落盘队列，当前未实现。
3. **Web 侧接口未鉴权**：面向局域网联调；公网部署需补 TLS/反向代理与 Web 侧鉴权。
4. **三维视图不做姿态解算**：QMA6100P 只有三轴加速度（无陀螺/磁力计），因此展示的是
   "加速度向量 + 轨迹 + 重力基准"，不是欧拉角姿态；要得到姿态角需要额外传感器融合。
5. **每设备每类型同时只跑一个任务**：去重范围是 `device_id + source + kind`，
   即同一设备可以同时挂着 1 条采集任务 + 1 条控制任务；多设备可各持有自己的任务，
   `/tasks/next` 按 `device_id` 过滤，互不干扰。方向相反的两条控制任务不会并存
   （新的把旧的置 `failed: superseded by newer control task`）。
6. **控制任务需要新版固件**：旧固件不认 `kind` 字段，会把 `pause` 当成一次普通采集
   （默认参数恰好合法），服务端以 `409 WRONG_KIND` 拒绝该上传（不写样本），任务最终 `failed`。
   先烧录再使用，详见 §八。
7. **暂停状态只存 RAM**：板端不持久化暂停标志，重启即恢复正常上报（"断电不该永久静音"）；
   服务端的 `device_control` 只用于展示与追溯，不反向控制板端。
8. **暂停粒度是"设备"而非"数据源"**：`pause` 针对 `device_id`，板端会停掉该板**所有**周期上报
   （当前板端只有 `qma6100p` 一路数据源，因此等价）。将来若一块板接多路传感器，
   需要把 `source` 也纳入板端暂停判断。

---

## 八、周期上报控制（暂停 / 恢复）

> 目标：Web 上点「暂停周期」就能让**板端**停掉 1 s/100 点的周期上报，到点自动恢复，
> 也可点「恢复周期」提前恢复。走的仍是既有的任务链路（`submitted → dispatched → acked →
> completed/failed/timeout`），没有另造一套控制通道。

### 8.1 为什么不能沿用采集任务的收尾方式

采集任务是「**上传即完成**」：板端把带 `request_id` 的批次 POST 上来，服务端在写库的同一事务里
把任务置 `completed`。暂停/恢复**不产生任何数据**，如果沿用这条路径，任务只会在有效期结束后变成
`timeout`——页面看到的永远是失败。

因此新增 **`POST /api/v1/tasks/{request_id}/applied`**：板端**确实切换了周期上报开关之后**调用它，
服务端在同一事务里：

1. `UPDATE tasks SET status='completed', completed_at=…`；
2. 写 `device_control`（暂停真相源：`periodic_paused` / `paused_until` / `request_id` / `updated_at`）。

两个方向都要防：`capture` 任务调 `/applied` → `409 WRONG_KIND`（否则会出现「任务 completed
但 0 样本」的假完成）；带控制任务 `request_id` 的 `POST /api/v1/upload` 同样 `409 WRONG_KIND`
（在 `_check_task_match()` 之前就拦下，不改任务状态、不写样本）。

### 8.2 服务端要点

| 主题 | 做法 |
|------|------|
| 去重键 | `device_id + source + kind`（**必须带 kind**，否则一条待执行的 `pause` 会把后续 `capture` 一直挡住） |
| 连点 | 同 kind 已有非终态任务 → `200 + duplicate=true` 复用，`duration_s` 不被改写 |
| 互斥 | 新建控制任务时把**反方向**那条未终态控制任务置 `failed: superseded by newer control task`（避免板端按 `created_at` 顺序来回抖动）；返回值带 `superseded` 计数 |
| 真相源 | `device_control` 表；`GET /api/v1/control`、`GET /api/v1/devices`（`periodic_paused`）都读它，页面不靠「periodic 批次数为 0」反推 |
| 到期 | **惰性归零**：`_lazy_reset_control()` 在每次读控制状态时把 `paused_until <= now` 的行置回未暂停（与 `_expire_stale_tasks()` 同一哲学，无后台线程）；归零后保留 `request_id` 便于追溯 |
| 板端上报终点 | `applied` 的可选 body `{"paused_until_ms": …}`：在 `[now-5s, now+duration_s+60s]` 内才采纳，使页面倒计时与板端自动恢复时刻一致；超出则回退 `now + duration_s` |
| 幂等重放 | 板端 POST 成功但响应丢失时会重试：任务已 `completed` 且控制行就是这条任务写的 → `updated=false, replay=true`，**不改写 `paused_until`**（否则重试会把暂停窗口越推越晚） |
| 迟到回执 | 任务已被判 `timeout`、但板端其实已生效时，`applied` **仍然**写 `device_control`（否则页面显示"上报中"而板端静默 120 s，与真相源矛盾）；但若已有更新的控制任务存在（`stale=true`）则不覆盖 |
| 历史兼容 | 老库用 `_migrate_tasks_columns()` 补 `kind`（默认 `'capture'`）/ `duration_s`；`_task_view()` 对 `kind` 缺失也按 `capture` 处理 |

`GET /api/v1/control` 的返回：`periodic_paused` / `paused_until`(+`_str`) / `remaining_s` /
`request_id` / `updated_at`(+`_str`)；缺 `device_id` 或超长返回 `400 INVALID`，
未知设备返回"未暂停且无控制任务"（不报错，页面首次加载即用得上）。

### 8.3 板端要点

| 主题 | 做法 |
|------|------|
| 状态 | `s_periodic_paused` + 双时钟终点（`s_pause_until_mono_ms` 主判据 / `s_pause_until_epoch_ms` 兜底），仅 RAM，重启即恢复上报 |
| 双时钟 | SNTP 未同步时 epoch 是 1970 起点，用它算差值会"立刻到点"；因此 `esp_timer` 单调钟是主判据，epoch 只在 `s_time_synced` 时才参与兜底（防单调钟被重启重置） |
| 到期 | `periodic_upload_paused()` 惰性判断：到点即清零标志并打一条 `[ctrl] pause window elapsed` 日志（只打一次） |
| 闸门 | sampler 每个采样点：`!manual_tick && !periodic_paused && s_wifi_connected` 才 `upload_accumulate()`；**采样、SD/CSV 落盘、LVGL 刷新完全不受影响**，手动采集照常可用 |
| 窗口连续性 | 暂停结束的那一拍 `s_upload_count = 0`，恢复后第一批仍是干净的 100 点（不把暂停前后的样本缝进同一批） |
| 应用 | `task_apply_control()` 在 `task_poll_task` 里切换标志（不在 sampler 里改 `s_upload_count`，避免绕过 `s_state_mutex`），随后 `POST ack` + `POST applied` |
| 回执重试 | `applied` **不是** best-effort：失败保留 `s_ctrl_pending_rid`，后续轮询重试 `TASK_APPLIED_RETRY_MAX` 次（`ack` 仍是 best-effort） |
| 坑 | **绝不停掉 `task_poll_task`**：它是唯一能领到 `resume` 任务的线程，停掉就再也恢复不了 |
| 上限 | `TASK_MAX_PAUSE_S` = 600 s（与服务端一致），`duration_s` 缺失/越界用 `TASK_PAUSE_FALLBACK_S` = 120 s 兜底 |

### 8.4 页面要点

- 控件：`暂停时长 s`（默认 120，本地校验 5–600）+ `暂停周期` + `恢复周期`。
- `createTask(kind)` 是三个按钮共用的唯一入口（`captureOnce()` / `pausePeriodic()` / `resumePeriodic()`），
  只有 `kind` 与收尾方式不同。
- 「周期上报控制」卡片读 `GET /api/v1/control`，控制任务走到终态时立刻回读；
  主轮询按 `SIDE_REFRESH_EVERY` 节流刷新。
- 任务卡片与任务历史按 `kind` 渲染：控制任务显示「暂停 120 s（到期自动恢复）」、
  `upload_id` 显示"控制任务不产生批次，靠板端 `/applied` 收尾"、历史多一列「类型」。
- 自动化钩子：`?autopause=1&pause=90`、`?autoresume=1`（见 5.3）。

### 8.5 端到端时序（一次暂停 + 提前恢复）

```
Web 点「暂停周期」→ POST /tasks {kind:pause, duration_s:120}   → tasks(kind=pause, submitted)
板端 ≤3 s 轮询 → GET /tasks/next                                → dispatched，返回 kind/duration_s
板端 task_apply_control()：置暂停标志 + 单调钟终点              → sampler 不再喂周期窗口
板端 POST /tasks/{id}/ack → acked（可选）
板端 POST /tasks/{id}/applied {paused_until_ms} → completed
服务端同事务写 device_control(periodic_paused=1, paused_until=…)  → GET /control 显示「停止中」
…… 期间服务端 0 条 periodic 批次；页面仍可正常「采集一次最新数据」……
点「恢复周期」→ POST /tasks {kind:resume} → 板端领取 → 清标志 → applied → completed
device_control(periodic_paused=0) → 周期批次重新出现
（若没人点恢复：板端到点自愈 + 服务端惰性归零，同样恢复）
```