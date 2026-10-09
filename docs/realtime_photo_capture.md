# 实时拍照（单帧 JPEG 上传 + 画廊/删除）

> 对应 Web 监控页「实时拍照」卡片上的「拍一张照片」按钮。
> 目标：在**不打断 IMU 采样、不暂停周期上报**的前提下，让板端按需拍一帧照片并回传、
> 服务端落盘入库、页面能看能删。
> 相关代码：`main/main.c`（板端）、`server/main.py`（服务端）、`server/static/*`（页面）、
> `server/test_photos.py` / `server/e2e_ui_check.py`（自检）。

---

## 一、链路总览

```
浏览器 /ui/ ── POST /api/v1/tasks {kind:"camera"} ──▶ 服务端 tasks 表（submitted）
   ▲                                                     │
   │ 1 s 轮询 GET /api/v1/tasks/{rid}                     │ 板端每 3 s GET /api/v1/tasks/next
   │                                                     ▼
   │                                            task_poll_task 领到 kind=camera
   │                                                     │
   │                                        POST /tasks/{rid}/ack（尽力而为）
   │                                                     │
   │                              app_camera_capture_jpeg()：V4L2 单帧 JPEG（640×480）
   │                                                     │
   │              POST /api/v1/photos?device_id=…&request_id=…&w=&h=&ts_ms=（body=JPEG 字节）
   │                                                     ▼
   └── 画廊 GET /api/v1/photos?device_id=…&limit=12 ◀── 同一事务：落盘 + photos 表 + 任务 completed
```

| 环节 | 实现 | 关键点 |
|------|------|--------|
| 触发 | 页面「拍一张照片」→ `POST /api/v1/tasks {kind:"camera"}` | 复用既有任务链路与状态机，不新增控制通道 |
| 派发 | 板端 `task_poll_task` 每 3 s `GET /api/v1/tasks/next` | `kind=camera` 在 `task_handle_next_response()` 里**先于**样本参数校验分流 |
| 拍摄 | `app_camera_capture_jpeg()`（= `_ex(out, CAMERA_MODE_PHOTO)`）：**持久会话**上 `DQBUF` → 拷贝 → 立刻 `QBUF` 归还 | 会话（`open`/`REQBUFS`/`STREAMON`）按需建立、空闲 2 s 回收；不再每帧 `STREAMON/STREAMOFF`（原因见第三节末的竞态说明） |
| 上传 | `photo_post_frame()`：`esp_http_client_open(len)` + `write` 流式发送 | 用 `Content-Length` 定长发送，不复制整帧到发送缓冲 |
| 入库 | `store_photo()`：写 `photos` 行 + 原子落盘 + 任务收尾 | 三件事同一事务；`request_id` 幂等 |
| 展示 | `refreshGallery()` + `photoCard()` | 缩略图直接 `GET /api/v1/photos/{id}`，`<img>` 无二次编码 |
| 删除 | `DELETE /api/v1/photos/{id}` | 先删库行再删文件；`file_deleted` 明确回报 |

---

## 二、为什么必须先把 IMU 从 legacy I2C 迁到 i2c_master

这是本功能最硬的一条约束，**不迁移则固件无法启动**：

1. IMU 原来用 `espressif/qma6100p` 组件，它 `#include "driver/i2c.h"`（legacy 驱动），
   在 `main.c` 里用 `i2c_param_config()` + `i2c_driver_install()` 自己装了 legacy 驱动。
2. 相机 SCCB 与 `esp_video` 只支持新驱动 `driver/i2c_master.h`
   （`esp_video_init.h` 里 `sccb_config.i2c_handle` 的类型就是 `i2c_master_bus_handle_t`），
   BSP 的 `bsp_camera_start()` 内部调用 `bsp_i2c_init()`（= `i2c_new_master_bus`）。
3. esp-idf 的 legacy 驱动里有一个启动期构造器
   （`components/driver/i2c/i2c.c` 的 `check_i2c_driver_conflict()`）：只要**新驱动被链接进镜像**，
   它就会 `ESP_EARLY_LOGE("CONFLICT! driver_ng is not allowed to be used with this old driver")`
   并直接 `abort()`。当前固件之所以能启动，只是因为新驱动还没被任何代码引用（弱符号为 NULL）。
4. 两者还都想用端口 1 / GPIO4/5，同时使用会破坏寄存器状态。

处理方式：

- **`main.c` 内置 QMA6100P 驱动**：`app_accel_init()` 改为 `bsp_i2c_init()` +
  `i2c_master_bus_add_device(0x12 / 0x13)`，读写用 `i2c_master_transmit()` /
  `i2c_master_transmit_receive()`。寄存器序列与 `qma6100p` 组件 1:1 对齐
  （`WHO_AM_I(0x00)==0x90` → `PWR_MGMT_1(0x11)|=BIT7` → `ACCEL_CONFIG(0x0F)=±2g`；
  读数 `raw = int16/4`、`/4096` → 单位 g，采样器再乘 9.80665 转 m/s²），
  所以改造前后**上报数值口径完全一致**。
- `main/idf_component.yml` 移除 `espressif/qma6100p` 依赖（不再需要，也不再引入 legacy 驱动代码）。
- `sdkconfig.defaults` 显式设置 `CONFIG_I2C_SKIP_LEGACY_CONFLICT_CHECK=y`：即便某个组件
  仍引用 legacy 驱动符号，也不会在启动期被这个检查 `abort()`；因为**运行时不再由 legacy
  驱动操作任何端口**，跳过检查是安全的（该选项在 esp-idf 里的用途正是这种迁移过渡场景）。
- `main/CMakeLists.txt` 增加 `esp_video` / `esp_cam_sensor`（`linux/videodev2.h`、
  `esp_video_device.h`、`esp_video_ioctl.h` 都由 `esp_video` 组件导出）。

> 副作用：`bsp_i2c_init()` 是幂等的，`app_accel_init()`（步骤 5）先建总线、
> `app_camera_init()`（步骤 5b）随后复用同一总线，两者按固定顺序调用。

---

## 三、相机相关的 Kconfig（写进 `sdkconfig.defaults`）

