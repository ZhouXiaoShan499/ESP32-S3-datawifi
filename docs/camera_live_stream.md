# 摄像头实时直播 + 本地 LCD 预览（长按 Button A 三态切换 + Web 触发）

> 对应 Web 监控页「摄像头实时画面」卡片 + 板端 **长按 Button A（2 s）** 三态切换：
> **关 → Web 直播 → 本地 LCD 预览 → 关**。
> 本地预览也可用 Web 下发 **`kind=preview`** 任务单独切换（页面「切换本地预览」按钮）。
> - **Web 直播**：把板端相机画面连续推到页面，得到 ~2-4 fps 的实时画面；
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
        live_stream_task 循环（每 ~250 ms 一轮）：
          app_camera_capture_jpeg_live()（持久会话上 DQBUF 一帧 JPEG 640×480，
                                          只在取帧期间持 s_camera_mutex）
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
                      └▶ 「摄像头实时画面」卡片（refreshLive()，独立节拍 + 每轮 poll() 各调一次）
```

| 环节 | 实现 | 关键点 |
|------|------|--------|
| 开关 | `button_a_long_press_cb()` → `camera_mode_toggle()` | **Button A 单击行为不变**（启停采集）；长按（2 s）在「关 → Web 直播 → 本地预览」之间循环，长按不会额外触发单击（见 `iot_button` 状态机） |
| 拍摄 | `app_camera_capture_jpeg_live()`（= `_ex(out, CAMERA_MODE_LIVE)`）：**持久会话**上 `DQBUF` → 拷贝 → 立刻 `QBUF` 归还 | 会话保持打开（不再每帧 `STREAMON/STREAMOFF`）、DQBUF 上限 800 ms、空闲 2 s 回收；与按需拍照同一套会话代码，只是模式不同。坏帧**不拆会话**（连续 `CAMERA_SOFT_FAIL_LIMIT(3)` 次才重建）+ `camera_resync_locked()` 非阻塞清一次驱动 done 列表残留 + ERROR 帧自校验兜底（`camera_jpeg_span()`，**2026-09-30 起直播/预览/拍照全模式启用**，拍照另加内容哈希新鲜度闸门 `camera_jpeg_hash()`）——机制见 `realtime_photo_capture.md` §三·补二 与 `camera_bad_frame_fix_work_log.md` |
| 串行化 | `s_camera_mutex`（`CAMERA_LOCK_TIMEOUT_MS` = 5 s） | `/dev/video2`（`BSP_CAMERA_DEVICE` = `ESP_VIDEO_DVP_DEVICE_NAME`）独占：只在**取帧**期间持锁，HTTP 上传不占锁，所以直播取帧与单帧拍照只是排队、不会互相饿死 |
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
| **JPEG 单帧循环（本项目采用）** | 复用按需拍照那条已跑通的 `open→DQBUF→拷贝→POST` 路径，没有新协议；代价是帧率 = 一次采集+上传的耗时 + `LIVE_FRAME_INTERVAL_MS`，实测约 2-4 fps |

取舍明确：这是「监控画面的实时感」，不是「流畅视频」。当前 `LIVE_FRAME_INTERVAL_MS` = 250 ms
（板端 `main.c`）：摄像头移动时页面要跟手，所以把它从 500 压到 250；再往下压会挤占 WiFi/采样带宽。

### 为什么服务端只留内存、不落盘

- 直播是连续流：2-4 fps × 每帧几十 KB，一天 ≈ 数 GB，落盘会立刻吃满磁盘；
- 逐帧入库还会让 `photos` 表与任务链路（幂等、`request_id`、删除）失去意义；
- 「最新一帧」对实时画面完全够用，丢帧也无所谓（下一帧 250 ms 后就到），
  于是刻意不做逐帧 ACK/重试/幂等 —— 这也是它与 `/api/v1/photos` 最本质的区别。

---

## 三、板端实现（`main/main.c`）

| 符号 | 作用 |
|------|------|
| `LIVE_API_PATH` / `LIVE_FRAME_INTERVAL_MS` / `LIVE_HTTP_TIMEOUT_MS` / `LIVE_FAIL_LIMIT` / `LIVE_TASK_STACK` / `LIVE_TASK_PRIORITY` | 直播参数（250 ms 节拍、8 s 单帧超时、连续 5 次失败自动停、8 KB 栈、优先级 2） |
| `s_live_streaming` / `s_live_frames` / `s_live_last_status` | 推流标志、已发送帧数（LCD 显示）、最近一帧 HTTP 状态 |
| `s_camera_mutex` | 相机串行化（在 `app_camera_init()` 里创建，`app_camera_capture_jpeg_ex()` 内获取/归还，成功与失败路径都归还） |
| `live_post_frame()` | 流式 POST 一帧到 `/api/v1/live`，返回是否 2xx，并回传状态码 |
| `live_stream_task()` | 推流循环；结束自删任务并 `refresh_ui()` |
| `live_streaming_toggle()` / `button_a_long_press_cb()` | 长按开关（只置位/清零 + 建任务） |

LCD 状态：状态栏显示 `… Live:ON/OFF`（240 px 宽的屏只留诊断信息：
`SD:OK 100Hz:OK Live:OFF`），第一行状态在推流时变为 `LIVE <n> fr`
（n = 已成功上传的帧数），也可用它确认板端侧推流是否在正常计数。

任务优先级刻意最低（2 < `task_poll` 3 < `uploader` 4 < `sampler` 5）：
直播是「锦上添花」的负载，网络突然变慢时先让位给采样与上传。

### 本地 LCD 预览（三态循环的第 3 态，也可由 Web `kind=preview` 触发）

- 触发：
  - 长按 Button A 依次 `关 → Web 直播 → 本地预览 → 关`（`camera_mode_toggle()`）；
  - Web 下发 `kind=preview` 任务单独 toggle（`task_handle_preview()` → 起/停 `s_preview_active`）。
- 实现：**传感器保持 JPEG 640×480，不做运行时格式切换**——esp_video 的 DVP 设备
  `VIDIOC_S_FMT` 会拒绝尺寸/格式变化，切换格式要私有 `VIDIOC_S_SENSOR_FMT` ioctl +
  传感器内部寄存器表，故不采用。预览复用 `app_camera_capture_jpeg_live()` 拍一帧 JPEG，
  用 `espressif/esp_jpeg` 软解到 RGB565（`JPEG_IMAGE_SCALE_1_2` → 320×240），中心裁剪
  成 240×240 后写进 LVGL `lv_canvas` 直接上屏。
- 符号：`CAMERA_PREVIEW_*`、`s_preview_active`、`camera_preview_decode()`、
  `camera_preview_task()`、`camera_preview_start()`、`task_handle_preview()`。
  帧缓冲都在 PSRAM（`240×240×2` + `320×240×2` ≈ 263 KiB）。
- 串行化：预览与拍照/直播共用 `s_camera_mutex`，每帧只持锁到 `DQBUF`+归还完成为止，
  按需拍照任务在预览期间最多等一帧，不会被饿死。相机会话由
  `camera_session_idle_close()`（`housekeeping_task`，空闲 `CAMERA_SESSION_IDLE_MS`=2 s）
  统一回收，所以停止预览/直播后 DVP 不会一直空转。
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
| `refreshLive()` | 由**独立节拍** `liveTickLoop()` 驱动（推流中 `LIVE_TICK_MS=250` ms、空闲退到 `LIVE_IDLE_TICK_MS=1` s；主 `poll()` 里也顺手调一次做设备切换/刷新的即时反应；内部 250 ms 最小间隔 + busy 保护，不 `await`）：`GET /api/v1/live` → `seq` 变了才刷新 `GET /api/v1/live/frame` 的 `<img src>` |
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
---

## 追加（2026-10-05）：帧尾被「上一帧残留的 EOI」截断 —— 直播画面下半幅灰带的根因

**症状**：Web 直播 / 本地预览的每一帧都是「顶部几十像素正常 → 中段一条条横向色带与错位小块
→ 底部 ~25 px 纯灰」，`server/photos/25.jpg` 也存着同样坏图。

**根因**（链路上有两环，缺一不可）：

1. **驱动侧**：本工程用的是 **ESP-IDF 5.4.3 自带**的 DVP 驱动
   （`components/esp_driver_cam/dvp/src/esp_cam_ctlr_dvp_cam.c`）。
   `managed_components/espressif__esp_cam_sensor/src/driver_dvp/esp_cam_ctlr_dvp_cam.c`
   被 `esp_cam_ctlr_dvp_ext.h` 的 `#if (ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5,5,2))`
   整个挡掉 —— 5.4.3 上 `ESP_CAM_CTLR_DVP_ENABLE` 未定义，该文件编译成空对象，
   `esp_cam_new_dvp_ctlr_ext()` 只是宏别名到 IDF 的实现。
   可核对：`build/config.env`（`IDF_VERSION=5.4.3`）与 `build/compile_commands.json`。
   该驱动把「一帧」的边界交给 VSYNC-EOF（`components/hal/cam_hal.c:67`：
   `cam_ll_enable_vsync_generate_eof(hw, 1)`），收到多少字节由
   `esp_cam_ctlr_dvp_dma_get_recv_size()`（描述符 `dw0.length` 求和）给出；**帧尾那截不在计数里**，
   于是 `esp_cam_ctlr_dvp_get_jpeg_size()` 在计数范围内往回扫不到 `FF D9` → 返回 0 →
   `trans.received_size = 0` →（5.4.3 的 `esp_cam_ctlr_recv_frame_done_isr()` **无条件**回调
   `on_trans_finished`）→ `esp_video_done_buffer(n = 0)` → `element->valid_size = 0` →
   `esp_video_ioctl.c:198` 填 `bytesused = 0` 并置 `V4L2_BUF_FLAG_ERROR`(0x41)。
   **⇒ 这就是历史上「`used=0/flags=0x41` 是常态」的真正机制**（不是 done 列表残留元素：
   5.4.3 这个驱动连 NO-EOI/NO-SOI 日志都没有，所以串口上完全看不出来）。
