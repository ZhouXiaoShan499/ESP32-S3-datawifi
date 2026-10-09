/*
 * Combined Example: IMU Acceleration + Wi-Fi + SNTP + SD Card Logging + LVGL UI
 *
 * Features:
 *  - QMA6100P accelerometer reading (from display_rotation)
 *  - Wi-Fi STA connection (from tcp_server)
 *  - SNTP time sync (from tcp_server)
 *  - Real-sensor upload: every 1 s batches the m/s² accelerometer samples
 *    as JSON and POSTs them to a local FastAPI receiver via esp_http_client
 *  - Button A start/stop SD card CSV logging (from data_capture_sim)
 *  - Button A long press (2s): cycles camera mode — Web live streaming (captures
 *    a JPEG every ~250 ms and POSTs it to /api/v1/live, the server keeps only
 *    the latest frame per device for the Web "摄像头实时画面" card, ~2-4 fps)
 *    → local LCD preview (decodes each JPEG and draws it on the 240x240 screen)
 *    → off.
 *  - LVGL real-time display (from data_capture_sim)
 *  - 6-face calibration mode: +X, -X, +Y, -Y, +Z, -Z, 10s each
 *
 * Hardware: ESP32-S3-EYE (BSP)
 */

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "bsp/esp-bsp.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "iot_button.h"
#include "lvgl.h"
#include "cJSON.h"
#include "esp_http_client.h"
#include "nvs_flash.h"
#include "sdmmc_cmd.h"
/* Shared BSP I2C bus (IMU + camera SCCB) - the NEW driver, never driver/i2c.h */
#include "driver/i2c_master.h"
/* 闭环事件用板载绿色 LED（GPIO3）；必须开漏，见「闭环事件 config」中的说明 */
#include "driver/gpio.h"
/* esp_video V4L2 user-space API: /dev/videoX ioctl + structs */
#include "linux/videodev2.h"
#include "esp_video_device.h"
#include "esp_video_ioctl.h"     /* VIDIOC_S_DQBUF_TIMEOUT 等私有 ioctl */
/* 相机初始化 API：esp_video_init() + esp_video_init_dvp_config_t
 * （esp_video_init.h 内部已包含 esp_cam_ctlr_dvp.h 与 esp_cam_sensor_xclk.h）；
 * esp_cam_sensor_xclk_* 用来自己驱动相机 XCLK（见 app_camera_init()）。 */
#include "esp_video_init.h"
#include "esp_cam_sensor_xclk.h"
#include "jpeg_decoder.h"      /* esp_jpeg 软解：把 JPEG 解成 RGB565（本地 LCD 预览用） */
#include <stdlib.h>
#include "freertos/queue.h"
/* 相机 mmap 缓冲在 PSRAM（esp_video 用 MALLOC_CAP_SPIRAM|CACHE_ALIGNED 分配），
 * 在「还给驱动之前把缓冲写 0」时必须做 C2M cache 同步（见 camera_wipe_map_locked()）。 */
#include "esp_cache.h"
#include "esp_memory_utils.h"

/* ================================================================
 *  Common Config
 * ================================================================ */
#define TARGET_SAMPLE_FREQ_HZ    100      /* Target: 100 Hz */
#define SAMPLE_PERIOD_MS         (1000 / TARGET_SAMPLE_FREQ_HZ)  /* 10 ms for 100 Hz */
#define START_BUTTON_INDEX       BSP_BUTTON_1
#define UI_TEXT_LEN              96
#define FILE_PATH_LEN            128
#define FREQ_MEASURE_SAMPLES     100      /* Measure frequency over 100 samples */

/* Data format config */
#define GRAVITY_ACCEL            9.80665f /* 1 g = 9.80665 m/s² */
#define DATA_LABEL               "raw"    /* Default label for data */

/* Real-sensor upload config */
#define UPLOAD_WINDOW_SIZE       100      /* 1 s at 100 Hz per POST batch */
#define UPLOAD_QUEUE_LEN         8        /* RAM buffer for slow nets (drop if full) */
#define UPLOAD_MAX_ATTEMPTS      3        /* bounded POST retries per batch */
#define UPLOAD_RETRY_BACKOFF_MS  500      /* 500 ms, 1 s (doubles per retry) */
#define UPLOAD_SOURCE            "qma6100p"
#define UPLOAD_UNIT              "m/s^2"

/* 按需采集任务（manual capture task）config。
 * 板端每 TASK_POLL_INTERVAL_MS 轮询 /api/v1/tasks/next：
 * 领到任务 → ack → 暂停周期上报 → 按任务节拍采集 N 点 → 带 request_id 回传。 */
#define TASK_POLL_INTERVAL_MS    3000     /* 轮询间隔 */
#define TASK_API_PATH_NEXT       "/api/v1/tasks/next"
#define TASK_API_PATH_ACK        "/api/v1/tasks/%s/ack"
#define TASK_API_PATH_FAIL       "/api/v1/tasks/%s/fail"
#define TASK_API_PATH_APPLIED    "/api/v1/tasks/%s/applied"
#define TASK_REQUEST_ID_LEN      40       /* 与服务端 uuid4().hex(32) 对齐 */
#define TASK_TRIGGER_LEN         16
#define TASK_KIND_LEN            12       /* "capture" / "pause" / "resume" */
#define TASK_MAX_SAMPLES         600      /* 板端单次采集上限（600×24B ≈ 14 KB 堆） */
#define TASK_MAX_PAUSE_S         600      /* 暂停时长上限（与 server PAUSE_MAX_S 对齐） */
#define TASK_PAUSE_FALLBACK_S    120      /* 服务端 duration_s 缺失/越界时的兜底时长 */
#define TASK_APPLIED_RETRY_MAX   2        /* applied 回执重试次数（每 3 s 一轮询） */
#define TASK_HTTP_TIMEOUT_MS     5000
#define TASK_RESP_BUF_LEN        2048     /* /tasks/next 响应缓冲 */

/* ================================================================
 *  闭环事件（loop event）config
 *
 *  「板端触发 → 本地反馈 → 远端显示 → 回应/取消」的完整闭环，方向与
 *  tasks 相反：**板端发起、Web 接收**。复用既有能力，不新增协议栈：
 *    - HTTP  ：复用 http_get_text() / http_post_text()（同一 TASK_HTTP_TIMEOUT_MS）
 *    - 鉴权  ：复用 task_http_common()（SENSOR_TOKEN 非空时自动带 Bearer）
 *    - 编号  ：request_id 由**板端生成**（板端必须先把灯闪了、屏幕改了，
 *              不能等服务端发号），服务端按主键去重，同一次按键重试只算一条。
 *
 *  板端状态机（全部改动都在 s_state_mutex 保护下）：
 *
 *     LOOP_IDLE ──按 B3 触发──▶ LOOP_TRIGGERED ──轮询到远端终态──▶ LOOP_COMPLETED
 *                    │                                              ▲   │
 *                    └──按 B4 回应/取消──▶ LOOP_WAITING_ACK ─────────┘   │
 *                                                                        │
 *                                           停留 LOOP_DONE_DWELL_MS 后 ─┘ 回到 IDLE
 *
 *  「远端已回来」的判据（故意不对 ack 自激，详见 loop_task 注释）：
 *    TRIGGERED   ：status ∈ {ack, cancelled, completed, expired} 都算远端有动作
 *    WAITING_ACK ：只有 {cancelled, completed, expired} 算；ack 是板端自己造成的
 * ================================================================ */
#define LOOP_API_PATH_TRIGGER    "/api/v1/events/trigger"
#define LOOP_API_PATH_RESPOND    "/api/v1/events/respond"
#define LOOP_API_PATH_STATUS     "/api/v1/events/status"

#define LOOP_POLL_INTERVAL_MS    1000     /* 闭环状态轮询间隔 */
#define LOOP_TASK_STACK          5120     /* cJSON + 1 次 HTTP（HTTP 自带缓冲） */
#define LOOP_TASK_PRIO           3        /* 与 task_poll 同级，低于 uploader(4) */
#define LOOP_RESP_BUF_LEN        1024     /* /events/status 响应缓冲 */
#define LOOP_REQUEST_ID_LEN      40       /* 32 hex + '\0'，与服务端 uuid4().hex 对齐 */
#define LOOP_STATUS_TEXT_LEN     32
#define LOOP_DONE_DWELL_MS       3000     /* COMPLETED 后停留多久再回 IDLE */
#define LOOP_LOCAL_TIMEOUT_MS    60000    /* 网络不通时的本地兜底：超时回 IDLE，
                                           * 免得板子永远卡在 TRIGGERED */

/* LED：板载绿色 LED 挂在 GPIO3（BSP_LED_1_IO）。
 * ⚠ 乐鑫官方硬件手册明确要求 **GPIO3 必须配成开漏输出**（GPIO_MODE_OUTPUT_OD）：
 *   "GPIO3 must be set up in open-drain mode. Pulling GPIO3 up may burn the LED."
 *   所以这里**不能用** bsp_led_indicator_create()/bsp_led_set()——那条路径走的是
 *   led_indicator_gpio，内部是 GPIO_MODE_OUTPUT（推挽），正是官方警告要避免的配置。
 *
 * 开漏下"线被释放(level=1)"与"被拉低(level=0)"哪个是亮，取决于板子 LED 的接法。
 * 默认按"释放=亮"（电源指示灯常态点亮、拉低即熄灭的接法）；
 * 若上板实测相反，把 LOOP_LED_ON_LEVEL 改成 0 即可，无需改其它代码。 */
#define LOOP_LED_GPIO            BSP_LED_1_IO
#define LOOP_LED_ON_LEVEL        1
/* 一组闪烁结束后灯是否保持常亮。
 *   0（默认）= 灯平时是灭的，"闪 N 下"就是亮 N 下 —— 反馈最直观；
 *   1        = 灯平时常亮（当电源指示灯用），"闪 N 下"表现为短暂熄灭 N 次。
 * 板载绿灯出厂是电源指示灯（通电即亮），若希望维持这个观感就把这里改成 1。 */
#define LOOP_LED_REST_ON         0

/* 闪烁次数（本地反馈的"语言"）：数字越小越"日常"，出错/取消用最多的次数区分。
 *   触发成功        → 2 下
 *   已回应（已上报）→ 1 下
 *   远端确认完成    → 3 下
 *   取消 / 过期     → 4 下
 *   上报失败        → 6 下（明显区别于所有正常路径） */
#define LOOP_BLINK_TRIGGER       2
#define LOOP_BLINK_RESPOND       1
#define LOOP_BLINK_DONE          3
#define LOOP_BLINK_CANCEL        4
#define LOOP_BLINK_FAIL          6
#define LOOP_BLINK_ON_MS         80
#define LOOP_BLINK_OFF_MS        120
#define LOOP_BLINK_GAP_MS        350      /* 两次"一组闪烁"之间 */
#define LOOP_BLINK_QUEUE_LEN     1        /* 用 xQueueOverwrite：永不阻塞、永不满 */

/* 闭环按键：BSP_BUTTON_1/2 已归 Button A/B，这里用未占用的 BSP_BUTTON_3/4。
 * 注意 ESP32-S3-EYE 的 BUTTON_1..4 是**同一个 ADC 通道上的电阻梯按键**
 * （bsp_button.c: ADC_CHANNEL_0，靠电压档位区分），不是独立 GPIO——
 * 但 iot_button 已把它们封装成 4 个独立设备，回调注册方式与 GPIO 按键一致。 */
#define LOOP_TRIGGER_BUTTON      BSP_BUTTON_3
#define LOOP_RESPOND_BUTTON      BSP_BUTTON_4
#define LOOP_RESPOND_LONG_MS     2000     /* 长按 = 取消（与 Button A/B 的单击/长按惯例一致） */

/* 实时拍照任务（kind=camera）config。
 * Web 点「拍一张照片」→ 建 camera 任务 → 本板拍一帧 JPEG → POST /api/v1/photos，
 * 服务端在同一事务里存图并把任务置 completed。不占用采样器、不暂停周期上报。 */
#define PHOTO_API_PATH           "/api/v1/photos"
#define PHOTO_HTTP_TIMEOUT_MS    20000    /* 一帧 JPEG（数十 KB）上传上限 */
#define PHOTO_RESP_BUF_LEN       512      /* 只看状态码，响应体只收一小段 */
/* 相机采集参数（详见下方「Camera — 持久会话」一节）。
 *   CAMERA_BUFFER_COUNT      : V4L2 mmap 缓冲数。3 = 驱动手里至少有一个可写缓冲，
 *                              我们的消费端（HTTP 上传 / JPEG 软解）慢一拍也不会让
 *                              DVP 空转；DVP 一空转，下一帧就从半帧开始（NO-SOI），
 *                              要等驱动内部重试 —— 串口上表现为随机坏帧。
 *   CAMERA_WARMUP_FRAMES     : 新会话刚 STREAMON 后主动丢掉的帧数。驱动自己会做
 *                              SOI/EOI 校验，这里再丢两帧只是保险（而且会记下
 *                              「有没有白丢好帧」，见 warmup_dropped_good）。
 *   CAMERA_DQBUF_TIMEOUT_MS  : 单帧拍照的 DQBUF 上限（宁等 3 s，也别让「拍一张
 *                              照片」失败）。
 *   CAMERA_DQBUF_TIMEOUT_LIVE_MS : 直播 / 本地预览的上限（坏帧就快速失败重试，
 *                              不把 250 ms 的推流节拍拖死）。
 *   CAMERA_SESSION_IDLE_MS   : 这么久没人取帧就关会话（STREAMOFF + munmap + close）。 */
#define CAMERA_BUFFER_COUNT          3
#define CAMERA_FRAME_SKIP_MAX        8
#define CAMERA_WARMUP_FRAMES         2
#define CAMERA_DQBUF_TIMEOUT_MS      3000
#define CAMERA_DQBUF_TIMEOUT_LIVE_MS 800
#define CAMERA_SESSION_IDLE_MS       2000
#define CAMERA_LOCK_TIMEOUT_MS       5000
#define CAMERA_DIAG_FRAMES           3    /* 会话前几帧打印诊断（flags/bytesused/SOI） */
/* 坏帧兜底与重同步（2026-09-29；机制见 camera_resync_locked() 的注释与
 * docs/realtime_photo_capture.md §三·补二）：
 *   CAMERA_RESYNC_POLL_MS    : 排空驱动 done 列表时下发的 DQBUF 上限。0 = 非阻塞
 *                              （esp_video_set_dqbuf_timeout()：0 ms → ticks = 0 →
 *                               xSemaphoreTake(sem, 0)，**不是** portMAX_DELAY）。
 *   CAMERA_RESYNC_MAX_FRAMES : 一次排空最多回收几个缓冲（缓冲数 3，留 1 个余量）。
 *   CAMERA_SOFT_FAIL_LIMIT   : 连续这么多次「没取到可用帧」才关会话重建 ——
 *                              关会话（STREAMOFF + close）本身正是制造 done 列表残留
 *                              元素的那一步（见 camera_resync_locked），能不关就不关。
 *   CAMERA_JPEG_MIN_BYTES    : 兜底接受驱动 ERROR 帧时，自校验出的 JPEG 至少这么大；
 *                              小于它一律当垃圾丢掉，绝不把半帧当图收下。
 *
 * 2026-09-30 真机实测（本机一轮 250 s 串口抓取，8 个 kind=camera 任务
 * + 90 s kind=preview）：**驱动对每一帧都报 used=0**（含每次会话的头两帧暖机帧），
 * 而同一块映射缓冲里自校验都能找到一整帧 JPEG（8927/8893/…/9028 B），
 * 且 0 次 NO-SOI / NO-EOI / RX-DA OVF —— 即 DVP 交回的元素与「真正装了这一帧的
 * 元素」不是同一个（done 列表里排着 valid_size=0 的元素，数据却已写进缓冲）。
 * 结论：`used=0` 是**本传感器配置下的常态**，不是偶发残留。于是「拍照模式不做兜底」
 * 等于拍照 100% 失败（8/8 任务），故拍照也启用兜底，只多一道新鲜度闸门
 * （见 camera_jpeg_hash()）。 */
#define CAMERA_RESYNC_POLL_MS        0
#define CAMERA_RESYNC_MAX_FRAMES     4
#define CAMERA_SOFT_FAIL_LIMIT       3
#define CAMERA_JPEG_MIN_BYTES        1024

/* 相机 XCLK 频率：必须 20 MHz。
 * BSP 的 bsp_camera_start() 把 XCLK 写死成 BSP_CAMERA_XCLK_CLOCK_MHZ(=16 MHz)，
 * 而 OV2640 的全部 DVP 格式表都叫 "DVP_8bit_20Minput_*"（ov2640.c:
 * `.name = "DVP_8bit_20Minput_JPEG_640x480_25fps"`, `.xclk = 20000000`）——
 * 它们是按 20 MHz 输入校准的寄存器序列。喂 16 MHz 时传感器内部 PLL 与格式表
 * 不匹配，JPEG 出不了完整帧（DQBUF 得到 flags=V4L2_BUF_FLAG_ERROR、bytesused=0），
 * 「拍一张照片」和长按 Button A 的实时直播会一起失败；esp_video_init() 也会正好
 * 打一条 "Configured xclk frequency ... the sensor output image may be unexpected"。
 * 所以 app_camera_init() 不走 bsp_camera_start()，自己按 20 MHz 初始化。 */
#define CAMERA_XCLK_FREQ_HZ      20000000

/* 摄像头实时直播（长按 Button A 切换）config。
 * 开启后 live_stream_task 循环「拍一帧 JPEG → POST /api/v1/live」，
 * 服务端只在内存里保留每台设备的最新一帧（不落盘、不入 photos 表），
 * Web 页「摄像头实时画面」卡片按 seq 轮询取图，形成 ~2-4 fps 的实时画面。
 * 再长按一次停止；不占用采样器、不暂停周期上报，与按需单帧拍照共用相机（加锁串行）。 */
#define LIVE_API_PATH            "/api/v1/live"
/* 帧间最小间隔：一轮 = 拍摄 + 上传 + 本延时，实测每轮约 400-550 ms（≈2-4 fps）。
 * 从 500 压到 250 是为了让摄像头移动时 Web 端画面跟得上；任务优先级只有 2
 * （低于 sampler(8)/uploader(4)/task_poll(3)），提速不会抢周期上报的 CPU。 */
#define LIVE_FRAME_INTERVAL_MS   250      /* 帧间最小间隔（Wi-Fi 下实际约 2-4 fps） */
#define LIVE_HTTP_TIMEOUT_MS     8000     /* 单帧上传上限（比单帧拍照短，避免拖慢节拍） */
#define LIVE_FAIL_LIMIT          5        /* 连续失败上限 → 自动停止推流并记日志 */
#define LIVE_TASK_STACK          8192     /* HTTP + 一帧 JPEG 拷贝 + LVGL 刷新 */
#define LIVE_TASK_PRIORITY       2        /* 低于 sampler(8)/uploader(4)/task_poll(3) */

/* ================================================================
 *  CSV 落盘缓冲 / 刷盘策略
 *
 *  背景（现场故障，见 docs 的 SD 写失败分析）：ESP32-S3 的 SDMMC 控制器
 *  **不能对 PSRAM 做 DMA**（SOC_SDMMC_PSRAM_DMA_CAPABLE=0）。一旦
 *  sdmmc_write_sectors() 收到 PSRAM 指针，它只能临时 heap_caps_aligned_alloc()
 *  一块 512 B 的内部 DMA 内存做 bounce buffer，写完再 free。
 *
 *  而 FatFs 的缓冲区默认放在 PSRAM（CONFIG_FATFS_ALLOC_PREFER_EXTRAM=y），
 *  所以「每采一个点就 fprintf + fflush」= 每秒 100 次 512 B 的内部 DMA
 *  申请/释放。内部 DMA 堆被相机（每帧 16 KB 连续块）切碎后，某一次申请失败
 *  就返回 FR_DISK_ERR → fprintf 返回 -1 → 原来的代码立刻 stop_collection()，
 *  整段录制报废，串口只留下一句 "Not enough heap memory" / "CSV write failed"。
 *
 *  对策分两层：
 *   1) 每个 CSV FILE* 挂一块**静态 .bss 的 4096 B 内部 RAM 缓冲**（天然的
 *      内部、DMA-capable、64 B 对齐）。stdio 攒满 4096 B 再下发，FatFs 直接
 *      从内部 RAM 走 Sdmmc 的直通 DMA 路径（一条 CMD25 写 8 个扇区），
 *      缓冲满触发的那次下发**一次 bounce 申请都不需要**，SD 命令数还少 8 倍。
 *   2) 把「每点 fflush」改成**按时间刷盘**：单扇区写的尾延迟（SD 卡内部
 *      GC/磨损均衡时可达 50~200 ms）远大于 10 ms 的采样周期，它本身就是
 *      采样抖动源之一。每秒 100 次写降到 ~2 次。
 *
 *  仍然要留一个「瞬时失败」的口子（也就是下面 3）：定时刷盘交出去的是一段
 *  **非 512 B 整数倍**的尾巴（CSV 每行长度不定）。FatFs 的直通写只认
 *  「偏移扇区对齐 + 长度 ≥512」，尾巴那个不满扇区只能走 FATFS.win 窗口
 *  缓冲（同样在 PSRAM）⇒ 依旧需要一次 512 B 的 MALLOC_CAP_DMA bounce。
 *  相机（直播/预览）正占着内部 DMA 堆时这次申请会失败 → errno=5(EIO)：
 *  **它是瞬时且自愈的**，等一拍（相机 DMA 让出）再申请就好，所以下单点重试
 *  成功不应当算失败 —— 只有全部重试用尽才置 csv_write_failed 并计入
 *  csv_fail_streak，否则「每 500 ms 一次重试即恢复」会写成满屏红色 ERROR。
 * ================================================================ */
#define CSV_IO_BUF_SIZE        4096    /* 必须 ≥512 且为 512 的整数倍 */
#define CSV_IO_BUF_COUNT       2       /* 同时最多两个 CSV 文件：数据文件 + calibration.csv
                                        * （stand/jump 的 *data_file 与 s_data_file 互为别名，
                                        *  见 start_stand/jump_protocol_locked 里的 s_data_file = ...） */
#define CSV_FLUSH_INTERVAL_MS  500     /* 按时间刷盘间隔：掉电最多丢 500 ms（协议阶段切换点仍立即刷） */
#define CSV_WRITE_FAIL_LIMIT   20      /* 连续这么多个采样周期写失败才停采集（20×10 ms = 200 ms） */
#define CSV_FLUSH_RETRY        3       /* 刷盘失败时在同一 tick 内重试的次数（重试成功
                                        * 即视为自愈的瞬时碎片，不算失败，见下） */

/* Device identity / server URL come from Kconfig (see Kconfig.projbuild).
 * Fallbacks keep the code compiling if the config header is stale -- keep them
 * as harmless placeholders: a real LAN address committed here would stay in the
 * git history forever (the same reason WIFI_SSID's default is a placeholder). */
#ifndef CONFIG_SENSOR_DEVICE_ID
#define CONFIG_SENSOR_DEVICE_ID "esp32s3-eye-0001"
#endif
#ifndef CONFIG_SENSOR_SERVER_URL
#define CONFIG_SENSOR_SERVER_URL "http://192.0.2.10:8000/api/v1/upload"   /* 192.0.2.0/24 = RFC 5737 doc range */
#endif
#ifndef CONFIG_SENSOR_TOKEN
#define CONFIG_SENSOR_TOKEN ""    /* empty = no auth (LAN debugging only) */
#endif

/* 6-face calibration config */
#define FACE_DURATION_SEC        10       /* Each face: 10 seconds */
#define FACE_DURATION_MS         (FACE_DURATION_SEC * 1000)
/* 这里原有 STATIONARY_THRESHOLD / STATIONARY_SAMPLES 两个宏（配合 check_stationary()
 * 做「静止判定」）。该函数没有任何调用点、其缓冲也没有读方，编译时只剩
 * "-Wunused-variable" 告警，故整组一起删除；若日后要把「先确认静止再采校准数据」
 * 接进 start_calibration_locked()，可从 git 历史里取回这组实现。 */

/* Stand action protocol config */
#define STAND_PREP_DURATION_SEC           3    /* 3s prep: hold stand pose */
#define STAND_EXECUTE_DURATION_MIN_SEC    20   /* 20-30s stand execution per group */
#define STAND_EXECUTE_DURATION_MAX_SEC    30   /* 20-30s stand execution per group */
#define STAND_INTERVAL_DURATION_SEC       5    /* 5s interval - stationary idle */
#define STAND_TOTAL_GROUPS                3    /* 3 groups total */

/* Stand protocol file path buffer */
#define STAND_FILE_PATH_LEN               128

/* Stairs action protocol config */
#define STAIRS_PREP_DURATION_SEC          3    /* 3s prep: hold stairs pose */
#define STAIRS_EXECUTE_DURATION_MIN_SEC   20   /* 20-30s stairs execution per group */
#define STAIRS_EXECUTE_DURATION_MAX_SEC   30   /* 20-30s stairs execution per group */
#define STAIRS_INTERVAL_DURATION_SEC      60   /* 60s interval - stationary idle (changed from 5s) */
#define STAIRS_TOTAL_GROUPS               3    /* 3 groups total */

/* Stairs protocol file path buffer */
#define STAIRS_FILE_PATH_LEN              128

/* Bend action protocol config */
#define BEND_DURATION_MIN_SEC         20       /* 20-30s bend execution per group */
#define BEND_DURATION_MAX_SEC         30       /* 20-30s bend execution per group */
#define BEND_INTERVAL_MIN_SEC         60       /* 60s minimum interval between groups */
#define BEND_INTERVAL_MAX_SEC         90       /* 90s maximum interval between groups */
#define BEND_TOTAL_GROUPS             3        /* 3 groups total */

/* Jump action protocol config */
#define JUMP_EXECUTE_DURATION_MIN_SEC   5      /* 5-10s jump execution per group (3-5 jumps) */
#define JUMP_EXECUTE_DURATION_MAX_SEC   10     /* 5-10s jump execution per group (3-5 jumps) */
#define JUMP_INTERVAL_MIN_SEC           5      /* 5s minimum interval between groups */
#define JUMP_INTERVAL_MAX_SEC           10     /* 10s maximum interval between groups */
#define JUMP_TOTAL_GROUPS               3      /* 3 groups total */

/* Fall action protocol config */
#define FALL_EXECUTE_DURATION_MIN_SEC 5        /* 5-10s fall execution per group (multiple falls) */
#define FALL_EXECUTE_DURATION_MAX_SEC 10       /* 5-10s fall execution per group (multiple falls) */
#define FALL_INTERVAL_MIN_SEC         15       /* 15s minimum interval between groups */
#define FALL_INTERVAL_MAX_SEC         30       /* 30s maximum interval between groups */
#define FALL_TOTAL_GROUPS             3        /* 3 groups total */

/* File path buffer for current group */
#define GROUP_FILE_PATH_LEN           128

/* WiFi 凭据改为从 Kconfig / sdkconfig 读取（CONFIG_WIFI_SSID / CONFIG_WIFI_PASSWORD），
   不再硬编码在源码里。真实热点信息只放在本地 sdkconfig（已被 .gitignore 忽略，
   不会随代码提交）。设置方法：idf.py menuconfig → "Project WiFi Configuration"，
   或编辑项目根目录的 sdkconfig 后重新编译（保持占位符即可安全提交）。 */
#define WIFI_SSID                CONFIG_WIFI_SSID
#define WIFI_PASSWORD            CONFIG_WIFI_PASSWORD
/* NTP 服务器同样来自 Kconfig（CONFIG_SNTP_SERVER，默认 pool.ntp.org）。
 * 可填域名（lwIP 每次请求都会重新解析，见 sntp.c: sntp_request()）或直接填 IP
 * （网内 DNS 坏掉时直接填 IP 也能对时）。设置方法：idf.py menuconfig →
 * "Time (SNTP) Configuration"，或编辑本地 sdkconfig。 */
#define SNTP_SERVER              CONFIG_SNTP_SERVER
/* 启动阶段等待联网的上限（WiFi 关联 + DHCP 拿到地址）。超过这个时间也继续启动，
 * 不能让 DHCP 卡住整个 app_main()——否则 SD/IMU/相机/按键/HTTP 服务全都起不来，
 * 板子表现为「串口没有任何后续日志、网页全废」。见 wifi_init_sta() / wifi_health_check()。 */
#define WIFI_CONNECT_TIMEOUT_MS    20000
/* 关联连续失败超过这个次数后，把重连节奏交给 wifi_health_check()（每 5 s 一次），
 * 只是「不再立刻重连」，永远不会放弃。 */
#define EXAMPLE_ESP_MAXIMUM_RETRY  5

/* 静态 IP 兜底（Kconfig: "Static IP Fallback"）。
 * 现场实测的形态：板子关联成功、但网段里 DHCP 完全不应答 → 永远没有租约 → 没有 IP
 * → SNTP 连域名都解析不了 → LCD 永远 "-- (up …)"。DHCP 重启/重关联都救不了，
 * 只能改用固定地址。打开后 wifi_health_check() 先给 DHCP 完整的窗口（3 次重启
 * DHCP 客户端 + 1 次强制重关联），确认没救才停掉 DHCP 客户端套固定地址。
 * STATIC_IP_GIVEUP_LIMIT 是「DHCP gave no lease」出现这么多次（每次约 45~60 s）之后
 * 才放弃；它故意与开关无关地定义——wifi_health_check() 里的判断是运行期 if（不是 #if），
 * 关掉开关时不定义就会编译不过（`STATIC_IP_FALLBACK_ENABLED && x >= …` 仍然要解析
 * 标识符，哪怕左边是常量 0）。 */
#define STATIC_IP_GIVEUP_LIMIT      2
#if CONFIG_USE_STATIC_IP_FALLBACK
#define STATIC_IP_FALLBACK_ENABLED  1
#else
#define STATIC_IP_FALLBACK_ENABLED  0
#endif

#define WIFI_CONNECTED_BIT       BIT0
#define WIFI_FAIL_BIT            BIT1

/* Accelerometer config */
#define ACCEL_FILTER_ALPHA       0.18f

/* I2C：IMU 与相机传感器共用 BSP 总线（CONFIG_BSP_I2C_NUM / GPIO4-5 / 400 kHz），
   统一走新驱动 driver/i2c_master.h；旧的 QMA6100P_I2C_PORT（legacy 驱动）已移除。 */

static const char *TAG = "imu_logger";

/* ------------------------------------------------------------------
 * Heap diagnostics.
 * PSRAM is enabled (CONFIG_SPIRAM=y): the big blocks (LVGL pool, WiFi/LWIP
 * buffers, JPEG frames) live there, but everything below
 * CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL still competes for internal SRAM. When
 * it runs dry the failures surface far from
 * the cause (wifi "fail to alloc timer", i2c "command link malloc error",
 * camera "no mem for CAM DVP DMA receive buffer"),
 * so log the real numbers instead of guessing.
 *   free      = total free heap (all capabilities)
 *   min_free  = low-water mark since boot
 *   int_largest = biggest single contiguous INTERNAL block; this is what
 *                 WiFi/i2c actually need, and it shrinks with
 *                 fragmentation long before "free" looks alarming.
 *   dma_free / dma_largest = the heap the SDMMC driver actually needs.
 *                 ESP32-S3 的 SDMMC **不能对 PSRAM DMA**，写一个 PSRAM 指针
 *                 必须临时申请 512 B 的 MALLOC_CAP_DMA 做 bounce buffer
 *                 （见 sdmmc_cmd.c 的 sdmmc_write_sectors / esp_dma_capable_malloc）。
 *                 这个申请失败时串口只有一句 "dma_utils: Not enough heap memory"，
 *                 和 int_largest 看起来完全对不上号——因为 int_largest 是
 *                 INTERNAL 整个capability的最大块，而失败的是它内部 DMA 子集。
 *                 dma_largest 一旦掉到几百字节，SD 写就开始随机失败。
 *   dma_min   = DMA 子堆的低水位（历次最小值）。SD 写失败的判据就是它：
 *               dma_free/dma_largest 是「此刻」的瞬时值，采样间隔里也可能塌下去，
 *               而 dma_min 记的是曾经到过的谷底（2026-09-30 的 errno=5 现场
 *               就是 dma_largest 掉到 96 B 时发生的）。
 *   psram_free = PSRAM 总空闲（相机帧缓冲 / LVGL 池在这里）
 * ------------------------------------------------------------------ */
static void log_heap(const char *where)
{
    ESP_LOGI(TAG, "[HEAP] %-22s free=%6u min=%6u int_largest=%6u | dma_free=%6u"
                  " dma_largest=%5u dma_min=%5u | psram=%7u",
             where,
             (unsigned)esp_get_free_heap_size(),
             (unsigned)esp_get_minimum_free_heap_size(),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_DMA),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_DMA),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

/* ================================================================
 *  CSV 文件静态 stdio 缓冲
 *
 *  setvbuf() 挂进来的缓冲必须活到 fclose() 为止，所以只能静态分配（.bss 天然
 *  在内部 DRAM → 天然 DMA-capable）。newlib 的 FILE 里没有回指缓冲的字段，
 *  所以这里用 owner 表记录「哪块静态缓冲当前归哪个 FILE*」，关闭时归还槽位。
 *
 *  注意：所有调用点都在 s_state_mutex 保护下（协议的 start/stop 与采样写盘
 *  同一把锁），因此这里不需要额外加锁。
 * ================================================================ */
static uint8_t s_csv_io_buf[CSV_IO_BUF_COUNT][CSV_IO_BUF_SIZE] __attribute__((aligned(64)));
static FILE *s_csv_io_owner[CSV_IO_BUF_COUNT];

/* 给 CSV 文件挂静态内部 RAM 缓冲。失败只告警，退回默认缓冲（功能不受影响）。 */
static void csv_io_attach(FILE *f)
{
    if (!f) {
        return;
    }
    for (int i = 0; i < CSV_IO_BUF_COUNT; i++) {
        if (s_csv_io_owner[i] == f) {
            return;                       /* 已挂过 */
        }
    }
    for (int i = 0; i < CSV_IO_BUF_COUNT; i++) {
        if (s_csv_io_owner[i] == NULL) {
            if (setvbuf(f, (char *)s_csv_io_buf[i], _IOFBF, CSV_IO_BUF_SIZE) == 0) {
                s_csv_io_owner[i] = f;
            } else {
                ESP_LOGW(TAG, "[csv_io] setvbuf failed, keeping default stdio buffer");
            }
            return;
        }
    }
    ESP_LOGW(TAG, "[csv_io] static buffer pool exhausted, keeping default stdio buffer");
}

/* fclose() + 归还静态缓冲槽位。**必须先 fclose 再归还**：fclose 内部要把
 * 残留数据刷出去，那一刻缓冲还得算在这个 FILE 名下（静态内存不会被 free，
 * 顺序其实都安全，但先关后还更不容易出错）。 */
static void csv_fclose(FILE **pf)
{
    if (!pf || !*pf) {
        return;
    }
    FILE *f = *pf;
    *pf = NULL;
    fclose(f);
    for (int i = 0; i < CSV_IO_BUF_COUNT; i++) {
        if (s_csv_io_owner[i] == f) {
            s_csv_io_owner[i] = NULL;
        }
    }
}

/* ================================================================
 *  Global State
 * ================================================================ */
static SemaphoreHandle_t s_state_mutex;
static button_handle_t s_buttons[BSP_BUTTON_NUM] = {0};

/* LVGL UI objects */
static lv_obj_t *s_title_label;
static lv_obj_t *s_state_label;
static lv_obj_t *s_time_display;
static lv_obj_t *s_accel_x_label;
static lv_obj_t *s_accel_y_label;
static lv_obj_t *s_accel_z_label;
static lv_obj_t *s_samples_label;
static lv_obj_t *s_mode_label;
static lv_obj_t *s_status_bar;
static lv_obj_t *s_face_label;
static lv_obj_t *s_accel_mag_label;
static lv_obj_t *s_stand_label_ui;
static bool s_ui_ready = false;

/* State variables */
static bool s_sd_ready;
static bool s_collecting;
/* 跨任务读写：WiFi 事件任务写，sampler / live / task_poll / loop 读。
 * 与 s_live_streaming / s_wifi_link_up 同口径加 volatile —— 编译器不能把它缓存进
 * 寄存器，否则各任务里的轮询循环可能永远看不到状态变化（关联成功却还在等 IP）。 */
static volatile bool s_wifi_connected;
static bool s_time_synced;
static FILE *s_data_file;
static float s_latest_accel_x;
static float s_latest_accel_y;
static float s_latest_accel_z;
static uint32_t s_sample_count;
static uint64_t s_base_timestamp_ms;          /* NTP-anchored base timestamp for sample #0 */
static char s_status_text[UI_TEXT_LEN];
static char s_file_path[FILE_PATH_LEN];
static char s_time_str[64];

/* 摄像头实时直播（长按 Button A 切换，见「实时直播」一节）：
 * 状态在 UI / 按键回调 / 推流任务之间共享，故放在全局状态里；
 * 推流任务由长按按键按需创建、结束时自删，任务句柄不外传。 */
static volatile bool s_live_streaming = false;    /* 正在推流？ */
static volatile uint32_t s_live_frames = 0;       /* 本次推流已成功上传的帧数 */
static volatile int s_live_last_status = 0;       /* 最近一帧的 HTTP 状态（0 = 网络层失败） */
static volatile bool s_preview_active = false;    /* 本地 LCD 相机预览中？ */

/* ================================================================
 *  闭环事件状态（见上方「闭环事件 config」与 loop_task 一节）
 *
 *  所有读写都在 s_state_mutex 保护下（按键回调、loop_task、refresh_ui
 *  三方都会碰），与其它全局状态同一把锁，避免 refresh_ui 读到半更新的文本。
 * ================================================================ */
typedef enum {
    LOOP_IDLE = 0,        /* 空闲：没有进行中的闭环 */
    LOOP_TRIGGERED,       /* 已按触发键，等远端处理 */
    LOOP_WAITING_ACK,     /* 已按回应/取消键，等远端收口 */
    LOOP_COMPLETED,       /* 远端已回来：本地闪灯 + 显示 DONE，停留后回 IDLE */
} loop_state_t;

static volatile loop_state_t s_loop_state = LOOP_IDLE;
static char     s_loop_status_text[LOOP_STATUS_TEXT_LEN] = "LOOP:IDLE";
static char     s_loop_request_id[LOOP_REQUEST_ID_LEN] = "";
/* 按键回调只置标志，真正的 HTTP 由 loop_task 发（按键回调栈很小，严禁阻塞） */
static volatile bool s_loop_need_trigger = false;
static volatile bool s_loop_need_respond = false;
static volatile bool s_loop_need_cancel  = false;
/* 本地兜底截止时刻：网络不通时不至于永远卡在 TRIGGERED/WAITING_ACK */
static volatile int64_t s_loop_deadline_ms = 0;
/* 进入 COMPLETED 的时刻，用于 DONE 文本停留后再回 IDLE */
static volatile int64_t s_loop_done_at_ms = 0;
static QueueHandle_t s_led_blink_queue = NULL;    /* 元素 uint8_t：还要闪几下 */

/* WiFi event group */
static EventGroupHandle_t s_wifi_event_group;
static int s_retry_num;

/* WiFi 状态必须分成两层来跟踪：
 *   s_wifi_link_up  = L2 已关联 AP
 *   s_wifi_connected= L3 已拿到 IP（DHCP 成功）
 * 只用 s_wifi_connected 会漏掉「关联成功但 DHCP 一直没发下地址」这种故障：
 * 那种情况下 GOT_IP 和 DISCONNECTED 两个事件都不来，旧实现用 portMAX_DELAY
 * 死等就再也醒不过来。详细分析见 docs/wifi_network_troubleshooting.md。 */