`sdkconfig` 被 gitignore，所以相机传感器的选择必须落在 `sdkconfig.defaults` 里才有可复现性：

| 配置项 | 值 | 原因 |
|--------|-----|------|
| `CONFIG_CAMERA_OV2640` | `y` | 不开启时 `esp_video_init()` 找不到传感器，运行期报 “sensor not found”，每次拍照都失败 |
| `CONFIG_CAMERA_OV2640_AUTO_DETECT_DVP_INTERFACE_SENSOR` | `y` | 让 `esp_video_init()` 自动探测 DVP 传感器，无需应用层显式探测 |
| `CONFIG_CAMERA_OV2640_DVP_JPEG_640X480_25FPS` | `y` | 只启用 JPEG VGA 这一格式；板端**只透传 JPEG**，不做编码/解码 |
| `CONFIG_CAMERA_OV2640_DVP_DEFAULT_FMT_JPEG_640X480_25FPS` | `y` | 设为传感器默认格式（见下一条约束） |
| `CONFIG_ESP_VIDEO_ENABLE_DVP_VIDEO_DEVICE` | `y` | 启用 DVP 视频设备 `/dev/video2`（BSP 的 `BSP_CAMERA_DEVICE`） |
| `CONFIG_ESP_VIDEO_CHECK_PARAMETERS` | `y` | esp_video 入参自检 |

**为什么必须先读格式再写回**：`esp_video_dvp_device.c` 的 `dvp_video_set_format()` 会校验
`pix->width/height` 与当前传感器格式**完全一致**，不一致直接 `ESP_ERR_INVALID_ARG`。
所以板端的做法是 `VIDIOC_G_FMT` 读当前格式 → 断言 `pixelformat == V4L2_PIX_FMT_JPEG`
（不是 JPEG 就给出 “enable CONFIG_CAMERA_OV2640_DVP_JPEG_640X480_25FPS” 的明确日志）
→ 用同样的宽高做 `VIDIOC_S_FMT`。

> **XCLK 必须 20 MHz（真机已验证，2026-09-27 修正）**：BSP 的 `bsp_camera_start()` 把 XCLK
> 固定成 `BSP_CAMERA_XCLK_CLOCK_MHZ`(16 MHz)，但 OV2640 的**全部** DVP 格式表都叫
> `DVP_8bit_20Minput_*`（`esp_cam_sensor/sensors/ov2640/ov2640.c`：`.xclk = 20000000`），
> 是按 20 MHz 输入校准的寄存器序列。喂 16 MHz 时传感器内部 PLL 与格式表不匹配，
> **JPEG 帧会退化成 0 字节**：`DQBUF` 返回
> `flags = V4L2_BUF_FLAG_MAPPED | V4L2_BUF_FLAG_ERROR`(0x41)、`bytesused = 0`
> → `dvp_calculate_jpeg_size()` 找不到 SOI/EOI → `valid_size = 0`。
> 于是「拍一张照片」与长按 Button A 的实时直播**同时失败**（直播连败 5 次后还会自动停）。
>
> 本文档此前写的「16 MHz 只打一条 warning、仍能出图」是**错的**：`esp_video_init()` 那条
> `Configured xclk frequency ... the sensor output image may be unexpected`
> （`esp_video_init.c:330`）描述的正是这个后果，不是无害提示。
>
> 因此板端 `app_camera_init()` **不再调用** `bsp_camera_start()`，而是复刻它的初始化流程、
> 只把频率换成 20 MHz：`esp_cam_sensor_xclk_start()`（LEDC 在 `BSP_CAMERA_GPIO_XCLK`/IO15 上
> 输出）+ `esp_video_init()` 的 `.xclk_freq`，**两处都必须是 20 MHz**——后者会被
> `esp_video_init()` 内部拿去调 `esp_cam_ctlr_dvp_output_clock()` 设 DVP 分频
> （`esp_video_init.c:515-520`）。该分频必须整除，而
> `CAM_CLK_SRC_DEFAULT = SOC_MOD_CLK_PLL_D2 = 80 MHz` → `80 / 20 = 4` ✓
> （旧值 `80 / 16 = 5` 也能整除，所以当时相机**能**出帧、只是帧本身是坏的——这也是该缺陷
> 一开始不易看出的原因）。
>
> **补充（2026-09-29）：`valid_size = 0`（`flags = 0x41`）不能由 `dvp_calculate_jpeg_size()`
> 直接产生** —— DVP 控制器那条路径在 JPEG 校验失败时只是把 `trans->received_size` 清零、
> 重启 DMA 重收，**不会**回调 `on_trans_finished`（`esp_cam_ctlr_dvp_cam.c:758-805`：
> `if (trans->received_size) { … on_trans_finished … }`），所以驱动不会把 0 字节元素交给 V4L2。
> 真正能把 `valid_size = 0` 交到 `DQBUF` 手里的只有 `esp_video_buffer_reset()`
> （`esp_video.c:150-155`），而它只在 `VIDIOC_STREAMOFF` 里被调用。
>
> **再次修正（2026-09-30 真机实测，本机脚本 `serial_round.py`；按 `.gitignore` 不进交付物）**：上面这条「只能是 STREAMOFF
> 的残留」**不完整**。一轮 250 s（8 个 `kind=camera` + 90 s `kind=preview`）的串口里，
> **每次会话的头两帧暖机帧就已经是 `flags=0x41 used=0`**、`owner=y`（元素属于本会话），
> 且全程 **0 次 `NO-SOI` / `NO-EOI` / `JPEG size` / `RX-DA OVF`**（DVP 控制器自己的报错一条没出）。
> 也就是：驱动**正常**（`dvp_calculate_jpeg_size()` 没失败）、数据也**正常**
> （同一块映射缓冲自校验能找到 8927/8893/…/9028 B 的完整 JPEG），但 `DQBUF` 交回的
> 元素就是 0 字节。⇒ `used=0` 在这块板 + 这套 OV2640 JPEG 配置下是**常态**，
> 不是偶发残留：`done_list` 里排着 `valid_size` 没被写过的元素（`esp_video_skip_buffer()`
> 那类语义），而真正装了这一帧的元素在另一条边上 —— app 读的是自己的 `map[idx]`，
> 所以照样能读到刚写进去的新帧。
>
> 直接后果：**「拍照模式不做兜底」= 拍照 100% 失败**（同轮 8/8 个任务全部
> `no usable frame in 11 attempts` → `session closed: frames=0 bad=11`），
> 而直播/预览因为 C 层兜底一直正常（205 帧全部救回，~4 fps）。
> 修正见下面 C 行。

