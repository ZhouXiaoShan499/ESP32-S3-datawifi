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
| 拍摄 | `app_camera_capture_jpeg()`：`open("/dev/video2")` → `VIDIOC_G_FMT/S_FMT` → `REQBUFS/QUERYBUF/mmap/QBUF` → `STREAMON` → `DQBUF` → `STREAMOFF` | 每次拍照 open/close 设备，硬件只在开机初始化一次 |
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

> BSP 的 XCLK 固定 16 MHz，而 JPEG 640×480 格式表里期望 20 MHz。`esp_video_init()` 对此
> **只打一条 warning**（`Configured xclk frequency ... is not equal to sensor xclk frequency`），
> DVP 数据通路按 PCLK 采样，仍能出图；真机首次烧录时确认这条告警存在即可。

---

## 四、板端实现（`main/main.c`）

| 函数 / 位置 | 职责 |
|-------------|------|
| `app_accel_init()` / `app_accel_read()` | QMA6100P（i2c_master，与相机共用 BSP 总线），量纲与旧库一致 |
| `app_camera_init()` | 开机调用一次：`bsp_camera_start()` → `s_camera_ready = true`；失败只告警，IMU/上传照常 |
| `app_camera_capture_jpeg()` | 一次完整的 V4L2 单帧采集，返回堆上 JPEG 副本（PSRAM 优先） |
| `photo_post_frame()` | 流式 POST 到 `/api/v1/photos`，`status_out` 回传 HTTP 状态码 |
| `task_handle_camera()` | camera 任务的业务流程：过期判定 → ack → 拍摄 → 上传 → 失败回执 |
| `task_handle_next_response()` | 在样本参数校验**之前**把 `kind=camera` 分流到上面那个函数 |

要点与取舍：

- **不占用采样器**：camera 任务不经过 `manual_capture_*` 状态机，也不打开采样闸门，
  因此周期上报不会暂停（与 `capture` 任务刻意不同）。代价：拍照瞬间的 IMU 上传照常发生，
  两者互不影响。
- **串行执行**：拍摄与上传都在 `task_poll_task`（栈 10240）里同步完成，**所以不会有两张照片
  同时在飞**；期间暂停领取新任务（约 0.3–2 s），其它任务的最坏延迟同样量级。
- **帧缓冲**：esp_video 为 JPEG 按 `640×480×8bit ≈ 300 KB/缓冲` 在 PSRAM 分配 2 个 mmap 缓冲；
  取到好帧后**立刻 memcpy** 到自己的堆缓冲再 `STREAMOFF`/`munmap`，避免回队列后数据被覆写。
- **坏帧处理**：没有 `V4L2_BUF_FLAG_DONE` 或 `bytesused == 0` 的帧丢回队列重取，
  最多跳过 `CAMERA_FRAME_SKIP_MAX(8)` 帧。
- **DQBUF 超时**：先 `VIDIOC_S_DQBUF_TIMEOUT`（esp_video 私有 ioctl）把单帧等待设成 3 s，
  传感器不出图时不会把 `task_poll_task` 永久卡死；老版本 esp_video 不支持该 ioctl 时只告警。
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
   `Detected QMA6100P at 0x12 … (i2c_master driver)`、`Camera ready: /dev/video2 (OV2640 DVP, JPEG)`、
   `[HEAP] after camera init …`；若有 `xclk frequency … not equal` 的 warning 属预期（见 §三）。
2. 网页 `/ui/` 点「拍一张照片」：任务卡片应在 ≤3 s 内从 `submitted` 走到 `completed`，
   串口出现 `[photo] captured … for <rid>` 与 `[photo] upload OK ts=… bytes=… http=201`。
3. 画廊出现 1 张缩略图，尺寸显示 `640×480`；点开是清晰照片（可对照镜头前的物体）。
4. `server/photos/<device_id>/<id>.jpg` 存在，且 `GET /api/v1/photos/<id>` 与文件字节一致。
5. 点「删除」：确认后缩略图消失、`total` 减 1、磁盘文件同时消失；再次删除返回 404。
6. 连续点两次「拍一张照片」：第二次复用未完成任务（`duplicate=true`），最终只产生 1 张照片。
7. 拍照期间观察 IMU 链路：周期批次照常上报（**不受影响**），`/api/v1/tasks` 里相机任务与采集任务互不干扰。

---

## 八、已知限制与回滚

**限制 / 未做**：

- 只支持**单张按需拍摄**，没有连拍 / 定时抓拍 / 视频流（Web 也不做实时预览）。
- 分辨率固定 640×480 JPEG（由 Kconfig 决定）；要换分辨率需同时改
  `CONFIG_CAMERA_OV2640_DVP_*` 与页面提示文字（板端不硬编码宽高，按 `VIDIOC_G_FMT` 上报实际值）。
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