static esp_netif_t *s_sta_netif;            /* 保留句柄：DHCP 卡住时重启客户端 */
static volatile bool s_wifi_link_up;        /* 已关联 AP（L2） */
static volatile int64_t s_link_up_us;       /* 本次关联的时刻，用于判断 DHCP 是否卡住 */
static int s_dhcp_restart_count;            /* 同一条链路下已重启 DHCP 客户端的次数 */
static int s_dhcp_giveup_count;             /* 已判定「DHCP 没发租约」并强制重关联的次数 */
static bool s_static_ip_armed;              /* DHCP 已判定没救：改用固定地址（Kconfig 开关） */
#if STATIC_IP_FALLBACK_ENABLED
static bool s_static_ip_warned;             /* Kconfig 里的静态地址非法时只报错一次 */
#endif

/* Accelerometer handle (new I2C master driver, shared BSP bus) */
static i2c_master_dev_handle_t s_accel = NULL;

/* Accelerometer low-pass filter state (reset per capture session) */
static float s_filter_x = 0, s_filter_y = 0, s_filter_z = 0;
static bool s_filter_init = false;

/* Frequency measurement variables */
static uint32_t s_freq_sample_count = 0;
static int64_t s_freq_start_time_us = 0;
static bool s_freq_measuring = false;
static float s_measured_freq_hz = 0.0f;
static bool s_freq_valid = false;

/* 6-face calibration state */
typedef enum {
    FACE_IDLE = 0,
    FACE_POS_X,      /* +X face up */
    FACE_NEG_X,      /* -X face up */
    FACE_POS_Y,      /* +Y face up */
    FACE_NEG_Y,      /* -Y face up */
    FACE_POS_Z,      /* +Z face up */
    FACE_NEG_Z,      /* -Z face up */
    FACE_COMPLETE
} calibration_face_t;

static calibration_face_t s_current_face = FACE_IDLE;
static uint32_t s_face_sample_count = 0;
static int64_t s_face_start_time_ms = 0;
static FILE *s_calibration_file = NULL;
static char s_face_name[16] = "IDLE";

/* Calibration mode accumulators */
static float s_calib_sum_x[7] = {0};  /* Index by face enum */
static float s_calib_sum_y[7] = {0};
static float s_calib_sum_z[7] = {0};
static uint32_t s_calib_count[7] = {0};

/* Stand action protocol state */
typedef enum {
    STAND_PROTOCOL_IDLE = 0,
    STAND_PROTOCOL_PREP,       /* 3s prep: hold stand pose, label = "stand" */
    STAND_PROTOCOL_EXECUTE,    /* 20-30s execute: standing still, label = "stand" */
    STAND_PROTOCOL_INTERVAL,   /* 5s interval: relax, label = "idle" */
    STAND_PROTOCOL_COMPLETE    /* All 3 groups done */
} stand_protocol_state_t;

#define STAND_PREP_DURATION_SEC    3        /* 3s prep: hold stand pose */

static stand_protocol_state_t s_stand_state = STAND_PROTOCOL_IDLE;
static int s_stand_group = 0;                        /* Current group index (0-based) */
static int64_t s_stand_phase_start_ms = 0;           /* Phase start time (ms) */
static int s_stand_duration_sec = 25;                /* Current stand duration (20-30s) */
static int s_stand_interval_sec = 5;                 /* Current stand interval (5s) */
static char s_stand_label[16] = "";                  /* Current label for stand protocol */
static char s_stand_display[64] = "Stand: IDLE";     /* UI display text */
static bool s_stand_protocol_active = false;          /* Is stand protocol running? */
static FILE *s_stand_data_file = NULL;               /* Current group data file */
static bool s_stand_file_open = false;               /* Track if stand file is open */

/* Stairs action protocol state */
#define STAIRS_PREP_DURATION_SEC    3        /* 3s prep: hold stairs pose */
typedef enum {
    STAIRS_PROTOCOL_IDLE = 0,
    STAIRS_PROTOCOL_PREP,      /* 3s prep: hold stairs pose, label = "stairs" */
    STAIRS_PROTOCOL_EXECUTE,   /* 20-30s stairs execution: label = "stairs" */
    STAIRS_PROTOCOL_INTERVAL,  /* 60s rest: label = "idle" */
    STAIRS_PROTOCOL_COMPLETE   /* All 3 groups done */
} stairs_protocol_state_t;

static stairs_protocol_state_t s_stairs_state = STAIRS_PROTOCOL_IDLE;
static int s_stairs_group = 0;                        /* Current group index (0-based) */
static int64_t s_stairs_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_stairs_label[16] = "";                   /* Current label for stairs protocol */
static char s_stairs_display[64] = "Stairs: IDLE";    /* UI display text */
static bool s_stairs_protocol_active = false;          /* Is stairs protocol running? */
static int s_stairs_duration_sec = 25;                 /* Current stairs duration (20-30s) */
static int s_stairs_interval_sec = 60;                 /* Current stairs interval (60s) */

/* Bend action protocol state */
#define BEND_PREP_DURATION_SEC    3        /* 3s prep: hold bend pose */
typedef enum {
    BEND_PROTOCOL_IDLE = 0,
    BEND_PROTOCOL_PREP,      /* 3s prep: hold bend pose, label = "bend" */
    BEND_PROTOCOL_EXECUTE,   /* 20-30s bend execution: label = "bend" */
    BEND_PROTOCOL_INTERVAL,  /* 60-90s rest: label = "idle" */
    BEND_PROTOCOL_COMPLETE   /* All 3 groups done */
} bend_protocol_state_t;

static bend_protocol_state_t s_bend_state = BEND_PROTOCOL_IDLE;
static int s_bend_group = 0;                        /* Current group index (0-based) */
static int64_t s_bend_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_bend_label[16] = "";                   /* Current label for bend protocol */
static char s_bend_display[64] = "Bend: IDLE";      /* UI display text */
static bool s_bend_protocol_active = false;          /* Is bend protocol running? */
static int s_bend_duration_sec = 25;                 /* Current bend duration (20-30s) */
static int s_bend_interval_duration_sec = 60;        /* Current interval duration (60-90s) */

/* Jump action protocol state */
#define JUMP_PREP_DURATION_SEC    3        /* 3s prep: hold jump pose */

typedef enum {
    JUMP_PROTOCOL_IDLE = 0,
    JUMP_PROTOCOL_PREP,      /* 3s prep: hold jump pose, label = "jump" */
    JUMP_PROTOCOL_EXECUTE,   /* 5-10s jump execution: label = "jump" */
    JUMP_PROTOCOL_INTERVAL,  /* 5-10s rest: label = "idle" */
    JUMP_PROTOCOL_COMPLETE   /* All 3 groups done */
} jump_protocol_state_t;

static jump_protocol_state_t s_jump_state = JUMP_PROTOCOL_IDLE;
static int s_jump_group = 0;                        /* Current group index (0-based) */
static int64_t s_jump_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_jump_label[16] = "";                   /* Current label for jump protocol */
static char s_jump_display[64] = "Jump: IDLE";      /* UI display text */
static bool s_jump_protocol_active = false;          /* Is jump protocol running? */
static int s_jump_duration_sec = 10;                 /* Current jump duration (5-10s) */
static int s_jump_interval_duration_sec = 5;         /* Current interval duration (5-10s) */
static char s_jump_file_path[FILE_PATH_LEN];         /* Current group file path */
static FILE *s_jump_data_file = NULL;                /* Current group data file */

/* Fall action protocol state */
#define FALL_PREP_DURATION_SEC    3        /* 3s prep: hold fall pose */
typedef enum {
    FALL_PROTOCOL_IDLE = 0,
    FALL_PROTOCOL_PREP,      /* 3s prep: hold fall pose, label = "fall" */
    FALL_PROTOCOL_EXECUTE,   /* 5-10s fall execution: label = "fall" */
    FALL_PROTOCOL_INTERVAL,  /* 15-30s rest: label = "idle" */
    FALL_PROTOCOL_COMPLETE   /* All 3 groups done */
} fall_protocol_state_t;

static fall_protocol_state_t s_fall_state = FALL_PROTOCOL_IDLE;
static int s_fall_group = 0;                        /* Current group index (0-based) */
static int64_t s_fall_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_fall_label[16] = "";                   /* Current label for fall protocol */
static char s_fall_display[64] = "Fall: IDLE";      /* UI display text */
static bool s_fall_protocol_active = false;          /* Is fall protocol running? */
static int s_fall_duration_sec = 5;                  /* Current fall duration (5-10s) */
static int s_fall_interval_duration_sec = 15;        /* Current interval duration (15-30s) */

/* Forward declaration */
static void refresh_ui(void);
/* 摄像头实时直播（长按 Button A 切换）：实现在「实时直播」一节，
 * 按键回调放在 Button 一节，因此这里先声明。 */
static void live_streaming_toggle(void);
static void button_a_long_press_cb(void *arg, void *data);
/* 闭环事件（详见「闭环事件」一节）：按键回调交给 init_buttons() 注册，
 * 但实现放在 HTTP 辅助函数之后（那里才有 http_post_text），故先声明。 */
static void loop_module_init(void);
static void loop_trigger_button_cb(void *arg, void *data);
static void loop_respond_button_cb(void *arg, void *data);
static void loop_respond_long_press_cb(void *arg, void *data);

/* ================================================================
 *  Utility
 * ================================================================ */
static void set_status_locked(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    vsnprintf(s_status_text, sizeof(s_status_text), fmt, args);
    va_end(args);
}

static void refresh_ui(void)
{
    bool collecting = false;
    bool sd_ready = false;
    float ax = 0, ay = 0, az = 0;
    uint32_t sample_count = 0;
    char time_buf[64];
    char face_buf[32];

    bool freq_valid = false;
    float measured_freq = 0.0f;

    /* 网络 / 对时状态：现场诊断用（以前 LCD 上完全没有联网信息，只能接串口） */
    bool wifi_link = false;
    bool time_synced = false;
    bool have_ip = false;
    char net_buf[24];

    /* Do not touch LVGL objects before they are created */
    if (!s_ui_ready) {
        return;
    }

    /* Check if critical UI objects are initialized */
    if (!s_status_bar || !s_face_label) {
        return;
    }

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) != pdTRUE) {
        return;
    }

    collecting = s_collecting;
    sd_ready = s_sd_ready;
    ax = s_latest_accel_x;
    ay = s_latest_accel_y;
    az = s_latest_accel_z;
    sample_count = s_sample_count;
    snprintf(time_buf, sizeof(time_buf), "%s", s_time_str);
    freq_valid = s_freq_valid;
    measured_freq = s_measured_freq_hz;
    wifi_link = s_wifi_link_up;
    time_synced = s_time_synced;
    
    /* Ensure s_face_name is valid before copying */
    if (s_face_name[0] != '\0') {
        snprintf(face_buf, sizeof(face_buf), "%s", s_face_name);
    } else {
        snprintf(face_buf, sizeof(face_buf), "IDLE");
    }

    xSemaphoreGive(s_state_mutex);

    /* 网络层 token（状态行右侧）：现场不接串口也能看出卡在哪一层。
     *   --    = 未关联 AP
     *   assoc = 已关联但一直没有 IP（DHCP 没发租约 —— 本网段实测到的故障）
     *   10.x  = 有 IP（DHCP 租约或静态 IP 兜底），SNTP 才有机会成功 */
    esp_netif_ip_info_t ip_info = {0};
    if (s_sta_netif != NULL &&
        esp_netif_get_ip_info(s_sta_netif, &ip_info) == ESP_OK && ip_info.ip.addr != 0) {
        have_ip = true;
    }
    if (!wifi_link) {
        snprintf(net_buf, sizeof(net_buf), "--");
    } else if (!have_ip) {
        snprintf(net_buf, sizeof(net_buf), "assoc");
    } else {
        snprintf(net_buf, sizeof(net_buf), IPSTR, IP2STR(&ip_info.ip));
    }

    if (!bsp_display_lock(0)) {
        return;
    }

    /* State display：直播中显示推流帧数（长按 Button A 开关，见「实时直播」一节）；
     * 右侧固定带网络 token，见上面 net_buf 的说明。 */
    if (s_state_label) {
        if (s_preview_active) {
            lv_label_set_text_fmt(s_state_label, "PREVIEW  NET:%s", net_buf);
        } else if (s_live_streaming) {
            lv_label_set_text_fmt(s_state_label, "LIVE %" PRIu32 " fr  NET:%s",
                                  s_live_frames, net_buf);
        } else {
            lv_label_set_text_fmt(s_state_label, "%s  NET:%s",
                                  collecting ? "Collecting" : "Idle", net_buf);
        }
    }
    
    /* Time display */
    if (s_time_display) {
        if (time_synced) {
            /* 有日期本身就是「对时成功」的证据，不再加 token 占宽度 */
            lv_label_set_text_fmt(s_time_display, "Time: %s", time_buf);
        } else {
            /* NTP:--  = 还没有 IP，SNTP 连请求都发不出去；
             * NTP:try = 有 IP、正在退避重试（等网段的 DNS/NTP 可达） */
            lv_label_set_text_fmt(s_time_display, "Time: %s NTP:%s",
                                  time_buf, have_ip ? "try" : "--");
        }
    }
    
    /* Acceleration axes - each on separate line (values in mm/s^2, integer) */
    int32_t ax_mm = (int32_t)(ax * 1000);
    int32_t ay_mm = (int32_t)(ay * 1000);
    int32_t az_mm = (int32_t)(az * 1000);
    if (s_accel_x_label) {
        lv_label_set_text_fmt(s_accel_x_label, "Accel X: %" PRId32 " mm/s^2", ax_mm);
    }
    if (s_accel_y_label) {
        lv_label_set_text_fmt(s_accel_y_label, "Accel Y: %" PRId32 " mm/s^2", ay_mm);
    }
    if (s_accel_z_label) {
        lv_label_set_text_fmt(s_accel_z_label, "Accel Z: %" PRId32 " mm/s^2", az_mm);
    }
    
    /* Samples and mode */
    if (s_samples_label) {
        lv_label_set_text_fmt(s_samples_label, "Samples: %" PRIu32, sample_count);
    }
    
    /* Frequency display */
    if (s_mode_label) {
        if (freq_valid) {
            int32_t freq_d1 = (int32_t)(measured_freq * 10);  /* Hz * 10 → 整数存储，0.1 Hz 精度 */
            /* 只印 "Freq: 100.6 Hz"：240 px 宽的屏上 "(Target: 100 Hz)" 会被切掉，
             * 而「是否达标」由底部状态栏的 100Hz:OK/ERR 负责说明。 */
            lv_label_set_text_fmt(s_mode_label, "Freq: %" PRId32 ".%" PRId32 " Hz",
                                  freq_d1 / 10, freq_d1 % 10);
        } else {
            lv_label_set_text(s_mode_label, collecting ? "Mode: RUN" : "Mode: STOP");
        }
    }

    /* Face display — strip "Face: " prefix from s_face_name, then prepend it back */
    const char *face_text = s_face_name;
    if (strncmp(face_text, "Face: ", 6) == 0) {
        face_text += 6;
    }
    lv_label_set_text_fmt(s_face_label, "Face: %s", face_text);
    
    /* Acceleration magnitude display - always show during collecting */
    /* This is used for calibration verification (target: ~9.81 m/s²) */
    float mag = sqrtf(ax * ax + ay * ay + az * az);
    int32_t mag_mm = (int32_t)(mag * 1000);
    if (s_accel_mag_label) {
        lv_label_set_text_fmt(s_accel_mag_label, "Mag: %" PRId32 " mm/s^2", mag_mm);
    }
    
    /* Calibration mode specific display - show remaining time */
    /* This is already handled by s_face_name update in sampler_task */
    
    /* Stand/Stairs/Bend/Jump/Fall protocol progress display */
    if (s_stand_label_ui) {
        char status_buf[64];
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(20)) == pdTRUE) {
            if (s_fall_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_fall_display);
            } else if (s_jump_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_jump_display);
            } else if (s_bend_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_bend_display);
            } else if (s_stairs_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_stairs_display);
            } else if (s_stand_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_stand_display);
            } else if (s_loop_state != LOOP_IDLE && s_loop_status_text[0]) {
                /* 闭环事件进行中：这一行让给闭环状态（LOOP:TRIG / WAIT / DONE…）。
                 * 优先于 s_status_text，是因为闭环状态是"用户刚刚按了按键"的直接
                 * 反馈，必须立刻看得见；而 s_status_text 多是启动期的联网/对时信息，
                 * 晚一点再看也不影响。注意：5 种动作协议运行时这一行归协议显示，
                 * 闭环文本会被盖住（见 docs/loop_trigger_callback.md 的边界说明）。 */
                snprintf(status_buf, sizeof(status_buf), "%s", s_loop_status_text);
            } else {
                /* 没有协议在跑时，这一行显示最后一条 set_status_locked() 状态
                 * （"WiFi: waiting for IP" / "Time synced" / "SD card ready" …）。
                 * 这些文本以前只写不读：LCD 上看不到任何联网/启动信息，现场只能靠
                 * 串口判断。没有状态文本时退回 "IDLE"。 */
                /* 这里只拷前 63 字节：s_status_text 比这一行宽（UI_TEXT_LEN），
                 * 用 "%s" 会被 -Werror=format-truncation 拦下。状态行本来就只有
                 * 一行的宽度，截断是预期行为。 */
                snprintf(status_buf, sizeof(status_buf), "%.63s",
                         s_status_text[0] ? s_status_text : "IDLE");
            }
            xSemaphoreGive(s_state_mutex);
        } else {
            snprintf(status_buf, sizeof(status_buf), "---");
        }
        lv_label_set_text(s_stand_label_ui, status_buf);
    }

    /* Status bar at bottom */
    const char *freq_status;
    if (freq_valid) {
        int32_t freq_diff_d2 = abs((int32_t)((measured_freq - TARGET_SAMPLE_FREQ_HZ) * 100));
        freq_status = (freq_diff_d2 < 500) ? "100Hz:OK" : "100Hz:ERR";  /* ±5.00 Hz * 100 */
    } else {
        freq_status = "100Hz:--";
    }
    /* 长按 Button A = 实时直播开关，状态打在状态栏上（不看 Web 页也能确认）。
     * 240 px 宽只放得下诊断信息（SD / 采样频率 / 直播）；旧的
     * "A:Stop A2s:Live:… B2s:Mode SD:…" 比一屏还长，后半截永远看不到，
     * 而按键提示在 docs / 板面丝印上已有说明。 */
    lv_label_set_text_fmt(s_status_bar, "SD:%s %s Live:%s",
                          sd_ready ? "OK" : "---", freq_status,
                          s_live_streaming ? "ON" : "OFF");

    bsp_display_unlock();
}

/* ================================================================
 *  LVGL UI
 * ================================================================ */
static void create_ui(void)
{
    s_ui_ready = false;  /* Reset in case this is called again */
    bsp_display_start();
    ESP_ERROR_CHECK(bsp_display_backlight_on());

    /* 0 = block indefinitely until lock is acquired */
    if (!bsp_display_lock(0)) {
        ESP_LOGE(TAG, "Failed to lock display for UI creation");
        return;
    }

    lv_obj_t *scr = lv_disp_get_scr_act(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(scr, lv_color_hex(0xE6E6E6), 0);

    /* 10 行内容必须全部落在 240x240 的屏内（BSP: esp32_s3_eye）。
     * 旧坐标最后两行在 y=230 / y=255：y=255 的底部状态栏整行在屏外、
     * y=230 的协议行只剩半行 —— 现场「LCD 状态栏看不到」的根因就在坐标，
     * 不在状态文本。现在按 22 px 行距重排，最底一行 y=210（底边 226）。 */

    /* Title */
    s_title_label = lv_label_create(scr);
    lv_label_set_text(s_title_label, "IMU Time Logger");
    lv_obj_set_style_text_color(s_title_label, lv_color_hex(0x6EE7FF), 0);
    lv_obj_align(s_title_label, LV_ALIGN_TOP_LEFT, 10, 6);

    /* State (+ NET token, filled in by refresh_ui) */
    s_state_label = lv_label_create(scr);
    lv_obj_align(s_state_label, LV_ALIGN_TOP_LEFT, 10, 28);

    /* Time (+ NTP token while unsynced) */
    s_time_display = lv_label_create(scr);
    lv_obj_align(s_time_display, LV_ALIGN_TOP_LEFT, 10, 50);

    /* Acceleration axes - each on separate line */
    s_accel_x_label = lv_label_create(scr);
    lv_label_set_text(s_accel_x_label, "Accel X: 0.000 m/s²");
    lv_obj_align(s_accel_x_label, LV_ALIGN_TOP_LEFT, 10, 75);

    s_accel_y_label = lv_label_create(scr);
    lv_label_set_text(s_accel_y_label, "Accel Y: 0.000 m/s²");
    lv_obj_align(s_accel_y_label, LV_ALIGN_TOP_LEFT, 10, 97);

    s_accel_z_label = lv_label_create(scr);
    lv_label_set_text(s_accel_z_label, "Accel Z: 0.000 m/s²");
    lv_obj_align(s_accel_z_label, LV_ALIGN_TOP_LEFT, 10, 119);

    /* Samples and mode */
    s_samples_label = lv_label_create(scr);
    lv_obj_align(s_samples_label, LV_ALIGN_TOP_LEFT, 10, 144);

    s_mode_label = lv_label_create(scr);
    lv_obj_align(s_mode_label, LV_ALIGN_TOP_LEFT, 140, 144);

    /* Face display for calibration */
    s_face_label = lv_label_create(scr);
    lv_label_set_text(s_face_label, "Face: IDLE");
    lv_obj_align(s_face_label, LV_ALIGN_TOP_LEFT, 10, 166);

    /* Acceleration magnitude display for calibration verification */
    s_accel_mag_label = lv_label_create(scr);
    lv_label_set_text(s_accel_mag_label, "Mag: --.- m/s^2");
    lv_obj_align(s_accel_mag_label, LV_ALIGN_TOP_LEFT, 140, 166);

    /* Stand/Stairs protocol progress display
     * (idle → last set_status_locked() message, see refresh_ui) */
    s_stand_label_ui = lv_label_create(scr);
    lv_label_set_text(s_stand_label_ui, "IDLE");
    lv_obj_set_style_text_color(s_stand_label_ui, lv_color_hex(0xFFD700), 0);
    lv_obj_align(s_stand_label_ui, LV_ALIGN_TOP_LEFT, 10, 188);

    /* Status bar at bottom */
    s_status_bar = lv_label_create(scr);
    lv_obj_align(s_status_bar, LV_ALIGN_TOP_LEFT, 10, 210);

    bsp_display_unlock();

    s_ui_ready = true;
}

/* ================================================================
 *  Wi-Fi + SNTP (from tcp_server)
 * ================================================================ */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_CONNECTED) {
        /* L2 关联成功，接下来等 DHCP 分配地址。这里把「关联」单独记下来，
         * wifi_health_check() 才能发现「关联上了却一直拿不到 IP」。 */
        s_wifi_link_up = true;
        s_link_up_us = esp_timer_get_time();
        ESP_LOGI(TAG, "WiFi associated (waiting for DHCP lease)");
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *disc = (wifi_event_sta_disconnected_t *)event_data;
        /* Update WiFi status immediately so UI reflects disconnect */
        s_wifi_link_up = false;
        s_link_up_us = 0;
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
            s_wifi_connected = false;
            set_status_locked("WiFi disconnected");
            xSemaphoreGive(s_state_mutex);
        }
        /* Do NOT call refresh_ui() here — this runs in the esp_event task (stack ~4KB),
         * while lv_label_set_text_fmt() needs much more stack. The sampler_task
         * (runs every 10ms) will pick up the state change automatically. */
        s_retry_num++;
        if (s_retry_num <= EXAMPLE_ESP_MAXIMUM_RETRY) {
            /* 前几次立刻重连：恢复得快，和 IDF 例程一致 */
            esp_wifi_connect();
            ESP_LOGI(TAG, "Retrying WiFi connection... (%d/%d) reason=%d",
                     s_retry_num, EXAMPLE_ESP_MAXIMUM_RETRY, disc ? disc->reason : -1);
        } else {
            /* 超过上限也绝不放弃：这里只降噪 + 解除启动阶段的等待，
             * 之后由 sampler_task 里的 wifi_health_check() 每 5 s 继续重连。
             * （旧实现在这里直接停手，一次几秒的抖动就变成永久离线，只能重启板子。） */
            if (s_retry_num == EXAMPLE_ESP_MAXIMUM_RETRY + 1 || (s_retry_num % 10) == 0) {
                ESP_LOGW(TAG, "WiFi still down after %d attempts (reason=%d) - background retry every 5 s",
                         s_retry_num, disc ? disc->reason : -1);
            }
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        s_dhcp_restart_count = 0;
        s_dhcp_giveup_count = 0;
        /* 关键修复：GOT_IP 之后必须把联网标志恢复回来。旧实现只在 wifi_init_sta()
         * 里置过一次 true，于是任何一次断连（哪怕几秒后重连成功）都会让周期上传、
         * 网页任务轮询、直播推流永久停摆，而日志却显示 WiFi 已经连上。 */
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
            s_wifi_connected = true;
            s_wifi_link_up = true;
            set_status_locked("WiFi connected");
            xSemaphoreGive(s_state_mutex);
        }
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        log_heap("after wifi got ip");
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_LOST_IP) {
        s_wifi_connected = false;
        ESP_LOGW(TAG, "WiFi lost IP (DHCP lease lost) - waiting for a new lease");
    }
}

static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    /* 保存句柄：DHCP 卡住时要靠它重启 DHCP 客户端（见 wifi_health_check()） */
    s_sta_netif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler,
                                                        NULL, NULL));
    /* 租约丢失（路由器换 IP / DHCP 过期）也要反映到状态里，否则板子会一直
     * 以为自己在网，上传却全部超时。 */
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_LOST_IP,
                                                        &wifi_event_handler,
                                                        NULL, NULL));

    /* 认证门槛保持 WPA2_PSK（2026-10-05 现场实测修正）：
     * 现场热点实测是纯 WPA2-PSK，16:43 的串口日志为证（SSID/BSSID 已脱敏，
     * 真实值只留在本机那份 git-ignored 的 sdkconfig 里）：
     *     wifi:connected with <ssid>, aid = 1, channel 6, BW20,
     *     bssid = <bssid>
     *     wifi:security: WPA2-PSK, phy: bgn, rssi: -43
     * （整段日志里一条 reason= 都没有。）
     *
     * 这里**不能**抬到 WIFI_AUTH_WPA2_WPA3_PSK：IDF 的强度表
     * （esp_wifi_types_generic.h:80-82）是
     *     OPEN < WEP < WPA_PSK < OWE < WPA2_PSK = WPA_WPA2_PSK
     *     < WAPI_PSK < WPA3_PSK = WPA2_WPA3_PSK < DPP
     * 即 WPA2_WPA3_PSK 属于 WPA3 档；而 threshold.authmode 的语义是
     * 「fast scan 里可接受的**最弱**认证」（esp_wifi_types_generic.h:364）。
     * 门槛设到 WPA3 档 = 要求 AP 至少是 WPA2/WPA3 混合，纯 WPA2 的热点反而
     * 低于门槛，可能在扫描阶段就被判成「同名 AP 但安全不兼容」。WPA2-only 的
     * AP 就用 WPA2_PSK；只有确认 AP 广播 WPA2/WPA3 混合时才换成混合值。
     *
     * pmf_cfg.capable=true 只把 STA 的 MFPC 能力写进 RSN IE（required=false
     * = 不强制），纯 WPA2 的老 AP 照旧可连，保留它没有副作用。
     * S3 的 SAE 支持来自 CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y（sdkconfig 已开）。 */
    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
            .pmf_cfg = { .capable = true, .required = false },
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi auth threshold: WPA2_PSK + PMF capable");

    /* 关掉 modem sleep：本项目是持续取样 + 周期上传 + 500 ms 推流，
     * 省电没有意义；而默认的 WIFI_PS_MIN_MODEM 会让 RTT 抖动到 200 ms 以上
     * （同网段另一块 ESP32 实测 ping 225~256 ms），正在推流的连接更容易超时。 */
    esp_err_t ps_ret = esp_wifi_set_ps(WIFI_PS_NONE);
    ESP_LOGI(TAG, "WiFi power save: WIFI_PS_NONE (%s)", esp_err_to_name(ps_ret));

    ESP_LOGI(TAG, "Connecting to WiFi: %s", WIFI_SSID);

    /* 有上限的等待：如果 DHCP 一直不发地址，也必须让 SD/IMU/相机/按键/HTTP 服务
     * 和各个任务继续启动（板子至少离线可用），联网由 wifi_health_check() 在后台救。
     * 旧实现在这里用 portMAX_DELAY 死等，于是「关联成功但拿不到 IP」直接把整个
     * app_main() 卡死，现场表现就是串口再也没有日志、网页和相机完全没反应。 */
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi connected: %s", WIFI_SSID);
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            s_wifi_connected = true;
            set_status_locked("WiFi connected");
            xSemaphoreGive(s_state_mutex);
        }
    } else if (bits & WIFI_FAIL_BIT) {
        ESP_LOGE(TAG, "WiFi failed: %s (background retry keeps running)", WIFI_SSID);
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            s_wifi_connected = false;
            set_status_locked("WiFi failed - retrying");
            xSemaphoreGive(s_state_mutex);
        }
    } else {
        /* 超时：实测最常见的形态是「已经关联上、但 DHCP 一直没发租约」 */
        esp_netif_ip_info_t ip_info = {0};
        if (s_sta_netif && esp_netif_get_ip_info(s_sta_netif, &ip_info) == ESP_OK) {
            ESP_LOGW(TAG, "WiFi: no IP after %d s (link_up=%d ip=" IPSTR
                          ") - booting offline, DHCP recovery runs in background",
                     WIFI_CONNECT_TIMEOUT_MS / 1000, (int)s_wifi_link_up, IP2STR(&ip_info.ip));
        } else {
            ESP_LOGW(TAG, "WiFi: no IP after %d s (link_up=%d) - booting offline, "
                          "DHCP recovery runs in background",
                     WIFI_CONNECT_TIMEOUT_MS / 1000, (int)s_wifi_link_up);
        }
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            s_wifi_connected = false;
            set_status_locked("WiFi: waiting for IP");
            xSemaphoreGive(s_state_mutex);
        }
    }

    refresh_ui();
}

/* ================================================================
 *  SNTP 对时（自动重试、非阻塞）
 *
 *  旧实现的三个问题（现场表现就是 LCD 的 Time: 永远停在 1970 / SNTP FAILED）：
 *   1) 只在 app_main() 开机时同步一次。开机瞬间还没拿到 IP（DHCP 慢/没网）就会
 *      失败，之后网络恢复也再不同步，只能重启 —— 而重启还是同样的顺序，永远失败。
 *   2) 失败前死等 30×2 s：离线开机时整个启动流程被卡 60 s（SD/IMU/相机/HTTP
 *      全部晚起），wifi_health_check() 又在 sampler_task 里，而 sampler_task 是
 *      最后一个被创建的任务 → 自愈也被一起推迟。
 *   3) 失败后把 s_time_str 写成固定的 "SNTP FAILED" 就再也不更新：现场无法区分
 *      「板子还在跑、只是没对上时间」和「板子卡死了」。
 *
 *  现在：initialize_sntp() 只设时区 + 建任务后立刻返回；sntp_sync_task 在后台
 *  「有 IP 才尝试 → 等 10 s → 失败就退避重试（5 s 翻倍，上限 60 s，永不放弃）
 *   → 成功就刷新 LCD 时间并每 30 s 复查」。重试用 esp_netif_sntp_start()，它内部
 *  是 sntp_stop() + sntp_init()，即真正重新解析域名并发一次新请求（否则
 *  sntp_init() 在 PCB 已存在时会直接返回，不会再发请求）。
 * ================================================================ */

/* epoch 小于此值即视为「没同步」：未同步时 time() 是从 1970 起算的上电时长，
 * 量级只有几十~几万秒。1600000000 = 2020-09-13，远小于任何真实的当前时间。 */
#define SNTP_MIN_VALID_EPOCH_S   1600000000

#define SNTP_SYNC_ATTEMPT_MS     10000    /* 单次尝试最多等 10 s */
#define SNTP_RETRY_MIN_MS        5000     /* 首次失败后 5 s 重试 */
#define SNTP_RETRY_MAX_MS        60000    /* 退避上限 60 s（一直重试，不放弃） */
#define SNTP_RESYNC_CHECK_MS     30000    /* 已同步后的复查周期 */
#define SNTP_TASK_STACK          4096
#define SNTP_TASK_PRIO           4

/* 把「当前时间」格式化成 LCD 「Time:」字段用的字符串：
 *   已同步 → "2026-09-23 10:19:59"（北京时间，时区由 initialize_sntp() 设定）
 *   未同步 → "-- (up 00:03:12)"（自启动以来的运行时间）
 * 未同步时不写死一句话：字段每秒都在动，现场一眼就能判断「板子是活的、只是没
 * 对上时间」，同时 uptime 也能直接看出期间有没有掉电重启。 */
static void format_time_str(char *out, size_t out_len)
{
    time_t now = time(NULL);
    if (now >= SNTP_MIN_VALID_EPOCH_S) {
        struct tm timeinfo;
        if (localtime_r(&now, &timeinfo) != NULL) {
            strftime(out, out_len, "%Y-%m-%d %H:%M:%S", &timeinfo);
            return;
        }
    }
    uint64_t up_s = (uint64_t)(esp_timer_get_time() / 1000000);
    snprintf(out, out_len, "-- (up %02u:%02u:%02u)",
             (unsigned)(up_s / 3600), (unsigned)((up_s / 60) % 60),
             (unsigned)(up_s % 60));
}

/* 刷新 s_time_str / s_time_synced（LCD 时间字段的唯一数据源，见 refresh_ui()）。
 * 由采样循环每 1 s 调用一次 + SNTP 任务同步成功时调用；只在值变化时才写。 */
static void refresh_time_str(void)
{
    char buf[64];
    format_time_str(buf, sizeof(buf));
    bool synced = (time(NULL) >= SNTP_MIN_VALID_EPOCH_S);

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) != pdTRUE) {
        return;
    }
    if (strcmp(buf, s_time_str) != 0) {
        snprintf(s_time_str, sizeof(s_time_str), "%s", buf);
    }
    s_time_synced = synced;
    xSemaphoreGive(s_state_mutex);
}

static void time_sync_notification_cb(struct timeval *tv)
{
    /* 在 tcpip 线程里被回调：只打日志（系统时间已由 lwIP 设好，
     * LCD/状态由 sntp_sync_task 负责刷新）。把同步到的时刻打进日志，
     * 现场用串口就能确认板子时间对不对，不必去看网页。 */
    char buf[64] = {0};
    struct tm timeinfo;
    time_t sec = tv ? (time_t)tv->tv_sec : time(NULL);
    if (localtime_r(&sec, &timeinfo) != NULL) {
        strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &timeinfo);
    }
    ESP_LOGI(TAG, "SNTP time synchronized: %s (UTC+8)", buf);
}

/* SNTP 同步任务：永不放弃地重试，成功后持续刷新 LCD 时间。
 * 只在「有 IP」时发请求（没有 IP 时 DNS/NTP 都不可达，重试只是白费力气）。 */
static void sntp_sync_task(void *arg)
{
    (void)arg;

    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(SNTP_SERVER);
    config.start = false;      /* 首次请求等本任务确认「有 IP」之后再发 */
    config.sync_cb = time_sync_notification_cb;

    bool inited = false;
    int retry_ms = SNTP_RETRY_MIN_MS;

    ESP_LOGI(TAG, "SNTP task started: server=%s, retry %d ms -> %d ms",
             SNTP_SERVER, SNTP_RETRY_MIN_MS, SNTP_RETRY_MAX_MS);

    while (true) {
        /* 已同步：lwIP 会按 SNTP_UPDATE_DELAY(1 h) 自动再同步，这里只把时间刷到
         * LCD 上，并低频复查（掉电/时钟被改时能及时发现并回到重试分支）。 */
        if (time(NULL) >= SNTP_MIN_VALID_EPOCH_S) {
            bool was_synced = false;
            if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
                was_synced = s_time_synced;
                xSemaphoreGive(s_state_mutex);
            }
            refresh_time_str();
            if (!was_synced) {
                if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
                    set_status_locked("Time synced");
                    xSemaphoreGive(s_state_mutex);
                }
                refresh_ui();      /* 同步成功：立刻把正确时间画到 LCD 上 */
            }
            retry_ms = SNTP_RETRY_MIN_MS;
            vTaskDelay(pdMS_TO_TICKS(SNTP_RESYNC_CHECK_MS));
            continue;
        }

        /* 未同步：先等 Wi-Fi 拿到 IP（DHCP 成功），否则 SNTP 连域名都解析不了 */
        if (!s_wifi_connected || !s_wifi_link_up) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        if (!inited) {
            esp_err_t err = esp_netif_sntp_init(&config);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "esp_netif_sntp_init failed: %s - retry in %d ms",
                         esp_err_to_name(err), SNTP_RETRY_MIN_MS);
                vTaskDelay(pdMS_TO_TICKS(SNTP_RETRY_MIN_MS));
                continue;
            }
            inited = true;
        } else {
            /* 显式重启客户端 = sntp_stop() + sntp_init()：立刻重新解析域名并发出
             * 一次真正的新请求（lwIP 自己的重试节奏是 15 s 且不可观测）。 */
            esp_netif_sntp_start();
        }

        esp_err_t err = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(SNTP_SYNC_ATTEMPT_MS));
        if (err == ESP_OK || time(NULL) >= SNTP_MIN_VALID_EPOCH_S) {
            continue;   /* 下一轮循环开头统一刷新 LCD / 状态文本 */
        }

        ESP_LOGW(TAG, "SNTP no sync yet (%s) - next try in %d ms (link_up=%d ip=%d)",
                 esp_err_to_name(err), retry_ms, (int)s_wifi_link_up,
                 (int)s_wifi_connected);
        vTaskDelay(pdMS_TO_TICKS(retry_ms));
        retry_ms = (retry_ms * 2 > SNTP_RETRY_MAX_MS) ? SNTP_RETRY_MAX_MS : retry_ms * 2;
    }
}

/* 只做「设时区 + 起后台对时任务」，不阻塞。
 * 旧实现（死等 30×2 s + 只尝试一次）是「板子开机后 LCD 时间永远不对」的直接原因：
 * 离线/慢 DHCP 开机时它既浪费 60 s 启动时间、又注定失败且不再重试。 */
static void initialize_sntp(void)
{
    /* 时区必须早于任何时间格式化：POSIX 的 TZ 符号与直觉相反，CST-8 = UTC+8 */
    setenv("TZ", "CST-8", 1);
    tzset();

    /* 先把 LCD 的 Time 字段填上（未同步时是 uptime），保证开机就有内容 */
    refresh_time_str();
    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
        set_status_locked("Syncing time (SNTP)");
        xSemaphoreGive(s_state_mutex);
    }
    refresh_ui();

    ESP_LOGI(TAG, "Initializing SNTP (non-blocking): server=%s, first request waits for an IP",
             SNTP_SERVER);
    xTaskCreate(sntp_sync_task, "sntp_sync", SNTP_TASK_STACK, NULL, SNTP_TASK_PRIO, NULL);
}