---

### 三·补、相机改「持久会话」：`0x41` 坏帧的第二个来源（2026-09-29 修正）

`esp_video_stop_capture()`（= `VIDIOC_STREAMOFF`）的顺序是（`esp_video.c:668-703`）：

```
video->ops->stop()                     // 停 DVP
→ 清空 stream->ready_sem
→ TAILQ_INIT(&stream->done_list)       // 丢掉所有「已完成」元素
→ esp_video_buffer_reset(stream->buffer)   // 元素标回 free 且 valid_size = 0
```

DVP 控制器在 `STREAMON` 之后是按 VSYNC **连续**采样的。我们 `DQBUF` 到第 1 帧时，
硬件已经在往第 2 个缓冲里写第 2 帧；此时我们 `STREAMOFF`。如果第 2 帧的完成回调
（`dvp_video_on_trans_finished` → `esp_video_done_buffer` → `esp_video_done_element`，
注意 `done_element` **只要求元素是 free**）赶在 `esp_video_buffer_reset()` **之后**落地，
这个元素就会被塞进**下一次会话**的 `done_list`，带着上一轮的、或已被清零的 `valid_size`。
于是下一次取帧可能拿到：

- 上一轮的旧帧（内容错，但看着「成功」——最坏的情况，静默错数据）；
- 或被 reset 清零的元素 → `DQBUF` 返回 `flags = MAPPED|ERROR(0x41)`、`bytesused = 0`，
  就是串口上那条 `[photo] dropping bad frame flags=0x41`。

「每帧都 `open→STREAMON→DQBUF→STREAMOFF→close`」等于把这枚骰子每帧掷一次 ——
在直播（250 ms 一次）与本地预览（50 ms 一次）下必然随机命中。

**改法（板端已实施）**：

| 改动 | 值 / 位置 | 作用 |
|------|-----------|------|
| 持久会话 | `camera_session_open_locked()` / `camera_grab_locked()` / `camera_session_close_locked()` | 一个会话只 `open`/`REQBUFS`/`STREAMON` 一次，之后只 `DQBUF`+`QBUF`，从根上消掉上面那条竞态 |
| 空闲回收 | `CAMERA_SESSION_IDLE_MS = 2000`，`housekeeping_task` → `camera_session_idle_close()` | 没人用相机时把 DVP + mmap + fd 放掉（会话开着时传感器一直满速出图）。**2026-09-30 起：`s_live_streaming` / `s_preview_active` 期间不回收** —— 一轮实测直播/预览开着时 `dma_largest` 从 10240 掉到 4096 B，而每次 open/close 都要重来一遍 16 KiB 内部 DMA + 3×307200 B PSRAM 分配，正是 SD 写 `errno=5` 的前置条件 |
| 缓冲数 | `CAMERA_BUFFER_COUNT` 2 → 3 | 消费端（HTTP 上传 / JPEG 软解）慢一拍也不会饿死 DVP |
| 归还顺序 | 好帧也「先拷贝、再立刻 `QBUF`」 | 驱动 queued 队列不断供，避免下一帧从半帧开始 |
| 暖机 | `CAMERA_WARMUP_FRAMES = 2` | 新会话头两帧无条件丢弃（并统计白丢了多少好帧） |
| 分用途超时 | `CAMERA_DQBUF_TIMEOUT_MS(3000)` / `CAMERA_DQBUF_TIMEOUT_LIVE_MS(800)` | 拍照宁等不失败；直播/预览坏帧快速失败重试 |
| 诊断日志 | `[cam] session open/closed`、`[cam] first frame`（STREAMON→首帧等待时间 + SOI 判定）、`[cam] dropping bad frame`（`flags`/`bytesused`/缓冲头 4 字节） | 「是不是相机问题」不再靠猜；`main.c` 里对应 `camera_log_frame_diag()` |

锁的粒度不变：`s_camera_mutex` 只在**取帧**期间持有，HTTP 上传不占锁，
所以直播与按需拍照照旧互不长时间阻塞（拍照最多等一帧）。

### 三·补二、`bytesused=0 / flags=0x41` 的第三个来源：跨会话的 done 列表残留（2026-09-29）

现场症状（真机，直播/预览跑一会儿之后）：串口稳定刷、且**永不恢复** ——

```
[cam] dropping bad frame idx=0 flags=0x41 used=0 wait=…ms first4=FF D8 FF E0
[cam] no usable frame in 11 attempts (bad=11)
[cam] session closed: frames=… resyncs=0 salvaged=0
[cam] session open: …          <- 立刻重建，下一轮照旧
```

也就是「每轮都失败、每轮都重建会话」，加长暖机 / 加大跳帧上限都治不了。

机制（全部可在 `managed_components` 里核对）：

1. `V4L2_BUF_FLAG_ERROR` 只有一个来源（`esp_video_ioctl.c:196-203`）：
   `vbuf->bytesused = element->valid_size; if (!bytesused) flags |= ERROR;`
   ⇒ `bytesused=0` 等价于「驱动认为这个元素的 `valid_size` 是 0」。
2. `valid_size` 的写点只有两处：`esp_video_done_buffer()`（控制器回调给的 n；DVP 路径
   `n>0` 才回调，见 `esp_cam_ctlr_dvp_cam.c:776`）与 `esp_video_buffer_reset()`（写 0，
   **只在 `VIDIOC_STREAMOFF` 里调用**）⇒ 0 字节元素不可能来自「DVP 正常收完一帧」。
