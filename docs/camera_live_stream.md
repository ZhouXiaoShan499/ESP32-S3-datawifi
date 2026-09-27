# 摄像头实时直播 + 本地 LCD 预览（长按 Button A 三态切换）

> 对应 Web 监控页「摄像头实时画面」卡片 + 板端 **长按 Button A（2 s）** 三态切换：
> **关 → Web 直播 → 本地 LCD 预览 → 关**。
> - **Web 直播**：把板端相机画面连续推到页面，得到 ~1-2 fps 的实时画面；
> - **本地 LCD 预览**：板端把每帧 JPEG 软解成 RGB565，直接画在 240×240 屏上。
>
> 两者都在**不打断 IMU 采样、不暂停周期上报、不刷爆数据库/磁盘**的前提下进行。
> 相关代码：`main/main.c`（板端）、`server/main.py`（服务端）、`server/static/*`（页面）、
> `server/e2e_live_check.py`（自检）。

---

## 一、链路总览

```
板端：长按 Button A(2s) ──▶ button_a_long_press_cb → live_streaming_toggle()
                                   │ 置 s_live_streaming = true，建 live_stream_task
                                   ▼
        live_stream_task 循环（每 ~500 ms 一轮）：
          app_camera_capture_jpeg()（V4L2 单帧 JPEG 640×480，持 s_camera_mutex）
                      │
                      ▼
          POST /api/v1/live?device_id=…&ts_ms=…&w=…&h=…   （body = JPEG 字节）
                      │
                      ▼
服务端：内存字典 _live_frames[device_id] = 最新一帧（覆盖写，**不落盘、不入库**）
                      ▲
                      │ GET /api/v1/live?device_id=…        （active / seq / age_ms …）
浏览器 /ui/ ──────────┤
                      │ GET /api/v1/live/frame?device_id=…&seq=N （<img src>，no-store）
                      └▶ 「摄像头实时画面」卡片（refreshLive()，每轮 poll() 调一次）
```

| 环节 | 实现 | 关键点 |
|------|------|--------|
| 开关 | `button_a_long_press_cb()` → `camera_mode_toggle()` | **Button A 单击行为不变**（启停采集）；长按（2 s）在「关 → Web 直播 → 本地预览」之间循环，长按不会额外触发单击（见 `iot_button` 状态机） |
| 拍摄 | `app_camera_capture_jpeg()`：`G_FMT/S_FMT → REQBUFS/QUERYBUF/mmap/QBUF → STREAMON → DQBUF → STREAMOFF` | 与按需拍照同一函数，复用既有 V4L2 路径 |
| 串行化 | `s_camera_mutex`（`CAMERA_LOCK_TIMEOUT_MS` = 5 s） | `/dev/video0` 独占：直播取帧与单帧拍照排队，避免两次 `STREAMON` 互踩 |
| 上传 | `live_post_frame()`：`esp_http_client_open(len)` + `write` | `Content-Length` 定长发送；**没有 request_id、没有任务收尾** |
| 服务端 | `upload_live_frame()` → `_live_frames[device_id]` | 只保留最新一帧；`_live_lock` 保护；上限 `LIVE_MAX_FRAME_BYTES` = 512 KiB |
| 状态 | `live_status()`：`active = now - received_at <= LIVE_TIMEOUT_S`（默认 5 s） | 超时即判定「已停止推流」，页面显示徽章而不是报错 |
| 展示 | `refreshLive()` + `renderLiveOff()` + `<img id="liveImg">` | seq 变化才换 `src`（附 `&seq=&t=`），`Cache-Control: no-store` |
| 停止 | 再长按一次（进下一态）/ WiFi 掉线 / 连续失败 5 次 | 任务自己 `vTaskDelete(NULL)`，按键回调只置标志，不做 `vTaskDelete` |

---

## 二、为什么用「JPEG 单帧循环」而不是 MJPEG 连续流

| 方案 | 结果 |
|------|------|
| MJPEG 连续流（`multipart/x-mixed-replace`） | 需要在板端保持一路长连接 HTTP 服务或长连接上传；本项目的 HTTP 只有 client 角色（`esp_http_client`），要额外起 httpd 与端口，和「板端 → 服务端」单向架构相悖 |
| **JPEG 单帧循环（本项目采用）** | 复用按需拍照那条已跑通的 `open→DQBUF→拷贝→POST` 路径，没有新协议；代价是帧率 = 一次采集+上传的耗时 + `LIVE_FRAME_INTERVAL_MS`，实测约 1-2 fps |