/* ================================================================
 *  QMA6100P Accelerometer (on the shared BSP I2C bus)
 *
 *  这里不再使用 espressif/qma6100p 组件：它基于 LEGACY I2C 驱动
 *  (driver/i2c.h)，而相机 SCCB 需要 driver/i2c_master.h，两者不能共存——
 *  esp-idf 的 legacy 驱动构造器 check_i2c_driver_conflict() 一旦发现新驱动
 *  被链接进镜像就会在启动时 abort()。所以 QMA6100P 改为直接用 i2c_master
 *  挂到 BSP 总线上（CONFIG_BSP_I2C_NUM=1, GPIO4/5, 400 kHz），与相机共用
 *  同一条总线（IMU 0x12/0x13 + 传感器 0x30，互不干扰）。
 *
 *  寄存器序列/量纲与 qma6100p 组件 1:1 对齐，保证改造前后读数一致：
 *    WHO_AM_I(0x00) == 0x90 → PWR_MGMT_1(0x11) |= BIT7 唤醒
 *    → ACCEL_CONFIG(0x0F) 低 4 位 = 0b0001（±2g）
 *    读数：XOUT_H(0x01) 起 6 字节小端；raw = int16 / 4；raw / 4096 → 单位 g
 *  采样器随后乘 GRAVITY_ACCEL(9.80665) 转 m/s² 再上报。
 * ================================================================ */
/* I2C pins from BSP config */
#define ACCEL_I2C_SDA             BSP_I2C_SDA    /* GPIO_NUM_4 */
#define ACCEL_I2C_SCL             BSP_I2C_SCL    /* GPIO_NUM_5 */
#define ACCEL_I2C_FREQ_HZ         400000

#define QMA6100P_REG_WHO_AM_I     0x00u
#define QMA6100P_REG_ACCEL_XOUT_H 0x01u
#define QMA6100P_REG_ACCEL_CONFIG 0x0Fu
#define QMA6100P_REG_PWR_MGMT_1   0x11u
#define QMA6100P_WHO_AM_I_EXPECT  0x90u
#define QMA6100P_ACCEL_FS_2G      0x01u
#define QMA6100P_ACCEL_LSB_PER_G  4096.0f   /* ±2g：raw/4 之后每 g 的 LSB 数 */
#define ACCEL_I2C_TIMEOUT_MS      100

/* 单寄存器写（寄存器地址 + 数据，一次传输） */
static esp_err_t accel_write_reg(uint8_t reg, uint8_t value)
{
    const uint8_t payload[2] = { reg, value };
    return i2c_master_transmit(s_accel, payload, sizeof(payload),
                               ACCEL_I2C_TIMEOUT_MS);
}

/* 从 reg 起连续读 len 字节（写地址 + 重复起始 + 读，i2c_master 内部完成） */
static esp_err_t accel_read_regs(uint8_t reg, uint8_t *out, size_t len)
{
    return i2c_master_transmit_receive(s_accel, &reg, 1, out, len,
                                       ACCEL_I2C_TIMEOUT_MS);
}

static esp_err_t app_accel_init(void)
{
    /* BSP 总线，与相机 SCCB 共用：bsp_i2c_init() 幂等（已初始化时直接返回
     * ESP_OK），所以先于 app_camera_init() 建好这条总线是安全的。 */
    esp_err_t ret = bsp_i2c_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "bsp_i2c_init failed: %s", esp_err_to_name(ret));
        return ret;
    }
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (bus == NULL) {
        ESP_LOGE(TAG, "BSP I2C bus handle is NULL");
        return ESP_FAIL;
    }

    /* AD0 拉低 → 0x12，拉高 → 0x13（保持与旧代码相同的探测顺序） */
    const uint8_t probe_addrs[] = { 0x12, 0x13 };

    for (size_t i = 0; i < sizeof(probe_addrs); i++) {
        i2c_device_config_t dev_cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address  = probe_addrs[i],
            .scl_speed_hz    = ACCEL_I2C_FREQ_HZ,
        };
        i2c_master_dev_handle_t dev = NULL;
        ret = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "i2c_master_bus_add_device(0x%02X) failed: %s",
                     probe_addrs[i], esp_err_to_name(ret));
            continue;
        }

        s_accel = dev;     /* 下面的读写辅助函数需要一个有效句柄 */
        uint8_t device_id = 0;
        if (accel_read_regs(QMA6100P_REG_WHO_AM_I, &device_id, 1) != ESP_OK
            || device_id != QMA6100P_WHO_AM_I_EXPECT) {
            i2c_master_bus_rm_device(dev);
            s_accel = NULL;
            continue;
        }
        ESP_LOGI(TAG, "Detected QMA6100P at 0x%02X, ID 0x%02X (i2c_master driver)",
                 probe_addrs[i], device_id);

        /* 唤醒（PWR_MGMT_1 BIT7）后选 ±2g，与旧库 qma6100p_wake_up()/config() 一致 */
        uint8_t pwr = 0;
        if (accel_read_regs(QMA6100P_REG_PWR_MGMT_1, &pwr, 1) != ESP_OK
            || accel_write_reg(QMA6100P_REG_PWR_MGMT_1, (uint8_t)(pwr | 0x80)) != ESP_OK) {
            ESP_LOGE(TAG, "QMA6100P wake up failed");
            i2c_master_bus_rm_device(dev);
            s_accel = NULL;
            continue;
        }
        uint8_t cfg = 0;
        if (accel_read_regs(QMA6100P_REG_ACCEL_CONFIG, &cfg, 1) != ESP_OK
            || accel_write_reg(QMA6100P_REG_ACCEL_CONFIG,
                               (uint8_t)((cfg & 0xF0) | QMA6100P_ACCEL_FS_2G)) != ESP_OK) {
            ESP_LOGE(TAG, "QMA6100P range config failed");
            i2c_master_bus_rm_device(dev);
            s_accel = NULL;
            continue;
        }
        ESP_LOGI(TAG, "QMA6100P ready: +-2g, %u LSB/g (raw/4 -> /%.0f)",
                 (unsigned)(QMA6100P_ACCEL_LSB_PER_G * 4.0f),
                 (double)QMA6100P_ACCEL_LSB_PER_G);
        return ESP_OK;
    }

    ESP_LOGW(TAG, "No QMA6100P accelerometer found");
    return ESP_ERR_NOT_FOUND;
}

static esp_err_t app_accel_read(float *out_x, float *out_y, float *out_z)
{
    if (out_x == NULL || out_y == NULL || out_z == NULL || s_accel == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 6 字节小端：XOUT_H/L, YOUT_H/L, ZOUT_H/L。
     * 与 qma6100p_get_acce() 完全同量纲：raw = int16 / 4（整数除法），
     * value = raw / 4096 → 单位 g（±2g）。 */
    uint8_t data[6] = {0};
    esp_err_t ret = accel_read_regs(QMA6100P_REG_ACCEL_XOUT_H, data, sizeof(data));
    if (ret != ESP_OK) {
        return ret;
    }
    float acce_x = (float)((int16_t)((data[1] << 8) | data[0]) / 4)
                   / QMA6100P_ACCEL_LSB_PER_G;
    float acce_y = (float)((int16_t)((data[3] << 8) | data[2]) / 4)
                   / QMA6100P_ACCEL_LSB_PER_G;
    float acce_z = (float)((int16_t)((data[5] << 8) | data[4]) / 4)
                   / QMA6100P_ACCEL_LSB_PER_G;

    /* Simple low-pass filter (state reset when starting a new capture session) */
    if (!s_filter_init) {
        s_filter_x = acce_x;
        s_filter_y = acce_y;
        s_filter_z = acce_z;
        s_filter_init = true;
    } else {
        s_filter_x += ACCEL_FILTER_ALPHA * (acce_x - s_filter_x);
        s_filter_y += ACCEL_FILTER_ALPHA * (acce_y - s_filter_y);
        s_filter_z += ACCEL_FILTER_ALPHA * (acce_z - s_filter_z);
    }

    *out_x = s_filter_x;
    *out_y = s_filter_y;
    *out_z = s_filter_z;
    return ESP_OK;
}

/* ================================================================
 *  Camera — on-demand single JPEG frame (esp_video, DVP + V4L2)
 *
 *  相机与 IMU 共用 BSP I2C 总线：相机初始化就是 bsp_i2c_init() +
 *  esp_video_init()（DVP + SCCB），因此 IMU 必须先改走 i2c_master
 *  （见上一节），两者才能共存于同一个固件。
 *  XCLK 由本文件自己起 20 MHz（见 app_camera_init()），不用 bsp_camera_start()
 *  —— BSP 把它写死成 16 MHz，OV2640 的 JPEG 格式表会因此出不了完整帧。
 *
 *  运行路径：设备只 open / REQBUFS / STREAMON 一次（= 一个「采集会话」），
 *  之后每次取帧只做 DQBUF → 拷贝 → 立刻 QBUF 归还，会话空闲
 *  CAMERA_SESSION_IDLE_MS 后才 STREAMOFF / munmap / close。
 *  为什么不再「每帧 open→STREAMON→DQBUF→STREAMOFF」：那正是随机坏帧
 *  （flags=0x41、bytesused=0）的来源，根因与修法见下面「持久会话」一节。
 *  帧缓冲由 esp_video 在 PSRAM 上按 640x480x8bit ≈ 300 KB/缓冲 分配，
 *  相机不占用采样器，也不影响周期上报。
 * ================================================================ */
static bool s_camera_ready = false;
/* 相机串行化：/dev/video0 是独占设备，实时直播的取帧与按需单帧拍照必须排队，
 * 否则两次 open→STREAMON 会互相踩（直播循环里只在「取帧」期间持锁，不含 HTTP 上传）。 */
static SemaphoreHandle_t s_camera_mutex = NULL;

typedef struct {
    uint8_t *data;      /* JPEG 帧的堆（PSRAM）副本，归本结构所有 */
    size_t   len;
    uint32_t width;
    uint32_t height;
} jpeg_frame_t;

static void jpeg_frame_free(jpeg_frame_t *frame)
{
    if (!frame) {
        return;
    }
    if (frame->data) {
        heap_caps_free(frame->data);
    }
    memset(frame, 0, sizeof(*frame));
}

/* 初始化相机（开机一次）。失败不 abort：没有相机时 IMU/上传照常工作，
 * camera 任务会得到明确的 fail 回执而不是无限等待。
 *
 * 这里刻意 **不复用 bsp_camera_start()**：它把 XCLK 固定成 BSP_CAMERA_XCLK_CLOCK_MHZ
 * (16 MHz)，而 OV2640 的 JPEG/VGA 格式表是按 20 MHz 输入校准的
 * （esp_cam_sensor/sensors/ov2640/ov2640.c: `.xclk = 20000000`）。16 MHz 下帧会退化成
 * 0 字节坏帧（DQBUF flags=V4L2_BUF_FLAG_ERROR、bytesused=0），拍照与直播同时失效——
 * 这正是 esp_video_init() 那条 "the sensor output image may be unexpected" 的真实含义。
 * 所以要复刻 BSP 的初始化流程，只把频率换成 CAMERA_XCLK_FREQ_HZ(20 MHz)。
 *
 * 两处都要给到 20 MHz，因为它们共同决定引脚上的实际波形：
 *   1) esp_cam_sensor_xclk_start()：LEDC 在 BSP_CAMERA_GPIO_XCLK(IO15) 上输出 XCLK；
 *   2) esp_video_init()：内部按 .xclk_freq 调 esp_cam_ctlr_dvp_output_clock() 设 DVP 分频。
 * 该分频必须整除（CAM_CLK_SRC_DEFAULT = SOC_MOD_CLK_PLL_D2 = 80 MHz）：
 *   80 / 20 = 4 ✓   （原来的 80 / 16 = 5 也能整除，所以相机能出帧、只是帧本身是坏的）。 */
static esp_err_t app_camera_init(void)
{
    if (!s_camera_mutex) {
        s_camera_mutex = xSemaphoreCreateMutex();
        if (!s_camera_mutex) {
            ESP_LOGE(TAG, "camera mutex create failed");
            return ESP_ERR_NO_MEM;
        }
    }

    /* 相机 SCCB 与 IMU 共用这条 I2C 总线（bsp_i2c_init() 幂等，IMU 初始化时通常已建好）。 */
    esp_err_t ret = bsp_i2c_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "bsp_i2c_init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 1. XCLK：LEDC 直出 20 MHz（等价 bsp_camera.c 的写法，只改频率）。 */
    esp_cam_sensor_xclk_handle_t xclk_handle = NULL;
    const esp_cam_sensor_xclk_config_t xclk_config = {
        .ledc_cfg = {
            .timer        = LEDC_TIMER_1,
            .clk_cfg      = LEDC_AUTO_CLK,
            .channel      = CONFIG_BSP_CAMERA_XCLK_LEDC_CH,
            .xclk_freq_hz = CAMERA_XCLK_FREQ_HZ,
            .xclk_pin     = BSP_CAMERA_GPIO_XCLK,
        },
    };
    ret = esp_cam_sensor_xclk_allocate(ESP_CAM_SENSOR_XCLK_LEDC, &xclk_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "xclk allocate failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = esp_cam_sensor_xclk_start(xclk_handle, &xclk_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "xclk start (%d Hz) failed: %s",
                 CAMERA_XCLK_FREQ_HZ, esp_err_to_name(ret));
        esp_cam_sensor_xclk_free(xclk_handle);
        return ret;
    }

    /* 2. DVP + SCCB + 传感器：引脚全部取自 BSP 宏，只把 xclk_freq 换成 20 MHz。 */
    const esp_video_init_dvp_config_t dvp_config = {
        .sccb_config = {
            .init_sccb = false,                 /* bsp_i2c_init() 已建好这条总线 */
            .i2c_handle = bsp_i2c_get_handle(),
            .freq = 400000,
        },
        .reset_pin = BSP_CAMERA_RST,            /* 板子复位脚为 NC，BSP 同 */
        .pwdn_pin  = -1,                        /* 板子无 PWDN 脚，BSP 同 */
        .dvp_pin = {
            .data_width = 8,
            .data_io = {
                BSP_CAMERA_D0, BSP_CAMERA_D1, BSP_CAMERA_D2, BSP_CAMERA_D3,
                BSP_CAMERA_D4, BSP_CAMERA_D5, BSP_CAMERA_D6, BSP_CAMERA_D7,
            },
            .vsync_io = BSP_CAMERA_VSYNC,
            .de_io    = BSP_CAMERA_HSYNC,
            .pclk_io  = BSP_CAMERA_PCLK,
            .xclk_io  = BSP_CAMERA_GPIO_XCLK,
        },
        .xclk_freq = CAMERA_XCLK_FREQ_HZ,
    };
    const esp_video_init_config_t video_config = {
        .dvp = &dvp_config,
    };
    ret = esp_video_init(&video_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_video_init failed: %s", esp_err_to_name(ret));
        esp_cam_sensor_xclk_stop(xclk_handle);
        esp_cam_sensor_xclk_free(xclk_handle);
        return ret;
    }

    s_camera_ready = true;
    ESP_LOGI(TAG, "Camera ready: %s (OV2640 DVP, JPEG, xclk=%d Hz)",
             BSP_CAMERA_DEVICE, CAMERA_XCLK_FREQ_HZ);
    return ESP_OK;
}

/* ================================================================
 *  相机采集 —— 持久会话（一次 open / STREAMON，多帧复用）
 *
 *  旧实现是「每次取帧都 open → REQBUFS → STREAMON → DQBUF 一帧 →
 *  STREAMOFF → close」。它能出图，但有一个**源码级的竞态**：
 *  DVP 控制器在 STREAMON 之后是按 VSYNC 连续采样的。我们拿到第 1 帧时，
 *  硬件已经在往第 2 个缓冲里写第 2 帧了；此时我们 STREAMOFF，而
 *  esp_video 的 esp_video_stop_capture() 会做：
 *      video->ops->stop()      // 停 DVP
 *      → 清空 stream->ready_sem
 *      → TAILQ_INIT(&stream->done_list)        // 丢掉所有「已完成」元素
 *      → esp_video_buffer_reset(stream->buffer) // 元素标回 free 且 valid_size = 0
 *  如果第 2 帧的完成回调（dvp_video_on_trans_finished →
 *  esp_video_done_buffer → esp_video_done_element，注意 done_element 只要求
 *  元素是 free）赶在 buffer_reset() 之后落地，这个元素就会被塞进**下一次
 *  会话**的 done_list，并且带着上一轮或已被清零的 valid_size。于是下一次
 *  取帧可能拿到：
 *    - 上一轮的旧帧（内容错，但看着「成功」——最坏的情况，静默错数据）；
 *    - 或被 reset 清零的元素 → DQBUF 返回
 *      flags = V4L2_BUF_FLAG_MAPPED | V4L2_BUF_FLAG_ERROR (0x41)、bytesused = 0，
 *      也就是串口上那条 "[photo] dropping bad frame flags=0x41"。
 *  每帧都开关一次设备，等于把这枚骰子每帧掷一次 —— 这正是「直播时随机
 *  坏帧 / 偶发旧帧」的机制。
 *
 *  改成**持久会话**后，一个会话只 open / REQBUFS / STREAMON 一次，
 *  之后每次取帧只做 DQBUF → 拷贝 →（立刻）QBUF 归还；空闲
 *  CAMERA_SESSION_IDLE_MS 后才由 housekeeping_task 关掉（放掉 DVP + mmap）。
 *  三条配套铁律：
 *    1) 好帧也必须**先拷贝、再立刻 QBUF 归还**：驱动只有在 queued 队列非空时
 *       才继续接收下一帧；还晚了硬件就空转，下一帧从半帧开始（NO-SOI），要
 *       等驱动内部重试 —— 同样表现为随机坏帧。归还靠前，坏帧自然消失。
 *    2) 缓冲数 3：消费端是 HTTP 上传 / JPEG 软解，慢一拍也不至于饿死 DVP。
 *    3) 锁（s_camera_mutex）只在「取帧」期间持有，HTTP 上传不占锁 ——
 *       直播与按需拍照照旧互不长时间阻塞。
 *  日志上配套给出诊断：每条会话 open/close 一行，前 CAMERA_DIAG_FRAMES 帧
 *  打印 V4L2 flags/bytesused/缓冲头 4 字节（判断 SOI=FF D8 FF）与等待时间，
 *  坏帧单独一行；遇到 0 字节元素还会打 `resync(...)`（排空了几个残留元素）与
 *  `salvaged ERROR frame ...`（自校验救回来的帧）。这样「是不是相机问题」不再靠猜。
 *
 *  2026-09-29 补：`used=0 / flags=0x41` 这类坏帧还有一个 app 侧就能自愈的来源 ——
 *  REQBUFS 不重置驱动 stream->done_list（残留元素会被当成「已完成」交回），而旧代码
 *  「一失败就关会话」恰好就是制造这些残留的动作，两者叠加成自持的 0 字节帧循环。
 *  改动与机制见 `camera_resync_locked()` 上方那段注释（A/B/C 三层）。
 * ================================================================ */
typedef struct {
    int      fd;                          /* <0 = 会话未建立 */
    uint32_t buf_count;                   /* 驱动实际给到的 mmap 缓冲数 */
    bool     streaming;
    uint8_t *map[CAMERA_BUFFER_COUNT];
    size_t   map_len[CAMERA_BUFFER_COUNT];
    uint32_t width;
    uint32_t height;
    int      dqbuf_timeout_ms;            /* 当前已下发的 DQBUF 上限 */
    int      warmup_left;                 /* 还要丢几帧（仅新会话 > 0） */
    uint32_t frames;                      /* 本会话成功取到的帧数 */
    uint32_t bad_frames;                  /* 本会话丢掉的坏帧数（驱动报 ERROR） */
    uint32_t warmup_dropped_good;         /* 暖机期被丢掉的「其实是好帧」的个数 */
    uint32_t soft_fail_streak;            /* 连续多少次没取到可用帧（软失败，决定要不要重建会话） */
    uint32_t resyncs;                     /* 本会话做过几次 done 列表重同步（排空残留元素） */
    uint32_t salvaged;                    /* 本会话有多少帧是「纠错兜底」救回来的（坏帧自校验通过） */
    uint32_t stale_frames;                /* 本会话有多少个元素「不属于本会话」（userptr 与 mmap 表对不上） */
    uint32_t bogus_len_frames;            /* 本会话有多少帧长度越界（> map_len，野值，如 0xFFFFE19F = -7777） */
    uint32_t dup_drops;                   /* 本会话有多少个「拍照兜底候选」因与上一张照片逐字节相同被丢弃 */
    int64_t  opened_us;
} camera_session_t;

static camera_session_t s_cam = { .fd = -1 };   /* 受 s_camera_mutex 保护 */
static int64_t s_cam_last_use_us  = 0;          /* 最后一次取帧时刻（空闲回收用） */
static bool    s_cam_timeout_warned = false;    /* S_DQBUF_TIMEOUT 不支持只告警一次 */

/* 诊断：打印一帧的 V4L2 元数据 + 缓冲头 4 字节（FF D8 FF = JPEG SOI）。
 *
 * 三个新字段的判读：
 *   userptr —— MMAP 模式下驱动会把 element->buffer 回填进这里
 *               （esp_video_ioctl.c:204-207），而 mmap() 返回的正是同一个指针
 *               （esp_video_mman.c → esp_video_get_element_index_payload()）。
 *               所以 owner=y（userptr == 本会话 map[idx]）⇒ 元素确实属于本会话；
 *               owner=N ⇒ done 列表里混进了上一次会话/已销毁缓冲对象的元素，
 *               它的 index/bytesused 读的都是别人的内存，一律不可信。
 *   cap     —— 本会话该缓冲的映射长度（307200）。bytesused 必须 ≤ cap：
 *              驱动能写出 0xFFFFE19F(= 4294959519 = -7777) 这种野长度的原因是
 *              trans->buflen = ELEMENT_SIZE(element) = element->video_buffer->info.size
 *              （esp_video_buffer.h:22、esp_video_dvp_device.c:158）：残留元素的
 *              video_buffer 已被释放/复用 ⇒ buflen 变野值 ⇒ esp_cam_ctlr_dvp_cam.c:754-758
 *              以 buflen 为界的夹取全部失效 ⇒ dvp_calculate_jpeg_size() 从远超缓冲的
 *              偏移上扫回一个「长度」，经 element->valid_size 交给 DQBUF。
 *   归属与长度两条检查的分工：owner 只用来**记账/诊断**（地址可能复用，会误判），
 *   真正拦死野值的是 bytesused ≤ cap —— 见 camera_grab_locked() 里的 usable 判定。 */
static void camera_log_frame_diag(const char *what, const struct v4l2_buffer *buf,
                                  const uint8_t *data, size_t cap, bool owner, int64_t waited_us)
{
    const uint8_t *p = data;
    ESP_LOGW(TAG, "[cam] %s idx=%" PRIu32 " flags=0x%08" PRIx32 " used=%" PRIu32
                  " cap=%u owner=%s userptr=0x%08" PRIx32 " wait=%" PRId64
                  "ms first4=%02X %02X %02X %02X",
             what, (uint32_t)buf->index, (uint32_t)buf->flags,
             (uint32_t)buf->bytesused, (unsigned)cap, owner ? "y" : "N",
             (uint32_t)(uintptr_t)buf->m.userptr, waited_us / 1000,
             p ? p[0] : 0, p ? p[1] : 0, p ? p[2] : 0, p ? p[3] : 0);
}

/* 就地下发 DQBUF 上限（esp_video 私有 ioctl）。同一个值不重复下发，
 * 驱动不支持时只告警一次并沿用驱动默认值。 */
static void camera_set_dqbuf_timeout_locked(int ms)
{
    if (s_cam.fd < 0 || s_cam.dqbuf_timeout_ms == ms) {
        return;
    }
    struct timeval tv = {
        .tv_sec  = ms / 1000,
        .tv_usec = (ms % 1000) * 1000,
    };
    if (ioctl(s_cam.fd, VIDIOC_S_DQBUF_TIMEOUT, &tv) != 0 && !s_cam_timeout_warned) {
        s_cam_timeout_warned = true;
        ESP_LOGW(TAG, "[cam] VIDIOC_S_DQBUF_TIMEOUT unsupported (errno=%d),"
                      " using driver default", errno);
    }
    s_cam.dqbuf_timeout_ms = ms;
}

/* ----------------------------------------------------------------------------
 * 2026-10-05 修复：还给驱动之前把整块 mmap 缓冲写 0
 *
 * 现场症状：直播/预览的 JPEG 下半幅是灰带 + 中段横向撕裂/色带（Web 上就是「花屏」）。
 *
 * 代码级根因（本轮真机 + 离线熵解码一起定出来的）：
 *   1) 本工程实际用的是 **ESP-IDF 5.4.3 自带的 DVP 驱动**
 *      （`components/esp_driver_cam/dvp/src/esp_cam_ctlr_dvp_cam.c`），
 *      不是 `espressif__esp_cam_sensor/src/driver_dvp/esp_cam_ctlr_dvp_cam.c`
 *      —— 后者被 `esp_cam_ctlr_dvp_ext.h` 的 `ESP_IDF_VERSION >= 5.5.2` 挡掉，
 *      在 5.4.3 上根本没编进固件（`build/compile_commands.json` 里搜不到它）。
 *   2) 5.4.3 的 HAL 在 `cam_hal_init()` 里执行 `cam_ll_enable_vsync_generate_eof(hw, 1)`
 *      （`components/hal/cam_hal.c:67`）：**VSYNC 直接产生 DMA 的 EOF**，也就是
 *      「一帧」的边界完全由 VSYNC 决定。对 JPEG 这种**变长、与行时序无关**的数据流，
 *      帧尾那部分字节就落在边界之外 ⇒ `esp_cam_ctlr_dvp_dma_get_recv_size()`
 *      （把 GDMA 描述符的 `dw0.length` 相加）算出来的长度**短于整帧**。
 *   3) 于是 `esp_cam_ctlr_dvp_get_jpeg_size()`（从收到的区域里往回找 FF D9）**找不到 EOI**
 *      ⇒ 返回 0 ⇒ `trans.received_size = 0` ⇒（5.4.3 这个驱动**无条件**回调
 *      `on_trans_finished`）⇒ `element->valid_size = 0` ⇒ DQBUF 交出
 *      `bytesused = 0` + `V4L2_BUF_FLAG_ERROR`(0x41)。
 *      —— 这就是历史上「used=0/flags=0x41 是常态」的真正机制（不是残留元素：
 *      5.4.3 的驱动没有 NO-EOI 日志，`ESP_LOGE` 也没有，所以串口上看不到线索）。
 *   4) app 的 C 层兜底 `camera_jpeg_span()` 会去扫**整块 307200 B 映射缓冲**；
 *      缓冲是复用的，没被本帧 DMA 覆盖的部分还是**上一帧的数据**，里面自然有
 *      上一帧的 EOI(FF D9)。自校验于是搜到「旧帧的结尾」，并把
 *      「本帧前缀 + 旧帧尾巴」当成一整帧上传 ⇒ 下半幅灰带/撕裂。
 *
 * 修法（只动 app，不碰 IDF / managed_components）：
 *   每次把 mmap 缓冲还给驱动（QBUF）之前，把**整块**缓冲写 0。这样残留区
 *   不可能再出现 FF D9，`camera_jpeg_span()` 只可能扫到**本帧 DMA 真写进去的**
 *   EOI —— 要么拿到完整帧，要么明确判为坏帧（不再把旧帧尾巴拼上来）。
 *
 * 缓冲在 PSRAM ⇒ 写完必须 C2M 同步：否则脏 cache 行会在 DMA 写完之后被回写，
 * 把刚收到的新帧数据冲掉（esp_video/驱动对同一块缓冲用的是 M2C 反向同步）。
 * -------------------------------------------------------------------------- */
static void camera_wipe_map_locked(uint32_t idx)
{
    if (idx >= s_cam.buf_count || !s_cam.map[idx] || s_cam.map_len[idx] == 0) {
        return;
    }
    memset(s_cam.map[idx], 0, s_cam.map_len[idx]);
    if (esp_ptr_external_ram(s_cam.map[idx])) {
        esp_cache_msync(s_cam.map[idx], s_cam.map_len[idx],
                        ESP_CACHE_MSYNC_FLAG_DIR_C2M);
    }
}

/* 诊断：在「写 0 过的」缓冲里找「已写入区」的边界
 * = 第一段连续 ≥ CAMERA_ZERO_RUN_MIN 个 0 的起点（= DMA 本帧实际写入的长度）。
 * 与 camera_jpeg_span() 的 span 一起看就能定性（2026-10-05 真机实测两者相等）：
 *   span == written ⇒ 本帧的 EOI 就在 DMA 写进来的数据末尾 ⇒ 帧是完整的；
 *   span == 0      ⇒ 帧尾（含 EOI）压根没进缓冲 ⇒ 帧被截断，应当判为坏帧。
 * 只在每个会话的头 CAMERA_DIAG_SALVAGE_FRAMES 个「兜底帧」上打印，避免刷屏
 * （每次调用要扫一遍缓冲，~300 KB，代价可忽略）。 */
#define CAMERA_ZERO_RUN_MIN 64
#define CAMERA_DIAG_SALVAGE_FRAMES 12
static size_t camera_zero_run_end(const uint8_t *p, size_t cap)
{
    size_t run = 0;

    for (size_t i = 0; i < cap; i++) {
        if (p[i] == 0) {
            if (++run >= CAMERA_ZERO_RUN_MIN) {
                return i + 1 - run;
            }
        } else {
            run = 0;
        }
    }
    return cap;
}

/* 关掉会话：STREAMOFF → munmap → close。必须由已持锁的调用者调用。 */
static void camera_session_close_locked(void)
{
    if (s_cam.fd < 0) {
        return;
    }
    const int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (s_cam.streaming && ioctl(s_cam.fd, VIDIOC_STREAMOFF, &type) != 0) {
        ESP_LOGW(TAG, "[cam] VIDIOC_STREAMOFF failed: errno=%d", errno);
    }
    for (uint32_t i = 0; i < CAMERA_BUFFER_COUNT; i++) {
        if (s_cam.map[i]) {
            munmap(s_cam.map[i], s_cam.map_len[i]);
            s_cam.map[i] = NULL;
        }
    }
    close(s_cam.fd);
    if (s_cam.opened_us) {
        ESP_LOGI(TAG, "[cam] session closed: frames=%" PRIu32 " bad=%" PRIu32
                      " warmup_dropped_good=%" PRIu32 " resyncs=%" PRIu32 " salvaged=%" PRIu32
                      " stale=%" PRIu32 " bogus_len=%" PRIu32 " dup=%" PRIu32 " uptime=%" PRId64 "ms",
                 s_cam.frames, s_cam.bad_frames, s_cam.warmup_dropped_good,
                 s_cam.resyncs, s_cam.salvaged, s_cam.stale_frames, s_cam.bogus_len_frames,
                 s_cam.dup_drops,
                 (esp_timer_get_time() - s_cam.opened_us) / 1000);
    }
    memset(&s_cam, 0, sizeof(s_cam));
    s_cam.fd = -1;
    s_cam_last_use_us = 0;
}

/* 建立会话：open → G_FMT（必须是 JPEG）→ S_FMT（宽高必须与传感器当前格式一致，
 * DVP 设备会校验）→ S_DQBUF_TIMEOUT → REQBUFS/QUERYBUF/mmap/QBUF → STREAMON。
 * 失败时内部自行清理（不会把半开的设备留给下一次）。必须由已持锁的调用者调用。 */
static esp_err_t camera_session_open_locked(int dqbuf_timeout_ms)
{
    const int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    struct v4l2_format format;

    memset(&s_cam, 0, sizeof(s_cam));
    s_cam.fd = open(BSP_CAMERA_DEVICE, O_RDONLY);
    if (s_cam.fd < 0) {
        ESP_LOGE(TAG, "[cam] open(%s) failed: errno=%d", BSP_CAMERA_DEVICE, errno);
        return ESP_FAIL;
    }

    /* 1. 读当前格式：默认格式由 Kconfig 决定（本项目选 OV2640 DVP JPEG 640x480）。 */
    memset(&format, 0, sizeof(format));
    format.type = type;
    if (ioctl(s_cam.fd, VIDIOC_G_FMT, &format) != 0) {
        ESP_LOGE(TAG, "[cam] VIDIOC_G_FMT failed: errno=%d", errno);
        goto fail;
    }
    if (format.fmt.pix.pixelformat != V4L2_PIX_FMT_JPEG) {
        ESP_LOGE(TAG, "[cam] sensor format 0x%08" PRIx32 " is not JPEG - enable"
                      " CONFIG_CAMERA_OV2640_DVP_JPEG_640X480_25FPS",
                 (uint32_t)format.fmt.pix.pixelformat);
        goto fail;
    }
    s_cam.width  = format.fmt.pix.width;
    s_cam.height = format.fmt.pix.height;

    memset(&format, 0, sizeof(format));
    format.type = type;
    format.fmt.pix.width       = s_cam.width;
    format.fmt.pix.height      = s_cam.height;
    format.fmt.pix.pixelformat = V4L2_PIX_FMT_JPEG;
    if (ioctl(s_cam.fd, VIDIOC_S_FMT, &format) != 0) {
        ESP_LOGE(TAG, "[cam] VIDIOC_S_FMT(%" PRIu32 "x%" PRIu32 " JPEG) failed: errno=%d",
                 s_cam.width, s_cam.height, errno);
        goto fail;
    }

    /* 2. DQBUF 等待上限（esp_video 私有扩展）。 */
    s_cam.dqbuf_timeout_ms = -1;
    camera_set_dqbuf_timeout_locked(dqbuf_timeout_ms);

    /* 3. mmap 缓冲并入队 */
    struct v4l2_requestbuffers req;
    memset(&req, 0, sizeof(req));
    req.count  = CAMERA_BUFFER_COUNT;
    req.type   = type;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(s_cam.fd, VIDIOC_REQBUFS, &req) != 0) {
        ESP_LOGE(TAG, "[cam] VIDIOC_REQBUFS failed: errno=%d", errno);
        goto fail;
    }
    if (req.count == 0 || req.count > CAMERA_BUFFER_COUNT) {
        /* 给多了我们也只用 CAMERA_BUFFER_COUNT 个（map[] 是定长数组）；
         * 给 0 个说明驱动没接受，直接失败比后面空指针好。 */
        ESP_LOGW(TAG, "[cam] REQBUFS granted %" PRIu32 " buffers (asked %u)",
                 (uint32_t)req.count, (unsigned)CAMERA_BUFFER_COUNT);
        if (req.count == 0) {
            goto fail;
        }
        req.count = CAMERA_BUFFER_COUNT;
    }
    s_cam.buf_count = req.count;

    for (uint32_t i = 0; i < s_cam.buf_count; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type   = type;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index  = i;
        if (ioctl(s_cam.fd, VIDIOC_QUERYBUF, &buf) != 0) {
            ESP_LOGE(TAG, "[cam] VIDIOC_QUERYBUF(%" PRIu32 ") failed: errno=%d", i, errno);
            goto fail;
        }
        s_cam.map[i] = (uint8_t *)mmap(NULL, buf.length, PROT_READ | PROT_WRITE,
                                       MAP_SHARED, s_cam.fd, buf.m.offset);
        if (s_cam.map[i] == MAP_FAILED) {
            s_cam.map[i] = NULL;
            ESP_LOGE(TAG, "[cam] mmap(%" PRIu32 ") failed: errno=%d", i, errno);
            goto fail;
        }
        s_cam.map_len[i] = buf.length;
        /* 新建会话的第一帧同样要防「上一轮残留」：入队前先写 0（见 camera_wipe_map_locked）。 */
        camera_wipe_map_locked(i);
        if (ioctl(s_cam.fd, VIDIOC_QBUF, &buf) != 0) {
            ESP_LOGE(TAG, "[cam] VIDIOC_QBUF(%" PRIu32 ") failed: errno=%d", i, errno);
            goto fail;
        }
    }
    if (ioctl(s_cam.fd, VIDIOC_STREAMON, &type) != 0) {
        ESP_LOGE(TAG, "[cam] VIDIOC_STREAMON failed: errno=%d", errno);
        goto fail;
    }

    s_cam.streaming   = true;
    s_cam.warmup_left = CAMERA_WARMUP_FRAMES;
    s_cam.opened_us   = esp_timer_get_time();
    ESP_LOGI(TAG, "[cam] session open: %s %" PRIu32 "x%" PRIu32 " JPEG, bufs=%" PRIu32
                  " (each %u B, PSRAM), dqbuf=%d ms",
             BSP_CAMERA_DEVICE, s_cam.width, s_cam.height, s_cam.buf_count,
             (unsigned)s_cam.map_len[0], dqbuf_timeout_ms);
    return ESP_OK;

fail:
    camera_session_close_locked();   /* opened_us == 0 → 不打 "session closed" */
    return ESP_FAIL;
}

/* ============================================================================
 * 【2026-10-05 更正】下面 2026-09-29/30 那套「done 列表残留元素」分析
 * **已经被真机推翻**，不要再照着它改代码：
 *
 *   - `used=0 / flags=0x41` 的真正机制在 **IDF 自带的 DVP 驱动**里，不在
 *     `managed_components`：
 *       * 本工程实际链接的是 ESP-IDF 5.4.3 的
 *         `components/esp_driver_cam/dvp/src/esp_cam_ctlr_dvp_cam.c`
 *         （`espressif__esp_cam_sensor` 里那份 DVP 驱动被
 *          `esp_cam_ctlr_dvp_ext.h` 的 `ESP_IDF_VERSION >= 5.5.2` 挡掉，
 *          在 5.4.3 上编译成空对象 —— 之前所有引用它的行号都作废；
 *          可用 `build/compile_commands.json` + `build/config.env` 核对）。
 *       * 该驱动的「一帧」边界由 VSYNC 决定（HAL 里
 *         `cam_ll_enable_vsync_generate_eof(hw, 1)`），收到的字节数则由
 *         `esp_cam_ctlr_dvp_dma_get_recv_size()`（把 GDMA 描述符的
 *         `dw0.length` 相加）给出；帧尾那截不在计数里 ⇒
 *         `esp_cam_ctlr_dvp_get_jpeg_size()` 扫不到 EOI ⇒ 返回 0 ⇒
 *         `trans.received_size = 0` ⇒（5.4.3 的驱动**无条件**回调
 *         `on_trans_finished`）⇒ `element->valid_size = 0` ⇒ DQBUF 交出
 *         `bytesused=0` + ERROR(0x41)。**每一帧都是这样**，与残留元素无关。
 *       * 5.4.3 的驱动没有任何 NO-EOI/NO-SOI 日志，所以串口上「看不出坏帧」。
 *   - 真正吃掉画面的不是驱动，而是 **app 自己的兜底扫描**：`camera_jpeg_span()`
 *     扫的是**整块 307200 B 映射缓冲**，而缓冲是复用的 —— 没被本帧 DMA 覆盖的
 *     部分还是**上一帧数据**，里面有上一帧的 EOI。于是自校验「找到」的其实是
 *     旧帧结尾，上传的是「本帧前缀 + 旧帧尾巴」⇒ 下半幅灰带 + 中段横向撕裂。
 *   - 修法见 `camera_wipe_map_locked()`：还给驱动之前把整块缓冲写 0，
 *     残留区不再可能出现 FF D9，自校验只可能扫到本帧 DMA 真写进去的 EOI。
 *     真机实测（2026-10-05）：`diag ... span=NNNN written=NNNN` 两者**完全相等**
 *     （9787=9787、10543=10543、10565=10565）⇒ DMA 其实把整帧（含 EOI）都写进了
 *     缓冲，只是驱动没算全 ⇒ 写 0 之后 app 拿到的是**完整帧**，不再是拼接帧。
 * ========================================================================== */