3. **元素对象本身可能不是本会话的**：`esp_video_setup_buffer()`（= `VIDIOC_REQBUFS`）
   会 `esp_video_buffer_destroy()` 掉旧缓冲对象再重建（`esp_video.c:861-878`），但
   **既不重新初始化挂在 stream 上的 `queued_list` / `done_list`，也不清引用**；而
   `esp_video_done_element()` 只要求元素 `free == true`（`esp_video.c:1015`）。
   DVP 任务在 `STREAMOFF`/`close` 之后仍可能让一次完成回调落地（`vTaskDelete` 是异步的）
   ⇒ 旧缓冲对象的元素被塞进**新会话**的 `done_list`，`DQBUF` 从表头取到它，`valid_size`
   是那块内存里残留的 0 → `flags=0x41`、`bytesused=0`。这也解释了「0 字节却能看到
   `FF D8 FF E0`」：`buf.index` 与缓冲内容都还是上一帧的残留值。
4. 旧代码**一失败就 `camera_session_close_locked()`**（`STREAMOFF` + `close`）——
   恰恰就是制造上面那条残留的动作，于是「一次偶发坏帧 → 自持的 0 字节帧循环」。

**改法（板端已实施；全部在 app 侧，不动 `managed_components`，组件升级不会被冲掉）**：

| 层 | 改动 | 位置 / 值 | 作用 |
|----|------|-----------|------|
| A | 软失败不拆会话 | `CAMERA_SOFT_FAIL_LIMIT = 3`，`app_camera_capture_jpeg_ex()` | `camera_grab_locked()` 用 `ESP_ERR_NOT_FOUND` 区分「这一轮没取到可用帧、**会话仍可用**」；连续 3 次才重建。硬失败（DQBUF/QBUF ioctl 错、没内存）仍立即重建 |
| B | done 列表重同步 | `camera_resync_locked()`，`CAMERA_RESYNC_POLL_MS = 0`、`CAMERA_RESYNC_MAX_FRAMES = 4` | 每次 grab 遇到第一个坏帧就非阻塞排空驱动 `done_list`（`VIDIOC_S_DQBUF_TIMEOUT` 传 0 时 `ticks = 0`，是真正的 0 超时轮询，不会退化成 `portMAX_DELAY`），每个元素**立刻** `QBUF` 归还；`index` 越界的残留元素直接丢掉 |
| C | ERROR 帧自校验兜底 | `camera_jpeg_span()`，`CAMERA_JPEG_MIN_BYTES = 1024`，**所有模式**（2026-09-30 起含拍照） | 驱动报 ERROR 但映射缓冲里确实躺着一整帧 JPEG（SOI 在偏移 0 + 找得到 EOI + 长度 ≥ 1 KiB）时收下并打 `salvaged bad frame`（拍照那次带 ` (photo)` 后缀）。拍照模式额外过一道**新鲜度闸门**：`camera_jpeg_hash()`（FNV-1a）与上一次成功拍照逐字节相同 ⇒ 判为跨会话残留旧图，丢掉再取下一帧（计数 `s_cam.dup_drops`，日志 `dup=`）。「拍照不做兜底」是 2026-09-29 的旧决定，2026-09-30 实测证明它等于拍照 100% 失败，故撤销 |
| D | 诊断 | `s_cam.resyncs` / `s_cam.salvaged`，并进 `session closed` 行 | 出问题时一眼看出「残留元素被清掉几次」「救回几帧」，不用靠猜 |
| D1 | **长度笼子**（2026-09-30 加） | `camera_grab_locked()` 里 `len_ok = bytesused ∈ (0, map_len[idx]]`；之后 `heap_caps_malloc` / `memcpy` / `out->len` 一律用夹取后的 `used` | 同一个残留根因漏出来的第二种表现：驱动把**野长度**（实例 `0xFFFFE19F` = 4294959519 = **−7777**）填进 `bytesused`。旧判据只有 `bytesused > 0` ⇒ 野值成了「好帧」⇒ 申请 4 GB 失败被误报成 OOM ⇒ 硬失败拆会话 ⇒ **又制造残留**。现在越界长度不可能进 malloc/memcpy，也不再有 4 GB 的越界读 |
| D2 | 归属诊断（2026-09-30 加） | 帧诊断日志里的 `owner=` / `userptr=`；计数 `s_cam.stale_frames`（`stale=`） | MMAP 模式下驱动会回填 `userptr = element->buffer`（`esp_video_ioctl.c:204-207`），而 `mmap()` 给 app 的正是同一指针（`esp_video_mman.c` → `esp_video_get_element_index_payload()`）⇒ `userptr == map[idx]` 即「元素属于本会话」。地址可能被复用（会误判），所以这条**只记账+打日志**，拦野值的主力是 D1 |
| D3 | 野长度降级（2026-09-30 加） | 计数 `s_cam.bogus_len_frames`（`bogus_len=`） | 野长度不再冒充 OOM：直播先走 C 层自校验兜底（扫描严格限制在 `map_len` 内），兜不住就当普通坏帧丢回队列 + `resync` 一次；`ESP_ERR_NO_MEM` 只留给「长度合法但堆真不够」的真 OOM |

野长度（`no heap for 4294959519 B`）的机制补充：

- 数值读法：`4294959519 = 0xFFFFE19F = −7777`（int32 补码），**不是内存不足**，是 `DQBUF`
  交回来的 `bytesused` 是野值。
- 野值怎么进 `bytesused`：DVP 侧 `trans->buflen = ELEMENT_SIZE(element) =
  element->video_buffer->info.size`（`esp_video_buffer.h:22`、`esp_video_dvp_device.c:158`）。
  元素若来自**已销毁的旧缓冲对象**（上面第 3 条），`video_buffer` 指向的内存已被释放/复用
  ⇒ `buflen` 变野值 ⇒ `esp_cam_ctlr_dvp_cam.c:754-758` 里所有以 `buflen` 为界的夹取全部失效
  ⇒ `dvp_calculate_jpeg_size()` 会**从远超 307200 的偏移**往回扫 EOI，扫到什么算什么，
  最后经 `element->valid_size = n`（`esp_video.c:1063`）交给 `DQBUF`。
- 所以「0 字节坏帧」和「4 GB 野长度」是**同一个根因的两种表现**：驱动 `done_list` 里
  混着上一代缓冲对象的元素。

自愈在日志里的样子：