2. **app 侧**：`camera_jpeg_span()` 的自校验扫的是**整块 307200 B 映射缓冲**。缓冲是复用的
   （`CAMERA_BUFFER_COUNT(3)` 轮转），没被本帧 DMA 覆盖的部分**还是上一帧的数据**，
   里面就有上一帧的 EOI。于是自校验「找到」的其实是**旧帧结尾**，
   上传/落盘的是「本帧前缀 + 旧帧尾巴」⇒ 下半幅灰带 + 中段错位条带。
   旧帧比本帧短多少，就被切掉多少：本次抓到的直播坏帧样本约 5%，`25.jpg` 约 15%。

**修复**（`main/main.c`，只动 app）：`camera_wipe_map_locked()` —— **每次把 mmap 缓冲还给驱动
（QBUF）之前，把整块缓冲写 0**（缓冲在 PSRAM，必须 `esp_cache_msync()` C2M 同步，否则脏 cache
行会在 DMA 写完之后被回写、冲掉新帧）。调用点：session open 的首轮 QBUF、
`camera_resync_locked()`、`camera_grab_locked()` 的四条归还路径（暖机 / 坏帧 / OOM / 好帧）。
残留区从此不可能出现 `FF D9`，`camera_jpeg_span()` 只可能扫到**本帧 DMA 真写进去的 EOI**：
帧完整就用完整帧，真被截断就明确判为坏帧（丢掉重取），再也不会把旧帧尾巴拼上来。