/* ----------------------------------------------------------------------------
 * 坏帧兜底 + done 列表重同步（2026-09-29）
 *
 * 现场症状：直播/预览时 `DQBUF` 周期性返回 flags=0x41 (MAPPED|ERROR)、bytesused=0，
 * 而映射缓冲里前 4 字节是 FF D8 FF E0（明明躺着一整帧 JPEG 的头）。
 *
 * 代码级定位（都在 managed_components 里可核对）：
 *   1) `V4L2_BUF_FLAG_ERROR` 只有一个来源：`esp_video_ioctl.c:196-203`
 *      `vbuf->bytesused = element->valid_size; if (!bytesused) flags |= ERROR;`
 *      ⇒ bytesused=0 等价于「驱动认为这个元素的 valid_size 是 0」。
 *   2) `valid_size` 的写点只有两处：`esp_video_done_buffer()`（控制器回调给的 n；
 *      DVP 那条路径 n>0 才回调，见 `esp_cam_ctlr_dvp_cam.c:776`）与
 *      `esp_video_buffer_reset()`（= 0，**只在 VIDIOC_STREAMOFF 里调用**）。
 *      ⇒ 0 字节元素不可能来自「DVP 正常收完一帧」，只能是**被 reset 过、
 *        或者元素对象本身已经不是本会话的了**。
 *   3) 元素对象为什么会「不是本会话的」：`esp_video_setup_buffer()`
 *      （= VIDIOC_REQBUFS）会 `esp_video_buffer_destroy()` 掉旧缓冲对象再重建
 *      （`esp_video.c:861-878`），但**既不重新初始化 stream->queued_list /
 *      done_list，也不清引用**，而这两个列表挂在 stream 上、跨会话存活；
 *      同时 `esp_video_done_element()` 只要求元素 `free == true`
 *      （`esp_video.c:1015`）。DVP 任务在 STREAMOFF/close 之后仍可能让一次完成
 *      回调落地（vTaskDelete 是异步的）⇒ **旧缓冲对象的元素会被塞进新会话的
 *      done_list**，DQBUF 从表头取到它，valid_size 就是那块内存里残留的 0
 *      → flags=0x41、bytesused=0。
 *      这也解释了「bytesused=0 却能看到 FF D8 FF E0」：index / 缓冲里都是残留值，
 *      而 app 用 buf.index 查自己的 mmap 表，读到的还是上一帧留下的数据。
 *   4) 旧代码一旦取帧失败就 `camera_session_close_locked()`（STREAMOFF + close）
 *      ——**恰恰就是制造上面那条残留的动作**。一次偶发失败因此变成自持循环。
 *
 * 处理（三层，全部在 app 侧，不动 managed_components —— 升级组件不会被冲掉）：
 *   A. 软失败（「没取到可用帧」）不再拆会话：连续 CAMERA_SOFT_FAIL_LIMIT 次才重建；
 *      硬失败（DQBUF/QBUF ioctl 报错、没内存）仍然立即重建。
 *   B. 每次取帧遇到第一个坏帧，就非阻塞排空一次驱动 done 列表并把缓冲**立刻**归还
 *      （camera_resync_locked()）：残留元素被消费掉一次，列表就干净了。
 *   C. 任何模式下，若驱动报 ERROR 但映射缓冲里确实有一整帧 JPEG
 *      （SOI 在偏移 0 + 找得到 EOI + 长度 ≥ CAMERA_JPEG_MIN_BYTES，自己校验），
 *      收下并打 WARN。拍照模式额外加一道「新鲜度」闸门（与上一张照片不同字节），
 *      防止会话切换时把上一轮的残留图当成本次照片；理由见 camera_jpeg_hash() 上方。
 *      ——「拍照不做兜底」是 2026-09-29 的旧决定，2026-09-30 真机实测证明它等于
 *      拍照 100% 失败（8/8）：驱动对**每一帧**都报 used=0，而缓冲里就是完整 JPEG。
 *
 * 2026-09-30 追加 D 层（同一个残留根因，但这次漏进的是「长度」而不是「0 字节」）：
 *   现场症状：`[cam] no heap for 4294959519 B JPEG frame` → `[live] capture failed:
 *   ESP_ERR_NO_MEM` → 会话被拆。4294959519 = 0xFFFFE19F = **−7777**，即 DQBUF 交回的
 *   `bytesused` 是野值（负数的补码），不是真的内存不够。
 *   野长度怎么来的：DVP 侧 `trans->buflen = ELEMENT_SIZE(element) =
 *   element->video_buffer->info.size`（`esp_video_buffer.h:22`、`esp_video_dvp_device.c:158`）。
 *   元素若来自**已销毁的旧缓冲对象**（上面第 3 条），`video_buffer` 指向的内存已被释放/复用
 *   ⇒ `buflen` 变成野值 ⇒ `esp_cam_ctlr_dvp_cam.c:754-758` 里所有以 buflen 为界的夹取
 *   全部失效 ⇒ `dvp_calculate_jpeg_size()` 会**从远超 307200 的偏移**上往回扫 EOI，
 *   扫到什么就是什么（0xFFFFE19F 这种值），最后经 `element->valid_size = n`
 *   （`esp_video.c:1063`）→ `DQBUF` → app。
 *   旧代码的可用性判据只有 `done && bytesused > 0`，野值于是成了「好帧」：
 *   `heap_caps_malloc(0xFFFFE19F)` 必然失败 → 被误报成 OOM → 走硬失败分支拆会话
 *   ⇒ **又制造一批残留元素**，坏帧被放大（和 A 层修掉的那个循环同源）。
 *   因此新增：
 *     D1. 长度笼子：`len_ok = bytesused ∈ (0, map_len[idx]]`，只有它成立才可能 usable；
 *         之后 malloc/memcpy/out->len 一律用夹取后的 `used`，越界读写得不可能发生。
 *     D2. 归属诊断：MMAP 模式下驱动会回填 `userptr = element->buffer`
 *         （`esp_video_ioctl.c:204-207`），而 mmap() 给 app 的正是同一个指针
 *         （`esp_video_mman.c` → `esp_video_get_element_index_payload()`）⇒
 *         `userptr == map[idx]` 就是「元素属于本会话」的判据。地址可能被复用（会误判），
 *         所以这一条**只记账 + 打日志**（`stale=`），拦野值的主力是 D1。
 *     D3. 野长度降级：不再当 OOM。直播走 C 层自校验兜底（扫描范围严格限制在 map_len 内），
 *         兜不住就当普通坏帧丢回队列 + resync 一次；`ESP_ERR_NO_MEM` 只留给
 *         「长度合法但堆真不够」的真 OOM。诊断计数：`stale=` / `bogus_len=`。
 *   根治仍在驱动侧（本项目不采用，避免被组件升级覆盖）：在 `esp_video_setup_buffer()`
 *   重建缓冲后顺手 `TAILQ_INIT()` 掉 `stream->queued_list` / `done_list`。
 * ---------------------------------------------------------------------------- */

/* 在映射缓冲里做完整性自检：SOI 必须在偏移 0，且在 cap 之内按 JPEG 段结构找得到 EOI。
 * 返回 JPEG 长度（含 EOI 两字节）；不像一帧完整 JPEG 就返回 0。
 *
 * 为什么不能裸扫 FF D9（旧实现的写法）：JPEG 是**分段**结构，段载荷里完全可能出现
 * 字节对 FF D9 —— 典型是 APPn/EXIF 里内嵌的缩略图（OV2640 输出的 JPEG 带 APP 段）。
 * 裸扫会把缩略图的 EOI 当成整帧结尾，span 于是被截短 ⇒ 拷走/上传的"这一帧"其实是
 * 半张图：HTTP 上传照样成功，但服务端存下来的图显示不完整（底部灰带/花屏）。
 * 正确做法是按段长跳转，只有进入 SOS 之后的熵编码数据才用裸扫 —— 那里 FF 后面
 * 只会跟 00 填充或 D0..D7 重启标记，不可能再出现带长度字段的段。
 * 全程读取都夹在 cap 之内（越界前一律返回 0），所以喂野缓冲也不会越界读。 */
static size_t camera_jpeg_span(const uint8_t *p, size_t cap)
{
    if (!p || cap < CAMERA_JPEG_MIN_BYTES || p[0] != 0xFF || p[1] != 0xD8) {
        return 0;
    }
    size_t i = 2;
    while (i + 1 < cap) {
        if (p[i] != 0xFF) {
            i++;
            continue;
        }
        const uint8_t marker = p[i + 1];
        if (marker == 0xD9) {                     /* EOI：帧尾就在这里 */
            const size_t len = i + 2;
            return (len >= CAMERA_JPEG_MIN_BYTES) ? len : 0;
        }
        if (marker == 0xFF || marker == 0x00) {   /* 填充字节 / 熵编码里的 FF00 转义 */
            i++;
            continue;
        }
        if (marker == 0xD8 || marker == 0x01 || (marker >= 0xD0 && marker <= 0xD7)) {
            i += 2;                               /* 无载荷标记：SOI / TEM / RSTn */
            continue;
        }
        if (i + 3 >= cap) {                       /* 段长字段本身越界 */
            return 0;
        }
        const size_t seg = ((size_t)p[i + 2] << 8) | (size_t)p[i + 3];
        if (seg < 2) {                            /* 段长含自身 2 字节，最小即 2 */
            return 0;
        }
        if (marker == 0xDA) {
            /* SOS：段头之后是熵编码数据，从这里开始只可能有 FF00 / RSTn / EOI */
            for (size_t j = i + 2 + seg; j + 1 < cap; j++) {
                if (p[j] == 0xFF && p[j + 1] == 0xD9) {
                    const size_t len = j + 2;
                    return (len >= CAMERA_JPEG_MIN_BYTES) ? len : 0;
                }
            }
            return 0;                             /* 熵编码里找不到 EOI ⇒ 不完整 */
        }
        i += 2 + seg;                             /* 跳到下一个段标记 */
    }
    return 0;
}

/* 拍照兜底的「新鲜度」指纹（FNV-1a 32）。为什么拍照路径需要它：
 * 驱动只交出 valid_size=0 的元素（见上面 2026-09-30 的实测结论），所以自校验兜底
 * 是拍照唯一能拿到图的路；而「一帧旧图混进照片」正是这条路的风险 —— 会话刚 STREAMON
 * 时读到的缓冲，可能还装着上一次会话最后那一帧。
 * 于是每次成功拍照都记下这一帧的指纹，下一个候选若逐字节相同就判为残留、丢掉再取下一帧
 * （真·DVP 连续两帧 JPEG 逐字节相同几乎不可能：同轮实测自校验长度在 8893..9028 B 抖动）。
 * 0 表示「还没记过」；故意做成 app 级静态量 —— 跨会话的残留正是要拦的对象。
 * 误判的代价只是多丢一帧（下一帧 ~40 ms 后到），不会让拍照失败。 */
static uint32_t s_photo_hash = 0;

static uint32_t camera_jpeg_hash(const uint8_t *p, size_t len)
{
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < len; i++) {
        h = (h ^ p[i]) * 16777619u;
    }
    return h ? h : 1u;
}

/* 把驱动 done 列表里的残留元素排空并**立刻 QBUF 归还**。
 * 用 0 ms 的 DQBUF 上限做非阻塞轮询：拿得到就还回去，拿不到就收手。
 * 必须由已持锁的调用者调用。 */
static void camera_resync_locked(const char *why)
{
    if (s_cam.fd < 0 || s_cam.buf_count == 0) {
        return;
    }
    const int type     = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    const int saved_ms = (s_cam.dqbuf_timeout_ms > 0) ? s_cam.dqbuf_timeout_ms
                                                      : CAMERA_DQBUF_TIMEOUT_LIVE_MS;
    uint32_t  drained  = 0;

    camera_set_dqbuf_timeout_locked(CAMERA_RESYNC_POLL_MS);   /* 0 ms = 非阻塞 */
    for (uint32_t i = 0; i < CAMERA_RESYNC_MAX_FRAMES; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type   = type;
        buf.memory = V4L2_MEMORY_MMAP;
        if (ioctl(s_cam.fd, VIDIOC_DQBUF, &buf) != 0) {
            break;                                   /* 没有待取元素了 */
        }
        if (buf.index >= s_cam.buf_count) {
            /* 残留元素的 index 越界：它不属于本会话的 mmap 表，只能在此丢掉
             * （它已经从 done 列表出队，不会再被取到）。 */
            ESP_LOGW(TAG, "[cam] resync(%s): dropping stale element idx=%" PRIu32 " (out of range)",
                     why, (uint32_t)buf.index);
            break;
        }
        drained++;
        camera_wipe_map_locked((uint32_t)buf.index);   /* 残留元素也要清干净再入队 */
        if (ioctl(s_cam.fd, VIDIOC_QBUF, &buf) != 0) {
            ESP_LOGW(TAG, "[cam] resync(%s): QBUF(idx=%" PRIu32 ") failed: errno=%d",
                     why, (uint32_t)buf.index, errno);
            break;
        }
    }
    camera_set_dqbuf_timeout_locked(saved_ms);
    if (drained) {
        s_cam.resyncs++;
        ESP_LOGW(TAG, "[cam] resync(%s): drained and returned %" PRIu32 " element(s)", why, drained);
    }
}

/* 取帧的两种用途。mode 决定 DQBUF 上限（见 CAMERA_DQBUF_TIMEOUT_*）与
 * 「ERROR 帧自救」（上面第 C 条）。 */
typedef enum {
    CAMERA_MODE_PHOTO = 0,   /* Web「拍一张照片」/ kind=camera 任务：宁等 3 s 也不失败 */
    CAMERA_MODE_LIVE,        /* 实时直播 / 本地预览：坏帧快速失败重试 */
} camera_mode_t;

/* 从会话里取一帧（好帧会**立刻**把缓冲还给驱动）。必须由已持锁的调用者调用。
 *
 * 返回：
 *   ESP_OK            —— out 里有一帧完整 JPEG（调用方负责 jpeg_frame_free()）；
 *   ESP_ERR_NOT_FOUND —— 软失败：这一轮全是坏帧/暖机帧，**会话本身仍然可用**，
 *                        调用方不要为此关会话（关会话正是制造 done 列表残留的动作）；
 *   ESP_ERR_TIMEOUT / ESP_FAIL / ESP_ERR_NO_MEM —— 硬失败，会话状态可疑，应当重建。
 *                        其中 ESP_ERR_NO_MEM 只表示「长度合法（≤ map_len）却仍然分配
 *                        失败」的真 OOM：驱动交回的野长度（如 0xFFFFE19F）会先被 D1 层
 *                        的长度笼子拦下，降级成软失败，不再冒充 OOM 去拆会话。 */
static esp_err_t camera_grab_locked(jpeg_frame_t *out, camera_mode_t mode)
{
    const int type     = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    const int attempts = CAMERA_FRAME_SKIP_MAX + CAMERA_WARMUP_FRAMES + 1;
    bool resynced      = false;

    for (int attempt = 0; attempt < attempts; attempt++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type   = type;
        buf.memory = V4L2_MEMORY_MMAP;

        const int64_t t0 = esp_timer_get_time();
        if (ioctl(s_cam.fd, VIDIOC_DQBUF, &buf) != 0) {
            ESP_LOGE(TAG, "[cam] VIDIOC_DQBUF failed after %" PRId64 " ms: errno=%d",
                     (esp_timer_get_time() - t0) / 1000, errno);
            return ESP_ERR_TIMEOUT;
        }
        const int64_t  waited_us = esp_timer_get_time() - t0;
        /* 元素归属 + 长度合法性校验（判读方法见 camera_log_frame_diag() 上方注释）：
         *   owner_ok : MMAP 模式下 userptr 必然等于本会话 map[index]（见注释）；
         *   len_ok   : bytesused 必须落在 (0, map_len[index]] 内。
         * 旧代码只判 `bytesused > 0`，于是驱动交回来的野长度（实例：0xFFFFE19F
         * = 4294959519 = -7777，即串口上的 `no heap for 4294959519 B JPEG frame`）
         * 会被当成「可用帧」直接进 heap_caps_malloc/memcpy：前者必然申请失败 →
         * 被误报成 ESP_ERR_NO_MEM → 走硬失败分支关会话重建，而关会话正是制造
         * 残留元素的动作（见 camera_resync_locked 上方注释），坏帧不但没治好还被放大。 */
        const uint32_t idx      = buf.index;
        const bool     idx_ok   = (idx < s_cam.buf_count) && (s_cam.map[idx] != NULL);
        const uint8_t *src      = idx_ok ? s_cam.map[idx] : NULL;
        const size_t   src_cap  = idx_ok ? s_cam.map_len[idx] : 0;
        const bool     owner_ok = idx_ok && ((const uint8_t *)(uintptr_t)buf.m.userptr == src);
        const bool     len_ok   = (buf.bytesused > 0) && (buf.bytesused <= src_cap);
        const bool     done     = (buf.flags & V4L2_BUF_FLAG_DONE) != 0;
        const bool     warmup   = s_cam.warmup_left > 0;
        bool           usable   = done && len_ok;   /* len_ok 已含 idx_ok 且保证 src != NULL */
        bool           rescued  = false;

        /* 归属/长度异常只记账（并在下面打诊断），当场处置交给同一套「坏帧」流程：
         * 直播先试自校验兜底，兜不住再丢回队列 + resync 一次；连败 CAMERA_SOFT_FAIL_LIMIT
         * 次才重建会话。这样野长度既不会触发 4 GB malloc，也不会引发无谓的拆会话。 */
        if (!owner_ok) {
            s_cam.stale_frames++;
        } else if (buf.bytesused > src_cap) {
            /* 只统计「长度越界」这种野值；used=0/flags=0x41 那类 0 字节坏帧不算 */
            s_cam.bogus_len_frames++;
        }

        /* 会话最开始那几帧把 V4L2 元数据和缓冲头 4 字节打出来（判断 SOI）。 */
        if (s_cam.frames + s_cam.bad_frames < CAMERA_DIAG_FRAMES) {
            camera_log_frame_diag(warmup ? "warmup frame" : "frame", &buf, src,
                                  src_cap, owner_ok, waited_us);
        }

        /* C. 兜底（直播/预览/拍照都启用）：驱动报 ERROR（V4L2 语义 = 这一帧数据不可信，
         * bytesused 常被填 0、也可能是上面那种野值），但映射缓冲里可能明明躺着
         * 一整帧 JPEG。只收自己校验过的：SOI 在偏移 0 + 找得到 EOI + 长度
         * ≥ CAMERA_JPEG_MIN_BYTES，且扫描范围严格限制在 src_cap 之内（不越界读）。
         * 拍照模式多一道新鲜度闸门（与上一张照片逐字节相同 ⇒ 残留旧图，丢掉再取），
         * 理由与代价见 camera_jpeg_hash() 上方注释。 */
        if (!usable && !warmup && idx_ok) {
            const size_t span = camera_jpeg_span(src, src_cap);
            /* 诊断（2026-10-05）：缓冲已按修复写 0，于是
             *   written = 「本帧 DMA 真正写入的长度」（第一段连续 ≥64 个 0 之前）
             *   span    = 自校验找到的 EOI 位置（0 = 没找到 ⇒ 本帧没写完）
             * 真机实测两者相等（见 camera_zero_run_end() 上方注释）。 */
            if (s_cam.salvaged < CAMERA_DIAG_SALVAGE_FRAMES) {
                const size_t written = camera_zero_run_end(src, src_cap);
                ESP_LOGW(TAG, "[cam] diag idx=%" PRIu32 " driver_used=%" PRIu32
                              " span=%u written=%u head=%02X %02X %02X %02X",
                         (uint32_t)buf.index, (uint32_t)buf.bytesused, (unsigned)span,
                         (unsigned)written, src[0], src[1], src[2], src[3]);
            }
            if (span > 0) {
                const uint32_t fp = (mode == CAMERA_MODE_LIVE) ? 0
                                                               : camera_jpeg_hash(src, span);
                if (fp != 0 && fp == s_photo_hash) {
                    s_cam.dup_drops++;   /* 与上一张照片同字节：当残留丢掉，继续取下一帧 */
                } else {
                    ESP_LOGW(TAG, "[cam] salvaged bad frame idx=%" PRIu32 ": driver used=%" PRIu32
                                  " cap=%u owner=%s flags=0x%" PRIx32 ", self-checked %u B%s",
                             (uint32_t)buf.index, (uint32_t)buf.bytesused, (unsigned)src_cap,
                             owner_ok ? "y" : "N", (uint32_t)buf.flags, (unsigned)span,
                             (mode == CAMERA_MODE_LIVE) ? "" : " (photo)");
                    buf.bytesused = (uint32_t)span;   /* 换成自校验长度，后续 malloc/memcpy 用它 */
                    usable        = true;
                    rescued       = true;
                    s_cam.salvaged++;
                    if (fp != 0) {
                        s_photo_hash = fp;
                    }
                }
            }
        }

        if (warmup) {
            /* 新会话刚 STREAMON：头几帧一律丢掉，好帧也丢（只记账，看有没有白丢）。 */
            s_cam.warmup_left--;
            if (usable) {
                s_cam.warmup_dropped_good++;
            } else {
                s_cam.bad_frames++;
            }
            camera_wipe_map_locked(idx);
            if (src && ioctl(s_cam.fd, VIDIOC_QBUF, &buf) != 0) {
                ESP_LOGE(TAG, "[cam] VIDIOC_QBUF(warmup) failed: errno=%d", errno);
                return ESP_FAIL;
            }
            continue;
        }

        if (!usable) {
            s_cam.bad_frames++;
            camera_log_frame_diag("dropping bad frame", &buf, src, src_cap, owner_ok, waited_us);
            camera_wipe_map_locked(idx);
            if (src && ioctl(s_cam.fd, VIDIOC_QBUF, &buf) != 0) {
                ESP_LOGE(TAG, "[cam] VIDIOC_QBUF(recycle) failed: errno=%d", errno);
                return ESP_FAIL;
            }
            /* B. 坏帧 ⇒ 驱动 done 列表里可能混着残留元素（valid_size = 0 的那类）。
             * 每个 grab 里只清一次：清多了会把真正的新帧也一起丢掉。 */
            if (!resynced) {
                resynced = true;
                camera_resync_locked("bad-frame");
            }
            continue;
        }

        /* 好帧（含兜底救回来的）：先拷走，再**立刻**归还。顺序不能反 —— 驱动只有
         * 在 queued 队列非空时才继续接收下一帧，还晚了硬件就空转，下一帧从半帧开始。 */
        const size_t used = buf.bytesused;   /* 已被 len_ok 夹在 (0, src_cap] 内 */
        uint8_t *copy = heap_caps_malloc(used, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!copy) {
            copy = heap_caps_malloc(used, MALLOC_CAP_8BIT);
        }
        if (!copy) {
            camera_wipe_map_locked(idx);
            ioctl(s_cam.fd, VIDIOC_QBUF, &buf);    /* 先把缓冲还回去再报错 */
            /* 走到这里 = 长度合法但堆真的不够 ⇒ 这才是货真价实的 OOM（可重建会话） */
            ESP_LOGE(TAG, "[cam] no heap for %u B JPEG frame (idx=%" PRIu32
                          " flags=0x%08" PRIx32 " cap=%u)",
                     (unsigned)used, (uint32_t)buf.index, (uint32_t)buf.flags, (unsigned)src_cap);
            return ESP_ERR_NO_MEM;
        }
        memcpy(copy, src, used);
        /* 拷走之后、入队之前把整块 mmap 缓冲写 0：下一帧的「残留区」就不可能是旧帧数据
         * （根因与代价见 camera_wipe_map_locked() 上方长注释）。 */
        camera_wipe_map_locked(idx);
        if (ioctl(s_cam.fd, VIDIOC_QBUF, &buf) != 0) {
            ESP_LOGE(TAG, "[cam] VIDIOC_QBUF(return) failed: errno=%d", errno);
            heap_caps_free(copy);
            return ESP_FAIL;
        }

        out->data   = copy;
        out->len    = used;
        out->width  = s_cam.width;
        out->height = s_cam.height;
        s_cam.frames++;
        s_cam.soft_fail_streak = 0;      /* 取到帧了，软失败连击清零 */
        if (s_cam.frames == 1) {
            /* STREAMON → 第一帧的等待时间：DVP 起振正常与否一眼可见。
             * 先判长度再取下标：len_ok 只要求 used > 0，一帧 1 字节的"好帧"也能
             * 走到这里，此时 copy[1] 是堆缓冲区外的 1 字节越界读（ASan 会直接报，
             * 裸机上则可能读到野值把 SOI 判成 yes）。 */
            const bool soi_ok = (used >= 2) && (copy[0] == 0xFF) && (copy[1] == 0xD8);
            ESP_LOGI(TAG, "[cam] first frame: %u B, wait=%" PRId64 " ms, SOI=%s",
                     (unsigned)used, waited_us / 1000, soi_ok ? "yes" : "NO");
        }
        if (rescued && !resynced) {
            /* 驱动报过 ERROR ⇒ done 列表里可能还有残留元素。要的这一帧已经拷走，
             * 顺手清一次，免得下一帧又取到 0 字节元素。 */
            camera_resync_locked("post-salvage");
        }
        return ESP_OK;
    }

    ESP_LOGE(TAG, "[cam] no usable frame in %d attempts (bad=%" PRIu32 ", resyncs=%" PRIu32
                  ", stale=%" PRIu32 ", bogus_len=%" PRIu32 ", dup=%" PRIu32 ")",
             attempts, s_cam.bad_frames, s_cam.resyncs,
             s_cam.stale_frames, s_cam.bogus_len_frames, s_cam.dup_drops);
    return ESP_ERR_NOT_FOUND;
}

/* 取一帧的两个入口（`camera_mode_t` 的说明见 camera_grab_locked() 上方）。
 * 会话按需建立：第一次取帧时 open + REQBUFS + STREAMON；空闲超过
 * CAMERA_SESSION_IDLE_MS 由 camera_session_idle_close() 关掉。
 * 成功时 out->data 由调用方用 jpeg_frame_free() 释放。 */

static esp_err_t app_camera_capture_jpeg_ex(jpeg_frame_t *out, camera_mode_t mode)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof(*out));
    if (!s_camera_ready || !s_camera_mutex) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 相机独占：只在「取帧」期间持锁（HTTP 上传不占锁），
     * 所以直播与按需拍照互相最多等一帧的时间。 */
    if (xSemaphoreTake(s_camera_mutex,
                       pdMS_TO_TICKS(CAMERA_LOCK_TIMEOUT_MS)) != pdTRUE) {
        ESP_LOGW(TAG, "[cam] busy (another capture in progress), skipped");
        return ESP_ERR_TIMEOUT;
    }

    const int want_timeout = (mode == CAMERA_MODE_LIVE)
                             ? CAMERA_DQBUF_TIMEOUT_LIVE_MS : CAMERA_DQBUF_TIMEOUT_MS;

    esp_err_t ret;
    if (s_cam.fd < 0) {
        ret = camera_session_open_locked(want_timeout);
    } else {
        /* 已有会话（多半是直播/预览留下的）：按本次用途调整等待上限 */
        camera_set_dqbuf_timeout_locked(want_timeout);
        ret = ESP_OK;
    }
    if (ret == ESP_OK) {
        ret = camera_grab_locked(out, mode);
    }
    if (ret == ESP_ERR_NOT_FOUND) {
        /* 软失败：这一轮没取到可用帧（坏帧/暖机帧用尽），**会话本身是好的**。
         * 绝不在这里关会话 —— STREAMOFF + close 正是把 DVP 完成回调变成
         * 「下一次会话 done_list 里残留元素」的那一步（见 camera_resync_locked()），
         * 旧代码「一失败就关会话」把一次偶发坏帧放大成自持的 0 字节帧循环
         * （串口上就是 bad=11 反复出现、永不恢复）。改为：留着会话就地重试，
         * 连续 CAMERA_SOFT_FAIL_LIMIT 次都不行才重建（重建本身只要一帧的钱）。 */
        s_cam.soft_fail_streak++;
        if (s_cam.soft_fail_streak >= CAMERA_SOFT_FAIL_LIMIT) {
            ESP_LOGW(TAG, "[cam] %" PRIu32 " grabs in a row without a usable frame - rebuilding session",
                     s_cam.soft_fail_streak);
            camera_session_close_locked();   /* soft_fail_streak 随会话一起清零 */
        }
    } else if (ret != ESP_OK) {
        /* 硬失败（DQBUF/QBUF ioctl 出错、没内存、会话没打开）：状态可疑，重建。 */
        camera_session_close_locked();
    }

    s_cam_last_use_us = esp_timer_get_time();   /* 空闲回收的计时基准 */
    xSemaphoreGive(s_camera_mutex);

    if (ret != ESP_OK) {
        jpeg_frame_free(out);
    }
    return ret;
}

/* 单帧拍照（Web「拍一张照片」/ kind=camera 任务）。 */
static esp_err_t app_camera_capture_jpeg(jpeg_frame_t *out)
{
    return app_camera_capture_jpeg_ex(out, CAMERA_MODE_PHOTO);
}

/* 实时直播 / 本地预览：会话保持打开，坏帧快速失败重试。 */
static esp_err_t app_camera_capture_jpeg_live(jpeg_frame_t *out)
{
    return app_camera_capture_jpeg_ex(out, CAMERA_MODE_LIVE);
}

/* 空闲回收会话：由 housekeeping_task（优先级最低）周期调用。
 * 用 0 超时拿锁 —— 采样器/推流任务正在取帧时直接跳过、下轮再看，
 * 绝不为了关相机去阻塞任何实时路径。 */
static void camera_session_idle_close(void)
{
    if (!s_camera_ready || !s_camera_mutex || s_cam.fd < 0) {
        return;
    }
    /* 直播/预览期间不回收会话。这两条路径本来就 ~50 ms 取一帧、永远不会「空闲」；
     * 能凑满 CAMERA_SESSION_IDLE_MS 的只有「HTTP 上传把节拍拖长」这种情况，
     * 而那正是最不该 STREAMOFF 的时刻：一次 open/close 要重来一遍
     * 16 KiB 内部 DMA + 3×307200 B PSRAM 的分配，还会把 dma_largest 压低
     * （2026-09-30 一轮 250 s 实测：直播/预览开着时 dma_largest 从 10240 掉到
     *  4096 B，正是 SD 写 errno=5 的前置条件）。 */
    if (s_live_streaming || s_preview_active) {
        return;
    }
    if (xSemaphoreTake(s_camera_mutex, 0) != pdTRUE) {
        return;
    }
    if (s_cam_last_use_us > 0
        && esp_timer_get_time() - s_cam_last_use_us > (int64_t)CAMERA_SESSION_IDLE_MS * 1000) {
        camera_session_close_locked();
    }
    xSemaphoreGive(s_camera_mutex);
}

/* ================================================================
 *  Local LCD camera preview（长按 Button A 三态循环的第 3 态）
 *
 *  传感器始终保持 JPEG 640x480，不做运行时格式切换（esp_video 的 DVP 设备
 *  VIDIOC_S_FMT 会拒绝尺寸/格式变化，而切格式要私有 ioctl + 传感器内部寄存器表）。
 *  这里复用 app_camera_capture_jpeg() 拍一帧 JPEG，用 esp_jpeg 软解成 RGB565、
 *  中心裁剪后画到 240x240 的 LVGL canvas，实现「把屏幕切成摄像头画面」。
 * ================================================================ */
#define CAMERA_PREVIEW_W              240
#define CAMERA_PREVIEW_H              240
#define CAMERA_PREVIEW_BYTES          (CAMERA_PREVIEW_W * CAMERA_PREVIEW_H * 2)  /* RGB565 */
#define CAMERA_PREVIEW_DECODE_BYTES   (320 * 240 * 2)  /* 640x480 JPEG 1/2 缩放 = 320x240 RGB565 */
#define CAMERA_PREVIEW_FRAME_INTERVAL_MS  50
#define CAMERA_PREVIEW_TASK_STACK     8192
#define CAMERA_PREVIEW_TASK_PRIORITY  2

static lv_obj_t *s_preview_canvas = NULL;
static uint8_t *s_preview_canvas_buf = NULL;   /* 240x240 RGB565，直接绑定给 canvas */
static uint8_t *s_preview_decode_buf = NULL;   /* 320x240 RGB565，解码中间缓冲 */
/* 最近一次解码结果的尺寸：中心裁剪要按它算偏移（见 camera_preview_blit）。
 * 只在预览任务里读写，不需要额外加锁。 */
static uint32_t s_preview_decoded_w = 0;
static uint32_t s_preview_decoded_h = 0;

/* 一帧 JPEG → RGB565 软解到 s_preview_decode_buf（**不碰 LVGL 缓冲**）。
 * 为什么不直接写 canvas 缓冲：canvas 缓冲归 LVGL 渲染线程读，跨任务写入必须在
 * bsp_display_lock 之内（见 camera_preview_blit），而软解要 30~80 ms，不该把显示锁
 * 按住那么久。因此拆成「锁外解码 + 锁内拷贝」两段。 */
static bool camera_preview_decode(const jpeg_frame_t *frame)
{
    if (!frame || !frame->data || frame->len < 16 || !s_preview_decode_buf) {
        return false;
    }
    esp_jpeg_image_cfg_t cfg = {
        .indata      = frame->data,
        .indata_size = frame->len,
        .out_format  = JPEG_IMAGE_FORMAT_RGB565,
        .out_scale   = JPEG_IMAGE_SCALE_1_2,
    };
    esp_jpeg_image_output_t out = { 0 };
    if (esp_jpeg_get_image_info(&cfg, &out) != ESP_OK ||
        out.output_len > CAMERA_PREVIEW_DECODE_BYTES) {
        return false;
    }
    cfg.outbuf      = s_preview_decode_buf;
    cfg.outbuf_size = CAMERA_PREVIEW_DECODE_BYTES;
    if (esp_jpeg_decode(&cfg, &out) != ESP_OK ||
        out.width < CAMERA_PREVIEW_W || out.height < CAMERA_PREVIEW_H) {
        return false;
    }
    s_preview_decoded_w = out.width;
    s_preview_decoded_h = out.height;
    return true;
}

/* 把最近一次解码结果中心裁剪进 LVGL canvas 的后备缓冲。
 * **必须在 bsp_display_lock 之内调用**：否则 LVGL 渲染线程可能正在读同一块内存
 * （旧写法在锁外直接 memcpy 进 canvas 缓冲，画面会撕裂/闪残帧）。 */
static void camera_preview_blit(uint8_t *dst)
{
    if (!dst || !s_preview_decode_buf ||
        s_preview_decoded_w < CAMERA_PREVIEW_W ||
        s_preview_decoded_h < CAMERA_PREVIEW_H) {
        return;
    }
    /* 中心裁剪：左右各去掉 (width - 240)/2 列，逐行拷贝 240 个像素。 */
    const int x_off = ((int)s_preview_decoded_w - CAMERA_PREVIEW_W) / 2;
    for (int y = 0; y < CAMERA_PREVIEW_H; y++) {
        memcpy(dst + (size_t)y * CAMERA_PREVIEW_W * 2,
               s_preview_decode_buf + ((size_t)y * s_preview_decoded_w + x_off) * 2,
               CAMERA_PREVIEW_W * 2);
    }
}