```
[cam] resync(bad-frame): drained and returned 2 element(s)
[cam] frame idx=1 flags=0x00000043 used=8192 cap=307200 owner=y userptr=0x3c0a1234 wait=42ms first4=FF D8 FF E0
[cam] salvaged bad frame idx=1: driver used=0 cap=307200 owner=y flags=0x41, self-checked 24576 B
[cam] session closed: frames=57 bad=2 warmup_dropped_good=1 resyncs=3 salvaged=2 stale=0 bogus_len=1 dup=0 uptime=…ms
```

判读：`owner=N` 或 `bogus_len` 增长 = 板上确实在发生跨会话残留（app 已兜住，不会拆会话）；
两者都是 0 而 `bad` 仍增长 = **别急着怪 DVP 起振/NO-SOI**：先看 `salvaged` 是不是
跟着 `bad` 一起涨（2026-09-30 实测正是如此：`bad=11` 的同时每次抓帧都
`salvaged … self-checked ~8.9 KB (photo)`），那说明「驱动报 0 字节、缓冲里却是完整 JPEG」
是**常态**，要改的是兜底策略，不是驱动。`dup=` 只在拍照兜底启用后可能 > 0，
代表「与上一张照片逐字节相同的候选」被新鲜度闸门挡下（跨会话残留旧图的直接证据）。

### 四、2026-09-30 一轮基线 vs 一轮修复（同 workload 对照）

> 完整过程记录（工具链、踩坑、端到端证据、判读口诀、遗留事项）见
> `docs/camera_bad_frame_fix_work_log.md`；本节只留对照数字。
>
> ⚠ **2026-10-05 更正**：坏帧的根因已重新定位（驱动把 VSYNC 当帧边界、少算帧尾字节 ⇒
> `used=0/flags=0x41` 是结构性常态；且兜底自校验会扫到**同一块复用缓冲里上一帧残留的 EOI**
> ⇒ 上传「本帧前缀 + 旧帧尾巴」）。修复与验证见
> `docs/camera_frame_truncation_fix_work_log.md`。

`serial_round.py`（本机脚本，同上不进交付物；同一脚本、相同时序：8×`kind=camera` 间隔 15 s + 90 s `kind=preview`
+ 10 s 空闲 + 4×`kind=camera`，共 250 s 抓串口），分析用 `analyze_round.py`：

| 指标 | round1（修复前） | round2（修复后） |
|------|------------------|------------------|
| 抓到的 `[HEAP]` 行 | 79+41（无 `dma_min` 字段） | 72+46（含新增 `dma_min`） |
| `session open` / `closed` | 9 / 9（约每 2.7 s 就空闲回收一次） | 7 / 7（任务数不同：round2 有 1 个 POST 被服务端判 `DUPLICATE`，且最后一个任务在抓包窗口后才下发） |
| 拍照任务结果 | **0 成功**（8 次 `no usable frame in 11 attempts`，`frames=0 bad=11 salvaged=0`） | **0 次失败**：每次抓到帧都是 `salvaged … (photo)`，`frames=1 bad=2 salvaged=1`，`dup=0`；服务端实际下发 7 个任务（1 个被判 `DUPLICATE`），抓包窗口内 6 次 `[photo] upload OK … http=201`（第 7 个任务在窗口结束后才被板端领取） |
| 服务端落库照片 | **0**（`GET /api/v1/photos` 无新增） | **6 张**（id 14–19，640×480，8.9–9.3 KB，`request_id` 与任务一一对应） |
| `no usable frame in …` | 8 | **0** |
| `salvaged` | 205（全部来自 preview） | 229（其中 6 次带 `(photo)`） |
| `NO-SOI` / `NO-EOI` / `RX-DA OVF` | 0 / 0 / 0 | 0 / 0 / 0 |
| `no heap for` / `[csv] write failed` / 崩溃 | 0 / 0 / 0 | 0 / 0 / 0 |
| preview 会话 | `frames=205 salvaged=205` uptime=87.7 s | `frames=223 salvaged=223` uptime=93.6 s |

round1 结论：**直播/预览功能正常**（靠 C 层兜底，205 帧全救回、约 4 fps），
**拍照 100% 失败**；`no heap for 4294959519 B` 与「一失败就拆会话」的自持循环都没再出现
（A/B/D 层有效）。`dma_largest` 在预览期间从 10240 B 掉到 4096 B —— 每次 open/close
都要重来一遍 16 KiB 内部 DMA + 3×307200 B PSRAM 分配，正是 SD 写 `errno=5` 的前置条件，
所以「直播/预览期间不空闲回收」也一起改了。

round2 结论：**拍照回来了**（未做兜底时的 100% 失败 → 0 失败 + 6 张落库图，字节数
8977→8997→9132→9279→9127 各不相同，说明拿到的是新帧而不是同一块残留），
`dup=0` 表示新鲜度闸门一次都没误伤。新 `dma_min` 读数：拍照会话期间 9123 B，
跑完 90 s preview 后掉到 **2611 B**（DMA 子堆谷底，SD 写失败要盯的就是它）。

彻底修需要动驱动（本项目不采用，避免被组件升级覆盖）：在 `esp_video_setup_buffer()`
重建缓冲后顺手 `TAILQ_INIT()` 掉 `stream->queued_list` / `done_list` 并清引用，
或在 `esp_video_done_element()` 里校验元素确实属于当前缓冲数组。

---

## 四、板端实现（`main/main.c`）