**真机证据（2026-10-05）**：串口 `diag idx=.. driver_used=0 span=NNNN written=NNNN` 中
**span 与 written 完全相等**（9787/10543/10565）⇒ DMA 其实把整帧（含 EOI）都写进了缓冲，
只是驱动没算全；修复后 `kind=camera` 的新照片 `photos/26.jpg`（10519 B）离线熵解码
**2400/2400 MCU**，肉眼干净无撕裂、无灰底（对比坏的 `25.jpg`）。
预览会话连续 35 帧自校验长度 10533~10591 B（修复前同场景只有 ~9000 B = 旧 EOI 的位置）。

**遗留**：驱动仍报 `used=0/flags=0x41`（我们没改 IDF）。要彻底干净可升级到 **IDF ≥ 5.5.2**，
让 `esp_cam_ctlr_dvp_ext`（JPEG 专用：`cam_vs_eof_en = 0` + 按 `cam_rec_data_bytelen` 分块）
生效；届时 `camera_wipe_map_locked()` 仍建议保留作保险。

> 完整工作记录（离线取证脚本、逐步根因对账、复现/复跑命令、变更清单）见
> `docs/camera_frame_truncation_fix_work_log.md`。

- HTTP 层（无浏览器也可跑）：推帧 201 + seq → 状态 active/尺寸/字节数 → `frame` 原样取回
  → 第二帧 seq 递增且只保留最新一帧 → 错误路径（400/413/404）→ **不落盘不入库**
  （`photos` 总数为 0、照片目录为空）→ 超时后 `active=false` 但最后一帧仍可取。