/* 惰性创建预览 canvas + 双缓冲；只做一次，之后反复 show/hide。 */
static void camera_preview_ui_create(void)
{
    if (s_preview_canvas) {
        return;
    }
    if (!s_preview_canvas_buf) {
        s_preview_canvas_buf = heap_caps_malloc(CAMERA_PREVIEW_BYTES,
                                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    if (!s_preview_decode_buf) {
        s_preview_decode_buf = heap_caps_malloc(CAMERA_PREVIEW_DECODE_BYTES,
                                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    if (!s_preview_canvas_buf || !s_preview_decode_buf) {
        ESP_LOGE(TAG, "[prev] no PSRAM for preview buffers");
        return;
    }
    if (!bsp_display_lock(0)) {
        return;
    }
    lv_obj_t *scr = lv_disp_get_scr_act(NULL);
    s_preview_canvas = lv_canvas_create(scr);
    lv_obj_set_size(s_preview_canvas, CAMERA_PREVIEW_W, CAMERA_PREVIEW_H);
    lv_obj_align(s_preview_canvas, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_obj_set_style_bg_color(s_preview_canvas, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s_preview_canvas, LV_OPA_COVER, 0);
    lv_canvas_set_buffer(s_preview_canvas, s_preview_canvas_buf,
                         CAMERA_PREVIEW_W, CAMERA_PREVIEW_H, LV_COLOR_FORMAT_RGB565);
    lv_obj_set_hidden(s_preview_canvas, true);
    bsp_display_unlock();
}

static void camera_preview_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "[prev] preview started (240x240 RGB565)");
    while (s_preview_active) {
        jpeg_frame_t frame = { 0 };
        /* 预览按 50 ms 节拍连续取帧 → 走 live 入口：会话保持打开，
         * DQBUF 上限 800 ms，坏帧快速失败重试（见「Camera — 持久会话」）。 */
        if (app_camera_capture_jpeg_live(&frame) == ESP_OK) {
            if (camera_preview_decode(&frame)) {
                /* 锁内只做「拷贝 + 标脏」：解码已在锁外完成，显示锁的持有时间只剩
                 * 115 KB memcpy 的量级（毫秒级），不会卡住 LVGL 的刷新节拍。 */
                if (bsp_display_lock(0)) {
                    camera_preview_blit(s_preview_canvas_buf);
                    lv_obj_invalidate(s_preview_canvas);
                    bsp_display_unlock();
                }
            }
            jpeg_frame_free(&frame);
        }
        vTaskDelay(pdMS_TO_TICKS(CAMERA_PREVIEW_FRAME_INTERVAL_MS));
    }
    if (bsp_display_lock(0)) {
        if (s_preview_canvas) {
            lv_obj_set_hidden(s_preview_canvas, true);
        }
        bsp_display_unlock();
    }
    s_preview_active = false;
    refresh_ui();
    log_heap("preview: task done");
    ESP_LOGI(TAG, "[prev] preview stopped");
    vTaskDelete(NULL);
}

static bool camera_preview_start(void)
{
    if (s_preview_active) {
        return true;
    }
    if (!s_camera_ready) {
        ESP_LOGW(TAG, "[prev] camera not initialized - cannot preview");
        return false;
    }
    camera_preview_ui_create();
    if (!s_preview_canvas || !s_preview_canvas_buf || !s_preview_decode_buf) {
        return false;
    }
    if (bsp_display_lock(0)) {
        lv_obj_set_hidden(s_preview_canvas, false);
        bsp_display_unlock();
    }
    s_preview_active = true;
    if (xTaskCreate(camera_preview_task, "preview_task", CAMERA_PREVIEW_TASK_STACK,
                    NULL, CAMERA_PREVIEW_TASK_PRIORITY, NULL) != pdPASS) {
        s_preview_active = false;
        ESP_LOGE(TAG, "[prev] task create failed (heap?)");
        return false;
    }
    refresh_ui();
    return true;
}

/* ================================================================
 *  6-Face Calibration Mode
 * ================================================================ */

/* Get face name */
static const char* get_face_name(calibration_face_t face)
{
    switch (face) {
        case FACE_POS_X: return "+X";
        case FACE_NEG_X: return "-X";
        case FACE_POS_Y: return "+Y";
        case FACE_NEG_Y: return "-Y";
        case FACE_POS_Z: return "+Z";
        case FACE_NEG_Z: return "-Z";
        default: return "IDLE";
    }
}

/* Get next face in sequence */
static calibration_face_t get_next_face(calibration_face_t current)
{
    switch (current) {
        case FACE_POS_X: return FACE_NEG_X;
        case FACE_NEG_X: return FACE_POS_Y;
        case FACE_POS_Y: return FACE_NEG_Y;
        case FACE_NEG_Y: return FACE_POS_Z;
        case FACE_POS_Z: return FACE_NEG_Z;
        case FACE_NEG_Z: return FACE_COMPLETE;
        default: return FACE_POS_X;
    }
}

/* Start calibration mode */
static esp_err_t start_calibration_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        return ESP_OK;
    }

    /* Create calibration data file */
    uint64_t now_ms = esp_timer_get_time() / 1000;
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/calibration_%013" PRIu64 ".csv", now_ms);

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    /* 静态内部 RAM 缓冲（见「CSV 落盘缓冲 / 刷盘策略」）：让 FatFs 拿到的是
     * 内部 DMA 指针，SDMMC 走直通路径，不再每 10 ms 申请一次 bounce buffer。 */
    csv_io_attach(s_data_file);

    /* CSV header for calibration data */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    /* Create calibration parameters output file */
    const char *calib_path = BSP_SD_MOUNT_POINT "/calibration.csv";
    s_calibration_file = fopen(calib_path, "w");
    if (!s_calibration_file) {
        csv_fclose(&s_data_file);
        int err = errno;
        set_status_locked("Open calibration.csv failed (errno=%d)", err);
        return ESP_FAIL;
    }
    csv_io_attach(s_calibration_file);
    fprintf(s_calibration_file, "axis,offset,scale,unit\n");
    fflush(s_calibration_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Clear calibration accumulators from any previous session */
    memset(s_calib_sum_x, 0, sizeof(s_calib_sum_x));
    memset(s_calib_sum_y, 0, sizeof(s_calib_sum_y));
    memset(s_calib_sum_z, 0, sizeof(s_calib_sum_z));
    memset(s_calib_count, 0, sizeof(s_calib_count));

    /* Reset calibration state */
    s_current_face = FACE_POS_X;
    s_face_sample_count = 0;
    s_face_start_time_ms = esp_timer_get_time() / 1000;
    snprintf(s_face_name, sizeof(s_face_name), "Face: +X (0s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter state for a clean new session */
    s_filter_x = 0;
    s_filter_y = 0;
    s_filter_z = 0;
    s_filter_init = false;

    set_status_locked("Calibration: +X face, 10s each");

    ESP_LOGI(TAG, "Calibration started: %s", s_file_path);
    ESP_LOGI(TAG, "Sequence: +X → -X → +Y → -Y → +Z → -Z, 10s per face");

    return ESP_OK;
}

/* Complete calibration and write calibration.csv */
static void complete_calibration_locked(void)
{
    csv_fclose(&s_data_file);

    s_collecting = false;
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: COMPLETE");

    csv_fclose(&s_calibration_file);

    set_status_locked("Calibration complete!");
    ESP_LOGI(TAG, "Calibration complete. See calibration.csv for parameters");
}

/* ================================================================
 *  SD Card Logging (Normal Mode)
 * ================================================================ */
static void stop_collection_locked(const char *reason)
{
    /* Alias protection: s_data_file / s_jump_data_file / s_stand_data_file may
     * point to the SAME FILE (see `s_data_file = s_jump_data_file;` in
     * start_jump_protocol_locked and `s_data_file = s_stand_data_file;` in
     * create_stand_file_for_current_group). Detach the aliases first so we
     * never fclose() the same FILE twice.
     *
     * 注意：以前只拦了 jump，stand 没拦 —— stand 会话 stop 之后
     * s_stand_data_file 仍然指向已关闭的 FILE，下一轮 stand 的
     * create_stand_file_for_current_group() 又去 fclose(s_stand_data_file)，
     * 就是一次 double free。这里一并修掉。 */
    if (s_data_file != NULL) {
        if (s_jump_data_file == s_data_file) {
            s_jump_data_file = NULL;
        }
        if (s_stand_data_file == s_data_file) {
            s_stand_data_file = NULL;
            s_stand_file_open = false;
        }
    }

    csv_fclose(&s_data_file);
    csv_fclose(&s_calibration_file);

    s_collecting = false;
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Also reset stand protocol state if active */
    s_stand_protocol_active = false;
    s_stand_state = STAND_PROTOCOL_IDLE;
    s_stand_label[0] = '\0';
    snprintf(s_stand_display, sizeof(s_stand_display), "Stand: IDLE");

    /* Also reset stairs protocol state if active */
    s_stairs_protocol_active = false;
    s_stairs_state = STAIRS_PROTOCOL_IDLE;
    s_stairs_label[0] = '\0';
    snprintf(s_stairs_display, sizeof(s_stairs_display), "Stairs: IDLE");

    /* Also reset bend protocol state if active */
    s_bend_protocol_active = false;
    s_bend_state = BEND_PROTOCOL_IDLE;
    s_bend_label[0] = '\0';
    snprintf(s_bend_display, sizeof(s_bend_display), "Bend: IDLE");

    /* Also reset jump protocol state if active */
    if (s_jump_protocol_active) {
        csv_fclose(&s_jump_data_file);
        s_jump_protocol_active = false;
        s_jump_state = JUMP_PROTOCOL_IDLE;
        s_jump_label[0] = '\0';
        snprintf(s_jump_display, sizeof(s_jump_display), "Jump: IDLE");
        s_jump_file_path[0] = '\0';
    }

    /* Also reset fall protocol state if active */
    s_fall_protocol_active = false;
    s_fall_state = FALL_PROTOCOL_IDLE;
    s_fall_label[0] = '\0';
    snprintf(s_fall_display, sizeof(s_fall_display), "Fall: IDLE");

    set_status_locked("%s", reason);
}

static void reset_accel_filter(void)
{
    s_filter_x = 0;
    s_filter_y = 0;
    s_filter_z = 0;
    s_filter_init = false;
}

static esp_err_t start_collection_locked(void)
{
    if (s_collecting) {
        return ESP_OK;
    }

    /* WiFi-only streaming: a missing / unmountable SD card is no longer fatal.
     * Button A still starts a 100 Hz session that is pushed to the web platform
     * over HTTP; the SD card merely becomes an optional CSV backup.
     * NOTE: the protocol sessions (calibration / stand / stairs / bend / jump /
     * fall) keep their own strict "!s_sd_ready" guard — they are unchanged. */
    s_data_file = NULL;
    s_file_path[0] = '\0';

    if (s_sd_ready) {
        /* Use unique filename based on uptime (milliseconds) for CSV */
        uint64_t now_ms = esp_timer_get_time() / 1000;
        snprintf(s_file_path, sizeof(s_file_path),
                 BSP_SD_MOUNT_POINT "/DATA_%013" PRIu64 ".csv", now_ms);

        s_data_file = fopen(s_file_path, "w");
        if (s_data_file) {
            csv_io_attach(s_data_file);
            /* CSV header - label first format */
            fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
            fflush(s_data_file);
        } else {
            /* Card present but unwritable: degrade to WiFi-only instead of
             * refusing to start, so a flaky card never blocks live streaming. */
            int err = errno;
            ESP_LOGW(TAG, "fopen(%s) failed (errno=%d) -> WiFi-only streaming",
                     s_file_path, err);
            s_file_path[0] = '\0';
            s_data_file = NULL;
        }
    } else {
        ESP_LOGW(TAG, "SD card not ready -> WiFi-only streaming (no CSV backup)");
    }

    if (!s_data_file && !s_wifi_connected) {
        /* Samples would go nowhere right now. We still start (WiFi may come up
         * later and upload_accumulate() is re-evaluated every sample), but make
         * the situation obvious on screen and in the log. */
        ESP_LOGW(TAG, "Neither SD nor WiFi available: samples will be dropped");
    }

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter state for a clean new session */
    reset_accel_filter();
    set_status_locked(s_data_file ? "Logging IMU data @ 100 Hz"
                                  : "WiFi-only streaming @ 100 Hz");

    return ESP_OK;
}

/* ================================================================
 *  Stand Action Protocol
 * ================================================================ */

/* Create stand file for current group */
static esp_err_t create_stand_file_for_current_group(void)
{
    /* Close previous file if open（顺带归还静态 stdio 缓冲槽位） */
    csv_fclose(&s_stand_data_file);
    s_stand_file_open = false;

    /* Create stand data file for current group */
    char stand_file_path[STAND_FILE_PATH_LEN];
    int group_num = s_stand_group + 1;  /* 1-based */
    snprintf(stand_file_path, sizeof(stand_file_path),
             BSP_SD_MOUNT_POINT "/stand_%d.csv", group_num);

    s_stand_data_file = fopen(stand_file_path, "w");
    if (!s_stand_data_file) {
        int err = errno;
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    s_stand_file_open = true;
    csv_io_attach(s_stand_data_file);   /* 静态内部 RAM stdio 缓冲，见「CSV 落盘缓冲」 */

    /* CSV header - label first format */
    fprintf(s_stand_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_stand_data_file);

    /* Sync data_file to stand_data_file so sampler_task can write */
    s_data_file = s_stand_data_file;

    ESP_LOGI(TAG, "[Stand] Created file: %s", stand_file_path);

    return ESP_OK;
}

/* Start stand protocol (called from Button B long press) */
static esp_err_t start_stand_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in stand protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize stand protocol state */
    s_stand_protocol_active = true;
    s_stand_state = STAND_PROTOCOL_PREP;
    s_stand_group = 0;
    s_stand_phase_start_ms = esp_timer_get_time() / 1000;
    s_stand_duration_sec = STAND_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (STAND_EXECUTE_DURATION_MAX_SEC - STAND_EXECUTE_DURATION_MIN_SEC + 1));
    snprintf(s_stand_label, sizeof(s_stand_label), "stand");
    snprintf(s_stand_display, sizeof(s_stand_display), "Stand: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    /* Create file for group 1 */
    esp_err_t ret = create_stand_file_for_current_group();
    if (ret != ESP_OK) {
        return ret;
    }

    set_status_locked("Stand: Group 1 prep (3s)");

    ESP_LOGI(TAG, "Stand protocol started: 3 groups, each in separate file");
    ESP_LOGI(TAG, "Protocol: 3x(prep 3s + stand 20-30s + idle 5s)");

    return ESP_OK;
}

/* Complete stand protocol and close file */
static void complete_stand_protocol_locked(void)
{
    csv_fclose(&s_data_file);
    /* s_data_file 是 s_stand_data_file 的别名（见 create_stand_file_for_current_group），
     * 上面已经关掉了，这里必须同步清空，否则留下悬空指针。 */
    s_stand_data_file = NULL;
    s_stand_file_open = false;

    s_collecting = false;
    s_stand_protocol_active = false;
    s_stand_state = STAND_PROTOCOL_COMPLETE;
    s_stand_label[0] = '\0';
    snprintf(s_stand_display, sizeof(s_stand_display), "Stand: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Stand protocol complete!");
    ESP_LOGI(TAG, "Stand protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start stairs protocol (called from Button B long press) */
static esp_err_t start_stairs_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create stairs data file for group 1 */
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/stairs_1.csv");

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    csv_io_attach(s_data_file);

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in stairs protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize stairs protocol state */
    s_stairs_protocol_active = true;
    s_stairs_state = STAIRS_PROTOCOL_PREP;
    s_stairs_group = 0;
    s_stairs_phase_start_ms = esp_timer_get_time() / 1000;
    s_stairs_duration_sec = STAIRS_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (STAIRS_EXECUTE_DURATION_MAX_SEC - STAIRS_EXECUTE_DURATION_MIN_SEC + 1));
    snprintf(s_stairs_label, sizeof(s_stairs_label), "stairs");
    snprintf(s_stairs_display, sizeof(s_stairs_display), "Stairs: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Stairs: Group 1 (25s)");

    ESP_LOGI(TAG, "Stairs protocol started: %s", s_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(stairs 20-30s), continuous groups");

    return ESP_OK;
}

/* Create stairs file for current group */
static esp_err_t create_stairs_file_for_current_group(void)
{
    /* Close previous file if open（顺带归还静态 stdio 缓冲槽位） */
    csv_fclose(&s_data_file);

    /* Create stairs data file for current group */
    char stairs_file_path[STAIRS_FILE_PATH_LEN];
    int group_num = s_stairs_group + 1;  /* 1-based */
    snprintf(stairs_file_path, sizeof(stairs_file_path),
             BSP_SD_MOUNT_POINT "/stairs_%d.csv", group_num);

    s_data_file = fopen(stairs_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    csv_io_attach(s_data_file);

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    ESP_LOGI(TAG, "[Stairs] Created file: %s", stairs_file_path);

    return ESP_OK;
}

/* Complete stairs protocol and close file */
static void complete_stairs_protocol_locked(void)
{
    csv_fclose(&s_data_file);

    s_collecting = false;
    s_stairs_protocol_active = false;
    s_stairs_state = STAIRS_PROTOCOL_COMPLETE;
    s_stairs_label[0] = '\0';
    snprintf(s_stairs_display, sizeof(s_stairs_display), "Stairs: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Stairs protocol complete!");
    ESP_LOGI(TAG, "Stairs protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start bend protocol (called from Button B long press) */
static esp_err_t start_bend_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create bend data file for group 1 */
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/bend_1.csv");

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    csv_io_attach(s_data_file);

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in bend protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize bend protocol state */
    s_bend_protocol_active = true;
    s_bend_state = BEND_PROTOCOL_PREP;
    s_bend_group = 0;
    s_bend_phase_start_ms = esp_timer_get_time() / 1000;
    s_bend_duration_sec = BEND_DURATION_MIN_SEC;  /* Start with 20s */
    s_bend_interval_duration_sec = BEND_INTERVAL_MIN_SEC;  /* Start with 60s */
    snprintf(s_bend_label, sizeof(s_bend_label), "bend");
    snprintf(s_bend_display, sizeof(s_bend_display), "Bend: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Bend: Group 1 (%ds)", s_bend_duration_sec);

    ESP_LOGI(TAG, "Bend protocol started: %s", s_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(bend 20-30s + rest 60-90s)");

    return ESP_OK;
}

/* Create bend file for current group */
static esp_err_t create_bend_file_for_current_group(void)
{
    /* Close previous file if open（顺带归还静态 stdio 缓冲槽位） */
    csv_fclose(&s_data_file);

    /* Create bend data file for current group */
    char bend_file_path[FILE_PATH_LEN];
    int group_num = s_bend_group + 1;  /* 1-based */
    snprintf(bend_file_path, sizeof(bend_file_path),
             BSP_SD_MOUNT_POINT "/bend_%d.csv", group_num);

    s_data_file = fopen(bend_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    csv_io_attach(s_data_file);

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    ESP_LOGI(TAG, "[Bend] Created file: %s", bend_file_path);

    return ESP_OK;
}

/* Complete bend protocol and close file */
static void complete_bend_protocol_locked(void)
{
    csv_fclose(&s_data_file);

    s_collecting = false;
    s_bend_protocol_active = false;
    s_bend_state = BEND_PROTOCOL_COMPLETE;
    s_bend_label[0] = '\0';
    snprintf(s_bend_display, sizeof(s_bend_display), "Bend: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Bend protocol complete!");
    ESP_LOGI(TAG, "Bend protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start jump protocol (called from Button B long press) */
static esp_err_t start_jump_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create jump data file for group 1 */
    snprintf(s_jump_file_path, sizeof(s_jump_file_path),
             BSP_SD_MOUNT_POINT "/jump_1.csv");

    s_jump_data_file = fopen(s_jump_file_path, "w");
    if (!s_jump_data_file) {
        int err = errno;
        s_jump_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    csv_io_attach(s_jump_data_file);

    /* CSV header - label first format */
    fprintf(s_jump_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_jump_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in jump protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize jump protocol state */
    s_jump_protocol_active = true;
    s_jump_state = JUMP_PROTOCOL_PREP;
    s_jump_group = 0;
    s_jump_phase_start_ms = esp_timer_get_time() / 1000;
    s_jump_duration_sec = JUMP_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (JUMP_EXECUTE_DURATION_MAX_SEC - JUMP_EXECUTE_DURATION_MIN_SEC + 1));
    snprintf(s_jump_label, sizeof(s_jump_label), "jump");
    snprintf(s_jump_display, sizeof(s_jump_display), "Jump: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Jump: Group 1 (10s)");

    /* Sync data_file to jump_data_file so sampler_task can write */
    s_data_file = s_jump_data_file;

    ESP_LOGI(TAG, "Jump protocol started: %s", s_jump_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(jump 5-10s + rest 5-10s)");

    return ESP_OK;
}

/* Complete jump protocol and close all files */
static void complete_jump_protocol_locked(void)
{
    csv_fclose(&s_jump_data_file);
    s_data_file = NULL;   /* s_data_file 是 s_jump_data_file 的别名，已随上面关闭 */

    s_collecting = false;
    s_jump_protocol_active = false;
    s_jump_state = JUMP_PROTOCOL_COMPLETE;
    s_jump_label[0] = '\0';
    snprintf(s_jump_display, sizeof(s_jump_display), "Jump: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Jump protocol complete!");
    ESP_LOGI(TAG, "Jump protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start fall protocol (called from Button B long press) */
static esp_err_t start_fall_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create fall data file for group 1 */
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/fall_1.csv");

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    csv_io_attach(s_data_file);

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in fall protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize fall protocol state */
    s_fall_protocol_active = true;
    s_fall_state = FALL_PROTOCOL_PREP;
    s_fall_group = 0;
    s_fall_phase_start_ms = esp_timer_get_time() / 1000;
    s_fall_duration_sec = FALL_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (FALL_EXECUTE_DURATION_MAX_SEC - FALL_EXECUTE_DURATION_MIN_SEC + 1));
    s_fall_interval_duration_sec = FALL_INTERVAL_MIN_SEC + 
        (esp_random() % (FALL_INTERVAL_MAX_SEC - FALL_INTERVAL_MIN_SEC + 1));
    snprintf(s_fall_label, sizeof(s_fall_label), "fall");
    snprintf(s_fall_display, sizeof(s_fall_display), "Fall: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Fall: Group 1 (%ds)", s_fall_duration_sec);

    ESP_LOGI(TAG, "Fall protocol started: %s", s_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(fall 5-10s + rest 15-30s)");

    return ESP_OK;
}

/* Complete fall protocol and close file */
static void complete_fall_protocol_locked(void)
{
    csv_fclose(&s_data_file);

    s_collecting = false;
    s_fall_protocol_active = false;
    s_fall_state = FALL_PROTOCOL_COMPLETE;
    s_fall_label[0] = '\0';
    snprintf(s_fall_display, sizeof(s_fall_display), "Fall: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Fall protocol complete!");
    ESP_LOGI(TAG, "Fall protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Button B single click callback — start/stop 6-face calibration */
static void button_b_single_click_cb(void *arg, void *data)
{
    (void)arg;
    (void)data;

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    /* If any protocol or collection is running, stop it first */
    if (s_stand_protocol_active || s_stairs_protocol_active || 
        s_bend_protocol_active || s_jump_protocol_active || 
        s_fall_protocol_active || s_collecting) {
        /* If already in calibration mode, complete it */
        if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
            ESP_LOGI(TAG, "Calibration aborted");
            stop_collection_locked("Calibration aborted by user");
        } else {
            /* Otherwise just stop the current session */
            char reason[UI_TEXT_LEN];
            snprintf(reason, sizeof(reason), "Session stopped");
            stop_collection_locked(reason);
        }
    } else {
        /* Start calibration mode */
        esp_err_t ret = start_calibration_locked();
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Failed to start calibration: %s", esp_err_to_name(ret));
        } else {
            ESP_LOGI(TAG, "Calibration started");
        }
    }

    xSemaphoreGive(s_state_mutex);
    refresh_ui();
}

/* Button B long press callback — cycle through stand, stairs, bend, jump, and fall protocol */
static void button_b_long_press_cb(void *arg, void *data)
{
    (void)arg;
    (void)data;

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    /* Cycle through stand, stairs, bend, jump, and fall protocols */
    esp_err_t ret;
    static int protocol_cycle = 0;
    
    /* If any protocol or collection is running, stop it first */
    if (s_stand_protocol_active || s_stairs_protocol_active || 
        s_bend_protocol_active || s_jump_protocol_active || 
        s_fall_protocol_active || s_collecting) {
        ESP_LOGI(TAG, "Current session aborted, switching protocol");
        stop_collection_locked("Protocol switch");
        vTaskDelay(pdMS_TO_TICKS(50));  /* Brief delay to ensure cleanup */
    }
    
    switch (protocol_cycle % 5) {
        case 0:
            /* Start stand protocol */
            ret = start_stand_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Stand protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start stand protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 1:
            /* Start stairs protocol */
            ret = start_stairs_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Stairs protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start stairs protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 2:
            /* Start bend protocol */
            ret = start_bend_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Bend protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start bend protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 3:
            /* Start jump protocol */
            ret = start_jump_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Jump protocol started: %s", s_jump_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start jump protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 4:
            /* Start fall protocol */
            ret = start_fall_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Fall protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start fall protocol: %s", esp_err_to_name(ret));
            }
            break;
    }
    protocol_cycle++;

    xSemaphoreGive(s_state_mutex);
    refresh_ui();
}

/* ================================================================
 *  Button Callback
 * ================================================================ */
static void button_a_single_click_cb(void *arg, void *data)
{
    (void)arg;
    (void)data;

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    if (s_collecting) {
        char reason[UI_TEXT_LEN];
        snprintf(reason, sizeof(reason), "Saved %" PRIu32 " samples", s_sample_count);
        stop_collection_locked(reason);
        
        /* Log final frequency measurement (Hz with one decimal — 以前这里同样把
         * Hz×100 标成 "Hz"，见采样循环里「6. Frequency measurement」的说明) */
        if (s_freq_valid) {
            int32_t freq_d10 = (int32_t)(s_measured_freq_hz * 10);
            ESP_LOGI(TAG, "Session complete. Measured frequency: %" PRId32 ".%" PRId32
                          " Hz (Target: %d Hz)",
                     freq_d10 / 10, freq_d10 % 10, TARGET_SAMPLE_FREQ_HZ);
        }
        
        ESP_LOGI(TAG, "Logging stopped");
    } else {
        esp_err_t ret = start_collection_locked();
        if (ret == ESP_OK) {
            if (s_data_file) {
                ESP_LOGI(TAG, "Logging started: %s", s_file_path);
            } else {
                ESP_LOGI(TAG, "Streaming started (WiFi-only, no CSV file)");
            }
        } else {
            ESP_LOGW(TAG, "Failed to start: %s", esp_err_to_name(ret));
        }
    }

    xSemaphoreGive(s_state_mutex);
    refresh_ui();
}

static void init_buttons(void)
{
    int button_count = 0;

    ESP_ERROR_CHECK(bsp_iot_button_create(s_buttons, &button_count, BSP_BUTTON_NUM));
    ESP_LOGI(TAG, "Initialized %d buttons, using BSP_BUTTON_1 + BSP_BUTTON_2", button_count);

    /* Button A: single click → start/stop normal collection
     *           long press (2s) → 三态循环：关 → Web 直播 → 本地 LCD 预览 */
    ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[START_BUTTON_INDEX],
                                           BUTTON_SINGLE_CLICK,
                                           NULL,
                                           button_a_single_click_cb,
                                           NULL));

    button_event_args_t a_long_press_args = {
        .long_press.press_time = 2000,  /* 2 second long press */
    };
    ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[START_BUTTON_INDEX],
                                           BUTTON_LONG_PRESS_START,
                                           &a_long_press_args,
                                           button_a_long_press_cb,
                                           NULL));
    ESP_LOGI(TAG, "Button A registered: single click = start/stop logging,"
                  " long press (2s) = Web live -> local preview -> off");

    /* Button B: single click → start/stop 6-face calibration
 *             long press (2s) → toggle stand/stairs/bend/jump/fall protocol */
    if (button_count > 1) {
        /* Single click: start/stop 6-face calibration */
        ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[BSP_BUTTON_2],
                                               BUTTON_SINGLE_CLICK,
                                               NULL,
                                               button_b_single_click_cb,
                                               NULL));
        
        /* Long press (2s): cycle through action protocols */
        button_event_args_t long_press_args = {
            .long_press.press_time = 2000,  /* 2 second long press */
        };
        ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[BSP_BUTTON_2],
                                               BUTTON_LONG_PRESS_START,
                                               &long_press_args,
                                               button_b_long_press_cb,
                                               NULL));
        ESP_LOGI(TAG, "Button B registered: single click = 6-face calibration, long press (2s) = toggle protocols");
    } else {
        ESP_LOGW(TAG, "Only 1 button found — Button B unavailable");
    }

    /* Button C / D：闭环事件（触发 → 本地反馈 → 远端显示 → 回应/取消）。
     * ESP32-S3-EYE 的 BUTTON_1..4 是同一个 ADC 电阻梯上的 4 档（bsp_button.c），
     * BUTTON_5 是 GPIO0 的 BOOT 键；1/2 已归 A/B，这里用 3/4，互不冲突。
     * 回调只置标志 + 闪灯 + 改屏，HTTP 交给 loop_task——回调运行在 iot_button
     * 的任务上下文里，栈很小，既不能 vTaskDelay 也不能发请求。 */
    if (button_count > (int)LOOP_RESPOND_BUTTON) {
        ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[LOOP_TRIGGER_BUTTON],
                                               BUTTON_SINGLE_CLICK,
                                               NULL,
                                               loop_trigger_button_cb,
                                               NULL));

        ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[LOOP_RESPOND_BUTTON],
                                               BUTTON_SINGLE_CLICK,
                                               NULL,
                                               loop_respond_button_cb,
                                               NULL));

        button_event_args_t respond_long_args = {
            .long_press.press_time = LOOP_RESPOND_LONG_MS,
        };
        ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[LOOP_RESPOND_BUTTON],
                                               BUTTON_LONG_PRESS_START,
                                               &respond_long_args,
                                               loop_respond_long_press_cb,
                                               NULL));
        ESP_LOGI(TAG, "Loop buttons registered: BSP_BUTTON_%d = trigger,"
                      " BSP_BUTTON_%d = respond (click) / cancel (long %d ms)",
                 (int)LOOP_TRIGGER_BUTTON + 1, (int)LOOP_RESPOND_BUTTON + 1,
                 LOOP_RESPOND_LONG_MS);
    } else {
        ESP_LOGW(TAG, "Only %d buttons found — loop trigger/respond unavailable",
                 button_count);
    }
}

static void init_sdcard(void)
{
    esp_err_t ret = bsp_sdcard_mount();

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    if (ret == ESP_OK) {
        s_sd_ready = true;
        set_status_locked("SD card ready");
        ESP_LOGI(TAG, "SD mounted at %s", BSP_SD_MOUNT_POINT);
        sdmmc_card_t *card = bsp_sdcard_get_handle();
        if (card) {
            sdmmc_card_print_info(stdout, card);
        }
    } else {
        s_sd_ready = false;
        set_status_locked("SD mount failed");
        ESP_LOGE(TAG, "SD mount failed: %s", esp_err_to_name(ret));
    }

    xSemaphoreGive(s_state_mutex);
}

/* ================================================================
 *  Real-Sensor WiFi Upload (esp_http_client)
 *
 *  sampler_task samples at 100 Hz. Every second (= UPLOAD_WINDOW_SIZE
 *  samples) it snapshots the m/s² accelerometer values into a heap batch
 *  and queues it. A lower-priority uploader_task then performs the blocking
 *  HTTP POST, so the round-trip never stalls the drift-free 100 Hz loop.
 *  The JSON body matches server/main.py POST /api/v1/upload.
 * ================================================================ */
typedef struct {
    uint64_t ts_ms;    /* absolute epoch ms of this sample */
    float    ax;       /* m/s² */
    float    ay;       /* m/s² */
    float    az;       /* m/s² */
} upload_sample_t;

/* Flexible-array batch: periodic windows hold UPLOAD_WINDOW_SIZE samples while
 * on-demand captures hold whatever the task asked for (<= TASK_MAX_SAMPLES),
 * so samples[] is sized per allocation and cap records the capacity. */
typedef struct {
    uint64_t ts_ms;                     /* epoch ms of first sample in batch */
    uint32_t count;                     /* samples in this batch */
    uint32_t cap;                       /* capacity of samples[] */
    char     request_id[TASK_REQUEST_ID_LEN];  /* empty = periodic batch */
    char     trigger[TASK_TRIGGER_LEN];        /* "periodic" / "manual" */
    upload_sample_t samples[];          /* flexible array, size = cap */
} upload_batch_t;

static QueueHandle_t s_upload_queue = NULL;    /* carries upload_batch_t* */
static upload_sample_t s_upload_buf[UPLOAD_WINDOW_SIZE];
static uint32_t s_upload_count = 0;

/* Allocate a batch with room for cap samples. Metadata only - no HTTP here. */
static upload_batch_t *upload_batch_alloc(uint32_t cap, const char *trigger,
                                          const char *request_id)
{
    upload_batch_t *batch =
        malloc(sizeof(upload_batch_t) + sizeof(upload_sample_t) * cap);
    if (!batch) {
        return NULL;
    }
    batch->ts_ms = 0;
    batch->count = 0;
    batch->cap = cap;
    batch->request_id[0] = '\0';
    batch->trigger[0] = '\0';
    if (trigger) {
        snprintf(batch->trigger, sizeof(batch->trigger), "%s", trigger);
    }
    if (request_id) {
        snprintf(batch->request_id, sizeof(batch->request_id), "%s", request_id);
    }
    return batch;
}

/* Accumulate one sample; when a full second is collected, enqueue a copy. */
static void upload_accumulate(uint64_t ts_ms, float ax, float ay, float az)
{
    if (s_upload_count >= UPLOAD_WINDOW_SIZE) {
        return;    /* safety guard */
    }

    s_upload_buf[s_upload_count].ts_ms = ts_ms;
    s_upload_buf[s_upload_count].ax    = ax;
    s_upload_buf[s_upload_count].ay    = ay;
    s_upload_buf[s_upload_count].az    = az;
    s_upload_count++;

    if (s_upload_count == UPLOAD_WINDOW_SIZE) {
        /* One full second collected → hand a private copy to the uploader. */
        upload_batch_t *batch =
            upload_batch_alloc(UPLOAD_WINDOW_SIZE, "periodic", NULL);
        if (batch) {
            batch->ts_ms = s_upload_buf[0].ts_ms;
            batch->count = UPLOAD_WINDOW_SIZE;
            memcpy(batch->samples, s_upload_buf, sizeof(s_upload_buf));
            if (xQueueSend(s_upload_queue, &batch, 0) != pdTRUE) {
                free(batch);   /* queue full → drop this batch, keep sampling */
            }
        }
        s_upload_count = 0;
    }
}

/* Build JSON and POST one batch to the configured FastAPI receiver.
 * Uses bounded retries so a transient WiFi/AP hiccup does not throw away a
 * whole second of samples on the first failure.
 * Returns true when the server accepted the batch (HTTP 2xx). */
static bool upload_post_batch(upload_batch_t *batch)
{
    if (!batch || batch->count == 0) {
        return false;
    }

    if (!s_wifi_connected) {
        ESP_LOGW(TAG, "[upload] skip: WiFi not connected");
        return false;
    }

    log_heap("upload: pre-cJSON");
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        ESP_LOGE(TAG, "[upload] cJSON_CreateObject failed - heap exhausted");
        return false;
    }
    cJSON_AddStringToObject(root, "device_id", CONFIG_SENSOR_DEVICE_ID);
    cJSON_AddStringToObject(root, "source",    UPLOAD_SOURCE);
    cJSON_AddStringToObject(root, "unit",      UPLOAD_UNIT);
    cJSON_AddNumberToObject(root, "ts_ms",     (double)batch->ts_ms);
    /* On-demand batches carry the task id so the server can close the task and
     * dedup replays; periodic batches keep the legacy body (no extra fields). */
    if (batch->trigger[0] != '\0') {
        cJSON_AddStringToObject(root, "trigger", batch->trigger);
    }
    if (batch->request_id[0] != '\0') {
        cJSON_AddStringToObject(root, "request_id", batch->request_id);
    }

    cJSON *arr = cJSON_CreateArray();
    cJSON_AddItemToObject(root, "samples", arr);
    for (uint32_t k = 0; k < batch->count; k++) {
        const upload_sample_t *sm = &batch->samples[k];
        cJSON *o = cJSON_CreateObject();
        cJSON_AddNumberToObject(o, "i", (double)k);
        cJSON_AddNumberToObject(o, "t_ms",
            (double)((int64_t)sm->ts_ms - (int64_t)batch->ts_ms));
        cJSON_AddNumberToObject(o, "ax", sm->ax);
        cJSON_AddNumberToObject(o, "ay", sm->ay);
        cJSON_AddNumberToObject(o, "az", sm->az);
        cJSON_AddItemToArray(arr, o);
    }

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!payload) {
        ESP_LOGE(TAG, "[upload] cJSON_PrintUnformatted failed - heap exhausted");
        return false;
    }
    ESP_LOGI(TAG, "[upload] payload=%u B for n=%" PRIu32 " samples",
             (unsigned)strlen(payload), batch->count);
    log_heap("upload: post-cJSON");

    /* Retrying here (not in uploader_task) keeps batches in order and does not
     * touch the drift-free 100 Hz sampler. A 4xx means the server rejected the
     * payload itself, so that is not retried; 5xx / network errors are. */
    const bool has_token = (CONFIG_SENSOR_TOKEN[0] != '\0');
    esp_err_t err = ESP_FAIL;
    int status = 0;

    for (int attempt = 1; attempt <= UPLOAD_MAX_ATTEMPTS; attempt++) {
        esp_http_client_config_t cfg = {
            .url         = CONFIG_SENSOR_SERVER_URL,
            .method      = HTTP_METHOD_POST,
            .timeout_ms  = 10000,
            .buffer_size = 4096,
        };
        esp_http_client_handle_t client = esp_http_client_init(&cfg);
        err = ESP_FAIL;
        status = 0;
        if (client) {
            esp_http_client_set_header(client, "Content-Type", "application/json");
            if (has_token) {
                /* Optional shared-secret auth; server only enforces it when
                 * SENSOR_TOKEN is configured there too. */
                char auth[160];
                snprintf(auth, sizeof(auth), "Bearer %s", CONFIG_SENSOR_TOKEN);
                esp_http_client_set_header(client, "Authorization", auth);
            }
            esp_http_client_set_post_field(client, payload, (int)strlen(payload));
            err = esp_http_client_perform(client);
            status = esp_http_client_get_status_code(client);
            esp_http_client_cleanup(client);
        }

        if (err == ESP_OK && status >= 200 && status < 300) {
            ESP_LOGI(TAG, "[upload] OK ts=%" PRIu64 " n=%" PRIu32
                          " http=%d attempt=%d",
                     batch->ts_ms, batch->count, status, attempt);
            free(payload);
            return true;
        }

        ESP_LOGW(TAG, "[upload] attempt %d/%d failed ts=%" PRIu64
                      " n=%" PRIu32 " err=%s http=%d",
                 attempt, UPLOAD_MAX_ATTEMPTS, batch->ts_ms, batch->count,
                 esp_err_to_name(err), status);

        if (status >= 400 && status < 500) {
            ESP_LOGW(TAG, "[upload] payload rejected (http=%d), stop retrying",
                     status);
            break;
        }
        if (attempt < UPLOAD_MAX_ATTEMPTS) {
            vTaskDelay(pdMS_TO_TICKS(UPLOAD_RETRY_BACKOFF_MS << (attempt - 1)));
        }
    }

    ESP_LOGW(TAG, "[upload] FAIL (gave up) ts=%" PRIu64 " n=%" PRIu32
                  " request_id=%s",
             batch->ts_ms, batch->count,
             batch->request_id[0] ? batch->request_id : "-");
    free(payload);
    return false;
}

/* ================================================================
 *  Manual capture task (on-demand, triggered from the Web page)
 *
 *  The Web page POSTs /api/v1/tasks; this module polls
 *  GET /api/v1/tasks/next?device_id=… every TASK_POLL_INTERVAL_MS.
 *  On a hit it acks, then sampler_task (the only QMA6100P reader)
 *  collects N samples at the requested rate while periodic uploads
 *  are paused. The finished batch goes to uploader_task, which POSTs
 *  it together with request_id so the server closes the task in the
 *  same transaction that stores the samples.
 *
 *  Invariants:
 *   * no I2C access here - the sampler keeps owning the sensor;
 *   * every HTTP call lives in task_poll_task / uploader_task;
 *   * sampler-side handoff is non-blocking (retried on the next tick).
 * ================================================================ */
typedef enum {
    MANUAL_IDLE = 0,
    MANUAL_PENDING,       /* task claimed, waiting for sampler to pick it up */
    MANUAL_CAPTURING,     /* sampler is filling the batch */
    MANUAL_UPLOADING,     /* batch queued, waiting for the upload result */
    MANUAL_ERROR,         /* capture/upload failed, fail receipt pending */
} manual_state_t;

typedef struct {
    manual_state_t state;
    char     request_id[TASK_REQUEST_ID_LEN];
    uint32_t rate_hz;             /* requested rate (1..TARGET_SAMPLE_FREQ_HZ) */
    uint32_t target;              /* samples requested by the task */
    uint32_t period_ticks;        /* one sample every N sampling ticks */
    uint32_t tick;                /* sampling ticks since capture started */
    int64_t  deadline_ms;         /* local safety deadline (esp_timer ms) */
    int64_t  server_expires_ms;   /* server expires_at in epoch ms (0 = unknown) */
    upload_batch_t *batch;        /* batch being filled (NULL after handoff) */
    char     error[96];
} manual_capture_t;

static SemaphoreHandle_t s_task_mutex = NULL;
static manual_capture_t s_manual;

static int64_t manual_now_epoch_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static manual_state_t manual_capture_state(void)
{
    manual_state_t state;
    if (!s_task_mutex) {
        return MANUAL_IDLE;
    }
    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    state = s_manual.state;
    xSemaphoreGive(s_task_mutex);
    return state;
}

/* True while a task owns the sampler (periodic uploads stay paused). */
static bool manual_capture_is_active(void)
{
    return manual_capture_state() != MANUAL_IDLE;
}

/* ---------------- 周期上报暂停（pause / resume 控制任务） ----------------
 *
 *  Web「暂停周期」POST 一条 kind=pause 的任务，板端领到后：
 *    sampler 照常采样/落盘/刷 UI，但**不再喂 1 s 周期上传窗口**，到点自动恢复。
 *
 *  两个关键设计点：
 *   1) 真相源是任务链路（pause 任务 applied 后服务端写 device_control），
 *      板端只是执行者；这里的状态仅 RAM，重启即恢复上报（不会永久静音）。
 *   2) 双时钟：esp_timer 单调钟是主判据（SNTP 没同步时 epoch 是 1970 起点，
 *      用它算差值会立刻「以为到点」）；epoch 只在 s_time_synced 时做二次兜底，
 *      以防单调钟因重启被重置。到点在 sampler / poll 两侧惰性清零，不引入定时器。
 */
static bool    s_periodic_paused = false;
static int64_t s_pause_until_mono_ms  = 0;  /* esp_timer 终点（主判据） */
static int64_t s_pause_until_epoch_ms = 0;  /* epoch 终点（仅已同步时非 0） */
/* 未被服务端确认的控制任务（applied 回执待重试），与人工采集的 fail 回执同构 */
static char    s_ctrl_pending_rid[TASK_REQUEST_ID_LEN];
static int64_t s_ctrl_pending_until_ms = 0;
static int     s_ctrl_pending_left = 0;