| 函数 / 位置 | 职责 |
|-------------|------|
| `app_accel_init()` / `app_accel_read()` | QMA6100P（i2c_master，与相机共用 BSP 总线），量纲与旧库一致 |
| `app_camera_init()` | 开机调用一次：自建 20 MHz XCLK + `esp_video_init()`（**不调用**写死 16 MHz 的 `bsp_camera_start()`）→ `s_camera_ready = true`；失败只告警，IMU/上传照常 |
| `camera_session_open_locked()` / `camera_session_close_locked()` | 建立 / 关闭采集会话：`G_FMT`→`S_FMT`→`S_DQBUF_TIMEOUT`→`REQBUFS/QUERYBUF/mmap/QBUF`→`STREAMON` / `STREAMOFF`+`munmap`+`close` |
| `camera_grab_locked(out, mode)` | 从会话取一帧：`DQBUF` →（好帧）拷贝 → **立刻 `QBUF` 归还**；丢暖机帧与坏帧并打诊断日志；坏帧时 `camera_resync_locked()` 非阻塞清一次驱动 done 列表残留；**所有模式**都对 ERROR 帧做 `camera_jpeg_span()` 自校验兜底（拍照另过 `camera_jpeg_hash()` 新鲜度闸门，见 §三·补二 C 行）。软失败返回 `ESP_ERR_NOT_FOUND`（会话仍可用），硬失败返回 `ESP_FAIL`/`ESP_ERR_TIMEOUT`/`ESP_ERR_NO_MEM`（见 §三·补二） |
| `app_camera_capture_jpeg()` / `_live()` | 单帧拍照（`CAMERA_MODE_PHOTO`，DQBUF 上限 3 s）/ 直播预览（`CAMERA_MODE_LIVE`，800 ms），成功时返回堆上 JPEG 副本（PSRAM 优先）。软失败**不拆会话**，连续 `CAMERA_SOFT_FAIL_LIMIT(3)` 次才重建（旧代码一失败就重建，正是 0 字节帧自持循环的来源） |
| `camera_session_idle_close()` | 空闲 2 s 后关会话，由 `housekeeping_task`（优先级 2）周期调用，用 0 超时拿相机锁；**`s_live_streaming` / `s_preview_active` 为真时直接返回**（推流/预览本来 50 ms 一帧、不会真空闲，能凑满 2 s 的只有 HTTP 上传变慢，而那正是最不该 STREAMOFF 的时刻） |
| `photo_post_frame()` | 流式 POST 到 `/api/v1/photos`，`status_out` 回传 HTTP 状态码 |
| `task_handle_camera()` | camera 任务的业务流程：过期判定 → ack → 拍摄 → 上传 → 失败回执 |
| `task_handle_next_response()` | 在样本参数校验**之前**把 `kind=camera` 分流到上面那个函数 |

要点与取舍：

- **不占用采样器**：camera 任务不经过 `manual_capture_*` 状态机，也不打开采样闸门，
  因此周期上报不会暂停（与 `capture` 任务刻意不同）。代价：拍照瞬间的 IMU 上传照常发生，
  两者互不影响。
- **串行执行**：拍摄与上传都在 `task_poll_task`（栈 10240）里同步完成，**所以不会有两张照片
  同时在飞**；期间暂停领取新任务（约 0.3–2 s），其它任务的最坏延迟同样量级。
- **帧缓冲**：esp_video 为 JPEG 按 `640×480×8bit ≈ 300 KB/缓冲` 在 PSRAM 分配
  `CAMERA_BUFFER_COUNT(3)` 个 mmap 缓冲（约 900 KiB PSRAM）。
- **缓冲归还必须靠前**：好帧也是「先 `memcpy` 到自己堆缓冲，再**立刻** `QBUF` 归还」。
  驱动只有在 queued 队列非空时才继续接收下一帧；还晚了硬件就空转，下一帧从半帧开始
  （`dvp_calculate_jpeg_size()` 报 NO-SOI）→ 要等驱动内部重试。归还靠前，这类坏帧自然消失。
- **坏帧处理**：没有 `V4L2_BUF_FLAG_DONE` 或 `bytesused == 0` 的帧丢回队列重取，
  最多跳过 `CAMERA_FRAME_SKIP_MAX(8)` 帧；新会话的前 `CAMERA_WARMUP_FRAMES(2)` 帧
  无条件丢弃（并统计「其中有多少其实是好帧」，见 `warmup_dropped_good`）。
  在此之上还有三层自愈（**2026-09-29**，机制与证据见 §三·补二）：
  (A) 「这一轮没取到可用帧」算**软失败**，`session` 不拆，连续 `CAMERA_SOFT_FAIL_LIMIT(3)`
  次才重建；(B) 每个 grab 遇到第一个坏帧就 `camera_resync_locked()` 用 0 ms 超时
  排空一次驱动 done 列表并把元素立刻 `QBUF` 还回去（清掉跨会话残留元素）；
  (C) **全模式**（直播/预览/拍照，2026-09-30 起）：驱动报 ERROR 但缓冲里确实有一整帧 JPEG 时，
  `camera_jpeg_span()` 自校验（SOI@0 + 有 EOI + ≥ `CAMERA_JPEG_MIN_BYTES(1024)`）后收下并打 WARN；
  拍照那次日志带 ` (photo)` 后缀，并额外过一道**新鲜度闸门** `camera_jpeg_hash()`
  （与上一张成功照片逐字节相同 ⇒ 判为跨会话残留旧图，丢掉重取，计数 `dup=`）——
  原因是 2026-09-30 真机实测：`used=0` 是常态，「拍照不做兜底」等于拍照 100% 失败
  （8/8 任务 `frames=0 bad=11 salvaged=0`）。见 §四 与 `camera_bad_frame_fix_work_log.md`。
  **2026-10-05 补充**：`used=0` 的机制已查清（驱动把 VSYNC 当帧边界、少算了帧尾字节），
  且兜底本身有一个陷阱 —— 扫**整块复用缓冲**时会扫到**上一帧残留的 EOI**。现已修：
  每次 QBUF 归还缓冲前把整块缓冲写 0（`camera_wipe_map_locked()`），详见
  `docs/camera_frame_truncation_fix_work_log.md`。
- **DQBUF 超时**：`VIDIOC_S_DQBUF_TIMEOUT`（esp_video 私有 ioctl）按用途下发 ——
  单帧拍照 `CAMERA_DQBUF_TIMEOUT_MS(3000)`，直播/预览 `CAMERA_DQBUF_TIMEOUT_LIVE_MS(800)`；
  传感器不出图时不会把 `task_poll_task` 永久卡死；老版本 esp_video 不支持该 ioctl 时只告警一次。
- **失败必回执**：拍摄失败、相机未初始化、有效期已过，都会 `POST /tasks/{id}/fail`，
  页面立刻显示原因（而不是等有效期到了变 `timeout`）。HTTP 4xx 不再补发 `fail`
  （服务端已在同一请求里判定并落库，重复上报只会互相覆盖）。