- 页面断言（找到 Edge/Chrome 时）：无头 `--dump-dom` 检查 `<img src=/api/v1/live/frame…>`
  不被隐藏、「推流中 · 640×480」徽章、元信息含 seq/帧龄；等过阈值再 dump，
  徽章变「已停止（保留最后一帧）」且画面不消失。
- 自检用 `SENSOR_LIVE_TIMEOUT_S=1` 把超时阈值压到 1 s，避免测试等待 5 s。

---

## 七、已知限制

- 帧率受「一次 V4L2 采集 + 一帧 HTTP 上传」限制，实测约 2-4 fps；不是视频编码流。
- 本地 LCD 预览是「软解 JPEG」：640×480 解到 1/2 再中心裁剪，单帧解码约几十 ms，
  实测约 5-10 fps；预览会占用更多 CPU（优先级 2，仍低于采样/上传）。
- 服务端内存里每台设备常驻一帧（≤ 512 KiB/设备），多设备长时间运行不会增长，但
  重启服务端需等板端下一帧才会恢复画面。
- 直播期间 LCD 每帧刷新状态（2 Hz），比原来的刷新频率略高；若担心影响界面观感，
  可把 `refresh_ui()` 改成每 N 帧一次。
- 直播/预览的 `flags=0x41 / bytesused=0` 坏帧**在这块板 + OV2640 JPEG 配置下是常态**（2026-09-30
  两轮真机实测：每场会话的暖机帧就是 0 字节，全程 `NO-SOI`/`NO-EOI` 各 0 次；同一块映射缓冲里
  自校验每次都能取到完整 JPEG）。板端据此把「app 自扫 SOI/EOI」当作**主路径**而非兜底：
  全模式启用 `camera_jpeg_span()`（拍照另加内容哈希新鲜度闸门，防跨会话残留旧图），
  坏帧不拆会话（连续 3 次才重建）+ `camera_resync_locked()` 排空残留；
  直播/预览期间**不做空闲回收**（避免反复重分配 16 KiB 内部 DMA + 3×307200 B PSRAM）。
  机制与 A/B 实测见 `realtime_photo_capture.md` §三·补二 / §四 与
  `camera_bad_frame_fix_work_log.md`。
- 同一根因还会让驱动交出**野长度**（串口 `[cam] no heap for 4294959519 B JPEG frame`，
  即 `0xFFFFE19F` = −7777，不是真的 OOM）。板端已加**长度笼子**：`bytesused` 必须落在
  `(0, map_len]` 内才可能算「好帧」，之后一律用夹取后的长度去 `malloc`/`memcpy`，野长度
  降级成软失败（计数 `bogus_len=`、`stale=`，见 `session closed` 行）。
- 扬声器/MIC 与相机无关；直播不改变 `bsp_camera_start()` 的初始格式（仍是
  Kconfig 选的 OV2640 DVP JPEG 640×480）。
- Web 侧仍是「按 seq 轮询」的伪流（800 ms 主轮询节奏），不是 WebSocket/SSE；
  与项目现有「全部接口都是轮询」的架构保持一致。