/* 周期上报是否处于暂停窗口内。惰性到点恢复；调用方可持 s_state_mutex
 * （sampler 路径与 manual_capture_consumes_sample 的加锁顺序一致）。 */
static bool periodic_upload_paused(void)
{
    if (!s_task_mutex) {
        return false;
    }
    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    bool paused = s_periodic_paused;
    if (paused) {
        int64_t mono_now = esp_timer_get_time() / 1000;
        bool expired = (s_pause_until_mono_ms > 0
                        && mono_now >= s_pause_until_mono_ms);
        if (!expired && s_time_synced && s_pause_until_epoch_ms > 0
            && manual_now_epoch_ms() >= s_pause_until_epoch_ms) {
            expired = true;   /* 单调钟重启后不可信：用已同步的 UTC 钟兜底 */
        }
        if (expired) {
            s_periodic_paused = false;
            s_pause_until_mono_ms = 0;
            s_pause_until_epoch_ms = 0;
            ESP_LOGI(TAG, "[ctrl] pause window elapsed - periodic uploads resumed");
        }
        paused = !expired;
    }
    xSemaphoreGive(s_task_mutex);
    return paused;
}

/* 应用一条控制任务（在 task_poll_task 里调用，不在 sampler 里）。
 * 返回 true = 已生效，调用方需发 ack + applied 回执。
 * 注意：这里绝不放 s_ctrl_pending_* 之外的阻塞操作，也不碰 s_upload_count
 * （它归 sampler 在 s_state_mutex 下管；恢复后的空窗口由 sampler 自己丢）。 */
static bool task_apply_control(const char *request_id, const char *kind,
                               double duration_s)
{
    if (!s_task_mutex || !request_id || !kind) {
        return false;
    }

    bool is_pause = (strcmp(kind, "pause") == 0);
    if (!is_pause && strcmp(kind, "resume") != 0) {
        return false;      /* 未知 kind：交给服务端超时，不猜 */
    }

    int64_t mono_now = esp_timer_get_time() / 1000;
    if (is_pause) {
        if (!(duration_s > 0) || duration_s > TASK_MAX_PAUSE_S) {
            duration_s = TASK_PAUSE_FALLBACK_S;
        }
    }

    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    if (is_pause) {
        s_periodic_paused = true;
        s_pause_until_mono_ms = mono_now + (int64_t)(duration_s * 1000.0 + 0.5);
        s_pause_until_epoch_ms = s_time_synced
            ? manual_now_epoch_ms() + (int64_t)(duration_s * 1000.0 + 0.5)
            : 0;      /* 未同步就不报绝对时刻，服务端回退到 now + duration_s */
    } else {
        s_periodic_paused = false;
        s_pause_until_mono_ms = 0;
        s_pause_until_epoch_ms = 0;
    }
    /* 新的控制决定覆盖旧的待重试回执（旧的已被服务端 supersede） */
    snprintf(s_ctrl_pending_rid, sizeof(s_ctrl_pending_rid), "%s", request_id);
    s_ctrl_pending_until_ms = is_pause ? s_pause_until_epoch_ms : 0;
    s_ctrl_pending_left = TASK_APPLIED_RETRY_MAX + 1;
    xSemaphoreGive(s_task_mutex);

    ESP_LOGI(TAG, "[ctrl] %s applied for %s (%.0f s window)",
             kind, request_id, is_pause ? duration_s : 0.0);
    return true;
}

/* Failure bookkeeping; caller must hold s_task_mutex. */
static void manual_fail_locked(const char *reason)
{
    snprintf(s_manual.error, sizeof(s_manual.error), "%s",
             reason ? reason : "unknown error");
    if (s_manual.batch) {
        free(s_manual.batch);
        s_manual.batch = NULL;
    }
    s_manual.state = MANUAL_ERROR;
    ESP_LOGW(TAG, "[task] capture failed: %s", s_manual.error);
}

/* Claim a task for the sampler. Returns false when a capture is already
 * running (e.g. a second poll landed while one is in flight). */
static bool manual_capture_begin(const char *request_id, uint32_t rate_hz,
                                 uint32_t target, upload_batch_t *batch,
                                 int64_t server_expires_ms)
{
    if (!s_task_mutex) {
        return false;
    }
    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    if (s_manual.state != MANUAL_IDLE) {
        xSemaphoreGive(s_task_mutex);
        return false;
    }
    memset(&s_manual, 0, sizeof(s_manual));
    snprintf(s_manual.request_id, sizeof(s_manual.request_id), "%s", request_id);
    s_manual.rate_hz = rate_hz;
    s_manual.target = target;
    /* The sampler always ticks at TARGET_SAMPLE_FREQ_HZ; lower rates are
     * produced by keeping every period_ticks-th tick. */
    s_manual.period_ticks = (rate_hz >= TARGET_SAMPLE_FREQ_HZ)
                                ? 1
                                : (TARGET_SAMPLE_FREQ_HZ / rate_hz);
    if (s_manual.period_ticks == 0) {
        s_manual.period_ticks = 1;
    }
    /* Local safety deadline: 2x the expected capture time + 5 s. */
    s_manual.deadline_ms = esp_timer_get_time() / 1000
        + ((int64_t)target * 1000 / (int64_t)rate_hz) * 2 + 5000;
    s_manual.server_expires_ms = server_expires_ms;
    s_manual.batch = batch;
    s_manual.state = MANUAL_PENDING;
    xSemaphoreGive(s_task_mutex);

    ESP_LOGI(TAG, "[task] accepted %s: %" PRIu32 " samples @ %" PRIu32 " Hz",
             request_id, target, rate_hz);
    return true;
}

/* uploader_task reports the upload result so the state machine can move on. */
static void manual_capture_on_upload_done(bool ok, const char *reason)
{
    if (!s_task_mutex) {
        return;
    }
    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    if (s_manual.state == MANUAL_UPLOADING) {
        if (ok) {
            s_manual.state = MANUAL_IDLE;
            s_manual.request_id[0] = '\0';
            ESP_LOGI(TAG, "[task] done, periodic uploads resumed");
        } else {
            manual_fail_locked(reason ? reason : "manual upload failed");
        }
    }
    xSemaphoreGive(s_task_mutex);
}

/* Called by sampler_task once per sampling tick (holding s_state_mutex).
 * Returns true when this tick belongs to the on-demand capture, in which case
 * the periodic upload window must not be fed - that is the documented
 * "periodic upload is paused while a manual capture runs" behaviour.
 * Non-blocking by design: a full queue is retried on the next tick. */
static bool manual_capture_consumes_sample(uint64_t ts_ms, float ax, float ay,
                                           float az)
{
    if (!s_task_mutex) {
        return false;
    }
    xSemaphoreTake(s_task_mutex, portMAX_DELAY);

    if (s_manual.state == MANUAL_PENDING) {
        /* First tick after claiming: drop the partially filled periodic window
         * and refuse to start when the task already expired server-side. */
        if (s_time_synced && s_manual.server_expires_ms > 0
            && manual_now_epoch_ms() > s_manual.server_expires_ms) {
            manual_fail_locked("task expired before capture");
            xSemaphoreGive(s_task_mutex);
            return false;
        }
        s_manual.state = MANUAL_CAPTURING;
        s_manual.tick = 0;
        s_upload_count = 0;
        ESP_LOGI(TAG, "[task] capturing %s", s_manual.request_id);
    }

    if (s_manual.state != MANUAL_CAPTURING) {
        xSemaphoreGive(s_task_mutex);
        return false;      /* idle / uploading / error → normal periodic path */
    }

    if (s_manual.batch == NULL) {
        manual_fail_locked("batch buffer missing");
        xSemaphoreGive(s_task_mutex);
        return true;
    }

    if ((s_manual.tick % s_manual.period_ticks) == 0
        && s_manual.batch->count < s_manual.batch->cap) {
        upload_sample_t *dst = &s_manual.batch->samples[s_manual.batch->count];
        dst->ts_ms = ts_ms;
        dst->ax = ax;
        dst->ay = ay;
        dst->az = az;
        if (s_manual.batch->count == 0) {
            s_manual.batch->ts_ms = ts_ms;   /* batch start time for the server */
        }
        s_manual.batch->count++;
    }
    s_manual.tick++;

    int64_t now_ms = esp_timer_get_time() / 1000;
    bool full = (s_manual.batch->count >= s_manual.target);

    if (!full) {
        if (now_ms > s_manual.deadline_ms) {
            manual_fail_locked("capture timeout");
        }
        xSemaphoreGive(s_task_mutex);
        return true;
    }

    /* Hand the finished batch to uploader_task. Never blocks the 10 ms loop:
     * if the queue is full we simply retry on the following tick. */
    upload_batch_t *batch = s_manual.batch;
    if (xQueueSend(s_upload_queue, &batch, 0) == pdTRUE) {
        s_manual.batch = NULL;
        s_manual.state = MANUAL_UPLOADING;
        ESP_LOGI(TAG, "[task] captured %" PRIu32 " samples for %s",
                 batch->count, s_manual.request_id);
    } else if (now_ms > s_manual.deadline_ms) {
        manual_fail_locked("upload queue full");
    }

    xSemaphoreGive(s_task_mutex);
    return true;
}

/* ---------------- task HTTP helpers ---------------- */
typedef struct {
    char  *buf;
    size_t len;
    size_t cap;
} http_buf_t;

static esp_err_t http_collect_event_handler(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        http_buf_t *hb = (http_buf_t *)evt->user_data;
        if (hb && hb->buf && (hb->len + 1) < hb->cap) {
            size_t room = hb->cap - hb->len - 1;
            size_t n = ((size_t)evt->data_len < room) ? (size_t)evt->data_len : room;
            memcpy(hb->buf + hb->len, evt->data, n);
            hb->len += n;
            hb->buf[hb->len] = '\0';
        }
    }
    return ESP_OK;
}

/* Derive sibling API URLs from the configured upload URL by swapping the
 * "/api/v1/upload" suffix for path. Returns false when the suffix is absent,
 * so callers can disable polling instead of hitting a wrong address.
 *
 * Contract: CONFIG_SENSOR_SERVER_URL must *end* with that suffix (an accidental
 * trailing '/' is tolerated). If something follows the suffix -- a query
 * string, a redirect target -- this function would silently drop it while the
 * upload itself keeps using the configured URL verbatim, i.e. uploads would
 * work while tasks/photos/live mysteriously fail. A URL that merely *contains*
 * the suffix has the opposite failure mode (we would build
 * "http://host/redir?u=/api/v1/tasks/next"). Both are rejected here so the
 * misconfiguration shows up as a loud, local "cannot derive API base" warning. */
static bool server_api_url(char *out, size_t out_len, const char *path)
{
    const char *base = CONFIG_SENSOR_SERVER_URL;
    const char *suffix = "/api/v1/upload";
    const char *pos = strstr(base, suffix);
    if (!pos) {
        return false;
    }
    /* The suffix must be the *tail* of the configured URL: */
    const char *tail = pos + strlen(suffix);
    if (*tail == '/') {
        tail++;
    }
    if (*tail != '\0') {
        return false;
    }
    int n = snprintf(out, out_len, "%.*s%s", (int)(pos - base), base, path);
    return (n > 0 && (size_t)n < out_len);
}

static void task_http_common(esp_http_client_handle_t client)
{
    if (CONFIG_SENSOR_TOKEN[0] != '\0') {
        char auth[160];
        snprintf(auth, sizeof(auth), "Bearer %s", CONFIG_SENSOR_TOKEN);
        esp_http_client_set_header(client, "Authorization", auth);
    }
}

/* GET url and copy the (truncated) body into out. true = HTTP 2xx. */
static bool http_get_text(const char *url, char *out, size_t out_len)
{
    http_buf_t hb = { .buf = out, .len = 0, .cap = out_len };
    if (out_len > 0) {
        out[0] = '\0';
    }
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = TASK_HTTP_TIMEOUT_MS,
        .event_handler = http_collect_event_handler,
        .user_data = &hb,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        return false;
    }
    task_http_common(client);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || status < 200 || status >= 300) {
        ESP_LOGW(TAG, "[task] GET failed err=%s http=%d",
                 esp_err_to_name(err), status);
        return false;
    }
    return true;
}

/* POST a JSON body ("{}" when json is NULL). true = HTTP 2xx. */
static bool http_post_text(const char *url, const char *json)
{
    const char *body = json ? json : "{}";
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = TASK_HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        return false;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, body, (int)strlen(body));
    task_http_common(client);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || status < 200 || status >= 300) {
        ESP_LOGW(TAG, "[task] POST %s failed err=%s http=%d",
                 url, esp_err_to_name(err), status);
        return false;
    }
    return true;
}

/* POST /api/v1/tasks/{id}/ack - best effort, the capture proceeds regardless. */
static bool task_post_ack(const char *request_id)
{
    char path[96];
    char url[192];
    snprintf(path, sizeof(path), TASK_API_PATH_ACK, request_id);
    if (!server_api_url(url, sizeof(url), path)) {
        return false;
    }
    return http_post_text(url, NULL);
}

/* POST /api/v1/tasks/{id}/fail with a human readable reason. */
static bool task_post_fail(const char *request_id, const char *reason)
{
    char path[96];
    char url[192];
    char body[192];
    snprintf(path, sizeof(path), TASK_API_PATH_FAIL, request_id);
    if (!server_api_url(url, sizeof(url), path)) {
        return false;
    }
    snprintf(body, sizeof(body), "{\"error\": \"%s\"}", reason ? reason : "");
    return http_post_text(url, body);
}

/* POST /api/v1/tasks/{id}/applied - "the control task took effect".
 * Unlike ack this one is NOT best effort: it is what closes a pause/resume task
 * server-side (control tasks carry no samples), and it is also what makes the
 * server write device_control, i.e. the Web page's source of truth. */
static bool task_post_applied(const char *request_id, int64_t paused_until_epoch_ms)
{
    char path[96];
    char url[192];
    char body[96];
    snprintf(path, sizeof(path), TASK_API_PATH_APPLIED, request_id);
    if (!server_api_url(url, sizeof(url), path)) {
        return false;
    }
    if (paused_until_epoch_ms > 0) {
        /* Board's own deadline in epoch ms: the server prefers it inside a sane
         * window so the page countdown matches the board's auto-resume exactly. */
        snprintf(body, sizeof(body), "{\"paused_until_ms\": %" PRId64 "}",
                 paused_until_epoch_ms);
    } else {
        snprintf(body, sizeof(body), "{}");   /* unsynced clock: server uses now+duration_s */
    }
    return http_post_text(url, body);
}

/* POST 一帧 JPEG 到 /api/v1/photos（流式发送，不再把整帧拷进 http 发送缓冲）。
 * 成功 = HTTP 2xx：服务端在同一事务里存图并把 camera 任务置 completed。
 * status_out 回传 HTTP 状态码（0 = 网络层失败，没拿到状态码）。 */
static bool photo_post_frame(const jpeg_frame_t *frame, const char *request_id,
                             uint64_t ts_ms, int *status_out)
{
    char path[224];
    char url[288];
    char resp[PHOTO_RESP_BUF_LEN];
    http_buf_t hb = { .buf = resp, .len = 0, .cap = sizeof(resp) };
    int status = 0;

    if (status_out) {
        *status_out = 0;
    }
    if (!frame || !frame->data || frame->len == 0 || !request_id) {
        return false;
    }
    memset(resp, 0, sizeof(resp));

    snprintf(path, sizeof(path),
             PHOTO_API_PATH "?device_id=%s&request_id=%s&ts_ms=%" PRIu64
             "&w=%" PRIu32 "&h=%" PRIu32,
             CONFIG_SENSOR_DEVICE_ID, request_id, ts_ms,
             frame->width, frame->height);
    if (!server_api_url(url, sizeof(url), path)) {
        ESP_LOGW(TAG, "[photo] cannot derive API base from '%s'",
                 CONFIG_SENSOR_SERVER_URL);
        return false;
    }

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = PHOTO_HTTP_TIMEOUT_MS,
        .event_handler = http_collect_event_handler,
        .user_data = &hb,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        return false;
    }

    bool ok = false;
    esp_http_client_set_header(client, "Content-Type", "image/jpeg");
    task_http_common(client);        /* 可选 Bearer token，与其它接口一致 */
    /* write_len >= 0 → 用 Content-Length 定长发送（避免分块编码） */
    if (esp_http_client_open(client, (int)frame->len) == ESP_OK) {
        int written = esp_http_client_write(client, (const char *)frame->data,
                                            (int)frame->len);
        if (written == (int)frame->len) {
            esp_http_client_fetch_headers(client);
            status = esp_http_client_get_status_code(client);
            ok = (status >= 200 && status < 300);
            if (!ok) {
                esp_http_client_read_response(client, resp, sizeof(resp) - 1);
                ESP_LOGW(TAG, "[photo] server rejected the frame: http=%d body=%s",
                         status, resp[0] ? resp : "-");
            }
        } else {
            ESP_LOGW(TAG, "[photo] short write %d/%u B",
                     written, (unsigned)frame->len);
        }
        esp_http_client_close(client);
    } else {
        ESP_LOGW(TAG, "[photo] HTTP open failed: %s", url);
    }
    esp_http_client_cleanup(client);

    if (status_out) {
        *status_out = status;
    }
    return ok;
}

/* 处理 kind=camera 任务：不占用采样器，直接拍一帧 JPEG 上传。
 * 上传成功 = 服务端在同一事务里把任务置 completed；失败走 /fail 回执，
 * 让 Web 立刻看到原因，而不是等有效期到了才超时。 */
static void task_handle_camera(const char *request_id, int64_t server_expires_ms)
{
    if (s_time_synced && server_expires_ms > 0
        && manual_now_epoch_ms() > server_expires_ms) {
        task_post_fail(request_id, "task expired before capture");
        return;
    }

    /* ack 是尽力而为：即便 ack 失败也要继续拍，服务端不靠 ack 收尾 */
    if (!task_post_ack(request_id)) {
        ESP_LOGW(TAG, "[photo] ack failed for %s (capture continues)", request_id);
    }

    if (!s_camera_ready) {
        task_post_fail(request_id, "camera not initialized on device");
        return;
    }

    jpeg_frame_t frame = { 0 };
    if (app_camera_capture_jpeg(&frame) != ESP_OK) {
        task_post_fail(request_id, "camera capture failed");
        return;
    }
    ESP_LOGI(TAG, "[photo] captured %u B %" PRIu32 "x%" PRIu32 " for %s",
             (unsigned)frame.len, frame.width, frame.height, request_id);

    const uint64_t ts_ms = (uint64_t)manual_now_epoch_ms();
    int status = 0;
    bool ok = photo_post_frame(&frame, request_id, ts_ms, &status);
    ESP_LOGI(TAG, "[photo] upload %s ts=%" PRIu64 " bytes=%u http=%d",
             ok ? "OK" : "FAIL", ts_ms, (unsigned)frame.len, status);
    jpeg_frame_free(&frame);

    if (!ok && (status == 0 || status >= 500)) {
        /* 5xx / 网络错误：服务端不一定知道这次拍照的下场，由板端补一条 fail。
         * 4xx 则相反——服务端已经判定这张照片不合法并把任务置 failed（或根本不
         * 认识这个 request_id），再发 /fail 只会互相覆盖，所以只记日志。 */
        task_post_fail(request_id, "photo upload failed");
    }
    log_heap("task: photo done");
}

/* 处理 kind=preview 任务：切换本地 LCD 相机预览开关（Web 触发；与长按 Button A
 * 的本地预览态共用 s_preview_active）。与 pause/resume 同属控制类、不产生观测数据，
 * 靠 /applied 收尾；但不影响周期上报，服务端 applied 只置 completed、不写 device_control。 */
static void task_handle_preview(const char *request_id)
{
    /* ack 尽力而为：失败也继续切，服务端不靠 ack 收尾 */
    if (!task_post_ack(request_id)) {
        ESP_LOGW(TAG, "[prev] ack failed for %s (toggle continues)", request_id);
    }
    if (!s_camera_ready) {
        task_post_fail(request_id, "camera not initialized on device");
        return;
    }
    if (s_preview_active) {
        s_preview_active = false;   /* 预览任务下一轮自行收尾 UI */
        ESP_LOGI(TAG, "[prev] stop requested (Web task %s)", request_id);
    } else if (!camera_preview_start()) {
        task_post_fail(request_id, "camera preview failed to start");
        return;
    } else {
        ESP_LOGI(TAG, "[prev] started (Web task %s)", request_id);
    }
    /* 0 = 不报 paused_until；服务端对 preview 直接置 completed */
    task_post_applied(request_id, 0);
    log_heap("task: preview done");
}

/* ================================================================
 *  摄像头实时直播（长按 Button A 开关 → POST /api/v1/live）
 *
 *  与「按需单帧拍照」的区别：直播是连续流，没有 request_id、不做任务收尾，
 *  服务端只在内存里保留该设备的最新一帧；丢一帧不需要重传（下一帧 500 ms 后到）。
 *  与相机的关系：两者共用 /dev/video0，靠 s_camera_mutex 串行
 *  （见 app_camera_capture_jpeg）。不占用采样器、不暂停周期上报。
 * ================================================================ */

/* POST 一帧 JPEG 到 /api/v1/live（流式发送，Content-Length 定长）。
 * 成功 = HTTP 2xx；status_out 回传状态码（0 = 网络层失败，没拿到状态码）。 */
static bool live_post_frame(const jpeg_frame_t *frame, uint64_t ts_ms, int *status_out)
{
    char path[224];
    char url[288];
    char resp[PHOTO_RESP_BUF_LEN];
    http_buf_t hb = { .buf = resp, .len = 0, .cap = sizeof(resp) };
    int status = 0;

    if (status_out) {
        *status_out = 0;
    }
    if (!frame || !frame->data || frame->len == 0) {
        return false;
    }
    memset(resp, 0, sizeof(resp));

    snprintf(path, sizeof(path),
             LIVE_API_PATH "?device_id=%s&ts_ms=%" PRIu64 "&w=%" PRIu32 "&h=%" PRIu32,
             CONFIG_SENSOR_DEVICE_ID, ts_ms, frame->width, frame->height);
    if (!server_api_url(url, sizeof(url), path)) {
        ESP_LOGW(TAG, "[live] cannot derive API base from '%s'",
                 CONFIG_SENSOR_SERVER_URL);
        return false;
    }

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = LIVE_HTTP_TIMEOUT_MS,
        .event_handler = http_collect_event_handler,
        .user_data = &hb,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        return false;
    }

    bool ok = false;
    esp_http_client_set_header(client, "Content-Type", "image/jpeg");
    task_http_common(client);        /* 可选 Bearer token，与其它接口一致 */
    /* write_len >= 0 → 用 Content-Length 定长发送（避免分块编码） */
    if (esp_http_client_open(client, (int)frame->len) == ESP_OK) {
        int written = esp_http_client_write(client, (const char *)frame->data,
                                            (int)frame->len);
        if (written == (int)frame->len) {
            esp_http_client_fetch_headers(client);
            status = esp_http_client_get_status_code(client);
            ok = (status >= 200 && status < 300);
            if (!ok) {
                esp_http_client_read_response(client, resp, sizeof(resp) - 1);
                ESP_LOGW(TAG, "[live] server rejected the frame: http=%d body=%s",
                         status, resp[0] ? resp : "-");
            }
        } else {
            ESP_LOGW(TAG, "[live] short write %d/%u B",
                     written, (unsigned)frame->len);
        }
        esp_http_client_close(client);
    } else {
        ESP_LOGW(TAG, "[live] HTTP open failed: %s", url);
    }
    esp_http_client_cleanup(client);

    if (status_out) {
        *status_out = status;
    }
    return ok;
}

/* 推流任务：循环「拍一帧 → 上传」，直到 s_live_streaming 被长按清零、WiFi 掉线
 * 或连续失败达到上限。结束时自己 vTaskDelete(NULL)：按键回调只负责置位/清零，
 * 绝不做 vTaskDelete，避免与「任务正在删自己」的竞态。 */
static void live_stream_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "[live] streaming started (%d ms/frame, POST %s)",
             LIVE_FRAME_INTERVAL_MS, LIVE_API_PATH);
    s_live_frames = 0;
    s_live_last_status = 0;
    refresh_ui();

    int fail_streak = 0;
    while (s_live_streaming) {
        if (!s_wifi_connected) {
            ESP_LOGW(TAG, "[live] stopped: WiFi disconnected");
            break;
        }

        jpeg_frame_t frame = { 0 };
        /* 直播走 live 入口：会话保持打开（不再每帧 STREAMON/STREAMOFF），
         * DQBUF 上限 800 ms，坏帧快速失败重试。 */
        esp_err_t ret = app_camera_capture_jpeg_live(&frame);
        if (ret != ESP_OK) {
            /* 相机忙（按需拍照正在用）也走这里，下一轮自然重试 */
            fail_streak++;
            ESP_LOGW(TAG, "[live] capture failed: %s (streak=%d)",
                     esp_err_to_name(ret), fail_streak);
        } else {
            const unsigned flen = (unsigned)frame.len;
            int status = 0;
            bool ok = live_post_frame(&frame, (uint64_t)manual_now_epoch_ms(), &status);
            s_live_last_status = status;
            jpeg_frame_free(&frame);
            if (ok) {
                fail_streak = 0;
                s_live_frames++;
                ESP_LOGI(TAG, "[live] frame %" PRIu32 " sent (%u B, http=%d)",
                         s_live_frames, flen, status);
            } else {
                fail_streak++;
                ESP_LOGW(TAG, "[live] upload failed: http=%d (streak=%d)",
                         status, fail_streak);
            }
        }

        if (fail_streak >= LIVE_FAIL_LIMIT) {
            ESP_LOGW(TAG, "[live] stopped after %d consecutive failures", fail_streak);
            break;
        }

        vTaskDelay(pdMS_TO_TICKS(LIVE_FRAME_INTERVAL_MS));
        refresh_ui();        /* LCD 的帧数 / 状态栏随推流实时更新 */
    }

    s_live_streaming = false;
    ESP_LOGI(TAG, "[live] streaming stopped (%" PRIu32 " frames sent, last http=%d)",
             s_live_frames, s_live_last_status);
    log_heap("live: task done");
    refresh_ui();
    vTaskDelete(NULL);
}

/* 长按 Button A（2 s）→ 切换直播。按键回调只做「置位 + 建任务」/「清零」这类轻活；
 * LCD 刷新交给 live_stream_task 的起止点，按键上下文不碰 LVGL。 */
static void live_streaming_toggle(void)
{
    if (s_live_streaming) {
        s_live_streaming = false;    /* 任务在下一轮循环检查时收尾（≤ 一次上传的时间） */
        ESP_LOGI(TAG, "[live] stop requested (long press Button A)");
        return;
    }

    if (!s_camera_ready) {
        ESP_LOGW(TAG, "[live] camera not initialized - cannot stream");
        return;
    }
    if (!s_wifi_connected) {
        ESP_LOGW(TAG, "[live] WiFi not connected - cannot stream");
        return;
    }

    s_live_streaming = true;
    if (xTaskCreate(live_stream_task, "live_task", LIVE_TASK_STACK, NULL,
                    LIVE_TASK_PRIORITY, NULL) != pdPASS) {
        s_live_streaming = false;    /* 任务没起来就回到「未推流」，避免状态骗人 */
        ESP_LOGE(TAG, "[live] task create failed (heap?)");
        return;
    }
    ESP_LOGI(TAG, "[live] streaming enabled: long press Button A again to stop");
}

/* 长按 Button A 三态循环：关 → Web 直播 → 本地 LCD 预览 → 关。 */
static void camera_mode_toggle(void)
{
    if (s_preview_active) {
        s_preview_active = false;    /* 预览 → 关（预览任务下一轮自行收尾 UI） */
        ESP_LOGI(TAG, "[prev] stop requested (long press Button A)");
        return;
    }
    if (s_live_streaming) {
        s_live_streaming = false;    /* Web 直播 → 本地预览（直播任务下一轮收尾） */
        ESP_LOGI(TAG, "[live] stop requested, switching to local preview");
        camera_preview_start();
        return;
    }
    live_streaming_toggle();         /* 关 → Web 直播 */
}

static void button_a_long_press_cb(void *arg, void *data)
{
    (void)arg;
    (void)data;
    camera_mode_toggle();
}

/* Retry the applied receipt of the last control task (bounded: a receipt older
 * than a couple of polls is no longer useful, the server task has timed out and
 * a newer control decision must win). Runs in task_poll_task. */
static void task_report_pending_control(void)
{
    char request_id[TASK_REQUEST_ID_LEN];
    int64_t until_ms;
    if (!s_task_mutex) {
        return;
    }
    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    if (s_ctrl_pending_rid[0] == '\0') {
        xSemaphoreGive(s_task_mutex);
        return;
    }
    if (s_ctrl_pending_left <= 0) {
        ESP_LOGW(TAG, "[ctrl] giving up on applied receipt for %s",
                 s_ctrl_pending_rid);
        s_ctrl_pending_rid[0] = '\0';
        xSemaphoreGive(s_task_mutex);
        return;
    }
    snprintf(request_id, sizeof(request_id), "%s", s_ctrl_pending_rid);
    until_ms = s_ctrl_pending_until_ms;
    s_ctrl_pending_left--;
    xSemaphoreGive(s_task_mutex);

    if (task_post_applied(request_id, until_ms)) {
        xSemaphoreTake(s_task_mutex, portMAX_DELAY);
        if (strcmp(s_ctrl_pending_rid, request_id) == 0) {
            s_ctrl_pending_rid[0] = '\0';
        }
        xSemaphoreGive(s_task_mutex);
        ESP_LOGI(TAG, "[ctrl] applied receipt delivered for %s", request_id);
    } else {
        ESP_LOGW(TAG, "[ctrl] applied receipt retry pending for %s", request_id);
    }
}

/* Send the pending fail receipt, then go back to IDLE. A report that fails is
 * retried on the next poll because the state stays MANUAL_ERROR. */
static void task_report_pending_failure(void)
{
    char request_id[TASK_REQUEST_ID_LEN];
    char reason[96];
    if (!s_task_mutex) {
        return;
    }
    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    if (s_manual.state != MANUAL_ERROR) {
        xSemaphoreGive(s_task_mutex);
        return;
    }
    snprintf(request_id, sizeof(request_id), "%s", s_manual.request_id);
    snprintf(reason, sizeof(reason), "%s", s_manual.error);
    xSemaphoreGive(s_task_mutex);

    if (!task_post_fail(request_id, reason)) {
        return;
    }

    xSemaphoreTake(s_task_mutex, portMAX_DELAY);
    if (s_manual.state == MANUAL_ERROR) {
        s_manual.state = MANUAL_IDLE;
        s_manual.request_id[0] = '\0';
        ESP_LOGW(TAG, "[task] failure reported to server: %s", reason);
    }
    xSemaphoreGive(s_task_mutex);
}

/* Normalise the server's `expires_at` into epoch ms.
 *
 * The server sends epoch SECONDS as a JSON float (created as `now + timeout_s`
 * in server/main.py, e.g. 1767123516.78), while this module compares it against
 * the epoch ms from gettimeofday(). Reading the raw value as ms made every
 * task look already expired ("task expired before capture"). Anything below
 * 1e12 is therefore treated as seconds, so a deployment that switches to
 * milliseconds keeps working too. */
static int64_t expires_to_epoch_ms(double value)
{
    if (!(value > 0)) {
        return 0;
    }
    return (value < 1.0e12) ? (int64_t)(value * 1000.0 + 0.5) : (int64_t)value;
}

/* Interpret one /api/v1/tasks/next response (runs in task_poll_task). */
static void task_handle_next_response(const char *resp)
{
    cJSON *root = cJSON_Parse(resp);
    if (!root) {
        ESP_LOGW(TAG, "[task] unparsable response from server");
        return;
    }
    const cJSON *task = cJSON_GetObjectItem(root, "task");
    const cJSON *rid = cJSON_IsObject(task)
                           ? cJSON_GetObjectItem(task, "request_id") : NULL;
    const cJSON *cnt = cJSON_IsObject(task)
                           ? cJSON_GetObjectItem(task, "sample_count") : NULL;
    const cJSON *rate = cJSON_IsObject(task)
                            ? cJSON_GetObjectItem(task, "sample_rate_hz") : NULL;
    const cJSON *exp = cJSON_IsObject(task)
                           ? cJSON_GetObjectItem(task, "expires_at") : NULL;
    const cJSON *kind = cJSON_IsObject(task)
                            ? cJSON_GetObjectItem(task, "kind") : NULL;
    const cJSON *dur = cJSON_IsObject(task)
                           ? cJSON_GetObjectItem(task, "duration_s") : NULL;
    if (!cJSON_IsString(rid)) {
        cJSON_Delete(root);
        return;         /* found=false or unexpected body: nothing to do */
    }

    char request_id[TASK_REQUEST_ID_LEN];
    snprintf(request_id, sizeof(request_id), "%s", rid->valuestring);

    /* Control tasks (pause/resume) are handled first and never reach the sample
     * bookkeeping below: they carry no samples, so sample_count / rate must not
     * be validated against them. */
    if (cJSON_IsString(kind) && kind->valuestring
        && (strcmp(kind->valuestring, "pause") == 0
            || strcmp(kind->valuestring, "resume") == 0)) {
        char ctrl_kind[TASK_KIND_LEN];
        snprintf(ctrl_kind, sizeof(ctrl_kind), "%s", kind->valuestring);
        double duration_s = cJSON_IsNumber(dur) ? dur->valuedouble : 0.0;
        cJSON_Delete(root);
        if (!task_apply_control(request_id, ctrl_kind, duration_s)) {
            return;
        }
        /* ack is optional bookkeeping; applied is what actually closes the task,
         * so a failed ack only logs (the applied receipt is retried by the poll
         * loop while a failed ack would just leave acked_at empty). */
        if (!task_post_ack(request_id)) {
            ESP_LOGW(TAG, "[ctrl] ack failed for %s (state already applied)",
                     request_id);
        }
        task_report_pending_control();
        return;
    }

    /* 拍照任务（kind=camera）：不采数据、不占用采样器，拍一帧 JPEG 直接上传，
     * 所以必须在下面的 sample_count / sample_rate_hz 校验之前分流。 */
    if (cJSON_IsString(kind) && kind->valuestring
        && strcmp(kind->valuestring, "camera") == 0) {
        int64_t cam_expires_ms = cJSON_IsNumber(exp)
                                     ? expires_to_epoch_ms(exp->valuedouble) : 0;
        cJSON_Delete(root);
        task_handle_camera(request_id, cam_expires_ms);
        return;
    }

    /* 本地 LCD 预览（kind=preview）：切换开关，不采数据、不占用采样器；
     * 与 pause/resume 一样靠 /applied 收尾，见 task_handle_preview()。 */
    if (cJSON_IsString(kind) && kind->valuestring
        && strcmp(kind->valuestring, "preview") == 0) {
        cJSON_Delete(root);
        task_handle_preview(request_id);
        return;
    }

    if (!cJSON_IsNumber(cnt) || !cJSON_IsNumber(rate)) {
        cJSON_Delete(root);
        return;         /* capture task without a usable spec: ignore */
    }
    uint32_t target = (uint32_t)cnt->valuedouble;
    uint32_t rate_hz = (uint32_t)rate->valuedouble;
    int64_t expires_ms = cJSON_IsNumber(exp)
                             ? expires_to_epoch_ms(exp->valuedouble) : 0;
    cJSON_Delete(root);

    if (target == 0 || target > TASK_MAX_SAMPLES || rate_hz == 0
        || rate_hz > TARGET_SAMPLE_FREQ_HZ) {
        ESP_LOGW(TAG, "[task] %s asks %" PRIu32 " samples @ %" PRIu32
                      " Hz - beyond device capability",
                 request_id, target, rate_hz);
        task_post_fail(request_id,
                       "sample_count or sample_rate_hz beyond device capability");
        return;
    }

    upload_batch_t *batch = upload_batch_alloc(target, "manual", request_id);
    if (!batch) {
        ESP_LOGE(TAG, "[task] out of memory for %" PRIu32 " samples", target);
        log_heap("task: batch alloc failed");
        task_post_fail(request_id, "out of memory on device");
        return;
    }

    if (!manual_capture_begin(request_id, rate_hz, target, batch, expires_ms)) {
        free(batch);                /* a capture is already in flight */
        return;
    }

    if (!task_post_ack(request_id)) {
        ESP_LOGW(TAG, "[task] ack failed for %s (capture continues)", request_id);
    }
}

/* Poll /api/v1/tasks/next and hand tasks to the sampler. Priority 3, i.e.
 * below sampler_task (5) and uploader_task (4). */
static void task_poll_task(void *arg)
{
    (void)arg;
    char base_url[192];
    char url[256];

    if (!server_api_url(base_url, sizeof(base_url), TASK_API_PATH_NEXT)) {
        ESP_LOGW(TAG, "[task] cannot derive API base from '%s' - polling disabled",
                 CONFIG_SENSOR_SERVER_URL);
        vTaskDelete(NULL);
        return;
    }
    snprintf(url, sizeof(url), "%s?device_id=%s", base_url,
             CONFIG_SENSOR_DEVICE_ID);

    char *resp = malloc(TASK_RESP_BUF_LEN);
    if (!resp) {
        ESP_LOGE(TAG, "[task] no heap for response buffer - polling disabled");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "[task] polling %s every %d ms", url, TASK_POLL_INTERVAL_MS);

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(TASK_POLL_INTERVAL_MS));

        if (!s_wifi_connected) {
            continue;
        }

        manual_state_t state = manual_capture_state();
        if (state == MANUAL_ERROR) {
            task_report_pending_failure();
            continue;
        }
        if (state != MANUAL_IDLE) {
            continue;       /* capture/upload in flight: do not claim another */
        }

        /* A pause/resume task that the server never acknowledged is re-reported
         * here (bounded), exactly like the failed-capture receipt above. This is
         * the one place that must keep running while uploads are paused: if the
         * poll stopped, the resume task could never be received. */
        task_report_pending_control();

        if (http_get_text(url, resp, TASK_RESP_BUF_LEN)) {
            task_handle_next_response(resp);
        }
    }
}

/* ================================================================
 *  闭环事件（loop event）—— 触发 / 本地反馈 / 远端显示 / 回应·取消
 *
 *  与 task_poll_task 的分工刻意分开，互不干扰：
 *    task_poll_task ：服务端 → 板端（领取 Web 下发的采集/拍照/控制任务）
 *    loop_task      ：板端 → 服务端（本板发起的闭环事件，反向）
 *  之所以不把闭环轮询塞进 task_poll_task：那里的每一次 HTTP 最长阻塞
 *  TASK_HTTP_TIMEOUT_MS(5 s)，而 task_poll_task 每 3 s 就要领一次任务——
 *  网络差时多塞两个请求会把它拖垮，直接连累「采集一次」「拍一张照片」。
 *  独立任务 + 独立周期，闭环再慢也不影响既有链路。
 * ================================================================ */

/* ---------- 本地反馈：板载 LED（GPIO3，开漏，非阻塞） ---------- */

static void loop_led_apply(bool on)
{
    if (LOOP_LED_GPIO < 0) {
        return;
    }
    /* 开漏：level=1 是释放总线（由外部电路决定亮灭），level=0 是把线拉低。
     * 具体哪个是"亮"由 LOOP_LED_ON_LEVEL 决定，见配置区说明。 */
    int level = on ? LOOP_LED_ON_LEVEL : (LOOP_LED_ON_LEVEL ? 0 : 1);
    gpio_set_level((gpio_num_t)LOOP_LED_GPIO, level);
}