取舍明确：这是「监控画面的实时感」，不是「流畅视频」。想提高帧率可调小
`LIVE_FRAME_INTERVAL_MS`（板端 `main.c`），但会挤占 WiFi/采样带宽。

### 为什么服务端只留内存、不落盘

- 直播是连续流：1-2 fps × 每帧几十 KB，一天 ≈ 数 GB，落盘会立刻吃满磁盘；
- 逐帧入库还会让 `photos` 表与任务链路（幂等、`request_id`、删除）失去意义；
- 「最新一帧」对实时画面完全够用，丢帧也无所谓（下一帧 500 ms 后就到），
  于是刻意不做逐帧 ACK/重试/幂等 —— 这也是它与 `/api/v1/photos` 最本质的区别。

---

## 三、板端实现（`main/main.c`）

| 符号 | 作用 |
|------|------|
| `LIVE_API_PATH` / `LIVE_FRAME_INTERVAL_MS` / `LIVE_HTTP_TIMEOUT_MS` / `LIVE_FAIL_LIMIT` / `LIVE_TASK_STACK` / `LIVE_TASK_PRIORITY` | 直播参数（500 ms 节拍、8 s 单帧超时、连续 5 次失败自动停、8 KB 栈、优先级 2） |
| `s_live_streaming` / `s_live_frames` / `s_live_last_status` | 推流标志、已发送帧数（LCD 显示）、最近一帧 HTTP 状态 |
| `s_camera_mutex` | 相机串行化（在 `app_camera_init()` 里创建，`app_camera_capture_jpeg()` 内获取/归还） |
| `live_post_frame()` | 流式 POST 一帧到 `/api/v1/live`，返回是否 2xx，并回传状态码 |
| `live_stream_task()` | 推流循环；结束自删任务并 `refresh_ui()` |
| `live_streaming_toggle()` / `button_a_long_press_cb()` | 长按开关（只置位/清零 + 建任务） |

LCD 状态：状态栏显示 `… Live:ON/OFF`（240 px 宽的屏只留诊断信息：
`SD:OK 100Hz:OK Live:OFF`），第一行状态在推流时变为 `LIVE <n> fr`
（n = 已成功上传的帧数），也可用它确认板端侧推流是否在正常计数。

任务优先级刻意最低（2 < `task_poll` 3 < `uploader` 4 < `sampler` 5）：
直播是「锦上添花」的负载，网络突然变慢时先让位给采样与上传。

### 本地 LCD 预览（三态循环的第 3 态）

- 触发：长按 Button A 依次 `关 → Web 直播 → 本地预览 → 关`（`camera_mode_toggle()`）。
- 实现：**传感器保持 JPEG 640×480，不做运行时格式切换**——esp_video 的 DVP 设备
  `VIDIOC_S_FMT` 会拒绝尺寸/格式变化，切换格式要私有 `VIDIOC_S_SENSOR_FMT` ioctl +
  传感器内部寄存器表，故不采用。预览复用 `app_camera_capture_jpeg()` 拍一帧 JPEG，
  用 `espressif/esp_jpeg` 软解到 RGB565（`JPEG_IMAGE_SCALE_1_2` → 320×240），中心裁剪
  成 240×240 后写进 LVGL `lv_canvas` 直接上屏。
- 符号：`CAMERA_PREVIEW_*`、`s_preview_active`、`camera_preview_decode()`、
  `camera_preview_task()`、`camera_preview_start()`。帧缓冲都在 PSRAM
  （`240×240×2` + `320×240×2` ≈ 263 KiB）。
- 串行化：预览与拍照/直播共用 `s_camera_mutex`，每帧只持锁到 `DQBUF` 完成即还，
  按需拍照任务在预览期间最多等一帧，不会被饿死。
- 依赖：`main/idf_component.yml` 新增 `espressif/esp_jpeg`（软件 JPEG 解码；ESP32-S3
  没有硬件 JPEG 编解码器）。
- LCD 状态：预览期间第一行状态显示 `PREVIEW  NET:<ip>`。

---

## 四、服务端接口（`server/main.py`）

| 接口 | 说明 |
|------|------|
| `POST /api/v1/live?device_id=…&ts_ms=…&w=…&h=…` | 板端推一帧（body = JPEG 字节）；201 + 全局单调 `seq`；可选 Bearer Token（`_device_auth_ok`） |
| `GET /api/v1/live?device_id=…` | 状态：`active` / `seq` / `age_ms` / 分辨率 / 字节数 / `ts_ms_str` / `received_at_str` / `devices[]`；**从未推流的设备也返回 200 + active=false**（页面据此显示「未推流」而不是接口错误） |
| `GET /api/v1/live/frame?device_id=…` | 最新一帧 JPEG（`Cache-Control: no-store`）；无帧 → 404 `NO_FRAME` |