- **日志**：`[photo] captured … for <rid>`、`[photo] upload OK/FAIL … http=…`，
  以及 `[HEAP] task: photo done`（沿用既有堆水位日志口径）。

---

## 五、服务端（`server/main.py`）

### 5.1 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/photos?device_id=…&request_id=…&ts_ms=…&w=…&h=…&note=…` | body 即 JPEG 字节。校验：`device_id` 必填（≤64）、body 非空且 ≤ `MAX_PHOTO_BYTES`(512 KiB)、SOI 魔数 `FF D8 FF`、`ts_ms`/`w`/`h`/`note` 合法。成功 201（幂等重传 200 + `idempotent=true`）；超大 413；参数问题 400；带 `request_id` 时先做归属校验 |
| `GET` | `/api/v1/photos?device_id=…&limit=12` | 列表（`id DESC`），`limit` 1–200 越界 400；返回 `count`/`total`/`photos[]`（含 `url`、`size_kb`、`received_at_str`、`ts_ms_str`） |
| `GET` | `/api/v1/photos/{id}` | `FileResponse`（`image/jpeg`，`Cache-Control: no-store`）；id 非法/不存在 404，元数据在而文件丢失 410 `FILE_MISSING` |
| `DELETE` | `/api/v1/photos/{id}` | 先删库行（提交）再删文件；返回 `deleted`/`file_deleted`/`warning`；重复删除 404 |
| `GET` | `/api/v1/tasks/{request_id}` | 额外返回 `photo`（该任务最近一张照片摘要），页面任务卡片据此显示「照片 #id」 |
| `GET` | `/api/v1/devices` | 设备集合改为 `uploads ∪ photos`（只有照片、没有样本的设备也必须出现在下拉里），并新增 `photos` 计数 |
| `GET` | `/api/v1/health` | 新增 `total_photos` 与 `latest_photo` |

### 5.2 归属校验与幂等

- `_check_photo_task_match()` 与样本侧的 `_check_task_match()` 同构，但**只认 `kind=camera`**：
  - `request_id` 不存在 → 404 `TASK_NOT_FOUND`；
  - 任务不是 camera（如 `capture` / `pause`）→ 409 `WRONG_KIND`，**任务状态不动**
    （采集任务必须靠样本收尾，否则会出现「完成但一个样本都没有」的假完成）；
  - `device_id` 不一致 → 400 + 任务置 `failed`（错误信息写入 `tasks.error`，页面可见）。
- 幂等键是 `request_id`（`photos.request_id` 上有索引）：同一任务重复上传不会再写第二行、
  第二个文件，返回 200 + `idempotent=true`。板端重试与页面连点都不会产生重复照片。
- 任务收尾与落盘在同一事务里（与 `store_upload()` 完全一致）：要么三件事都成功，要么都不写。

### 5.3 落盘与删除

- 路径 `server/photos/<安全 device_id>/<photo_id>.jpg`（`photos.path` 存的是**相对** `PHOTO_DIR`
  的路径，换机器/换目录仍可读；`PHOTO_DIR` 可用环境变量 `SENSOR_PHOTO_DIR` 覆盖，测试脚本靠它隔离）。
- 写入先落 `xxx.jpg.part` 再 `os.replace()` 原子改名：进程被杀/断电不会留下半张 JPEG 被当成完整照片。
- 文件字节**不入库**（BLOB 会让备份/查询变重），因此 `photos` 表与磁盘目录要成对备份。
- 删除先删库行再删文件：文件早被手工删掉也算删除成功（`file_deleted=false`），
  因为用户看到的结果是「库里没有这张照片了」。

---

## 六、前端（`server/static/`）

| 位置 | 行为 |
|------|------|
| 卡片「实时拍照」 | 按钮 + 状态文字 + 画廊网格；徽标显示当前设备照片张数 |
| `capturePhoto()` | 建 `kind=camera` 任务并跟踪；终态后 `refreshGallery()` 自动刷新 |
| `refreshGallery()` | `GET /api/v1/photos?device_id=…&limit=12`，失败只在卡片内提示，不影响主面板 |
| `photoCard(p)` | 缩略图（点击看原图）+ `#id / KB / 宽×高 / 接收时间 / 任务号 / 备注` + 「原图 / 删除」 |
| `deletePhoto(id)` | `confirm()` → `DELETE /api/v1/photos/{id}` → 无论成败都重拉画廊（避免界面与库不一致） |
| `renderTask(..., photo)` | `kind=camera` 时把「关联数据 upload_id」一栏显示为「照片 #id（KB · 尺寸 · 时间）」 |

自动化核对：`/ui/?autophoto=1` 会走与按钮**完全相同**的代码路径（不是测试专用分支），
便于无头浏览器验证「建任务 → 板端拍照上传 → 画廊出现新照片 → 删除后归零」。

---

## 七、验证方法

| 层 | 命令 | 覆盖内容 |
|----|------|----------|
| 固件编译 | `build_idf.bat`（写 `build_log.txt`，末尾 `BUILD_EXIT=0`） | 新驱动 + esp_video/相机代码可编译、可链接（无新增 warning） |
| 服务端单元 | `python server/test_photos.py` | 参数校验、413、落盘、取图、列表/limit、404/410、camera 任务收尾、幂等、设备不匹配、capture 任务不能被照片收尾、删除、health/devices 统计、临时目录清理 |
| 服务端回归 | `python server/test_receive.py` | 原有采集/任务/控制链路未被破坏（同一 `tasks` 表新增 `camera` 类型不改变既有行为） |
| 真实 HTTP | `python server/e2e_server_check.py` | 真实 uvicorn 下的上传/任务/控制链路 |
| 界面 | `python server/e2e_ui_check.py`（需本机 Edge/Chrome，无则 SKIP） | `/ui/?autophoto=1` 建 camera 任务 → 模拟板端领取并上传 JPEG → 任务详情带 photo、画廊渲染缩略图/尺寸/删除按钮、任务卡片显示照片 → DELETE 后画廊空态与计数归零 |