/* 闪烁任务：唯一允许 vTaskDelay 的地方。按键回调只往队列里丢"还要闪几下"，
 * 绝不在回调里等待——iot_button 的任务栈很小，阻塞会拖垮按键识别。 */
static void loop_led_task(void *arg)
{
    (void)arg;
    uint8_t times = 0;

    loop_led_apply(LOOP_LED_REST_ON ? true : false);

    while (true) {
        if (xQueueReceive(s_led_blink_queue, &times, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        for (uint8_t i = 0; i < times; i++) {
            loop_led_apply(true);
            vTaskDelay(pdMS_TO_TICKS(LOOP_BLINK_ON_MS));
            loop_led_apply(false);
            vTaskDelay(pdMS_TO_TICKS(LOOP_BLINK_OFF_MS));
        }
        loop_led_apply(LOOP_LED_REST_ON ? true : false);
        /* 两组反馈之间留空隙，避免连按事件时糊成一片看不出次数 */
        vTaskDelay(pdMS_TO_TICKS(LOOP_BLINK_GAP_MS));
    }
}

/* 请求"闪 N 下"。用 xQueueOverwrite（队列长度 1）：永不阻塞、永不失败，
 * 连按只会保留最后一次，正是想要的行为。 */
static void loop_led_flash(uint8_t times)
{
    if (s_led_blink_queue != NULL && times > 0) {
        xQueueOverwrite(s_led_blink_queue, &times);
    }
}

/* ---------- 闭环状态（全部在 s_state_mutex 下读写） ---------- */

static const char *loop_state_name(loop_state_t st)
{
    switch (st) {
    case LOOP_IDLE:        return "IDLE";
    case LOOP_TRIGGERED:   return "TRIGGERED";
    case LOOP_WAITING_ACK: return "WAITING_ACK";
    case LOOP_COMPLETED:   return "COMPLETED";
    default:               return "?";
    }
}

/* 更新状态 + LCD 文本。req 后缀用于把 request_id 前 4 位显示出来，
 * 便于现场比对「板子这条」和「网页那条」是不是同一个事件。 */
static void loop_set_state(loop_state_t st, const char *prefix)
{
    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        s_loop_state = st;
        if (prefix && prefix[0]) {
            if (s_loop_request_id[0]) {
                snprintf(s_loop_status_text, sizeof(s_loop_status_text),
                         "%s %.4s", prefix, s_loop_request_id);
            } else {
                snprintf(s_loop_status_text, sizeof(s_loop_status_text),
                         "%s", prefix);
            }
        }
        xSemaphoreGive(s_state_mutex);
    }
    refresh_ui();       /* 本地反馈要"立刻"看得见，不等下一次周期刷新 */
    ESP_LOGI(TAG, "[loop] state=%s text=%s rid=%s",
             loop_state_name(st), s_loop_status_text, s_loop_request_id);
}

/* 读取当前状态 / request_id 的快照（在锁内拷出来，避免用到半更新的字符串）。
 * rid_out 允许传 NULL，表示"只要状态，不要编号"（按键回调里判断用）。 */
static loop_state_t loop_snapshot(char *rid_out, size_t rid_len)
{
    loop_state_t st = LOOP_IDLE;
    if (rid_out != NULL && rid_len > 0) {
        rid_out[0] = '\0';
    }
    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        st = s_loop_state;
        if (rid_out != NULL && rid_len > 0) {
            snprintf(rid_out, rid_len, "%s", s_loop_request_id);
        }
        xSemaphoreGive(s_state_mutex);
    }
    return st;
}

/* 板端生成闭环编号：128 bit 随机数 → 32 个十六进制字符。
 * 必须板端生成（不能等服务端发号）：按键按下时灯就已经闪了、屏幕就改了，
 * 本地反馈不能依赖网络往返；服务端以 request_id 为主键，重试天然幂等。 */
static void loop_make_request_id(char *out, size_t out_len)
{
    snprintf(out, out_len, "%08" PRIx32 "%08" PRIx32 "%08" PRIx32 "%08" PRIx32,
             esp_random(), esp_random(), esp_random(), esp_random());
}

/* ---------- 按键回调：只置标志 + 本地反馈，绝不阻塞 ---------- */

static void loop_trigger_button_cb(void *arg, void *data)
{
    (void)arg; (void)data;

    loop_state_t st = loop_snapshot(NULL, 0);
    if (st == LOOP_TRIGGERED || st == LOOP_WAITING_ACK) {
        ESP_LOGI(TAG, "[loop] trigger ignored: already in %s", loop_state_name(st));
        return;
    }

    char rid[LOOP_REQUEST_ID_LEN];
    loop_make_request_id(rid, sizeof(rid));

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        snprintf(s_loop_request_id, sizeof(s_loop_request_id), "%s", rid);
        s_loop_need_trigger = true;
        s_loop_need_respond = false;
        s_loop_need_cancel = false;
        xSemaphoreGive(s_state_mutex);
    }

    /* 本地反馈先走：不等 HTTP。灯闪 2 下 + 屏幕立刻变 TRIG */
    loop_led_flash(LOOP_BLINK_TRIGGER);
    loop_set_state(LOOP_TRIGGERED, "LOOP:TRIG");
}

static void loop_respond_button_cb(void *arg, void *data)
{
    (void)arg; (void)data;

    loop_state_t st = loop_snapshot(NULL, 0);
    if (st != LOOP_TRIGGERED && st != LOOP_WAITING_ACK) {
        ESP_LOGI(TAG, "[loop] respond ignored: nothing to respond to (state=%s)",
                 loop_state_name(st));
        return;
    }

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        s_loop_need_respond = true;
        xSemaphoreGive(s_state_mutex);
    }
    /* 灯闪 1 下 + 屏幕变 WAIT：表示"我这边已经回应了，等远端收口" */
    loop_led_flash(LOOP_BLINK_RESPOND);
    loop_set_state(LOOP_WAITING_ACK, "LOOP:WAIT");
}

static void loop_respond_long_press_cb(void *arg, void *data)
{
    (void)arg; (void)data;

    loop_state_t st = loop_snapshot(NULL, 0);
    if (st != LOOP_TRIGGERED && st != LOOP_WAITING_ACK) {
        ESP_LOGI(TAG, "[loop] cancel ignored: nothing to cancel (state=%s)",
                 loop_state_name(st));
        return;
    }

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        s_loop_need_cancel = true;
        s_loop_need_respond = false;
        xSemaphoreGive(s_state_mutex);
    }
    /* 取消用 4 下，与"回应 1 下 / 完成 3 下"一眼可区分 */
    loop_led_flash(LOOP_BLINK_CANCEL);
    loop_set_state(LOOP_WAITING_ACK, "LOOP:CANCEL");
}

/* ---------- 网络侧：触发上报 / 回应上报 / 轮询远端状态 ---------- */

static bool loop_post_trigger(const char *rid)
{
    char url[192];
    if (!server_api_url(url, sizeof(url), LOOP_API_PATH_TRIGGER)) {
        return false;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return false;
    }
    cJSON_AddStringToObject(root, "device_id", CONFIG_SENSOR_DEVICE_ID);
    cJSON_AddStringToObject(root, "source", UPLOAD_SOURCE);
    cJSON_AddStringToObject(root, "kind", "alert");
    cJSON_AddStringToObject(root, "request_id", rid);
    cJSON_AddNumberToObject(root, "ts_ms", (double)(time(NULL) * 1000LL));

    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) {
        log_heap("loop: trigger print failed");
        return false;
    }

    bool ok = http_post_text(url, body);
    free(body);
    ESP_LOGI(TAG, "[loop] trigger %s -> %s", ok ? "OK" : "FAIL", rid);
    return ok;
}

static bool loop_post_respond(const char *rid, const char *action)
{
    char url[192];
    if (!server_api_url(url, sizeof(url), LOOP_API_PATH_RESPOND)) {
        return false;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return false;
    }
    cJSON_AddStringToObject(root, "request_id", rid);
    cJSON_AddStringToObject(root, "action", action);
    cJSON_AddStringToObject(root, "by", "device");

    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) {
        return false;
    }

    bool ok = http_post_text(url, body);
    free(body);
    ESP_LOGI(TAG, "[loop] respond %s (%s) -> %s", action, rid, ok ? "OK" : "FAIL");
    return ok;
}

/* 轮询远端状态。返回远端状态字符串（静态字符串，无网/解析失败时返回 NULL）。 */
static const char *loop_fetch_remote_status(const char *rid)
{
    static const char *names[] = {"pending", "ack", "cancelled", "completed", "expired"};
    char base[192];
    char url[320];
    char resp[LOOP_RESP_BUF_LEN];

    if (!server_api_url(base, sizeof(base), LOOP_API_PATH_STATUS)) {
        return NULL;
    }
    snprintf(url, sizeof(url), "%s?device_id=%s&request_id=%s",
             base, CONFIG_SENSOR_DEVICE_ID, rid);

    if (!http_get_text(url, resp, sizeof(resp))) {
        return NULL;
    }

    cJSON *root = cJSON_Parse(resp);
    if (!root) {
        ESP_LOGW(TAG, "[loop] status JSON parse failed");
        return NULL;
    }

    const char *found = NULL;
    cJSON *j_found = cJSON_GetObjectItem(root, "found");
    if (cJSON_IsBool(j_found) && !cJSON_IsTrue(j_found)) {
        cJSON_Delete(root);
        return NULL;    /* 事件还没登记上：正常过渡态，不当错误 */
    }

    cJSON *j_status = cJSON_GetObjectItem(root, "status");
    if (cJSON_IsString(j_status) && j_status->valuestring) {
        for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
            if (strcmp(names[i], j_status->valuestring) == 0) {
                found = names[i];
                break;
            }
        }
        if (found == NULL) {
            ESP_LOGW(TAG, "[loop] unknown remote status '%s'", j_status->valuestring);
        }
    }
    cJSON_Delete(root);     /* 必须在 return 之前释放：这里只用静态字符串，安全 */
    return found;
}

/* 闭环主循环：1 s 一跳，只做三件事——发触发、发回应/取消、轮询远端。 */
static void loop_task(void *arg)
{
    (void)arg;
    char rid[LOOP_REQUEST_ID_LEN];
    const char *remote;

    ESP_LOGI(TAG, "[loop] task started: poll %d ms, trigger=BSP_BUTTON_%d,"
                  " respond/cancel(long %dms)=BSP_BUTTON_%d",
             LOOP_POLL_INTERVAL_MS,
             (int)LOOP_TRIGGER_BUTTON + 1, LOOP_RESPOND_LONG_MS,
             (int)LOOP_RESPOND_BUTTON + 1);

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(LOOP_POLL_INTERVAL_MS));

        /* 1) 把按键置下的标志变成真正的 HTTP 请求（按键回调里不能发请求） */
        bool do_trigger = false, do_respond = false, do_cancel = false;
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            do_trigger = s_loop_need_trigger;
            do_respond = s_loop_need_respond;
            do_cancel  = s_loop_need_cancel;
            s_loop_need_trigger = false;
            s_loop_need_respond = false;
            s_loop_need_cancel  = false;
            xSemaphoreGive(s_state_mutex);
        }

        if (!s_wifi_connected) {
            /* 离线：标志已经清了，但本地反馈（灯/屏）早于网络发生，
             * 这里只提示失败，不让状态机永久卡住。 */
            if (do_trigger) {
                loop_led_flash(LOOP_BLINK_FAIL);
                loop_set_state(LOOP_COMPLETED, "LOOP:NO NET");
                s_loop_done_at_ms = esp_timer_get_time() / 1000;
            }
            continue;
        }

        if (do_trigger) {
            loop_snapshot(rid, sizeof(rid));
            if (loop_post_trigger(rid)) {
                s_loop_deadline_ms = esp_timer_get_time() / 1000 + LOOP_LOCAL_TIMEOUT_MS;
            } else {
                loop_led_flash(LOOP_BLINK_FAIL);
                loop_set_state(LOOP_COMPLETED, "LOOP:FAIL");
                s_loop_done_at_ms = esp_timer_get_time() / 1000;
                continue;
            }
        }

        if (do_respond || do_cancel) {
            loop_snapshot(rid, sizeof(rid));
            if (rid[0]) {
                if (loop_post_respond(rid, do_cancel ? "cancel" : "accept")) {
                    s_loop_deadline_ms = esp_timer_get_time() / 1000 + LOOP_LOCAL_TIMEOUT_MS;
                } else {
                    loop_led_flash(LOOP_BLINK_FAIL);
                    loop_set_state(LOOP_COMPLETED, "LOOP:FAIL");
                    s_loop_done_at_ms = esp_timer_get_time() / 1000;
                    continue;
                }
            }
        }

        /* 2) 状态机推进 */
        loop_state_t st = loop_snapshot(rid, sizeof(rid));
        int64_t now_ms = esp_timer_get_time() / 1000;

        if (st == LOOP_TRIGGERED || st == LOOP_WAITING_ACK) {
            remote = rid[0] ? loop_fetch_remote_status(rid) : NULL;

            if (remote != NULL) {
                /* "远端已回来"的判据刻意保守，避免自激：
                 *   TRIGGERED   —— 板端还没回应，远端任何动作都是新信息；
                 *   WAITING_ACK —— status=ack 是板端自己刚发的那次 accept 的回声，
                 *                  不算远端收口，必须继续等 confirm/cancel。
                 * 少了这条判断，按一下"回应"就会立刻自我完成、闭环形同虚设。 */
                bool remote_acted = false;
                if (st == LOOP_TRIGGERED) {
                    remote_acted = (strcmp(remote, "pending") != 0);
                } else {
                    remote_acted = (strcmp(remote, "pending") != 0 &&
                                    strcmp(remote, "ack") != 0);
                }

                if (remote_acted) {
                    bool cancelled = (strcmp(remote, "cancelled") == 0 ||
                                      strcmp(remote, "expired") == 0);
                    loop_led_flash(cancelled ? LOOP_BLINK_CANCEL : LOOP_BLINK_DONE);
                    loop_set_state(LOOP_COMPLETED,
                                   cancelled ? "LOOP:CANCEL" : "LOOP:DONE");
                    s_loop_done_at_ms = now_ms;
                }
            }

            /* 本地兜底超时：远程一直没动静（或网络断了）时回 IDLE，
             * 免得板子永远停在 TRIGGERED 再也触发不了。 */
            if (s_loop_deadline_ms > 0 && now_ms > s_loop_deadline_ms) {
                ESP_LOGW(TAG, "[loop] local timeout in state=%s", loop_state_name(st));
                loop_led_flash(LOOP_BLINK_FAIL);
                loop_set_state(LOOP_COMPLETED, "LOOP:TIMEOUT");
                s_loop_done_at_ms = now_ms;
                s_loop_deadline_ms = 0;
            }
        } else if (st == LOOP_COMPLETED) {
            /* DONE 文本停留几秒让现场看清，然后自动回到空闲，可以再触发 */
            if (s_loop_done_at_ms > 0 &&
                now_ms - s_loop_done_at_ms > LOOP_DONE_DWELL_MS) {
                if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
                    s_loop_request_id[0] = '\0';
                    s_loop_need_trigger = false;
                    s_loop_need_respond = false;
                    s_loop_need_cancel = false;
                    xSemaphoreGive(s_state_mutex);
                }
                s_loop_done_at_ms = 0;
                s_loop_deadline_ms = 0;
                loop_set_state(LOOP_IDLE, "LOOP:IDLE");
            }
        }
    }
}

/* ---------- 闭环模块初始化（LED + 任务；按键在 init_buttons 里注册） ---------- */

static void loop_module_init(void)
{
    /* LED：必须开漏。推挽会把 GPIO3 拉高，官方手册明确警告可能烧掉板载绿灯
     * （见「闭环事件 config」）。BSP 的 bsp_led_set() 走的是推挽，故不采用。 */
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << LOOP_LED_GPIO,
        .mode         = GPIO_MODE_OUTPUT_OD,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&io) != ESP_OK) {
        ESP_LOGW(TAG, "[loop] LED GPIO%d config failed - local light feedback off",
                 (int)LOOP_LED_GPIO);
    } else {
        loop_led_apply(LOOP_LED_REST_ON ? true : false);
    }

    s_led_blink_queue = xQueueCreate(LOOP_BLINK_QUEUE_LEN, sizeof(uint8_t));
    assert(s_led_blink_queue != NULL);
    xTaskCreate(loop_led_task, "loop_led", 2560, NULL, 2, NULL);

    xTaskCreate(loop_task, "loop_task", LOOP_TASK_STACK, NULL, LOOP_TASK_PRIO, NULL);

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        snprintf(s_loop_status_text, sizeof(s_loop_status_text), "LOOP:IDLE");
        xSemaphoreGive(s_state_mutex);
    }
    ESP_LOGI(TAG, "[loop] module ready (LED=GPIO%d open-drain, rest=%s)",
             (int)LOOP_LED_GPIO, LOOP_LED_REST_ON ? "on" : "off");
}

/* Uploader task: drain the queue and POST batches one at a time. */
static void uploader_task(void *arg)
{
    (void)arg;
    upload_batch_t *batch;
    while (true) {
        if (xQueueReceive(s_upload_queue, &batch, portMAX_DELAY) == pdTRUE) {
            bool ok = upload_post_batch(batch);
            if (batch->request_id[0] != '\0') {
                /* On-demand batch: releasing the manual state here is what lets
                 * the sampler resume periodic uploads (or report a failure). */
                manual_capture_on_upload_done(ok,
                                              ok ? NULL : "manual upload failed");
            }
            free(batch);
        }
    }
}

/* ================================================================
 *  Sampler Task — reads IMU + SNTP time → writes CSV
 * ================================================================ */
/* ------------------------------------------------------------------
 * WiFi 健康检查 / 自愈（由 sampler_task 每 5 s 调一次）
 *
 * 覆盖三种现场真实故障：
 *  1) 完全掉线（没有 L2）：每 5 s 重新 esp_wifi_connect()，永不放弃。
 *     旧实现重试 5 次后就不再重连，一次抖动 = 永久离线，只能断电重启。
 *  2) 已关联但一直拿不到 IP：15 s 还没租约就重启 DHCP 客户端（最多 3 次），
 *     再不行就断开重连、重走一遍 DHCPDISCOVER。
 *     这是本板实测到的故障：串口日志停在 "AP's beacon interval"，
 *     既没有 Got IP 也没有断连事件，DHCP 静默失败。
 *     LWIP 在 CONFIG_LWIP_DHCP_DOES_ARP_CHECK=y 时会先 ARP 探测拿到的地址，
 *     只要局域网里有设备/陈旧 ARP 应答该地址，客户端就丢弃租约并无限重来，
 *     所以 sdkconfig 里同时关掉了这个 ARP 检查（详见
 *     docs/wifi_network_troubleshooting.md）。
 *  3) 拿到租约又被路由器收回（IP_EVENT_STA_LOST_IP）：状态会回到未联网，
 *     这里同样按 (2) 处理。
 *  4) 整个网段没有 DHCP 服务在应答（现场实测：192.0.2.0/24 三种客户端 MAC
 *     全部 8 s 零回应）：DHCP 重启/重关联都注定失败。Kconfig 打开
 *     "Static IP Fallback" 后，这里在确认 DHCP 无救时改用固定地址，
 *     让板子至少能联网对时（细节见 apply_static_ip_fallback()）。
 *
 * 必须放在任务上下文里调用：重启 DHCP / 重新关联都会走 tcpip 线程，
 * 在 esp_event 回调里做会阻塞事件循环。
 * ------------------------------------------------------------------ */

/* 静态 IP 兜底：停掉 DHCP 客户端、套上 Kconfig 里的固定地址。
 * 只有在 s_static_ip_armed（已判定 DHCP 没救）时才会被调用。
 * 返回 true = 板子现在有 IP（联网状态已经补好）。
 *
 * 事件路径不需要重复实现：esp_netif_set_ip_info() 在 netif 已 up、DHCP 客户端
 * 已停止时会自己 post IP_EVENT_STA_GOT_IP（IDF 5.4
 * components/esp_netif/lwip/esp_netif_lwip.c:1919-1936），于是
 * wifi_event_handler() 会把 s_wifi_connected / 状态文本 / 事件位一并恢复。
 * 但事件是异步的，所以这里立刻把 s_wifi_connected 与计数补上，否则 5 s 后的
 * 下一轮 wifi_health_check() 会以为仍然没有 IP 而强制重新关联，把刚套好的地址
 * 又拆掉。
 *
 * DNS 必须显式设置：DHCP 死了也就没有 DNS 服务器，不设的话 SNTP 依然解析不了
 * CONFIG_SNTP_SERVER（除非那里直接填 IP）。 */
static bool apply_static_ip_fallback(void)
{
#if STATIC_IP_FALLBACK_ENABLED
    if (s_sta_netif == NULL || !s_wifi_link_up) {
        return false;
    }

    /* 已经有地址（正常租约，或上一次兜底套上的、链路抖动后被 lwIP 清掉的）
     * 就不要再 set 一次，set 会重新 post 一次 GOT_IP。 */
    esp_netif_ip_info_t cur = {0};
    if (esp_netif_get_ip_info(s_sta_netif, &cur) == ESP_OK && cur.ip.addr != 0) {
        return true;
    }

    esp_netif_ip_info_t ip = {0};
    esp_netif_dns_info_t dns = {0};
    if (esp_netif_str_to_ip4(CONFIG_STATIC_IP_ADDR, &ip.ip) != ESP_OK ||
        esp_netif_str_to_ip4(CONFIG_STATIC_IP_NETMASK, &ip.netmask) != ESP_OK ||
        esp_netif_str_to_ip4(CONFIG_STATIC_IP_GATEWAY, &ip.gw) != ESP_OK ||
        ip.ip.addr == 0) {
        if (!s_static_ip_warned) {
            s_static_ip_warned = true;
            ESP_LOGE(TAG, "Static IP fallback: invalid Kconfig values \"%s\" / \"%s\" gw \"%s\" "
                          "- static fallback disabled for this boot",
                     CONFIG_STATIC_IP_ADDR, CONFIG_STATIC_IP_NETMASK, CONFIG_STATIC_IP_GATEWAY);
        }
        s_static_ip_armed = false;   /* 配置错了就别每 5 s 再试一遍 */
        return false;
    }

    esp_netif_dhcpc_stop(s_sta_netif);     /* set_ip_info() 要求 DHCP 客户端已停止 */
    esp_err_t ret = esp_netif_set_ip_info(s_sta_netif, &ip);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Static IP fallback: esp_netif_set_ip_info failed: %s", esp_err_to_name(ret));
        return false;
    }

    if (esp_netif_str_to_ip4(CONFIG_STATIC_IP_DNS, &dns.ip.u_addr.ip4) == ESP_OK) {
        /* IDF 5.4 的 esp_netif_dns_info_t 只有一个 ip 成员（esp_ip_addr_t），
         * DNS 角色（MAIN/BACKUP）是 set_dns_info() 的参数；但 esp_ip_addr_t 的
         * type 要自己标成 IPv4 —— esp_netif_str_to_ip4() 只写地址本身。 */
        dns.ip.type = ESP_IPADDR_TYPE_V4;
        ret = esp_netif_set_dns_info(s_sta_netif, ESP_NETIF_DNS_MAIN, &dns);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Static IP fallback: set DNS failed: %s - SNTP needs an IP in "
                          "CONFIG_SNTP_SERVER", esp_err_to_name(ret));
        }
    } else if (!s_static_ip_warned) {
        s_static_ip_warned = true;
        ESP_LOGW(TAG, "Static IP fallback: invalid DNS \"%s\" - relying on CONFIG_SNTP_SERVER "
                      "being a literal IP", CONFIG_STATIC_IP_DNS);
    }

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
        s_wifi_connected = true;
        set_status_locked("WiFi connected (static IP)");
        xSemaphoreGive(s_state_mutex);
    }
    s_dhcp_restart_count = 0;
    s_dhcp_giveup_count = 0;
    s_link_up_us = 0;

    ESP_LOGW(TAG, "Static IP fallback applied: " IPSTR "/" IPSTR " gw " IPSTR " dns %s "
                  "(no DHCP server on this subnet - set the same values in the server's "
                  "docs/wifi_network_troubleshooting.md checklist)",
             IP2STR(&ip.ip), IP2STR(&ip.netmask), IP2STR(&ip.gw), CONFIG_STATIC_IP_DNS);
    return true;
#else
    return false;
#endif
}

static void wifi_health_check(void)
{
    if (s_wifi_connected) {
        s_dhcp_restart_count = 0;
        s_dhcp_giveup_count = 0;
        s_link_up_us = 0;
        return;
    }

    int64_t now_us = esp_timer_get_time();

    if (!s_wifi_link_up) {
        ESP_LOGW(TAG, "WiFi offline - reconnecting (never gives up)");
        esp_wifi_connect();
        return;
    }

    /* 已进入静态 IP 模式：链路重连后 lwIP 会清掉地址（DHCP 客户端又是停着的，
     * 不会自己回来），这里直接把地址补回去。 */
    if (s_static_ip_armed && apply_static_ip_fallback()) {
        return;
    }

    /* 已关联：给 DHCP 一个窗口，超时先重启客户端 */
    if (s_link_up_us == 0) {
        s_link_up_us = now_us;
        return;
    }
    if (now_us - s_link_up_us < 15000000) {
        return;
    }

    if (s_sta_netif && s_dhcp_restart_count < 3) {
        s_dhcp_restart_count++;
        ESP_LOGW(TAG, "No IP 15 s after association - restarting DHCP client (%d/3)",
                 s_dhcp_restart_count);
        esp_netif_dhcpc_stop(s_sta_netif);
        esp_err_t ret = esp_netif_dhcpc_start(s_sta_netif);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "dhcpc restart failed: %s", esp_err_to_name(ret));
        }
        s_link_up_us = now_us;   /* 再给 15 s */
    } else {
        s_dhcp_giveup_count++;
        /* Kconfig 打开静态 IP 兜底时，确认 DHCP 没救（第 2 次放弃 ≈ 关联后 100 s）
         * 就换固定地址；没打开时行为与以前完全一致。 */
        if (STATIC_IP_FALLBACK_ENABLED && s_dhcp_giveup_count >= STATIC_IP_GIVEUP_LIMIT) {
            s_static_ip_armed = true;
            if (apply_static_ip_fallback()) {
                return;
            }
        }
        ESP_LOGW(TAG, "DHCP gave no lease - forcing re-association");
        s_dhcp_restart_count = 0;
        s_link_up_us = now_us;
        esp_wifi_disconnect();   /* -> STA_DISCONNECTED 事件里立刻重连 */
    }
}

/* ================================================================
 *  后台维护任务（housekeeping）
 *
 *  这些活以前都挂在 sampler_task（优先级 5）里，和 10 ms 采样共用一个循环：
 *    - refresh_ui()        ：LVGL 上屏；本地预览态下还要 JPEG 软解 320×240，
 *                            一次 30~80 ms —— 直接把 7 个采样周期吃掉。
 *    - refresh_time_str()  ：每秒一次，本身便宜，但它在同一个临界区里排队。
 *    - wifi_health_check() ：每 5 s 一次；DHCP 重启/重新关联会阻塞上百 ms。
 *    - log_heap()          ：每 2 s 一次，printf 本身在 921600 波特率下也要
 *                            几毫秒到几十毫秒（串口是阻塞写）。
 *  它们都不是「采样」该干的事，却直接决定了采样抖动有多大 —— 正是
 *  「频率虚高 + 追赶连打」的燃料。全部搬到这里，优先级 2（低于
 *  task_poll(3)/uploader(4)/sampler(8)），只在采样器空转时才跑。
 *
 *  另外还顺手做一件小事：**相机采集会话的空闲回收**（
 *  camera_session_idle_close）。相机改成「持久会话」后不再每帧 STREAMON，
 *  必须有人在没人用相机时把它关掉（放掉 DVP + mmap + fd），否则传感器会一直
 *  满速出图白烧电。用 0 超时拿相机锁，正在取帧就跳过，绝不阻塞实时路径。
 *
 *  这三个函数各自内部都会取 s_state_mutex（log_heap 无锁），所以从哪个任务
 *  调用都安全。周期是各自独立的，故本任务只按 100 ms 心跳醒来判断一次。
 * ================================================================ */
#define HK_TICK_MS              100
#define HK_UI_PERIOD_US         1000000      /* refresh_ui / refresh_time_str */
#define HK_HEAP_PERIOD_US       2000000      /* log_heap */
#define HK_WIFI_PERIOD_US       5000000      /* wifi_health_check */
#define HK_CAM_PERIOD_US        500000      /* 相机会话空闲回收（相机锁用 0 超时，很便宜） */