校验：`device_id` 必填且 ≤ `MAX_DEVICE_ID_LEN`；body 空 → 400；非 JPEG（首字节不是
`FF D8 FF`）→ 400；`ts_ms`/`w`/`h` 非法 → 400；超大帧（> `LIVE_MAX_FRAME_BYTES`，
默认 512 KiB）→ 413。

内存态结构（`server/main.py`）：

```python
LIVE_TIMEOUT_S = float(os.environ.get("SENSOR_LIVE_TIMEOUT_S", "5"))  # 无新帧阈值
LIVE_MAX_FRAME_BYTES = MAX_PHOTO_BYTES
_live_lock = threading.Lock()
_live_frames = {}      # device_id -> {jpeg, width, height, ts_ms, received_at, seq, ip}
_live_seq = 0          # 全局单调序号（页面用它判断「有新帧了」）
```

多设备共用一张字典：每台设备各自保留自己的最新一帧，页面按选中设备取图；
服务端重启后内存清空是很自然的（画面回到「未推流」），不需要任何持久化迁移。

---

## 五、页面（`server/static/index.html` + `app.js`）

| 元素 / 函数 | 说明 |
|-------------|------|
| 卡片「摄像头实时画面」 | 大图（4:3，`object-fit: contain`）+ 徽章 + 元信息（seq / 分辨率 / 单帧大小 / 帧龄 / 板端拍摄时刻 / 来源 IP） |
| `refreshLive()` | 在每轮 800 ms `poll()` 里调用（不 `await`，内部 500 ms 最小间隔 + busy 保护）：`GET /api/v1/live` → `seq` 变了才刷新 `GET /api/v1/live/frame` 的 `<img src>` |
| `renderLiveOff()` | 未推流 / 查询失败 / 无帧时的统一出口：占位提示 + 隐藏 `<img>` |
| `liveMetaText()` | 状态 → 多行元信息（CSS `white-space: pre-line`） |
| 未选设备时 | 自动回退到「正在推流的那台」设备（板端可能还没上传过 IMU 数据，下拉里暂时没有它） |

徽章状态：`推流中 · 640×480`（active）/ `已停止（保留最后一帧）`（超时但内存里还有帧）/
`未推流`（该设备从未推过流）。

---

## 六、自检

```
python server/e2e_live_check.py
```

- HTTP 层（无浏览器也可跑）：推帧 201 + seq → 状态 active/尺寸/字节数 → `frame` 原样取回
  → 第二帧 seq 递增且只保留最新一帧 → 错误路径（400/413/404）→ **不落盘不入库**
  （`photos` 总数为 0、照片目录为空）→ 超时后 `active=false` 但最后一帧仍可取。
- 页面断言（找到 Edge/Chrome 时）：无头 `--dump-dom` 检查 `<img src=/api/v1/live/frame…>`
  不被隐藏、「推流中 · 640×480」徽章、元信息含 seq/帧龄；等过阈值再 dump，
  徽章变「已停止（保留最后一帧）」且画面不消失。
- 自检用 `SENSOR_LIVE_TIMEOUT_S=1` 把超时阈值压到 1 s，避免测试等待 5 s。

---

## 七、已知限制

- 帧率受「一次 V4L2 采集 + 一帧 HTTP 上传」限制，实测约 1-2 fps；不是视频编码流。
- 本地 LCD 预览是「软解 JPEG」：640×480 解到 1/2 再中心裁剪，单帧解码约几十 ms，
  实测约 5-10 fps；预览会占用更多 CPU（优先级 2，仍低于采样/上传）。
- 服务端内存里每台设备常驻一帧（≤ 512 KiB/设备），多设备长时间运行不会增长，但
  重启服务端需等板端下一帧才会恢复画面。
- 直播期间 LCD 每帧刷新状态（2 Hz），比原来的刷新频率略高；若担心影响界面观感，
  可把 `refresh_ui()` 改成每 N 帧一次。
- 扬声器/MIC 与相机无关；直播不改变 `bsp_camera_start()` 的初始格式（仍是
  Kconfig 选的 OV2640 DVP JPEG 640×480）。
- Web 侧仍是「按 seq 轮询」的伪流（800 ms 主轮询节奏），不是 WebSocket/SSE；
  与项目现有「全部接口都是轮询」的架构保持一致。