**真机验收步骤**（需要板子，自动化脚本 `e2e_device_check.py` 目前只覆盖 IMU/任务链路）：

1. `idf.py build && idf.py -p COMx flash monitor`；串口应看到
   `Detected QMA6100P at 0x12 … (i2c_master driver)`、
   `Camera ready: /dev/video2 (OV2640 DVP, JPEG, xclk=20000000 Hz)`、`[HEAP] after camera init …`；
   **不应出现** `Configured xclk frequency … is not equal to sensor xclk frequency` 的 warning
   ——一旦出现就说明 XCLK 又不是 20 MHz 了（见 §三）。
2. 网页 `/ui/` 点「拍一张照片」：任务卡片应在 ≤3 s 内从 `submitted` 走到 `completed`，
   串口出现 `[photo] captured … for <rid>` 与 `[photo] upload OK ts=… bytes=… http=201`。
3. 画廊出现 1 张缩略图，尺寸显示 `640×480`；点开是清晰照片（可对照镜头前的物体）。
4. `server/photos/<device_id>/<id>.jpg` 存在，且 `GET /api/v1/photos/<id>` 与文件字节一致。
5. 点「删除」：确认后缩略图消失、`total` 减 1、磁盘文件同时消失；再次删除返回 404。
6. 连续点两次「拍一张照片」：第二次复用未完成任务（`duplicate=true`），最终只产生 1 张照片。
7. 拍照期间观察 IMU 链路：周期批次照常上报（**不受影响**），`/api/v1/tasks` 里相机任务与采集任务互不干扰。
8. 长按 Button A 进「Web 直播」，跑 5-10 分钟：串口应出现
   `[cam] session open: … bufs=3 (each 307200 B, PSRAM)` 与逐帧 `[live] … http=201`；
   允许出现 `resync(bad-frame): drained …`、`salvaged bad frame … self-checked N B`
   （自愈在工作），但**不应**出现：
   - `no heap for 4294959519 B JPEG frame`（野长度，已由 D1 长度笼子拦下）；真 OOM 才会打
     `no heap for N B JPEG frame (idx=… flags=… cap=…)`，且此时 N 必然 ≤ 307200；
   - `no usable frame in 11 attempts (bad=11)` 的自持循环；
   - 反复的 `session closed` → `session open`（软失败不该拆会话）。
   收尾/自愈读数看 `session closed: frames=… bad=… resyncs=… salvaged=… stale=… bogus_len=…`：
   `stale`/`bogus_len` 增长说明跨会话残留确实在发生（app 已兜住），两者恒为 0 而 `bad` 增长
   则说明坏帧另有出处（DVP 起振 / NO-SOI），该往驱动侧查。

---

## 八、已知限制与回滚

**限制 / 未做**：

- 只支持**单张按需拍摄**，没有连拍 / 定时抓拍 / 视频流（Web 也不做实时预览）。
- 分辨率固定 640×480 JPEG（由 Kconfig 决定）；要换分辨率需同时改
  `CONFIG_CAMERA_OV2640_DVP_*` 与页面提示文字（板端不硬编码宽高，按 `VIDIOC_G_FMT` 上报实际值）。
- **XCLK 固定 20 MHz**（`main.c` 的 `CAMERA_XCLK_FREQ_HZ`）：这是 OV2640 所有
  `DVP_8bit_20Minput_*` 格式表的要求（见 §三），也是板端不再用 `bsp_camera_start()` 的唯一原因。
  换其它传感器/格式表时要跟着改，否则会又回到「帧为 0 字节」的状态。
- **相机需要 16 KiB 连续内部 DMA 内存**：`esp_cam_ctlr_dvp_cam.c` 每次 `STREAMON` 都会按
  `CONFIG_CAM_CTRL_DVP_DMA_BUFFER_SIZE` 申请一块连续 `MALLOC_CAP_DMA|MALLOC_CAP_INTERNAL`。
  本固件内部 DRAM 只有 ~186 KiB、其中 app 自身的 `.data/.bss` 就占 ~117 KiB，所以 32 KiB
  默认值会在负载高峰触发 ENOMEM（`VIDIOC_STREAMON failed: errno=12`）。`sdkconfig.defaults`
  里已把 `CONFIG_CAM_CTRL_DVP_DMA_BUFFER_SIZE` 降到 16384，并把
  `SPIRAM_MALLOC_ALWAYSINTERNAL` 降到 4096（让 cJSON/HTTP 等 ≥4 KiB 分配走 PSRAM），
  上传高峰下最大连续内部块仍保持 ~29 KiB（真机连拍 3 张全部 `http=201` 验证）。
- 照片**不参与自动清理/分页**：画廊固定取最近 12 张（`limit` 上限 200），磁盘占用需人工关注。
- `note` 字段服务端已支持，但板端暂不上送（页面也不提供输入框），目前只有 `e2e`/第三方客户端会用到。
- 拍照与上传在 `task_poll_task` 内同步执行，期间其它任务的下发/回执延迟 0.3–2 s。
- 真机拍摄效果（曝光/白平衡/是否需 H/V flip）尚未实机核对；BSP 已定义
  `BSP_CAMERA_VFLIP=1`、`BSP_CAMERA_HFLIP=0`，如画面倒置可在 `app_camera_init()` 之后用
  `V4L2_CID_VFLIP/V4L2_CID_HFLIP` 通过 `VIDIOC_S_EXT_CTRLS` 调整（参考 esp_video 的
  `capture_stream` 示例）。

**回滚方式**（按层次独立）：

1. 前端：删除「实时拍照」卡片与 `app.js` 里 `photo*` 相关函数即可（服务端接口留着不影响）。
2. 服务端：删掉 `/api/v1/photos*` 路由与 `photos` 表建表语句；`tasks.kind` 多出的 `camera`
   只是枚举成员，历史行不会被影响。
3. 板端：`task_handle_next_response()` 里的 camera 分流 + 相机初始化 + 拍照函数
   （服务端仍能正常工作，只是 camera 任务会被旧固件当成采集任务而被 `409` 拒绝）。
4. I2C 迁移**不建议单独回滚**：回退到 legacy 驱动会再次与相机 SCCB 冲突（启动 `abort()`）。