static void housekeeping_task(void *arg)
{
    (void)arg;

    int64_t last_ui_us   = 0;
    int64_t last_heap_us = 0;
    int64_t last_wifi_us = 0;
    int64_t last_cam_us  = 0;

    while (true) {
        int64_t now_us = esp_timer_get_time();

        if (now_us - last_heap_us >= HK_HEAP_PERIOD_US) {
            last_heap_us = now_us;
            log_heap(s_collecting ? "sampler(collecting)" : "sampler(idle)");
        }

        /* LCD「Time:」每秒刷新：已同步写北京时间，未同步写 uptime ——
         * 无论联不联网这一行都在动，所以「时间不对」和「板子卡死」在 LCD 上
         * 可以直接区分开。 */
        if (now_us - last_ui_us >= HK_UI_PERIOD_US) {
            last_ui_us = now_us;
            refresh_time_str();
            refresh_ui();
        }

        /* WiFi 自愈：里面可能重启 DHCP 客户端甚至重新关联，耗时不可控，
         * 所以放在优先级最低的任务里，绝不阻塞采样。 */
        if (now_us - last_wifi_us >= HK_WIFI_PERIOD_US) {
            last_wifi_us = now_us;
            wifi_health_check();
        }

        /* 相机会话空闲回收：直播/预览停了、或者上一次拍照之后没人再用相机，
         * 就把 STREAMON 的会话关掉（STREAMOFF + munmap + close）。见「Camera —
         * 持久会话」：会话开着时传感器一直满速出图，不能一直不管。 */
        if (now_us - last_cam_us >= HK_CAM_PERIOD_US) {
            last_cam_us = now_us;
            camera_session_idle_close();
        }

        vTaskDelay(pdMS_TO_TICKS(HK_TICK_MS));
    }
}
static void sampler_task(void *arg)
{
    (void)arg;

    /* Use a phase accumulator for drift-free timing.
     * next_wake_us is the absolute target time for the NEXT tick. */
    int64_t next_wake_us = esp_timer_get_time() + (SAMPLE_PERIOD_MS * 1000);

    /* Sampling-gate state of the previous iteration: a gate that opens after
     * being closed is a brand-new sampling session (see the gate below). */
    bool gate_was_open = false;
    /* Previous tick's periodic-upload pause state: the falling edge (paused →
     * resumed) is where the half-filled 1 s window is dropped. */
    bool periodic_pause_was_on = false;

    /* ----------------------------------------------------------------
     * CSV 刷盘节流 + 写失败容忍（见「CSV 落盘缓冲 / 刷盘策略」）
     *   csv_flush_next_us : 下一次按时间刷盘的目标时刻
     *   csv_fail_streak   : 连续「一个采样周期都没写成功」的次数
     *   csv_flush_retried : 「同一个 tick 内重试一次就写成功」的次数（自愈的
     *                       瞬时内部 DMA 碎片，不算失败，只在会话结束汇总）
     * ---------------------------------------------------------------- */
    int64_t  csv_flush_next_us = 0;
    uint32_t csv_fail_streak = 0;
    uint32_t csv_flush_retried = 0;

    /* ----------------------------------------------------------------
     * 节拍遥测（每 1 s 一行 [TICK]）：把「100 Hz」从"信以为真"变成可观测。
     * 旧实现只有一个 100 点平均值，而且打印时把 Hz×100 当 Hz、把 0.01 s 当 s
     * （串口上会出现 "Frequency measured: 12419 Hz ... Time: 124 s" 这种鬼数字），
     * 更关键的是采样器的追赶逻辑会把漏掉的 tick 一次性连打出来 —— 平均值
     * 看着还行，数据的实际时间分布却是一串「快-快-快-停」。
     * ---------------------------------------------------------------- */
    int64_t  tick_stat_start_us   = esp_timer_get_time();
    uint32_t tick_stat_ticks      = 0;   /* 本轮统计实际执行的 tick 数 */
    uint32_t tick_stat_late       = 0;   /* 从前一 tick 至此超过 1.5 个周期的次数 */
    uint32_t tick_stat_catchup    = 0;   /* 「醒来时已过目标时刻」的次数（旧实现会连打） */
    uint32_t tick_stat_missed     = 0;   /* 已彻底放弃、只重同步的次数 */
    int32_t  tick_stat_max_late_us = 0;

    while (true) {
        /* 注意：这里**不再**做 1 s 的 refresh_ui()/refresh_time_str()、
         * 5 s 的 wifi_health_check()、2 s 的 log_heap()。它们是采样抖动的主要
         * 来源（LVGL 上屏 + JPEG 解码共 30~80 ms、DHCP 重启上百 ms），
         * 已全部搬到优先级更低的 housekeeping_task（见「后台维护任务」一节）。
         * 本任务只干三件事：读 IMU、写 CSV、跑协议状态机。 */

        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(5)) == pdTRUE) {
            /* Sampling gate: s_collecting (button/SD session) OR an on-demand
             * capture task. s_data_file may legitimately be NULL in WiFi-only
             * mode; the CSV write below is NULL-guarded. Protocol sessions
             * always have a valid s_data_file because their start_* functions
             * still require a mounted SD card. */
            bool gate_open = (s_collecting || manual_capture_is_active());

            /* A gate that opens after being closed starts a brand-new sampling
             * session, so its timestamp anchor (s_base_timestamp_ms, taken at
             * the first sample of a session) must be taken again. The button /
             * SD session start_* paths zero s_sample_count themselves, but an
             * on-demand capture claims the sampler while the board is idle, so
             * without this its timestamps would keep counting from the previous
             * session (s_base_timestamp_ms + n * 10 ms) and land in the past -
             * the server rejects such a batch with "ts_ms is older than the
             * task creation time". A session that never stopped (s_collecting
             * stays true across a manual capture) keeps its timeline. */
            if (gate_open && !gate_was_open) {
                s_sample_count = 0;
                s_upload_count = 0;
                /* 新会话：立刻允许第一次刷盘，并清掉上个会话残留的失败计数 */
                csv_flush_next_us = 0;
                csv_fail_streak = 0;
                csv_flush_retried = 0;
            } else if (!gate_open && gate_was_open) {
                /* 会话结束：把「靠同 tick 重试自愈」的瞬时 SD 写失败汇总一行。
                 * 它们是自愈的（数据一条没丢），所以不打 E 级、不计 fail_streak；
                 * 但全静默又会让人以为 SD 从未抖动过，故这里留个可观测点。 */
                if (csv_flush_retried > 0) {
                    ESP_LOGW(TAG, "[csv] session end: %u flush(es) needed an in-tick "
                                  "retry (transient internal-DMA fragmentation for the "
                                  "512 B SD bounce buffer)",
                             (unsigned)csv_flush_retried);
                    csv_flush_retried = 0;
                }
            }
            gate_was_open = gate_open;

            if (gate_open) {
                /* 1. Read accelerometer */
                float ax, ay, az;
                esp_err_t ret = app_accel_read(&ax, &ay, &az);
                if (ret != ESP_OK) {
                    stop_collection_locked("IMU read failed");
                    xSemaphoreGive(s_state_mutex);
                    /* Resync timing after error */
                    next_wake_us = esp_timer_get_time() + (SAMPLE_PERIOD_MS * 1000);
                    continue;
                }

                /* 2. Anchor base timestamp on first sample of the session */
                if (s_sample_count == 0) {
                    struct timeval tv;
                    gettimeofday(&tv, NULL);
                    s_base_timestamp_ms = (uint64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
                    /* New capture session: start a fresh 1 s upload window. */
                    s_upload_count = 0;
                }
                
                /* Generate strictly monotonic timestamp: base + sample_index * 10ms
                 * This guarantees np.diff(timestamp_ms) == 10 for all records,
                 * independent of FreeRTOS scheduling jitter. */
                uint64_t timestamp_ms = s_base_timestamp_ms + (uint64_t)s_sample_count * SAMPLE_PERIOD_MS;

                /* 2b. 时间字段由采样循环每 1 s 的 refresh_time_str() 统一维护
                 *     （未同步时显示 uptime）。这里不再做 strftime —— 10 ms 的
                 *     采样临界区里不该出现格式化开销，也不需要重复实现一套。 */

                /* 3. Convert acceleration from g to m/s² */
                float ax_ms2 = ax * GRAVITY_ACCEL;
                float ay_ms2 = ay * GRAVITY_ACCEL;
                float az_ms2 = az * GRAVITY_ACCEL;

                /* 4. Write CSV row - format: label,timestamp_ms,accel_x,accel_y,accel_z */
                char label_buf[16];
                if (s_stand_protocol_active && s_stand_label[0] != '\0') {
                    /* Stand protocol mode - use stand label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_stand_label);
                } else if (s_stairs_protocol_active && s_stairs_label[0] != '\0') {
                    /* Stairs protocol mode - use stairs label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_stairs_label);
                } else if (s_bend_protocol_active && s_bend_label[0] != '\0') {
                    /* Bend protocol mode - use bend label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_bend_label);
                } else if (s_jump_protocol_active && s_jump_label[0] != '\0') {
                    /* Jump protocol mode - use jump label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_jump_label);
                } else if (s_fall_protocol_active && s_fall_label[0] != '\0') {
                    /* Fall protocol mode - use fall label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_fall_label);
                } else if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
                    /* Calibration mode - use face name as label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_face_name + 6);  /* Skip "Face: " prefix */
                } else {
                    /* Normal mode */
                    snprintf(label_buf, sizeof(label_buf), "%s", DATA_LABEL);
                }
                
                /* Use jump_data_file if jump protocol is active, otherwise use data_file.
                 * write_file may be NULL in WiFi-only mode (no SD card). */
                FILE *write_file = s_data_file;
                if (s_jump_protocol_active && s_jump_data_file) {
                    write_file = s_jump_data_file;
                }

                bool csv_write_failed = false;
                if (write_file) {
                    if (fprintf(write_file, "%s,%" PRIu64 ",%.4f,%.4f,%.4f\n",
                                label_buf,
                                timestamp_ms,
                                ax_ms2, ay_ms2, az_ms2) < 0) {
                        /* 4096 B 缓冲下 fprintf 只在「缓冲写满、触发真实写盘」时
                         * 才失败，所以走到这里就是 SD 写盘失败本身。 */
                        csv_write_failed = true;
                    } else {
                        /* ---- 刷盘策略：按时间，不按样点 ----
                         * 旧实现每个点都 fflush()：100 次/秒的单扇区写，每次都要
                         * 在内部 DMA 堆里申请 512 B 的 bounce buffer（ESP32-S3 的
                         * SDMMC 不能对 PSRAM DMA），而单扇区写的尾延迟（SD 卡
                         * 内部 GC/磨损均衡时可达 50~200 ms）又直接砸进 10 ms 的
                         * 采样周期里 —— 它既是 SD 写失败的元凶，也是采样抖动的元凶。
                         * 现在改成最多每 CSV_FLUSH_INTERVAL_MS 一次整块刷盘；
                         * 协议阶段切换点与 stop_collection_locked() 仍立即刷盘，
                         * 所以掉电最多丢 CSV_FLUSH_INTERVAL_MS 的数据。 */
                        int64_t now_us_flush = esp_timer_get_time();
                        if (now_us_flush >= csv_flush_next_us) {
                            csv_flush_next_us = now_us_flush
                                + (int64_t)CSV_FLUSH_INTERVAL_MS * 1000;
                            int  flush_tries = 0;
                            bool flush_ok    = false;
                            for (; flush_tries < CSV_FLUSH_RETRY; flush_tries++) {
                                if (fflush(write_file) == 0) {
                                    flush_ok = true;
                                    break;
                                }
                                /* 错误标志是粘的：不清掉后面每次都会立刻失败。
                                 * 清掉并立刻重试（= 同一个 tick 里再申请一次
                                 * bounce buffer）；因为这一次什么都没真正写出
                                 * 去，重试不会产生重复行。 */
                                clearerr(write_file);
                                if (flush_tries + 1 < CSV_FLUSH_RETRY) {
                                    vTaskDelay(1);   /* 让一拍，等相机 DMA 释放 */
                                }
                            }
                            if (!flush_ok) {
                                /* 全部重试都用尽，这才是真失败：交给下面的
                                 * fail_streak / ERROR 日志（含 dma_free/dma_min
                                 * 现场）处理。 */
                                csv_write_failed = true;
                            } else if (flush_tries > 0) {
                                /* 重试一次才成功 = 自愈的瞬时 DMA 碎片：数据没丢、
                                 * 也不该计入 fail_streak。旧代码在这里对「第
                                 * 一次失败」就置 csv_write_failed，于是一次
                                 * 「重试即恢复」也被打成满屏红色 ERROR，与本段
                                 * 「只有持续失败才会计入」的本意相反。现在只
                                 * 统计次数，会话结束时汇总一行。 */
                                csv_flush_retried++;
                            }
                        }
                    }
                }

                if (csv_write_failed) {
                    csv_fail_streak++;
                    if (csv_fail_streak == 1) {
                        /* 第一次失败就把现场打全：以前只有一句
                         * "CSV write failed"，完全看不出失败点是内部 DMA 堆
                         * （MALLOC_CAP_DMA）碎片，而不是"总内存不够"。 */
                        ESP_LOGE(TAG, "[csv] write failed #%u/%d errno=%d ferror=%d"
                                      " file=%s samples=%" PRIu32
                                      " | dma_free=%u dma_largest=%u int_largest=%u",
                                 (unsigned)csv_fail_streak, CSV_WRITE_FAIL_LIMIT,
                                 errno, ferror(write_file),
                                 s_file_path[0] ? s_file_path : "(none)",
                                 s_sample_count,
                                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL),
                                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_DMA),
                                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
                        log_heap("csv write failed");
                    }
                    if (csv_fail_streak >= CSV_WRITE_FAIL_LIMIT && s_collecting) {
                        /* 连续 CSV_WRITE_FAIL_LIMIT 个采样周期一次都没写出去：
                         * 这不是偶发抖动。停采集并明确报错，而不是默默写出错数据。
                         * （旧实现是第一次失败就停，一次 5 ms 的内存毛刺就能毁掉
                         *   一整段录制。） */
                        stop_collection_locked("Write failed");
                        ESP_LOGE(TAG, "CSV write failed %d times in a row -> session stopped",
                                 CSV_WRITE_FAIL_LIMIT);
                    }
                }
                if (!csv_write_failed && csv_fail_streak != 0) {
                    ESP_LOGW(TAG, "[csv] write recovered after %u failed tick(s)",
                             (unsigned)csv_fail_streak);
                    csv_fail_streak = 0;
                }

                if (!csv_write_failed) {
                    /* Store raw m/s² values for UI (refresh_ui converts to mm/s² for display) */
                    s_latest_accel_x = ax_ms2;
                    s_latest_accel_y = ay_ms2;
                    s_latest_accel_z = az_ms2;
                    s_sample_count++;

                    /* An on-demand capture owns this tick while it runs, which
                     * is exactly what pauses the periodic 1 s upload window. */
                    bool manual_tick = manual_capture_consumes_sample(
                        timestamp_ms, ax_ms2, ay_ms2, az_ms2);

                    /* Web「暂停周期」窗口内：照常采样/落盘/刷 UI，只是不再喂 1 s
                     * 上传窗口（手动采集不受影响）。窗口结束的第一拍丢掉未满的
                     * 周期窗口，恢复后的第一批仍是干净的 UPLOAD_WINDOW_SIZE 点，
                     * 不会把暂停前后的样本缝进同一批。 */
                    bool periodic_paused = periodic_upload_paused();
                    if (!periodic_paused && periodic_pause_was_on) {
                        s_upload_count = 0;
                    }
                    periodic_pause_was_on = periodic_paused;

                    /* Feed this sample into the 1 s upload window (WiFi only). */
                    if (!manual_tick && !periodic_paused && s_wifi_connected) {
                        upload_accumulate(timestamp_ms, ax_ms2, ay_ms2, az_ms2);
                    }
                    
                    /* Accumulate calibration data if in calibration mode */
                    if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
                        s_calib_sum_x[s_current_face] += ax_ms2;
                        s_calib_sum_y[s_current_face] += ay_ms2;
                        s_calib_sum_z[s_current_face] += az_ms2;
                        s_calib_count[s_current_face]++;
                        s_face_sample_count++;
                    }
                }

                /* 5. Calibration mode state machine */
                if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_face_start_time_ms;

                    /* Calculate acceleration magnitude for verification */
                    float accel_mag = sqrtf(ax_ms2 * ax_ms2 + ay_ms2 * ay_ms2 + az_ms2 * az_ms2);
                    
                    /* Log magnitude for calibration verification (every 1 second) */
                    static int64_t last_mag_log_ms = 0;
                    if (elapsed_ms - last_mag_log_ms >= 1000) {
                        last_mag_log_ms = elapsed_ms;
                        const char *face_str = get_face_name(s_current_face);
                        int32_t accel_mag_mm = (int32_t)(accel_mag * 1000);
                        ESP_LOGI(TAG, "[%s] Mag: %" PRId32 " mm/s² (Target: 9810, OK: 9600-10000)", 
                                 face_str, accel_mag_mm);
                    }

                    /* Check if face duration complete */
                    if (elapsed_ms >= FACE_DURATION_MS) {
                        /* Face complete, move to next */
                        calibration_face_t prev_face = s_current_face;
                        s_current_face = get_next_face(s_current_face);
                        s_face_sample_count = 0;
                        s_face_start_time_ms = now_ms;

                        if (s_current_face == FACE_COMPLETE) {
                            /* All faces complete - calculate and write calibration */
                            const float g = GRAVITY_ACCEL;

                            /* Calculate offset and scale for each axis */
                            /* offset = (pos_sum - neg_sum) / (2 * count) */
                            /* scale = g / ((pos_sum + neg_sum) / (2 * count)) */

                            /* X axis: +X face (ax ≈ -g), -X face (ax ≈ +g) */
                            float x_offset = 0, x_scale = 1.0f;
                            if (s_calib_count[FACE_POS_X] > 0 && s_calib_count[FACE_NEG_X] > 0) {
                                float x_pos_mean = s_calib_sum_x[FACE_NEG_X] / s_calib_count[FACE_NEG_X];  /* Should be +g */
                                float x_neg_mean = s_calib_sum_x[FACE_POS_X] / s_calib_count[FACE_POS_X];  /* Should be -g */
                                x_offset = (x_pos_mean + x_neg_mean) / 2.0f;
                                x_scale = g / fabsf((x_pos_mean - x_neg_mean) / 2.0f);
                            }

                            /* Y axis: +Y face (ay ≈ -g), -Y face (ay ≈ +g) */
                            float y_offset = 0, y_scale = 1.0f;
                            if (s_calib_count[FACE_POS_Y] > 0 && s_calib_count[FACE_NEG_Y] > 0) {
                                float y_pos_mean = s_calib_sum_y[FACE_NEG_Y] / s_calib_count[FACE_NEG_Y];
                                float y_neg_mean = s_calib_sum_y[FACE_POS_Y] / s_calib_count[FACE_POS_Y];
                                y_offset = (y_pos_mean + y_neg_mean) / 2.0f;
                                y_scale = g / fabsf((y_pos_mean - y_neg_mean) / 2.0f);
                            }

                            /* Z axis: +Z face (az ≈ -g), -Z face (az ≈ +g) */
                            float z_offset = 0, z_scale = 1.0f;
                            if (s_calib_count[FACE_POS_Z] > 0 && s_calib_count[FACE_NEG_Z] > 0) {
                                float z_pos_mean = s_calib_sum_z[FACE_NEG_Z] / s_calib_count[FACE_NEG_Z];
                                float z_neg_mean = s_calib_sum_z[FACE_POS_Z] / s_calib_count[FACE_POS_Z];
                                z_offset = (z_pos_mean + z_neg_mean) / 2.0f;
                                z_scale = g / fabsf((z_pos_mean - z_neg_mean) / 2.0f);
                            }

                            /* Write calibration.csv in m/s² (matches protocol doc format) */
                            if (s_calibration_file) {
                                fprintf(s_calibration_file, "x,%.3f,%.3f,m/s²\n", x_offset, x_scale);
                                fprintf(s_calibration_file, "y,%.3f,%.3f,m/s²\n", y_offset, y_scale);
                                fprintf(s_calibration_file, "z,%.3f,%.3f,m/s²\n", z_offset, z_scale);
                                fflush(s_calibration_file);
                                
                                ESP_LOGI(TAG, "Calibration parameters written to calibration.csv");
                                ESP_LOGI(TAG, "X: offset=%.3f, scale=%.3f", x_offset, x_scale);
                                ESP_LOGI(TAG, "Y: offset=%.3f, scale=%.3f", y_offset, y_scale);
                                ESP_LOGI(TAG, "Z: offset=%.3f, scale=%.3f", z_offset, z_scale);
                            }

                            complete_calibration_locked();
                        } else {
                            /* Log transition */
                            ESP_LOGI(TAG, "Face %s complete (%lu samples). Moving to %s",
                                     get_face_name(prev_face), (unsigned long)s_face_sample_count,
                                     get_face_name(s_current_face));
                            snprintf(s_face_name, sizeof(s_face_name), "Face: %s (0s)",
                                     get_face_name(s_current_face));
                        }
                    } else {
                        /* Update face timer display */
                        uint32_t remaining_sec = (FACE_DURATION_MS - elapsed_ms) / 1000;
                        snprintf(s_face_name, sizeof(s_face_name), "Face: %s (%lu s)",
                                 get_face_name(s_current_face), (unsigned long)remaining_sec);
                    }
                }

                /* 6. Frequency measurement */
                if (s_freq_measuring) {
                    s_freq_sample_count++;
                    
                    if (s_freq_sample_count >= FREQ_MEASURE_SAMPLES) {
                        int64_t elapsed_us = esp_timer_get_time() - s_freq_start_time_us;
                        if (elapsed_us > 0) {
                            s_measured_freq_hz = (float)s_freq_sample_count * 1000000.0f / (float)elapsed_us;
                            s_freq_valid = true;

                            /* 单位必须写对：以前这里打印 freq*100 却标成 "Hz"、
                             * 打印 elapsed/10000 却标成 "s"，于是串口上出现
                             * "Frequency measured: 12419 Hz ... Time: 124 s"
                             * 这种把 124.19 Hz / 12.4 s 放大 100 倍的鬼数字。
                             * 现在用整数 + 明确小数位的形式打印，避免 %f 的开销。 */
                            int32_t freq_d10 = (int32_t)(s_measured_freq_hz * 10);
                            int32_t freq_err_d10 = (int32_t)(fabsf(s_measured_freq_hz
                                                                   - TARGET_SAMPLE_FREQ_HZ) * 10);
                            ESP_LOGI(TAG, "Frequency measured: %" PRId32 ".%" PRId32
                                          " Hz over %" PRId32 ".%" PRId32 " s"
                                          " (%" PRIu32 " samples, target %d Hz)",
                                     freq_d10 / 10, freq_d10 % 10,
                                     (int32_t)(elapsed_us / 1000000),
                                     (int32_t)((elapsed_us / 100000) % 10),
                                     s_freq_sample_count, TARGET_SAMPLE_FREQ_HZ);

                            if (freq_err_d10 < 50) {   /* 5.0 Hz = 50 × 0.1 Hz */
                                ESP_LOGI(TAG, "✓ Sampling frequency OK (within ±5 Hz of %d Hz target)",
                                         TARGET_SAMPLE_FREQ_HZ);
                            } else {
                                ESP_LOGW(TAG, "✗ Sampling frequency OUT OF RANGE (±5 Hz of %d Hz target)"
                                              " — see the [TICK] line above for late/missed counts",
                                         TARGET_SAMPLE_FREQ_HZ);
                            }
                        }
                        s_freq_measuring = false;
                    }
                }

                /* 7. Stand protocol state machine */
                if (s_stand_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_stand_phase_start_ms;
                    int group_num = s_stand_group + 1;  /* 1-based for display */

                    switch (s_stand_state) {
                    case STAND_PROTOCOL_PREP:
                        /* 3s prep phase: hold stand pose, label = "stand" */
                        snprintf(s_stand_display, sizeof(s_stand_display),
                                 "Stand: Prep G%d (%ds)", group_num,
                                 (int)((STAND_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= STAND_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_stand_state = STAND_PROTOCOL_EXECUTE;
                            s_stand_phase_start_ms = now_ms;
                            snprintf(s_stand_label, sizeof(s_stand_label), "stand");
                            ESP_LOGI(TAG, "[Stand] Group %d: PREP done → EXECUTE (%ds)", group_num, s_stand_duration_sec);
                        }
                        break;

    case STAND_PROTOCOL_EXECUTE:
        /* 20-30s execute phase: standing still, label = "stand" */
        snprintf(s_stand_display, sizeof(s_stand_display),
                 "Stand: Exec G%d (%ds)", group_num,
                 (int)((s_stand_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
        if (elapsed_ms >= s_stand_duration_sec * 1000) {
            /* Transition to interval phase */
            s_stand_state = STAND_PROTOCOL_INTERVAL;
            s_stand_phase_start_ms = now_ms;
            snprintf(s_stand_label, sizeof(s_stand_label), "idle");
            ESP_LOGI(TAG, "[Stand] Group %d: EXECUTE done → INTERVAL", group_num);
        }
        break;

                    case STAND_PROTOCOL_INTERVAL: {
                        /* 5s interval: relax, label = "idle" */
                        snprintf(s_stand_display, sizeof(s_stand_display),
                                 "Stand: Rest G%d→%d (%ds)",
                                 group_num, group_num + 1,
                                 (int)((s_stand_interval_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_stand_interval_sec * 1000) {
                            s_stand_group++;
                            if (s_stand_group >= STAND_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Stand] All %d groups complete!", STAND_TOTAL_GROUPS);
                                complete_stand_protocol_locked();
                            } else {
                                /* Close previous file and create new file for next group */
                                esp_err_t ret = create_stand_file_for_current_group();
                                if (ret != ESP_OK) {
                                    ESP_LOGE(TAG, "[Stand] Failed to create file for group %d", s_stand_group + 1);
                                    complete_stand_protocol_locked();
                                    break;
                                }
                                
                                /* Start next group's prep phase */
                                s_stand_state = STAND_PROTOCOL_PREP;
                                s_stand_phase_start_ms = now_ms;
                                s_stand_duration_sec = STAND_EXECUTE_DURATION_MIN_SEC + 
                                    (esp_random() % (STAND_EXECUTE_DURATION_MAX_SEC - STAND_EXECUTE_DURATION_MIN_SEC + 1));
                                snprintf(s_stand_label, sizeof(s_stand_label), "stand");
                                ESP_LOGI(TAG, "[Stand] Group %d: REST done → PREP G%d (3s)", 
                                         group_num, s_stand_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Stairs protocol state machine */
                if (s_stairs_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_stairs_phase_start_ms;
                    int group_num = s_stairs_group + 1;  /* 1-based for display */

                    switch (s_stairs_state) {
                    case STAIRS_PROTOCOL_PREP:
                        /* 3s prep phase: hold stairs pose, label = "stairs" */
                        snprintf(s_stairs_display, sizeof(s_stairs_display),
                                 "Stairs: Prep G%d (%ds)", group_num,
                                 (int)((STAIRS_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= STAIRS_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_stairs_state = STAIRS_PROTOCOL_EXECUTE;
                            s_stairs_phase_start_ms = now_ms;
                            snprintf(s_stairs_label, sizeof(s_stairs_label), "stairs");
                            ESP_LOGI(TAG, "[Stairs] Group %d: PREP done → EXECUTE (25s)", group_num);
                        }
                        break;

                    case STAIRS_PROTOCOL_EXECUTE:
                        /* 20-30s stairs execution: label = "stairs" */
                        snprintf(s_stairs_display, sizeof(s_stairs_display),
                                 "Stairs: Group %d (%ds)", group_num,
                                 (int)((s_stairs_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_stairs_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_stairs_state = STAIRS_PROTOCOL_INTERVAL;
                            s_stairs_phase_start_ms = now_ms;
                            snprintf(s_stairs_label, sizeof(s_stairs_label), "idle");
                            ESP_LOGI(TAG, "[Stairs] Group %d: DONE", group_num);
                        }
                        break;

                    case STAIRS_PROTOCOL_INTERVAL: {
                        /* 60s rest interval: label = "idle" */
                        int next_group = (group_num < STAIRS_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_stairs_display, sizeof(s_stairs_display),
                                 "Stairs: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_stairs_interval_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_stairs_interval_sec * 1000) {
                            s_stairs_group++;
                            if (s_stairs_group >= STAIRS_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Stairs] All %d groups complete!", STAIRS_TOTAL_GROUPS);
                                complete_stairs_protocol_locked();
                            } else {
                                /* Create new file for next group */
                                esp_err_t ret = create_stairs_file_for_current_group();
                                if (ret != ESP_OK) {
                                    ESP_LOGE(TAG, "[Stairs] Failed to create file for group %d", s_stairs_group + 1);
                                    complete_stairs_protocol_locked();
                                    break;
                                }
                                /* Start next group's PREP phase */
                                s_stairs_state = STAIRS_PROTOCOL_PREP;
                                s_stairs_phase_start_ms = now_ms;
                                s_stairs_duration_sec = STAIRS_EXECUTE_DURATION_MIN_SEC + 
                                    (esp_random() % (STAIRS_EXECUTE_DURATION_MAX_SEC - STAIRS_EXECUTE_DURATION_MIN_SEC + 1));
                                snprintf(s_stairs_label, sizeof(s_stairs_label), "stairs");
                                ESP_LOGI(TAG, "[Stairs] Group %d: REST done → PREP G%d (3s)", 
                                         group_num, s_stairs_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Bend protocol state machine */
                if (s_bend_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_bend_phase_start_ms;
                    int group_num = s_bend_group + 1;  /* 1-based for display */

                    switch (s_bend_state) {
                    case BEND_PROTOCOL_PREP:
                        /* 3s prep phase: hold bend pose, label = "bend" */
                        snprintf(s_bend_display, sizeof(s_bend_display),
                                 "Bend: Prep G%d (%ds)", group_num,
                                 (int)((BEND_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= BEND_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_bend_state = BEND_PROTOCOL_EXECUTE;
                            s_bend_phase_start_ms = now_ms;
                            snprintf(s_bend_label, sizeof(s_bend_label), "bend");
                            /* Randomize duration for first group */
                            s_bend_duration_sec = BEND_DURATION_MIN_SEC + 
                                (esp_random() % (BEND_DURATION_MAX_SEC - BEND_DURATION_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Bend] Group %d: PREP done → EXECUTE (%ds)", group_num, s_bend_duration_sec);
                        }
                        break;

                    case BEND_PROTOCOL_EXECUTE:
                        /* 20-30s bend execution: label = "bend" */
                        snprintf(s_bend_display, sizeof(s_bend_display),
                                 "Bend: Group %d (%ds)", group_num,
                                 (int)((s_bend_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_bend_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_bend_state = BEND_PROTOCOL_INTERVAL;
                            s_bend_phase_start_ms = now_ms;
                            snprintf(s_bend_label, sizeof(s_bend_label), "idle");
                            /* Randomize duration and interval */
                            s_bend_duration_sec = BEND_DURATION_MIN_SEC + 
                                (esp_random() % (BEND_DURATION_MAX_SEC - BEND_DURATION_MIN_SEC + 1));
                            s_bend_interval_duration_sec = BEND_INTERVAL_MIN_SEC + 
                                (esp_random() % (BEND_INTERVAL_MAX_SEC - BEND_INTERVAL_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Bend] Group %d: DONE → REST (bend=%ds, rest=%ds)", 
                                     group_num, s_bend_duration_sec, s_bend_interval_duration_sec);
                        }
                        break;

                    case BEND_PROTOCOL_INTERVAL: {
                        /* 60-90s interval phase: rest, label = "idle" */
                        int next_group = (group_num < BEND_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_bend_display, sizeof(s_bend_display),
                                 "Bend: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_bend_interval_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_bend_interval_duration_sec * 1000) {
                            s_bend_group++;
                            if (s_bend_group >= BEND_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Bend] All %d groups complete!", BEND_TOTAL_GROUPS);
                                complete_bend_protocol_locked();
                            } else {
                                /* Create new file for next group */
                                esp_err_t ret = create_bend_file_for_current_group();
                                if (ret != ESP_OK) {
                                    ESP_LOGE(TAG, "[Bend] Failed to create file for group %d", s_bend_group + 1);
                                    complete_bend_protocol_locked();
                                    break;
                                }
                                /* Start next group's PREP phase */
                                s_bend_state = BEND_PROTOCOL_PREP;
                                s_bend_phase_start_ms = now_ms;
                                snprintf(s_bend_label, sizeof(s_bend_label), "bend");
                                ESP_LOGI(TAG, "[Bend] Group %d: REST done → PREP G%d (3s)", 
                                         group_num, s_bend_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Jump protocol state machine */
                if (s_jump_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_jump_phase_start_ms;
                    int group_num = s_jump_group + 1;  /* 1-based for display */

                    switch (s_jump_state) {
                    case JUMP_PROTOCOL_PREP:
                        /* 3s prep phase: hold jump pose, label = "jump" */
                        snprintf(s_jump_display, sizeof(s_jump_display),
                                 "Jump: Prep G%d (%ds)", group_num,
                                 (int)((JUMP_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= JUMP_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_jump_state = JUMP_PROTOCOL_EXECUTE;
                            s_jump_phase_start_ms = now_ms;
                            snprintf(s_jump_label, sizeof(s_jump_label), "jump");
                            ESP_LOGI(TAG, "[Jump] Group %d: PREP done → EXECUTE (10s)", group_num);
                        }
                        break;

                    case JUMP_PROTOCOL_EXECUTE:
                        /* 5-10s jump execution: label = "jump" */
                        snprintf(s_jump_display, sizeof(s_jump_display),
                                 "Jump: Group %d (%ds)", group_num,
                                 (int)((s_jump_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_jump_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_jump_state = JUMP_PROTOCOL_INTERVAL;
                            s_jump_phase_start_ms = now_ms;
                            snprintf(s_jump_label, sizeof(s_jump_label), "idle");
                            /* Randomize interval between 5-10s */
                            s_jump_interval_duration_sec = JUMP_INTERVAL_MIN_SEC + 
                                (esp_random() % (JUMP_INTERVAL_MAX_SEC - JUMP_INTERVAL_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Jump] Group %d: DONE → REST (%ds idle)", 
                                     group_num, s_jump_interval_duration_sec);
                        }
                        break;

                    case JUMP_PROTOCOL_INTERVAL: {
                        /* 5-10s interval phase: rest, label = "idle" */
                        int next_group = (group_num < JUMP_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_jump_display, sizeof(s_jump_display),
                                 "Jump: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_jump_interval_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_jump_interval_duration_sec * 1000) {
                            s_jump_group++;
                            if (s_jump_group >= JUMP_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Jump] All %d groups complete!", JUMP_TOTAL_GROUPS);
                                complete_jump_protocol_locked();
                            } else {
                                /* Close current group file and open next group file
                                 * （csv_fclose 会归还静态 stdio 缓冲槽位；此刻
                                 *   s_data_file 是它的别名，一并清掉避免悬空） */
                                csv_fclose(&s_jump_data_file);
                                s_data_file = NULL;
                                
                                snprintf(s_jump_file_path, sizeof(s_jump_file_path),
                                         BSP_SD_MOUNT_POINT "/jump_%d.csv",
                                         s_jump_group + 1);
                                
                                s_jump_data_file = fopen(s_jump_file_path, "w");
                                if (!s_jump_data_file) {
                                    ESP_LOGE(TAG, "[Jump] Failed to open group %d file", s_jump_group + 1);
                                    complete_jump_protocol_locked();
                                    break;
                                }
                                csv_io_attach(s_jump_data_file);
                                
                                fprintf(s_jump_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
                                fflush(s_jump_data_file);
                                
                                /* Sync data_file to jump_data_file so sampler_task can write */
                                s_data_file = s_jump_data_file;
                                
                                /* Start next group's PREP phase */
                                s_jump_state = JUMP_PROTOCOL_PREP;
                                s_jump_phase_start_ms = now_ms;
                                snprintf(s_jump_label, sizeof(s_jump_label), "jump");
                                ESP_LOGI(TAG, "[Jump] Group %d: REST done → PREP G%d (3s)", 
                                         group_num + 1, s_jump_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Fall protocol state machine */
                if (s_fall_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_fall_phase_start_ms;
                    int group_num = s_fall_group + 1;  /* 1-based for display */

                    switch (s_fall_state) {
                    case FALL_PROTOCOL_PREP:
                        /* 3s prep phase: hold fall pose, label = "fall" */
                        snprintf(s_fall_display, sizeof(s_fall_display),
                                 "Fall: Prep G%d (%ds)", group_num,
                                 (int)((FALL_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= FALL_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_fall_state = FALL_PROTOCOL_EXECUTE;
                            s_fall_phase_start_ms = now_ms;
                            snprintf(s_fall_label, sizeof(s_fall_label), "fall");
                            /* Randomize duration for first group */
                            s_fall_duration_sec = FALL_EXECUTE_DURATION_MIN_SEC + 
                                (esp_random() % (FALL_EXECUTE_DURATION_MAX_SEC - FALL_EXECUTE_DURATION_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Fall] Group %d: PREP done → EXECUTE (%ds)", group_num, s_fall_duration_sec);
                        }
                        break;

                    case FALL_PROTOCOL_EXECUTE:
                        /* 5-10s fall execution: label = "fall" */
                        snprintf(s_fall_display, sizeof(s_fall_display),
                                 "Fall: Group %d (%ds)", group_num,
                                 (int)((s_fall_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_fall_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_fall_state = FALL_PROTOCOL_INTERVAL;
                            s_fall_phase_start_ms = now_ms;
                            snprintf(s_fall_label, sizeof(s_fall_label), "idle");
                            /* Randomize duration and interval */
                            s_fall_duration_sec = FALL_EXECUTE_DURATION_MIN_SEC + 
                                (esp_random() % (FALL_EXECUTE_DURATION_MAX_SEC - FALL_EXECUTE_DURATION_MIN_SEC + 1));
                            s_fall_interval_duration_sec = FALL_INTERVAL_MIN_SEC + 
                                (esp_random() % (FALL_INTERVAL_MAX_SEC - FALL_INTERVAL_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Fall] Group %d: DONE → REST (fall=%ds, rest=%ds)", 
                                     group_num, s_fall_duration_sec, s_fall_interval_duration_sec);
                        }
                        break;

                    case FALL_PROTOCOL_INTERVAL: {
                        /* 15-30s interval phase: rest, label = "idle" */
                        int next_group = (group_num < FALL_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_fall_display, sizeof(s_fall_display),
                                 "Fall: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_fall_interval_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_fall_interval_duration_sec * 1000) {
                            s_fall_group++;
                            if (s_fall_group >= FALL_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Fall] All %d groups complete!", FALL_TOTAL_GROUPS);
                                complete_fall_protocol_locked();
                            } else {
                                /* Create new file for next group（旧代码漏了 fclose：既泄漏 fd/FILE，
                                 * 又会让静态 stdio 缓冲槽位一直占着，第 3 组就只能退回
                                 * PSRAM 缓冲 → 又踩回 SDMMC 的 bounce buffer 老路） */
                                csv_fclose(&s_data_file);
                                snprintf(s_file_path, sizeof(s_file_path),
                                         BSP_SD_MOUNT_POINT "/fall_%d.csv",
                                         s_fall_group + 1);
                                s_data_file = fopen(s_file_path, "w");
                                if (!s_data_file) {
                                    ESP_LOGE(TAG, "[Fall] Failed to open group %d file", s_fall_group + 1);
                                    complete_fall_protocol_locked();
                                    break;
                                }
                                csv_io_attach(s_data_file);
                                fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
                                fflush(s_data_file);
                                /* Start next group's PREP phase */
                                s_fall_state = FALL_PROTOCOL_PREP;
                                s_fall_phase_start_ms = now_ms;
                                snprintf(s_fall_label, sizeof(s_fall_label), "fall");
                                ESP_LOGI(TAG, "[Fall] Group %d: REST done → PREP G%d (3s)", 
                                         group_num + 1, s_fall_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }
            }
            xSemaphoreGive(s_state_mutex);
        }

        /* ---- 节拍：绝对时间累加，但**绝不追赶** ----
         * 旧实现只做 next_wake_us += 10 ms，从不重新同步：一旦某个 tick 迟到
         * （LVGL 上屏、DHCP 重启、SD 擦除……），后续循环就是
         * 「delay_us <= 0 → 完全不等待」，把欠下的 tick 背靠背连打出来。
         * 于是串口的 100 点平均频率看着还能看（"12419" 其实是 124.19 Hz），
         * 而数据的真实时间分布是「一串贴在一起的点 + 一个空洞」；更糟的是
         * CSV 时间戳是按 base + n*10 ms 合成的，空洞在数据里根本看不出来。
         *
         * 改成「前沿对齐」：
         *   - 迟到 ≤ 1.5 个周期（正常调度抖动）：照常等到目标时刻，最多只
         *     补一拍，不改变平均速率；
         *   - 迟到 > 1.5 个周期：欠下的 tick 一律**丢弃**，把相位重新对齐到
         *     「现在 + 一个周期」，并计入 tick_stat_missed。
         * 宁可少几个点（并在日志里明确记下来），也不要制造假数据。 */
        int64_t now_us    = esp_timer_get_time();
        int64_t target_us = next_wake_us;
        int64_t late_us   = now_us - target_us;   /* > 0 = 已经过了目标时刻 */

        tick_stat_ticks++;
        if (late_us > 0) {
            tick_stat_catchup++;
            if (late_us > tick_stat_max_late_us) {
                tick_stat_max_late_us = (int32_t)late_us;
            }
            if (late_us > (int64_t)SAMPLE_PERIOD_MS * 1500) {
                tick_stat_missed +=
                    (uint32_t)(late_us / ((int64_t)SAMPLE_PERIOD_MS * 1000));
                next_wake_us = now_us + (int64_t)SAMPLE_PERIOD_MS * 1000;
                target_us    = next_wake_us;
            }
            if (late_us > (int64_t)SAMPLE_PERIOD_MS * 1000 / 2) {
                tick_stat_late++;
            }
        }

        int64_t delay_us = target_us - now_us;
        if (delay_us > 0) {
            /* Use precise us delay when < 2ms, otherwise use vTaskDelay */
            if (delay_us < 2000) {
                esp_rom_delay_us((uint32_t)delay_us);
            } else {
                vTaskDelay(pdMS_TO_TICKS((delay_us + 500) / 1000));  /* rounded */
            }
        }
        /* Advance to next period (phase accumulator — prevents drift) */
        next_wake_us += (int64_t)SAMPLE_PERIOD_MS * 1000;

        /* ---- 每 1 s 汇总一次「节拍质量」----
         * 单位写清楚（Hz，一位小数），不再出现 Hz×100 冒充 Hz、0.01 s 冒充 s。 */
        int64_t stat_now_us = esp_timer_get_time();
        if (stat_now_us - tick_stat_start_us >= 1000000) {
            int64_t span_us = stat_now_us - tick_stat_start_us;
            int32_t freq_d10 = (int32_t)((int64_t)tick_stat_ticks * 10000000 / span_us);
            ESP_LOGI(TAG, "[TICK] %" PRId32 ".%" PRId32 " Hz | ticks=%" PRIu32
                          " late=%" PRIu32 " catchup=%" PRIu32 " missed=%" PRIu32
                          " max_late=%" PRId32 "ms",
                     freq_d10 / 10, freq_d10 % 10,
                     tick_stat_ticks, tick_stat_late, tick_stat_catchup,
                     tick_stat_missed, tick_stat_max_late_us / 1000);
            tick_stat_start_us = stat_now_us;
            tick_stat_ticks = 0;
            tick_stat_late = 0;
            tick_stat_catchup = 0;
            tick_stat_missed = 0;
            tick_stat_max_late_us = 0;
        }
    }
}

/* ================================================================
 *  Main — Boot Sequence
 * ================================================================ */
void app_main(void)
{
    /* 0. Mutex */
    s_state_mutex = xSemaphoreCreateMutex();
    assert(s_state_mutex != NULL);

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        set_status_locked("Booting");
        s_file_path[0] = '\0';
        xSemaphoreGive(s_state_mutex);
    }

    /* 1. NVS (required by WiFi) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* 2. LVGL UI */
    create_ui();
    log_heap("after create_ui");

    /* 3. Wi-Fi + SNTP
     *    wifi_init_sta()     ：最多等 WIFI_CONNECT_TIMEOUT_MS，超时就离线启动（后台自愈）
     *    initialize_sntp()   ：不阻塞，只起后台对时任务（见「SNTP 对时」一节）——
     *                          网络后到也能自动对上时间，不用重启板子。 */
    wifi_init_sta();         // blocks until connected or failed (bounded)
    log_heap("after wifi_init_sta");
    initialize_sntp();       // non-blocking: SNTP sync runs in sntp_sync_task
    log_heap("after sntp");

    /* 4. SD card */
    init_sdcard();
    log_heap("after init_sdcard");

    /* 5. Accelerometer */
    if (app_accel_init() == ESP_OK) {
        ESP_LOGI(TAG, "Accelerometer initialized");
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            set_status_locked("IMU ready");
            xSemaphoreGive(s_state_mutex);
        }
    } else {
        ESP_LOGW(TAG, "Accelerometer not found, continuing without IMU");
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            set_status_locked("No IMU found");
            xSemaphoreGive(s_state_mutex);
        }
    }

    /* 5b. Camera (OV2640 DVP via esp_video; JPEG frames for the Web "photo" task).
     *     Must come after app_accel_init(): both share the BSP I2C bus, and the
     *     IMU now owns it through the new i2c_master driver, so app_camera_init()
     *     just reuses that bus for SCCB. It drives the sensor XCLK itself at
     *     20 MHz rather than calling bsp_camera_start() (fixed at 16 MHz).
     *     A failure here is not fatal: sampling/uploading continue and camera
     *     tasks fail with a clear reason. */
    if (app_camera_init() != ESP_OK) {
        ESP_LOGW(TAG, "Camera not available, photo tasks will report failure");
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            set_status_locked("No camera found");
            xSemaphoreGive(s_state_mutex);
        }
    }
    log_heap("after camera init");

    /* 5c. 闭环事件模块（LED + loop_task）。
     *     必须排在 init_buttons() 之前：按键回调会往 LED 闪烁队列里丢东西，
     *     队列得先存在。它只依赖 s_state_mutex（步骤 0 已建）与 HTTP 辅助函数，
     *     不碰采样器 / 上传 / 直播，是纯旁路。 */
    loop_module_init();
    log_heap("after loop_module_init");

    /* 6. Buttons */
    init_buttons();

    /* 7. Refresh UI once */
    refresh_ui();

    /* 8. Start sampler task.
     *    优先级 8：高于 task_poll(3)/uploader(4)/loop(3)/live(2)/housekeeping(2)，
     *    保证 10 ms 到点时能立刻抢到 CPU。UX 上完全没代价 —— 采样本身只有
     *    几十微秒，剩下的时间还是让给别的任务的。 */
    xTaskCreate(sampler_task, "sampler_task", 6144, NULL, 8, NULL);

    /* 8b. Housekeeping (UI 刷新 / 堆遥测 / WiFi 自愈) —— 从采样器里搬出来的
     *     所有抖动源都在这里，优先级最低。 */
    xTaskCreate(housekeeping_task, "housekeeping", 5120, NULL, 2, NULL);

    /* 9. Real-sensor upload queue + task (lower priority than sampler) */
    s_upload_queue = xQueueCreate(UPLOAD_QUEUE_LEN, sizeof(upload_batch_t *));
    assert(s_upload_queue != NULL);
    xTaskCreate(uploader_task, "upload_task", 8192, NULL, 4, NULL);

    /* 10. On-demand task polling (Web "capture once" / "photo" buttons).
     *     The mutex is created first: task_poll_task uses it as its
     *     "module is up" guard. The stack covers both the sample tasks and an
     *     inline camera capture + JPEG upload (10 KB). */
    s_task_mutex = xSemaphoreCreateMutex();
    assert(s_task_mutex != NULL);
    xTaskCreate(task_poll_task, "task_poll", 10240, NULL, 3, NULL);

    /* (sizeof(upload_batch_t) + 100 samples) x UPLOAD_QUEUE_LEN is the worst-case
     * heap held by in-flight periodic batches; one on-demand capture adds
     * TASK_MAX_SAMPLES samples on top of that while it runs. Logged so the cost
     * stays visible instead of assumed. */
    ESP_LOGI(TAG, "[HEAP] upload_batch_t=%u B x queue %d = %u B worst case"
                  " (+%u B for one on-demand capture)",
             (unsigned)sizeof(upload_batch_t), UPLOAD_QUEUE_LEN,
             (unsigned)(sizeof(upload_batch_t) * UPLOAD_QUEUE_LEN),
             (unsigned)(sizeof(upload_batch_t)
                        + sizeof(upload_sample_t) * TASK_MAX_SAMPLES));
    log_heap("system ready");

    ESP_LOGI(TAG, "System ready. Button A: start/stop normal IMU logging");
    ESP_LOGI(TAG, "Button A long press (2s): toggle camera live streaming"
                  " (POST %s, ~%d ms/frame, Web card 摄像头实时画面)",
             LIVE_API_PATH, LIVE_FRAME_INTERVAL_MS);
    ESP_LOGI(TAG, "Protocol toggle: Long-press Button B (2s) cycles through 5 protocols");
    ESP_LOGI(TAG, "  → Stand: 3x(prep 3s + stand 20-30s + idle 5s), CSV: stand_1/2/3.csv");
    ESP_LOGI(TAG, "  → Stairs: 3x(prep 3s + stairs 20-30s + idle 60s), CSV: stairs_1/2/3.csv");
    ESP_LOGI(TAG, "  → Bend: 3x(prep 3s + bend 20-30s + idle 60-90s), CSV: bend_1/2/3.csv");
    ESP_LOGI(TAG, "  → Jump: 3x(prep 3s + jump 5-10s + idle 5-10s), CSV: jump_1/2/3.csv");
    ESP_LOGI(TAG, "  → Fall: 3x(prep 3s + fall 5-10s + idle 15-30s), CSV: fall_1/2/3.csv");
    ESP_LOGI(TAG, "Data format: CSV (UTF-8), Fields: label, timestamp(ms), x(m/s²), y(m/s²), z(m/s²)");
    ESP_LOGI(TAG, "Target sampling frequency: %d Hz", TARGET_SAMPLE_FREQ_HZ);
}
