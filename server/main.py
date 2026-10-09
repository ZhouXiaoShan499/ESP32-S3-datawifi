# -*- coding: utf-8 -*-
"""
ESP32-S3-EYE 真实传感数据接收与存储服务（Web 平台端，FastAPI）

本文件是"板端 -> 平台"链路中的**服务端接收程序**（本地电脑运行）：
  * 接收开发板通过 HTTP POST 上传的 IMU 传感数据批次
  * 校验 设备来源 / 单位 / 时间 等关键字段（非法返回 400 + 原因，便于板端提示）
  * 存入 SQLite（标准库，零额外 DB 依赖）
  * 提供查询接口与一个实时读库的最小 Web 页面（数值不写死）

数据约定（与板端上传逻辑保持一致）：
  POST /api/v1/upload
  {
    "device_id": "esp32s3-eye-0001",   # 本组设备唯一标识
    "source":    "qma6100p",            # 传感源型号
    "unit":      "m/s^2",               # ax/ay/az 的单位
    "ts_ms":     1767123456789,         # 板端 NTP 同步后的采样起点时间戳(epoch ms)
    "request_id": "8f3c...",            # 可选：按需采集任务 id（manual 批次才有）
    "trigger":   "manual",              # 可选：periodic(周期上报) / manual(按需采集)
    "samples": [
        {"i": 0, "t_ms": 0,    "ax": 0.02, "ay": -0.15, "az": 9.81},
        {"i": 1, "t_ms": 10,   "ax": 0.03, "ay": -0.14, "az": 9.80}
    ]
  }

按需采集任务（manual capture task）—— 对应 Web 上「采集一次最新数据」按钮：
  * Web 侧：POST /api/v1/tasks 创建任务 → GET /api/v1/tasks/{request_id} 追踪状态
  * 板端侧：GET /api/v1/tasks/next?device_id=… 轮询取任务 → 采集一次 → 带 request_id 回传
  状态机：submitted → dispatched → acked → completed；失败为 failed，过期未完成变 timeout。
  request_id 是任务主键，并贯穿任务、回执、上传与样本（uploads.request_id 部分唯一索引保证幂等：
  同一 request_id 重复上传不会重复写样本）。
  鉴权范围：Web 侧接口不做 Token 校验（与既有查询接口一致）；设备侧接口
  （/api/v1/upload、/api/v1/tasks/next、/tasks/{id}/ack、/tasks/{id}/fail）在设置了 SENSOR_TOKEN
  时才要求 Bearer Token。

实时拍照（kind=camera）—— 对应 Web 上「拍一张照片」按钮：
  * Web 侧：POST /api/v1/tasks {kind:"camera"} 创建任务 → 轮询任务详情 → 刷新画廊
  * 板端侧：领到 camera 任务后拍一帧 JPEG，POST /api/v1/photos?device_id=…&request_id=… 上传
  * 服务端：图片原样落盘 server/photos/<device_id>/<photo_id>.jpg，元数据进 photos 表，
    并在同一事务里把任务置 completed（幂等：同一 request_id 重复上传不会重复存图）
  查询/管理接口：GET /api/v1/photos（列表）· GET /api/v1/photos/{id}（JPEG 字节）
  · DELETE /api/v1/photos/{id}（删库行 + 删文件）。

摄像头实时直播（live camera）—— 对应板端长按 Button A 的「实时画面」：
  * 板端侧：长按 Button A 开启后，live_stream_task 循环「拍一帧 JPEG → POST /api/v1/live」
    （约 2-4 fps，body 即 JPEG 字节，带 device_id/ts_ms/w/h 查询参数），再长按一次停止。
  * 服务端：**只在内存里保留每台设备的最新一帧**（不落盘、不进 photos 表、不建任务），
    Web 侧 GET /api/v1/live 看状态、GET /api/v1/live/frame 取最新一帧 JPEG，
    <img> 按 seq 轮询即形成实时画面。直播是连续流，刻意不做逐帧 ACK/幂等：
    丢一帧下一帧立刻补上，服务端重启/板端断网只是画面停在最后一帧（active=false）。
  超过 LIVE_TIMEOUT_S 没有新帧即视为「已停止推流」（板端被按键停止 / 断网 / 掉电）。
  内存里最多保留 LIVE_MAX_DEVICES 台设备的帧（每帧 ≤LIVE_MAX_FRAME_BYTES），
  超过 LIVE_STALE_EVICT_S 无新帧或超出台数上限时淘汰最久未推流的那台 ——
  否则内存只增不减，而 device_id 来自（默认免鉴权的）POST，是一处内存放大点。

运行（本地电脑）：
  python server/main.py          # 默认 http://127.0.0.1:8000
可用环境变量覆盖：SENSOR_HOST / SENSOR_PORT / SENSOR_DB / SENSOR_TOKEN
（SENSOR_TOKEN 为空=不鉴权，仅局域网联调；非空=要求 Bearer Token）
"""

import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
HOST = os.environ.get("SENSOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("SENSOR_PORT", "8000"))

# 可选上传鉴权：为空则不做校验（仅局域网联调）；非空则要求 Bearer Token
SENSOR_TOKEN = os.environ.get("SENSOR_TOKEN", "").strip()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
DB_PATH = os.environ.get(
    "SENSOR_DB", os.path.join(_DATA_DIR, "upload.db")
)
# sqlite 写锁等待上限（毫秒）：与 sqlite3.connect(timeout=) 同义，显式写出来是为了
# 让「撞锁时等多久」一眼可见（真正生效的设置在 _tune_conn 里）。
DB_BUSY_TIMEOUT_MS = 10000

# 限制与常量
MAX_DEVICE_ID_LEN = 64
MAX_SOURCE_LEN = 32
MAX_SAMPLES_PER_BATCH = 2000          # 单批最多样本数（板端约 100 Hz，分批上传）
MAX_BODY_BYTES = 2 * 1024 * 1024      # 单次请求体上限 2 MB
# 通用 JSON 接口（events / tasks 这些小对象）的请求体上限。
# 这几个接口过去直接 `await request.json()`，**完全没有上限** —— 几百 MB 的 body
# 会先被读进内存，才轮到任何字段校验，等于一个免鉴权的内存放大点（见 _read_body_limited）。
MAX_JSON_BODY_BYTES = 64 * 1024       # 64 KiB
SUPPORTED_AXES = ("ax", "ay", "az")

# /api/v1/window（实时波形）上限：
#   300 s × 100 Hz = 30000 点，这里限 12000 点（约 120 s @100Hz）避免响应过大
MAX_WINDOW_SECONDS = 300
MAX_WINDOW_POINTS = 12000

# ---------------------------------------------------------------------------
# 按需采集任务（manual capture task）参数与状态
# ---------------------------------------------------------------------------
TASK_DEFAULT_SOURCE = "qma6100p"     # 沿用板端真实传感源
TASK_DEFAULT_UNIT = "m/s^2"
TASK_DEFAULT_RATE_HZ = 100           # QMA6100P 100 Hz → 100 点约为 1 s 数据
TASK_DEFAULT_COUNT = 100
TASK_DEFAULT_TIMEOUT_S = 60
TASK_MIN_RATE_HZ, TASK_MAX_RATE_HZ = 10, 200
TASK_MIN_TIMEOUT_S, TASK_MAX_TIMEOUT_S = 5, 600
TASK_TERMINAL_STATES = ("completed", "failed", "timeout")
TASK_STATUS_CN = {
    "submitted": "已提交",
    "dispatched": "已下发",
    "acked": "设备接收",
    "completed": "完成",
    "failed": "失败",
    "timeout": "超时",
}
# 任务类型 kind（Web 三个按钮共用同一套任务链路与状态机）：
#   capture = 按需采集一批数据（「采集一次最新数据」），靠带 request_id 的上传收尾；
#   camera  = 按需拍一帧 JPEG（「拍一张照片」），靠带 request_id 的照片上传收尾；
#   pause   = 暂停板端周期上报（「暂停周期」）；
#   resume  = 提前恢复周期上报（「恢复周期」）。
# pause/resume 不产生观测数据，无法靠 upload 收尾，改由板端确认「已生效」的
# POST /api/v1/tasks/{id}/applied 收尾（完成后同事务写 device_control）。
TASK_KIND_CAPTURE = "capture"
TASK_KIND_CAMERA = "camera"
TASK_KIND_PAUSE = "pause"
TASK_KIND_RESUME = "resume"
TASK_KIND_PREVIEW = "preview"
TASK_KINDS = (TASK_KIND_CAPTURE, TASK_KIND_CAMERA, TASK_KIND_PAUSE, TASK_KIND_RESUME, TASK_KIND_PREVIEW)
TASK_KIND_CN = {
    TASK_KIND_CAPTURE: "按需采集",
    TASK_KIND_CAMERA: "实时拍照",
    TASK_KIND_PAUSE: "暂停周期上报",
    TASK_KIND_RESUME: "恢复周期上报",
    TASK_KIND_PREVIEW: "本地预览",
}
# preview 也走「控制类」收尾（板端 /applied），但不产生 device_control 状态：
# 它只切换板端 LCD 预览开关，不影响 periodic_paused。
TASK_CONTROL_KINDS = (TASK_KIND_PAUSE, TASK_KIND_RESUME, TASK_KIND_PREVIEW)

# 暂停时长（秒）：到点板端自愈恢复、服务端惰性归零，忘点「恢复周期」也不会永久哑掉。
PAUSE_DEFAULT_S = 120
PAUSE_MIN_S, PAUSE_MAX_S = 5, 600

# 实时拍照（kind=camera）：板端拍一帧 JPEG 后 POST /api/v1/photos（body 即 JPEG 字节）。
# 图片按 device_id 分目录落盘（server/photos/<device_id>/<photo_id>.jpg），
# 元数据进 photos 表；带 request_id 时在同一事务里把 camera 任务置 completed，
# 与「上传样本收尾采集任务」完全同一哲学（store_upload）。
PHOTO_DIR = os.environ.get("SENSOR_PHOTO_DIR") or os.path.join(_BASE_DIR, "photos")
MAX_PHOTO_BYTES = 512 * 1024          # 单张 JPEG 上限 512 KiB（640x480 约 20-60 KB）
PHOTO_DEFAULT_LIMIT = 20              # GET /api/v1/photos 默认返回张数
PHOTO_MAX_LIMIT = 200
PHOTO_JPEG_MAGIC = b"\xff\xd8\xff"    # JPEG SOI + 首个 marker，用于拒绝非图片 body
PHOTO_MAX_NOTE_LEN = 200
PHOTO_ID_RE = re.compile(r"^\d{1,18}$")

# request_id 允许的字符集（uuid4().hex 天然满足，也兼容自定 id）
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

# device_id 允许的字符集：与 _safe_device_dir() 的落盘规则保持一致（字母/数字/点/横线/下划线）。
# 为什么要在这里卡住：device_id 由板端上报，会被拼进首页 HTML（index()）和
# server/photos/<device_id>/ 目录名 —— 白名单一次同时挡住存储型 XSS 与目录穿越，
# 比在每个输出点补转义更不容易漏（板端 id 形如 esp32s3-eye-0001，天然满足）。
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# 「板端时钟是否可信」的判据。
# 板端 SNTP 成功之前 gettimeofday() 返回的只是**开机以来的时间**，不是真实 epoch
# （固件 main.c:5820-5824 的会话锚点就是这么取的），因此它约等于 0 而不是 1.7e12。
# 拿这种 ts_ms 去和 task.created_at 比大小没有任何意义：判据恒成立，
# 「网段里没有可用 NTP」的现场就永远做不了按需采集（会被误判成「上传的数据是陈旧的」）。
# 固件侧对所有「本地消费 epoch」的地方都用 s_time_synced 守卫过
# （main.c:4042 / 4191 / 4505），但上报给服务端的 ts_ms 本身无法自卫 ——
# 所以这条判据只服务端兜得住：看起来不像真实毫秒时间的 ts_ms 一律不参与新鲜度比较。
EPOCH_SANE_MS = 1_600_000_000_000   # 2020-09-13；低于它视为「板端时钟未同步」

# 摄像头实时直播（板端长按 Button A 开启）：
#   板端循环 POST /api/v1/live（body 即 JPEG 字节）→ 服务端只在内存里保留每台设备的
#   「最新一帧」，供 Web「摄像头实时画面」卡片按 seq 轮询取图（<img> 即实时画面）。
#   刻意不落盘、不进 photos 表、不产生任务：直播是连续流，逐帧 ACK/幂等只会拖慢节拍。
LIVE_TIMEOUT_S = float(os.environ.get("SENSOR_LIVE_TIMEOUT_S", "5"))   # 无新帧超过该秒数 = 已停止
LIVE_MAX_FRAME_BYTES = MAX_PHOTO_BYTES          # 单帧上限沿用拍照（512 KiB）
LIVE_JPEG_MAGIC = PHOTO_JPEG_MAGIC              # 同样用 JPEG SOI 拒绝非图片 body
LIVE_DEFAULT_FPS = 4.0                          # 板端 LIVE_FRAME_INTERVAL_MS=250 的名义帧率
# 内存里最多保留几台设备的帧、多久没新帧就彻底丢掉。
# 不设上限的后果是**内存单调增长**：每条最多 LIVE_MAX_FRAME_BYTES（512 KiB），
# 任何能访问 POST /api/v1/live 的客户端都能用随机 device_id 把它灌满；
# 板端停推流后最后一帧也会永久驻留（没有淘汰点）。上限 8 台 ≈ 4 MiB，多板联调够用。
LIVE_MAX_DEVICES = 8
LIVE_STALE_EVICT_S = 600                        # 10 分钟没有新帧 ⇒ 连最后一帧也放掉
_live_lock = threading.Lock()
# device_id -> {"jpeg": bytes, "width":…, "height":…, "ts_ms":…, "received_at":…, "seq":…, "ip":…}
_live_frames = {}
_live_seq = 0                                   # 全局单调帧序号（Web 侧用它判断“有新帧了”）

# ---------------------------------------------------------------------------
# 闭环事件（loop event）—— 板端按键触发 → 本地反馈 → 远端显示 → 回应/取消
#
# 与 tasks（Web 下发、板端执行）方向正好相反：这里是**板端发起、Web 接收**。
# 因此不复用 tasks 表（tasks 的主键语义是「服务端派发的任务」，把板端发起的事件
# 塞进去会让 /tasks/next 把它当成待执行任务下发给板子，形成回环）。
#
# 状态机（互斥、单向，除 pending 外都不可回退）：
#
#        ┌──────────── trigger ────────────┐
#        │                                 ▼
#      (板端按键)                        pending ── respond(cancel) ──▶ cancelled
#                                          │  │
#                                          │  └── respond(accept) ──▶ ack ──┐
#                                          │                               │
#                                          └── respond(confirm) ──────────┴──▶ completed
#
#   pending   板端已触发，等远端（Web）处理 —— 页面显示「待处理」
#   ack       板端/远端已回应「受理」 —— 页面显示「已回应」
#   cancelled 被取消（板端长按取消键，或页面点「取消」）
#   completed 闭环完成（页面点「确认完成」，或板端在已 ack 后再次回应）
# 板端把 ack / cancelled / completed 都视为「远端已回来」，随即回到 IDLE。
# ---------------------------------------------------------------------------
EVENT_STATUS_PENDING = "pending"
EVENT_STATUS_ACK = "ack"
EVENT_STATUS_CANCELLED = "cancelled"
EVENT_STATUS_COMPLETED = "completed"
EVENT_STATUS_EXPIRED = "expired"
EVENT_STATUSES = (EVENT_STATUS_PENDING, EVENT_STATUS_ACK,
                  EVENT_STATUS_CANCELLED, EVENT_STATUS_COMPLETED,
                  EVENT_STATUS_EXPIRED)
EVENT_TERMINAL_STATES = (EVENT_STATUS_CANCELLED, EVENT_STATUS_COMPLETED,
                         EVENT_STATUS_EXPIRED)
EVENT_STATUS_CN = {
    EVENT_STATUS_PENDING: "待处理",
    EVENT_STATUS_ACK: "已回应",
    EVENT_STATUS_CANCELLED: "已取消",
    EVENT_STATUS_COMPLETED: "已完成",
    EVENT_STATUS_EXPIRED: "已过期",
}
# respond 的 action → 目标状态。accept/cancel/confirm 三个动作共用一条接口，
# 所以「板端回应/取消」和「Web 远端确认」走的是同一个 POST /api/v1/events/respond。
EVENT_ACTION_TO_STATUS = {
    "accept": EVENT_STATUS_ACK,
    "cancel": EVENT_STATUS_CANCELLED,
    "confirm": EVENT_STATUS_COMPLETED,
}
EVENT_DEFAULT_KIND = "alert"      # 触发语义：当前只有一种「呼叫/求助」型事件
EVENT_MAX_KIND_LEN = 32
EVENT_MAX_NOTE_LEN = 200
EVENT_DEFAULT_LIMIT = 20          # GET /api/v1/events 默认返回条数
EVENT_MAX_LIMIT = 200
# 事件有效期：超过即惰性判为过期（板端掉电/断网后不会永远挂在「待处理」）。
# 与 tasks 的惰性超时同一哲学，不引入后台线程。
EVENT_DEFAULT_TTL_S = float(os.environ.get("SENSOR_EVENT_TTL_S", "300"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """ASGI 启动钩子：建表。

    两种启动方式都要覆盖——`python main.py` 走本文件底部的 serve()（那里也会
    显式调一次 init_db()，幂等，好处是绑端口之前就能因权限/路径问题报错退出）；
    而 `uvicorn main:app` / `--reload` / gunicorn 等 ASGI 服务器**不会执行
    serve()**，只发 lifespan。少了这里，全新库上的第一个请求就是
    500 "no such table: uploads"。
    init_db 定义在文件下方：函数体在 startup 时才执行，前向引用没问题。
    """
    init_db()
    yield


app = FastAPI(
    title="ESP32-S3 IMU Sensor Receiver",
    description="接收并存储开发板 IMU 传感数据，提供查询接口。",
    version="1.0.0",
    lifespan=_lifespan,
)

# 交互式实时监控页（server/static 下静态文件，托管于 http://host:port/ui/）
_UI_DIR = os.path.join(_BASE_DIR, "static")
os.makedirs(_UI_DIR, exist_ok=True)
app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")

# ---------------------------------------------------------------------------
# AI Agent 路由（自然语言 → 意图 → 受限工具调用）
#   * agent 包与 main.py 同级于 server/，运行期按**顶层包 agent** 导入
#     （与 server/test_*.py 里 `import main` 同一 sys.path 约定）；
#     从仓库根目录以 server.agent 布局导入时也兼容。
#   * 纯附加能力：注册失败只打印一行告警，主接收服务照常启动。
# ---------------------------------------------------------------------------
try:
    try:
        from agent.api import router as _agent_router
    except ImportError:
        from server.agent.api import router as _agent_router
    app.include_router(_agent_router)
    _AGENT_PATHS = sorted(r.path for r in _agent_router.routes)   # 自测/排障用
    print("[sensor-server] AI Agent 已挂载: " + " · ".join(_AGENT_PATHS))
except Exception as _agent_exc:                       # noqa: BLE001
    print(f"[sensor-server] [agent] AI Agent 路由注册失败"
          f"（该功能不可用，主服务不受影响）: {_agent_exc}")

# sqlite 连接：多线程下为每个请求单独建连接，见 get_db()
_db_init_lock = threading.Lock()
_initialized = False

# _table_columns() 的白名单（见该函数 docstring）。
_SQL_TABLES = ("uploads", "samples", "tasks", "device_control", "photos", "events")


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def _tune_conn(conn):
    """连接级调优：WAL + 显式 busy_timeout。

    WAL 的必要性来自本服务的访问模式 ——「100 Hz 采样按秒批量写 + 高频读」
    （Web 每 800 ms 轮询、/api/v1/window 画波形）。默认的 rollback-journal 模式下，
    写事务提交的那一小段是 EXCLUSIVE 锁，读方会直接撞上 SQLITE_BUSY 变成 500。
    WAL 下写不阻塞读、读不阻塞写，正好对上这个模式。

    journal_mode 是**记在库文件头里的持久设置**，重复执行只是读回来确认一次，
    代价可忽略；busy_timeout 是连接级设置，所以每个连接都要设。
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(DB_BUSY_TIMEOUT_MS)}")
    except sqlite3.Error as e:
        # 只读目录 / 不支持 -wal -shm 附属文件的网络盘：不要因此起不来，
        # 退化成默认日志模式即可（功能不受影响，只是并发读写更容易撞锁）。
        print(f"[sensor-server] WAL/busy_timeout 设置失败（继续用默认日志模式）: {e}")


def _connect():
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    _tune_conn(conn)
    return conn


def _table_columns(conn, table):
    """读取某表已有列名，用于老库补列判断。

    表名在 SQLite 里**不能**用参数占位符（`PRAGMA table_info(?)` 不合法），只能
    拼字符串 —— 当前所有调用方都传硬编码字面量，所以是安全的。加一层白名单是为了
    让"将来有人把变量传进来"时立刻抛错，而不是静默变成注入点。
    """
    if table not in _SQL_TABLES:
        raise ValueError(f"unexpected table name for PRAGMA: {table!r}")
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_uploads_columns(conn):
    """老库补列 + 建立 request_id 唯一索引。

    CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段，所以升级服务端后
    旧库必须靠 ALTER TABLE 补 request_id / trigger，否则上传会直接报错。
    """
    cols = _table_columns(conn, "uploads")
    if "request_id" not in cols:
        conn.execute("ALTER TABLE uploads ADD COLUMN request_id TEXT")
    if "trigger" not in cols:
        conn.execute(
            "ALTER TABLE uploads ADD COLUMN trigger TEXT NOT NULL DEFAULT 'periodic'"
        )
    # 一条 request_id 最多对应一条上传：同一任务重复上传不会重复写样本。
    # 必须在补列之后创建，否则老库上会因缺列失败。
    #
    # 与 _migrate_photos_index() 同一套防御：唯一的**建索引动作**只有在「没有重复行」
    # 时才执行，否则 init_db() 会整段抛 IntegrityError ⇒ 服务直接起不来。这不是假想场景：
    # 加索引之前的历史代码是「SELECT 查重 → INSERT」，并发重试确实能写出重复行
    # （photos 侧就是这么留下的重复数据，见 _migrate_photos_index 的注释）。
    # 有重复行时只告警跳过 —— 写路径有 BEGIN IMMEDIATE 串行化 + IntegrityError 幂等兜底，
    # 不依赖这个索引才正确，服务照常启动。
    dup = conn.execute(
        "SELECT request_id FROM uploads WHERE request_id IS NOT NULL"
        " GROUP BY request_id HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        print(f"[sensor-server] uploads: request_id={dup['request_id']!r} 存在重复行，"
              "跳过唯一索引（去重后可自动创建）")
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_request"
        " ON uploads (request_id) WHERE request_id IS NOT NULL"
    )


def _migrate_tasks_columns(conn):
    """老库补列：tasks.kind / tasks.duration_s。

    同 _migrate_uploads_columns：CREATE TABLE IF NOT EXISTS 不会给已存在的表
    加字段，升级服务端后旧库必须靠 ALTER TABLE 补齐，否则插入控制任务会报错。
    kind 默认 'capture'，所以历史任务在新接口下仍按「按需采集」渲染。
    """
    cols = _table_columns(conn, "tasks")
    if "kind" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'capture'"
        )
    if "duration_s" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN duration_s REAL")
    # 去重键从 device+source 变成 device+source+kind，补一个匹配索引。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_device_kind"
        " ON tasks (device_id, source, kind, status)"
    )


def _migrate_photos_index(conn):
    """把 photos.request_id 加固成「部分唯一索引」，与 uploads 侧对齐。

    uploads 那边可以无条件 `CREATE UNIQUE INDEX`，photos 不行：老库里可能已经存在
    同一 request_id 的重复行（那正是过去没有唯一索引时并发写出来的结果），此时建唯一
    索引会整段失败，让 init_db 抛错、服务直接起不来。

    所以这里的策略是**非破坏性**的：
      * 有重复行 → 只告警、跳过（由运维决定留哪一张；服务照常启动。写路径已有
        BEGIN IMMEDIATE 串行化 + IntegrityError 幂等兜底，不依赖这个索引才正确）；
      * 没有重复行 → 建唯一索引，并顺手删掉被它完全覆盖的普通索引。
    """
    dup = conn.execute(
        "SELECT request_id FROM photos WHERE request_id IS NOT NULL"
        " GROUP BY request_id HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        print(f"[sensor-server] photos: request_id={dup['request_id']!r} 存在重复行，"
              "跳过唯一索引（去重后可自动创建）")
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_photos_request_uq"
        " ON photos (request_id) WHERE request_id IS NOT NULL"
    )
    # idx_photos_request 是 (request_id) 上的普通索引，被上面的部分唯一索引完全覆盖。
    conn.execute("DROP INDEX IF EXISTS idx_photos_request")


def init_db():
    """建表：uploads(每个批次) + samples(每批内的每个采样点) + tasks(按需采集任务)。"""
    global _initialized
    if _initialized:
        return
    with _db_init_lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id     TEXT NOT NULL,
                    source        TEXT NOT NULL,
                    unit          TEXT NOT NULL,
                    ts_ms         INTEGER NOT NULL,   -- 板端上报的批次起点时间(epoch ms)
                    received_at   REAL NOT NULL,      -- 服务端入库时间(epoch s, 带小数)
                    sample_count  INTEGER NOT NULL,
                    ip            TEXT,
                    request_id    TEXT,               -- 按需采集任务 id（manual 批次才有）
                    trigger       TEXT NOT NULL DEFAULT 'periodic',  -- periodic / manual
                    payload       TEXT NOT NULL       -- 原始 JSON，便于追溯原始记录
                );
                CREATE INDEX IF NOT EXISTS idx_uploads_device_ts
                    ON uploads (device_id, ts_ms);
                -- /api/v1/window 按服务端入库时间做范围过滤，需要这个索引，
                -- 否则库变大后每 800ms 一次的轮询会退化成全表扫描。
                CREATE INDEX IF NOT EXISTS idx_uploads_received
                    ON uploads (received_at);
                -- 带 device_id 过滤时的窗口查询走 (device_id, received_at) 范围扫描，
                -- 代价只与「窗口内的批次数」有关，与历史总量无关。
                CREATE INDEX IF NOT EXISTS idx_uploads_device_received
                    ON uploads (device_id, received_at);
                CREATE TABLE IF NOT EXISTS samples (
                    upload_id INTEGER NOT NULL REFERENCES uploads(id),
                    device_id TEXT NOT NULL,
                    seq       INTEGER NOT NULL,       -- 板内序号 i
                    t_ms      INTEGER NOT NULL,       -- 相对批次起点的偏移(ms)
                    ax REAL, ay REAL, az REAL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_upload
                    ON samples (upload_id);
                -- 按需采集任务：request_id 即任务主键，贯穿任务/回执/上传
                CREATE TABLE IF NOT EXISTS tasks (
                    request_id     TEXT PRIMARY KEY,
                    device_id      TEXT NOT NULL,
                    source         TEXT NOT NULL,
                    unit           TEXT NOT NULL,
                    sample_rate_hz INTEGER NOT NULL,
                    sample_count   INTEGER NOT NULL,
                    trigger        TEXT NOT NULL DEFAULT 'manual',
                    kind           TEXT NOT NULL DEFAULT 'capture',  -- capture/pause/resume
                    duration_s     REAL,             -- 仅 pause：暂停时长（到期板端自愈恢复）
                    status         TEXT NOT NULL,   -- submitted/dispatched/acked/completed/failed/timeout
                    created_at     REAL NOT NULL,
                    dispatched_at  REAL,
                    acked_at       REAL,
                    completed_at   REAL,
                    expires_at     REAL NOT NULL,   -- 超过即判 timeout（惰性判定）
                    upload_id      INTEGER,
                    error          TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_device_status
                    ON tasks (device_id, status);
                CREATE INDEX IF NOT EXISTS idx_tasks_pending
                    ON tasks (device_id, status, created_at);
                -- 周期上报暂停状态（每个 device+source 一行）：这是「暂停是否真的生效」
                -- 的真相源，Web 直接查 /api/v1/control，不必靠「periodic 批次数为 0」反推。
                -- paused_until 到点即视为自动恢复（惰性归零，不引入后台线程）。
                CREATE TABLE IF NOT EXISTS device_control (
                    device_id       TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    periodic_paused INTEGER NOT NULL DEFAULT 0,
                    paused_until    REAL,
                    request_id      TEXT,
                    updated_at      REAL,
                    PRIMARY KEY (device_id, source)
                );
                -- 实时拍照：一行 = 一张 JPEG 文件（path 是相对 server/ 的路径）。
                -- 图片字节不进库（BLOB 会让备份/查询变重），只存元数据 + 路径。
                CREATE TABLE IF NOT EXISTS photos (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id   TEXT NOT NULL,
                    request_id  TEXT,             -- 来自 kind=camera 任务（手动拍照才有）
                    ts_ms       INTEGER,          -- 板端拍摄时刻(epoch ms)
                    received_at REAL NOT NULL,    -- 服务端入库时间(epoch s)
                    bytes       INTEGER NOT NULL,
                    width       INTEGER,
                    height      INTEGER,
                    ip          TEXT,
                    path        TEXT NOT NULL,    -- 相对 _BASE_DIR 的 jpg 路径
                    note        TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_photos_device_time
                    ON photos (device_id, received_at);
                -- request_id 上的索引刻意不在这里建：老库可能已有重复行，唯一索引
                -- 必须走 _migrate_photos_index() 的「先去重判断、失败只告警」路径。
                -- 闭环事件：板端按键触发 → 远端（Web）显示 → 板端回应/取消。
                -- 与 tasks 方向相反（板端发起、Web 接收），故独立成表，避免被
                -- /api/v1/tasks/next 当成待下发任务回环给板子。
                CREATE TABLE IF NOT EXISTS events (
                    request_id     TEXT PRIMARY KEY,   -- 板端生成的闭环请求号
                    device_id      TEXT NOT NULL,
                    source         TEXT NOT NULL,
                    kind           TEXT NOT NULL DEFAULT 'alert',
                    status         TEXT NOT NULL,      -- pending/ack/cancelled/completed
                    created_at     REAL NOT NULL,      -- 服务端接收触发时刻(epoch s)
                    device_ts_ms   INTEGER,            -- 板端触发时刻(epoch ms, NTP)
                    responded_at   REAL,               -- 最近一次响应时刻(epoch s)
                    response       TEXT,               -- 最近一次 action：accept/cancel/confirm
                    responded_by   TEXT,               -- device / web
                    closed_at      REAL,               -- 进入终态的时刻(epoch s)
                    expires_at     REAL NOT NULL,      -- 超过即惰性判 expired
                    ip             TEXT,
                    note           TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_device_time
                    ON events (device_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_status
                    ON events (status, created_at);
                """
            )
            _migrate_uploads_columns(conn)
            _migrate_tasks_columns(conn)
            _migrate_photos_index(conn)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _normalize_unit(raw):
    """把板端可能的不同写法归一成 m/s^2，未知单位返回 None。"""
    if not isinstance(raw, str):
        return None
    s = re.sub(r"\s+", "", raw).strip()
    if s in ("m/s^2", "m/s2", "m/s\u00b2", "ms^-2"):
        return "m/s^2"
    if s in ("g",):
        return "g"
    return None


def _device_id_ok(device_id):
    """device_id 字符白名单（规则与 _safe_device_dir() 一致，见 DEVICE_ID_RE 注释）。

    调用点都在 strip() + 长度检查之后，所以这里只需再查字符集：
    device_id 会被拼进首页 HTML（index()）与 photos 目录名，卡住字符集一次
    同时消除存储型 XSS 与目录穿越，比在每处输出点补转义更不易漏。
    """
    return DEVICE_ID_RE.match(device_id) is not None


def validate_payload(payload):
    """返回 (ok, data|error_message)。"""
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return False, "missing or empty 'device_id'"
    device_id = device_id.strip()
    if len(device_id) > MAX_DEVICE_ID_LEN:
        return False, f"'device_id' too long (max {MAX_DEVICE_ID_LEN})"
    if not _device_id_ok(device_id):
        return False, ("invalid 'device_id' (only letters, digits, '.', '-' and '_'"
                       " are accepted)")

    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        return False, "missing or empty 'source'"
    source = source.strip()[:MAX_SOURCE_LEN]

    unit = _normalize_unit(payload.get("unit"))
    if unit is None:
        return False, "invalid or missing 'unit' (expected m/s^2 or g)"

    ts_ms = payload.get("ts_ms")
    if not isinstance(ts_ms, int) and not (
        isinstance(ts_ms, float) and ts_ms.is_integer()
    ):
        return False, "missing or non-integer 'ts_ms'"
    ts_ms = int(ts_ms)
    if ts_ms <= 0:
        return False, "'ts_ms' must be a positive epoch millisecond"

    samples = payload.get("samples")
    if not isinstance(samples, list) or len(samples) == 0:
        return False, "missing or empty 'samples' list"
    if len(samples) > MAX_SAMPLES_PER_BATCH:
        return False, f"'samples' too large (max {MAX_SAMPLES_PER_BATCH})"

    cleaned = []
    for idx, s in enumerate(samples):
        if not isinstance(s, dict):
            return False, f"samples[{idx}] is not an object"
        try:
            vals = {axis: float(s[axis]) for axis in SUPPORTED_AXES}
        except (KeyError, TypeError, ValueError):
            return False, f"samples[{idx}] must have numeric ax/ay/az"
        t_ms = s.get("t_ms", 0)
        try:
            t_ms = int(t_ms)
        except (TypeError, ValueError):
            return False, f"samples[{idx}].t_ms must be an integer"
        seq = s.get("i", idx)
        # 与 ts_ms 同一套判据：只接受整数（或整数值的 float）。过去这里直接透传，
        # 字符串/对象会一路写进 samples.seq；查询侧（/api/v1/window、首页 latest_az
        # 的 ORDER BY seq）依赖它排序，SQLite 的类型亲和性会让非数值变成字符串比较，
        # 排序结果就错了。
        if not isinstance(seq, int) and not (
            isinstance(seq, float) and seq.is_integer()
        ):
            return False, f"samples[{idx}].i must be an integer"
        cleaned.append({"i": int(seq), "t_ms": t_ms, **vals})

    # 可选字段 request_id / trigger：按需采集任务才有；
    # 缺省时行为与旧版完全一致（trigger 归一为 periodic）。
    request_id = payload.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id.strip()):
            return False, "invalid 'request_id' (expect 6-64 chars of [A-Za-z0-9_-])"
        request_id = request_id.strip()

    trigger = payload.get("trigger")
    if isinstance(trigger, str) and trigger.strip() in ("periodic", "manual"):
        trigger = trigger.strip()
    else:
        trigger = "manual" if request_id else "periodic"

    return True, {
        "device_id": device_id,
        "source": source,
        "unit": unit,
        "ts_ms": ts_ms,
        "samples": cleaned,
        "request_id": request_id,
        "trigger": trigger,
    }


def validate_photo_payload(payload):
    """校验 POST /api/v1/photos 的参数，返回 (ok, data|error_message)。

    payload 是「查询参数 + body」拼出来的 dict：
      device_id(必填) / request_id(可选) / ts_ms(可选) / width,height(可选)
      / note(可选) / jpeg(body 原始字节) / ip。
    """
    if not isinstance(payload, dict):
        return False, "payload must be a mapping"

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return False, "missing or empty 'device_id'"
    device_id = device_id.strip()
    if len(device_id) > MAX_DEVICE_ID_LEN:
        return False, f"'device_id' too long (max {MAX_DEVICE_ID_LEN})"
    if not _device_id_ok(device_id):
        return False, ("invalid 'device_id' (only letters, digits, '.', '-' and '_'"
                       " are accepted)")

    jpeg = payload.get("jpeg")
    if not isinstance(jpeg, (bytes, bytearray)) or len(jpeg) < 4:
        return False, "empty request body (expected raw JPEG bytes)"
    if len(jpeg) > MAX_PHOTO_BYTES:
        return False, (f"photo too large ({len(jpeg)} > {MAX_PHOTO_BYTES} bytes)")
    # 只接受 JPEG：服务端按 .jpg 落盘并原样回吐给 <img>，非图片 body 一律拒绝
    if bytes(jpeg[:len(PHOTO_JPEG_MAGIC)]) != PHOTO_JPEG_MAGIC:
        return False, "body is not a JPEG image (expected SOI marker FF D8 FF)"

    request_id = payload.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id.strip()):
            return False, "invalid 'request_id'"
        request_id = request_id.strip() or None

    raw_ts = payload.get("ts_ms")
    if raw_ts in (None, ""):
        ts_ms = None
    else:
        try:
            ts_ms = int(raw_ts)
        except (TypeError, ValueError):
            return False, "invalid 'ts_ms'"
        if ts_ms < 0:
            return False, "invalid 'ts_ms'"

    dims = {}
    for name in ("width", "height"):
        raw = payload.get(name)
        if raw in (None, ""):
            dims[name] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return False, f"invalid '{name}'"
        if value <= 0 or value > 8192:
            return False, f"invalid '{name}' (expect 1-8192)"
        dims[name] = value

    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return False, "invalid 'note'"
    note = (note or "").strip()[:PHOTO_MAX_NOTE_LEN] or None

    return True, {
        "device_id": device_id,
        "request_id": request_id,
        "ts_ms": ts_ms,
        "width": dims["width"],
        "height": dims["height"],
        "note": note,
        "jpeg": bytes(jpeg),
        "ip": payload.get("ip"),
    }


def validate_task_request(payload):
    """校验 POST /api/v1/tasks 的请求体，返回 (ok, data|error_message)。"""
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return False, "missing or empty 'device_id'"
    device_id = device_id.strip()
    if len(device_id) > MAX_DEVICE_ID_LEN:
        return False, f"'device_id' too long (max {MAX_DEVICE_ID_LEN})"
    if not _device_id_ok(device_id):
        return False, ("invalid 'device_id' (only letters, digits, '.', '-' and '_'"
                       " are accepted)")

    source = payload.get("source") or TASK_DEFAULT_SOURCE
    if not isinstance(source, str) or not source.strip():
        return False, "'source' must be a non-empty string"
    source = source.strip()[:MAX_SOURCE_LEN]

    unit = _normalize_unit(payload.get("unit") or TASK_DEFAULT_UNIT)
    if unit is None:
        return False, "invalid 'unit' (expected m/s^2 or g)"

    limits = (
        ("sample_rate_hz", TASK_DEFAULT_RATE_HZ, TASK_MIN_RATE_HZ, TASK_MAX_RATE_HZ),
        ("sample_count", TASK_DEFAULT_COUNT, 1, MAX_SAMPLES_PER_BATCH),
        ("timeout_s", TASK_DEFAULT_TIMEOUT_S, TASK_MIN_TIMEOUT_S, TASK_MAX_TIMEOUT_S),
    )
    parsed = {}
    for name, default, low, high in limits:
        raw = payload.get(name, default)
        if isinstance(raw, bool):
            return False, f"invalid '{name}'"
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return False, f"invalid '{name}'"
        if value < low or value > high:
            return False, (
                f"invalid '{name}' (sample_rate_hz {TASK_MIN_RATE_HZ}-{TASK_MAX_RATE_HZ},"
                f" sample_count 1-{MAX_SAMPLES_PER_BATCH},"
                f" timeout_s {TASK_MIN_TIMEOUT_S}-{TASK_MAX_TIMEOUT_S})"
            )
        parsed[name] = value

    # kind：capture（缺省，兼容旧客户端）/ pause / resume。
    # 控制任务的 sample_count / sample_rate_hz 对板端无意义（不采数据），
    # 但仍照常校验默认值，保证 tasks 表的 NOT NULL 字段有合法值。
    raw_kind = payload.get("kind")
    if raw_kind is None or (isinstance(raw_kind, str) and not raw_kind.strip()):
        task_kind = TASK_KIND_CAPTURE
    elif isinstance(raw_kind, str) and raw_kind.strip() in TASK_KINDS:
        task_kind = raw_kind.strip()
    else:
        return False, (
            f"invalid 'kind' (expect one of {'/'.join(TASK_KINDS)})"
        )

    # duration_s：仅 pause 有效（到点板端自愈恢复）；capture/resume 恒为 None。
    duration_s = None
    if task_kind == TASK_KIND_PAUSE:
        raw_dur = payload.get("duration_s", PAUSE_DEFAULT_S)
        if isinstance(raw_dur, bool):
            return False, "invalid 'duration_s'"
        try:
            duration_s = float(raw_dur)
        except (TypeError, ValueError):
            return False, "invalid 'duration_s'"
        if duration_s < PAUSE_MIN_S or duration_s > PAUSE_MAX_S:
            return False, (
                f"invalid 'duration_s' (expect {PAUSE_MIN_S}-{PAUSE_MAX_S} seconds)"
            )

    return True, {
        "device_id": device_id,
        "source": source,
        "unit": unit,
        "sample_rate_hz": parsed["sample_rate_hz"],
        "sample_count": parsed["sample_count"],
        "timeout_s": parsed["timeout_s"],
        "kind": task_kind,
        "duration_s": duration_s,
    }


# ---------------------------------------------------------------------------
# 业务
# ---------------------------------------------------------------------------
def store_upload(data, ip=None):
    """一个事务内写 uploads + samples；带 request_id 时联动 tasks 收尾。

    返回 (upload_id, idempotent)。idempotent=True 表示该 request_id 之前已入库，
    本次没有写任何行（板端重试 / 重复点击不会重复写样本）。
    """
    request_id = data.get("request_id")
    trigger = data.get("trigger") or ("manual" if request_id else "periodic")
    conn = _connect()
    try:
        # 显式 BEGIN IMMEDIATE：把「查重 → 插入」整体放进同一把写锁里。
        # 否则两个并发重试（板端重发 + Web 重复点击）可能同时通过上面的 SELECT，
        # 后一个 INSERT 撞上 uploads 的 request_id 部分唯一索引 → IntegrityError
        # → 500，而契约要求幂等返回 200/201。
        conn.execute("BEGIN IMMEDIATE")
        if request_id:
            row = conn.execute(
                "SELECT id FROM uploads WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                conn.rollback()      # 未写任何行，回滚只是保险
                return row["id"], True

        samples = data["samples"]
        payload_json = json.dumps(
            {
                "device_id": data["device_id"],
                "source": data["source"],
                "unit": data["unit"],
                "ts_ms": data["ts_ms"],
                "request_id": request_id,
                "trigger": trigger,
                "samples": samples,
            },
            ensure_ascii=False,
        )
        cur = conn.execute(
            "INSERT INTO uploads (device_id, source, unit, ts_ms, received_at,"
            " sample_count, ip, payload, request_id, trigger)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                data["device_id"],
                data["source"],
                data["unit"],
                data["ts_ms"],
                time.time(),
                len(samples),
                ip,
                payload_json,
                request_id,
                trigger,
            ),
        )
        upload_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO samples (upload_id, device_id, seq, t_ms, ax, ay, az)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (upload_id, data["device_id"], s["i"], s["t_ms"],
                 s["ax"], s["ay"], s["az"])
                for s in samples
            ],
        )
        if request_id:
            # 任务收尾与数据入库放在同一事务：要么都成功，要么都不写，
            # 不会出现「任务已完成但没有数据」或反之。
            conn.execute(
                "UPDATE tasks SET status='completed', completed_at=?,"
                " upload_id=?, error=NULL WHERE request_id=?",
                (time.time(), upload_id, request_id),
            )
        conn.commit()
        return upload_id, False
    except sqlite3.IntegrityError:
        # 兜底（BEGIN IMMEDIATE 已经把窗口堵住，这里是双保险）：唯一索引拒绝说明
        # 另一路并发请求刚写了同一个 request_id —— 按幂等契约回它已落库的 id，
        # 而不是把 500 丢给正在重试的板端。
        conn.rollback()
        if request_id:
            row = conn.execute(
                "SELECT id FROM uploads WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                return row["id"], True
        raise
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 实时拍照（JPEG）— 与 store_upload 同一哲学：图片落盘 + 元数据入库 + 任务收尾
# ---------------------------------------------------------------------------
def _safe_device_dir(device_id):
    """device_id → 文件系统安全的目录名（path traversal 防护）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", device_id or "")
    safe = safe.strip("._") or "unknown"
    return safe[:MAX_DEVICE_ID_LEN]


def _photo_rel_path(device_id, photo_id):
    """照片文件相对 PHOTO_DIR 的路径（存进 photos.path，换机器也能读）。"""
    return os.path.join(_safe_device_dir(device_id), f"{photo_id}.jpg")


def _photo_view(row):
    """photos 行 → 对外结构（附可读时间、KB 与取图 URL）。"""
    photo = dict(row)
    photo["received_at_str"] = _fmt_time(photo.get("received_at"))
    photo["ts_ms_str"] = _fmt_epoch_ms(photo["ts_ms"]) if photo.get("ts_ms") else None
    photo["size_kb"] = round((photo.get("bytes") or 0) / 1024.0, 1)
    photo["url"] = f"/api/v1/photos/{photo['id']}"
    return photo


def store_photo(data):
    """一个事务内写 photos 行 + 落盘 JPEG；带 request_id 时联动 camera 任务收尾。

    data 由 validate_photo_payload() 产出（含已读进内存的 jpeg 字节）。
    返回 (photo_id, idempotent)。幂等规则与 store_upload 一致：同一 request_id
    已入库时不再写第二行、第二个文件 —— 板端重试/重复点击不会产生重复照片。

    文件先写 xxx.jpg.part 再 os.replace() 原子改名：进程被杀/断电不会留下
    半张 JPEG 被当成完整照片。
    """
    request_id = data.get("request_id")
    conn = _connect()
    abs_path = None
    try:
        # 同 store_upload：把「查重 → 写行 → 落盘」放进同一把写锁，避免并发的
        # 同 request_id 上传各自通过 SELECT 后撞唯一索引（→ 500 + 可能多一张图）。
        conn.execute("BEGIN IMMEDIATE")
        if request_id:
            row = conn.execute(
                "SELECT id FROM photos WHERE request_id=? ORDER BY id LIMIT 1",
                (request_id,),
            ).fetchone()
            if row is not None:
                conn.rollback()      # 未写任何行，回滚只是保险
                return row["id"], True

        received_at = time.time()
        cur = conn.execute(
            "INSERT INTO photos (device_id, request_id, ts_ms, received_at, bytes,"
            " width, height, ip, path, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                data["device_id"],
                request_id,
                data["ts_ms"],
                received_at,
                len(data["jpeg"]),
                data["width"],
                data["height"],
                data.get("ip"),
                "",                      # 真实路径依赖自增 id，写完文件再回填
                data.get("note"),
            ),
        )
        photo_id = cur.lastrowid
        rel_path = _photo_rel_path(data["device_id"], photo_id)
        abs_path = os.path.join(PHOTO_DIR, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        tmp_path = abs_path + ".part"
        with open(tmp_path, "wb") as fh:
            fh.write(data["jpeg"])
        os.replace(tmp_path, abs_path)
        conn.execute("UPDATE photos SET path=? WHERE id=?", (rel_path, photo_id))

        if request_id:
            # 任务收尾与图片入库同一事务：要么都成功，要么都不写，
            # 不会出现「任务已完成但没有照片」或反之（与 store_upload 相同）。
            conn.execute(
                "UPDATE tasks SET status='completed', completed_at=?, error=NULL"
                " WHERE request_id=?",
                (received_at, request_id),
            )
        conn.commit()
        return photo_id, False
    except sqlite3.IntegrityError:
        # 并发同 request_id（见 store_upload 同名分支）：唯一索引拒绝时文件可能已经
        # 写进磁盘，先清掉临时/正式文件，再按幂等契约回已存在的那一行。
        conn.rollback()
        if abs_path:
            for path in (abs_path, abs_path + ".part"):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass          # 清理失败不应掩盖原始错误
        if request_id:
            row = conn.execute(
                "SELECT id FROM photos WHERE request_id=? ORDER BY id LIMIT 1",
                (request_id,),
            ).fetchone()
            if row is not None:
                return row["id"], True
        raise
    except (sqlite3.Error, OSError):
        conn.rollback()
        if abs_path:
            for path in (abs_path, abs_path + ".part"):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass          # 清理失败不应掩盖原始错误
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _fmt_time(epoch_s):
    """epoch 秒 → 本地时间串；异常值（None / 0 / 超出平台范围）不抛异常。

    与 _fmt_epoch_ms() 同口径：格式化属于展示层，不该因为库里出现一个极端时间戳就
    让整个接口 500（Windows 上 datetime.fromtimestamp 对越界值是抛 OverflowError，
    而 _fmt_time 会被 health / tasks / events / photos 等所有读路径调用）。
    """
    if not epoch_s:
        return None
    try:
        return datetime.fromtimestamp(epoch_s).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(epoch_s)


def _device_auth_ok(request):
    """设备侧接口的可选鉴权。

    Web 侧接口（创建/查询任务、/devices、/latest …）不做 Token 校验，与既有
    查询接口保持一致；只有板端调用的接口（upload / tasks/next / ack / fail）
    在 SENSOR_TOKEN 非空时才要求 Bearer Token。
    """
    if not SENSOR_TOKEN:
        return True
    got = request.headers.get("authorization", "")
    want = f"Bearer {SENSOR_TOKEN}"
    return hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8"))


def _unauthorized():
    return JSONResponse(
        status_code=401,
        content={"code": "UNAUTHORIZED", "ok": False,
                 "error": "missing or invalid bearer token"},
    )


def _too_large(label, limit):
    """统一的 413：消息里带上上限，便于板端/脚本一眼定位（原来四个接口各写一套）。"""
    return JSONResponse(
        status_code=413,
        content={"code": "TOO_LARGE", "ok": False,
                 "error": f"{label} too large (max {limit} bytes)"},
    )


async def _read_body_limited(request, limit, label="request body"):
    """读请求体并强制上限，返回 (raw, error_response)，二者恰有一个为 None。

    两道闸：
      ① 先看 Content-Length —— 能在**不把 body 读进内存**的前提下拒掉超大请求；
      ② 读完再量一次，兜住没有 Content-Length 的 chunked 请求。
    旧写法是「先把整个 body 读进内存，再比较长度」：body 有 1 GB 时内存先就没了，
    长度检查根本没机会执行 —— 大小上限等于形同虚设。
    """
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                return None, _too_large(label, limit)
        except (TypeError, ValueError):
            pass                      # 头不合法就当没给，靠第 ② 道闸兜住
    raw = await request.body()
    if len(raw) > limit:
        return None, _too_large(label, limit)
    return raw, None


async def _read_json_limited(request, limit=MAX_JSON_BODY_BYTES, label="request body"):
    """读 JSON 请求体并强制上限，返回 (payload, error_response)。

    要么给出 payload，要么给出 413/400 响应；调用方只判 error_response 是否为 None。
    """
    raw, too_large = await _read_body_limited(request, limit, label)
    if too_large is not None:
        return None, too_large
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "request body must be valid JSON"},
        )


def _expire_stale_tasks(conn, device_id=None):
    """惰性超时判定：把过了 expires_at 且未终态的任务标记为 timeout。

    不引入后台定时线程——所有创建/查询/领取路径都先调用它，因此服务重启后
    任务状态同样自洽，且过期任务绝不会被 /api/v1/tasks/next 下发。
    """
    now = time.time()
    sql = ("UPDATE tasks SET status='timeout', completed_at=?,"
           " error=COALESCE(error, 'expired before device upload')"
           " WHERE status NOT IN ('completed','failed','timeout') AND expires_at < ?")
    args = [now, now]
    if device_id:
        sql += " AND device_id = ?"
        args.append(device_id)
    return conn.execute(sql, args).rowcount


def _expire_stale_events(conn, device_id=None):
    """闭环事件的惰性过期判定：pending/ack 超过 expires_at 即视为「无人处理」。

    与 _expire_stale_tasks 同一哲学——不引入后台线程，任何读事件的路径先调用，
    因此服务重启后状态自洽，且板端掉电/断网的事件不会永远挂在「待处理」。
    """
    now = time.time()
    sql = ("UPDATE events SET status='expired', closed_at=?,"
           " note=COALESCE(note, 'no response before ttl')"
           " WHERE status IN ('pending','ack') AND expires_at < ?")
    args = [now, now]
    if device_id:
        sql += " AND device_id = ?"
        args.append(device_id)
    return conn.execute(sql, args).rowcount


def _event_view(row):
    """events 行 → 对外结构（附中文状态、剩余 TTL 与各时刻字符串）。"""
    ev = dict(row)
    ev["status_cn"] = EVENT_STATUS_CN.get(ev["status"],
                                          ev["status"])
    ev["terminal"] = ev["status"] in EVENT_TERMINAL_STATES
    ev["needs_attention"] = ev["status"] == EVENT_STATUS_PENDING
    for key in ("created_at", "responded_at", "closed_at", "expires_at"):
        ev[f"{key}_str"] = _fmt_time(ev.get(key))
    ev["ttl_left_s"] = round(ev["expires_at"] - time.time(), 1)
    return ev


def validate_event_trigger(payload):
    """校验板端触发请求，返回 (ok, data|error)。

    与 validate_payload 风格一致：字段缺失/类型错误都给出可读原因，
    便于板端只写串口日志也能定位。
    """
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return False, "missing or empty 'device_id'"
    device_id = device_id.strip()
    if len(device_id) > MAX_DEVICE_ID_LEN:
        return False, f"'device_id' too long (max {MAX_DEVICE_ID_LEN})"
    if not _device_id_ok(device_id):
        return False, ("invalid 'device_id' (only letters, digits, '.', '-' and '_'"
                       " are accepted)")

    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        return False, "missing or empty 'source'"
    source = source.strip()[:MAX_SOURCE_LEN]

    kind = payload.get("kind", EVENT_DEFAULT_KIND)
    if not isinstance(kind, str) or not kind.strip():
        kind = EVENT_DEFAULT_KIND
    kind = kind.strip()[:EVENT_MAX_KIND_LEN]

    # request_id 由**板端生成**（板端先本地亮灯、再上报，不能等服务端发号）：
    # 这样即使 HTTP 失败重试，同一次按键也只会产生一条事件（主键幂等）。
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id.strip()):
        return False, "invalid or missing 'request_id' (expect 6-64 chars of [A-Za-z0-9_-])"
    request_id = request_id.strip()

    device_ts_ms = payload.get("ts_ms")
    if device_ts_ms is not None:
        if not isinstance(device_ts_ms, int) and not (
            isinstance(device_ts_ms, float) and device_ts_ms.is_integer()
        ):
            return False, "'ts_ms' must be an integer epoch millisecond"
        device_ts_ms = int(device_ts_ms)

    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return False, "'note' must be a string"
    if isinstance(note, str):
        note = note.strip()[:EVENT_MAX_NOTE_LEN] or None

    return True, {
        "request_id": request_id,
        "device_id": device_id,
        "source": source,
        "kind": kind,
        "device_ts_ms": device_ts_ms,
        "note": note,
    }


def _task_view(row):
    """tasks 行 → 对外结构（附中文状态与剩余有效期，便于 Web 直接展示）。"""
    task = dict(row)
    task["status_cn"] = TASK_STATUS_CN.get(task["status"], task["status"])
    task["terminal"] = task["status"] in TASK_TERMINAL_STATES
    # 老库/老客户端没有 kind 列时按 capture 处理，保持向前兼容
    if not task.get("kind"):
        task["kind"] = TASK_KIND_CAPTURE
    task["kind_cn"] = TASK_KIND_CN.get(task["kind"], task["kind"])
    task["is_control"] = task["kind"] in TASK_CONTROL_KINDS
    for key in ("created_at", "dispatched_at", "acked_at", "completed_at", "expires_at"):
        task[f"{key}_str"] = _fmt_time(task.get(key))
    task["expires_in_s"] = round(task["expires_at"] - time.time(), 1)
    return task


def _lazy_reset_control(conn):
    """到点的暂停状态惰性归零（自动恢复），返回归零行数。

    与 _expire_stale_tasks 同一哲学：不引入后台线程，任何读控制状态的路径先调用，
    所以服务重启/长时间没人访问之后状态依然自洽。request_id 保留，便于追溯
    「上一次是哪条任务把它暂停/恢复的」。
    """
    now = time.time()
    return conn.execute(
        "UPDATE device_control SET periodic_paused=0, paused_until=NULL, updated_at=?"
        " WHERE periodic_paused=1 AND paused_until IS NOT NULL AND paused_until <= ?",
        (now, now),
    ).rowcount


def _control_view(conn, device_id, source):
    """device_control 行 → 对外结构（paused_until 到点则先惰性归零）。"""
    _lazy_reset_control(conn)
    row = conn.execute(
        "SELECT * FROM device_control WHERE device_id=? AND source=?",
        (device_id, source),
    ).fetchone()
    now = time.time()
    if row is None:
        return {
            "device_id": device_id,
            "source": source,
            "periodic_paused": False,
            "paused_until": None,
            "paused_until_str": None,
            "remaining_s": None,
            "request_id": None,
            "updated_at": None,
            "updated_at_str": None,
        }
    ctrl = dict(row)
    paused = bool(ctrl["periodic_paused"])
    remaining = None
    if paused and ctrl["paused_until"] is not None:
        remaining = round(max(0.0, ctrl["paused_until"] - now), 1)
    return {
        "device_id": ctrl["device_id"],
        "source": ctrl["source"],
        "periodic_paused": paused,
        "paused_until": ctrl["paused_until"],
        "paused_until_str": _fmt_time(ctrl["paused_until"]),
        "remaining_s": remaining,
        "request_id": ctrl["request_id"],
        "updated_at": ctrl["updated_at"],
        "updated_at_str": _fmt_time(ctrl["updated_at"]),
    }


def _paused_devices(conn):
    """当前处于暂停中的 device_id 集合（先惰性归零），供 /api/v1/devices 标注。"""
    _lazy_reset_control(conn)
    rows = conn.execute(
        "SELECT DISTINCT device_id FROM device_control WHERE periodic_paused=1"
    ).fetchall()
    return {r["device_id"] for r in rows}


def _check_task_match(request_id, data):
    """校验带 request_id 的上传是否属于该任务；不匹配则把任务置为 failed。

    返回 None 表示校验通过；否则返回应当发给板端的错误响应（4xx → 板端不重试）。
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return JSONResponse(
                status_code=404,
                content={"code": "TASK_NOT_FOUND", "ok": False,
                         "error": f"unknown request_id: {request_id}"},
            )
        task = dict(row)
        # 控制任务（pause/resume）不产生观测数据，因此永远不应该收到带它 request_id
        # 的上传。板端若真这么干了，属于板端 bug，直接拒绝且不动任务状态。
        if task.get("kind") in TASK_CONTROL_KINDS:
            conn.rollback()
            return JSONResponse(
                status_code=409,
                content={"code": "WRONG_KIND", "ok": False,
                         "error": (f"request_id {request_id} belongs to a "
                                   f"'{task['kind']}' control task"
                                   " which never carries samples"),
                         "request_id": request_id},
            )
        problems = []
        if task["device_id"] != data["device_id"]:
            problems.append(
                f"device_id mismatch (task={task['device_id']}, upload={data['device_id']})"
            )
        if task["source"] != data["source"]:
            problems.append(
                f"source mismatch (task={task['source']}, upload={data['source']})"
            )
        if task["unit"] != data["unit"]:
            problems.append(
                f"unit mismatch (task={task['unit']}, upload={data['unit']})"
            )
        if len(data["samples"]) < task["sample_count"]:
            problems.append(
                f"sample_count too small ({len(data['samples'])} < {task['sample_count']})"
            )
        # 新鲜度（防回放）只在板端时钟可信时才有意义，判据见 EPOCH_SANE_MS 注释：
        # SNTP 没成功的板子 ts_ms 只是开机毫秒数，与 created_at 相比恒为「陈旧」，
        # 那样会把「该现场根本没网对时」错报成「板端上传了旧数据」，任务被置 failed
        # 且板端收到 4xx 不再重试 —— 按需采集在现场彻底不可用。
        if data["ts_ms"] >= EPOCH_SANE_MS:
            if data["ts_ms"] < int(task["created_at"] * 1000) - 5000:
                problems.append("ts_ms is older than the task creation time")
        else:
            print(f"[sensor-server] request_id={request_id}: ts_ms={data['ts_ms']} 低于"
                  f" {EPOCH_SANE_MS}，判定板端时钟未同步 -> 跳过新鲜度检查")
        if problems:
            message = "; ".join(problems)
            conn.execute(
                "UPDATE tasks SET status='failed', completed_at=?, error=?"
                " WHERE request_id=? AND status NOT IN ('completed','failed','timeout')",
                (time.time(), message, request_id),
            )
            conn.commit()
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID", "ok": False, "error": message,
                         "request_id": request_id},
            )
        conn.commit()
        return None
    finally:
        conn.close()


def _check_photo_task_match(request_id, data):
    """校验带 request_id 的照片是否属于该 camera 任务；不匹配则把任务置 failed。

    返回 None 表示校验通过；否则返回应当发给板端的错误响应（4xx → 板端不重试）。
    与 _check_task_match 的区别：只认 kind='camera'（采集任务必须靠样本收尾，
    照片不能替它收尾，否则会出现「任务完成但一个样本都没有」的假完成），
    并且不校验样本数/单位，只看 device_id 归属。
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return JSONResponse(
                status_code=404,
                content={"code": "TASK_NOT_FOUND", "ok": False,
                         "error": f"unknown request_id: {request_id}"},
            )
        task = dict(row)
        kind = task.get("kind") or TASK_KIND_CAPTURE
        if kind != TASK_KIND_CAMERA:
            conn.rollback()
            return JSONResponse(
                status_code=409,
                content={"code": "WRONG_KIND", "ok": False,
                         "error": (f"request_id {request_id} belongs to a '{kind}'"
                                   " task, not a 'camera' task"),
                         "request_id": request_id},
            )
        problems = []
        if task["device_id"] != data["device_id"]:
            problems.append(
                f"device_id mismatch (task={task['device_id']},"
                f" photo={data['device_id']})"
            )
        if problems:
            message = "; ".join(problems)
            conn.execute(
                "UPDATE tasks SET status='failed', completed_at=?, error=?"
                " WHERE request_id=? AND status NOT IN ('completed','failed','timeout')",
                (time.time(), message, request_id),
            )
            conn.commit()
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID", "ok": False, "error": message,
                         "request_id": request_id},
            )
        conn.commit()
        return None
    finally:
        conn.close()


@app.post("/api/v1/upload")
async def upload(request: Request):
    """接收开发板上传的传感数据批次（periodic 周期上报 / manual 按需采集）。"""
    # 可选鉴权：仅当服务端设置了 SENSOR_TOKEN 时校验（恒定时比较，避免时序侧信道）
    if not _device_auth_ok(request):
        return _unauthorized()

    # 上限校验必须在读内存之前：旧顺序是「先整包读进来，再比长度」，
    # body 有 1 GB 时内存先就没了，长度检查根本执行不到（见 _read_body_limited）。
    raw, too_large = await _read_body_limited(request, MAX_BODY_BYTES)
    if too_large is not None:
        return too_large
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(
            status_code=400,
            content={"code": "BAD_JSON", "ok": False,
                     "error": "request body must be valid UTF-8 JSON"},
        )

    ip = request.client.host if request.client else None
    ok, result = validate_payload(payload)
    if not ok:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": result},
        )

    request_id = result.get("request_id")
    if request_id:
        # 带任务号的上传先校验归属；不匹配则把任务置 failed 并返回 4xx（板端不重试）
        mismatch = await run_in_threadpool(_check_task_match, request_id, result)
        if mismatch is not None:
            return mismatch

    # 板端时钟是否可信（见 EPOCH_SANE_MS 注释）：未同步时 ts_ms 只是开机毫秒数。
    # 回给调用方，Web 页面就能提示「板端未对时」，而不是让波形时间轴默默错到 1970。
    clock_synced = result["ts_ms"] >= EPOCH_SANE_MS

    try:
        # store_upload 是同步 sqlite 写（1 条 INSERT + N 条 executemany），直接在
        # async 处理器里调用会阻塞事件循环：板端上传与 Web 轮询会互相拖慢。
        # 丢到线程池，事件循环继续服务其他请求（sqlite 连接本身每请求独立，线程安全）。
        upload_id, idempotent = await run_in_threadpool(store_upload, result, ip)
    except sqlite3.Error as e:
        return JSONResponse(
            status_code=500,
            content={"code": "DB_ERROR", "ok": False,
                     "error": f"store failed: {e}"},
        )
    # 同一 request_id 重复上传 → 200 + idempotent=True（没有重复写样本）
    return JSONResponse(
        status_code=200 if idempotent else 201,
        content={
            "code": "OK",
            "ok": True,
            "upload_id": upload_id,
            "device_id": result["device_id"],
            "sample_count": len(result["samples"]),
            "unit": result["unit"],
            "ts_ms": result["ts_ms"],
            "request_id": request_id,
            "trigger": result["trigger"],
            "idempotent": idempotent,
            "clock_synced": clock_synced,
        },
    )


# ---------------------------------------------------------------------------
# 实时拍照路由（POST 上传 / 列表 / 取图 / 删除）
#
# 板端 = GET /api/v1/tasks/next 领到 kind=camera 任务后拍一帧 JPEG，
# 再 POST /api/v1/photos?device_id=…&request_id=… 上传；服务端在同一事务里
# 落盘 + 入库 + 把任务置 completed（幂等：同一 request_id 重复上传不重复存）。
# 注意：/api/v1/photos/{photo_id} 与 /api/v1/tasks/{request_id} 不同前缀，
# 无路由顺序问题。
# ---------------------------------------------------------------------------
@app.post("/api/v1/photos")
async def upload_photo(request: Request):
    """接收板端拍摄的一帧 JPEG（body 即图片字节，Content-Type: image/jpeg）。

    查询参数：device_id(必填) · request_id(camera 任务号, 可选) ·
    ts_ms(板端拍摄时刻 epoch ms) · w / h(尺寸) · note(备注)。
    """
    if not _device_auth_ok(request):
        return _unauthorized()

    # 同上：先用 Content-Length 挡掉超大 body 再读（见 _read_body_limited）
    raw, too_large = await _read_body_limited(request, MAX_PHOTO_BYTES, "photo")
    if too_large is not None:
        return too_large

    qp = request.query_params
    params = {
        "device_id": qp.get("device_id"),
        "request_id": qp.get("request_id"),
        "ts_ms": qp.get("ts_ms"),
        "width": qp.get("w"),
        "height": qp.get("h"),
        "note": qp.get("note"),
        "jpeg": raw,
        "ip": request.client.host if request.client else None,
    }
    ok, result = validate_photo_payload(params)
    if not ok:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": result},
        )

    request_id = result.get("request_id")
    if request_id:
        # 带任务号的照片先校验归属；不匹配则把任务置 failed 并返回 4xx（板端不重试）
        mismatch = await run_in_threadpool(_check_photo_task_match, request_id, result)
        if mismatch is not None:
            return mismatch

    try:
        # 同 /api/v1/upload：store_photo 内是同步的「写库 + 落盘 JPEG」，放到线程池
        # 执行，避免一次拍照上传把事件循环（以及直播取帧）卡住。
        photo_id, idempotent = await run_in_threadpool(store_photo, result)
    except (sqlite3.Error, OSError) as e:
        return JSONResponse(
            status_code=500,
            content={"code": "STORE_ERROR", "ok": False,
                     "error": f"store photo failed: {e}"},
        )

    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    finally:
        conn.close()
    return JSONResponse(
        status_code=200 if idempotent else 201,
        content={
            "code": "OK",
            "ok": True,
            "photo_id": photo_id,
            # 板端时钟是否可信（见 EPOCH_SANE_MS）：未同步时 ts_ms 只是开机毫秒数；
            # 没带 ts_ms（None）也按「不可信」处理，页面据此提示「板端未对时」。
            "clock_synced": bool(result["ts_ms"] and result["ts_ms"] >= EPOCH_SANE_MS),
            "device_id": result["device_id"],
            "request_id": request_id,
            # 顶层 bytes 取**库里那一行**的值，而不是本次请求体的长度：幂等重放时
            # 板端若重传了长度不同的字节，store_photo 会忽略新字节、只回老 photo_id，
            # 此时用 len(result["jpeg"]) 会让顶层 bytes / photo.bytes 互相矛盾。
            "bytes": row["bytes"] if row else len(result["jpeg"]),
            "idempotent": idempotent,
            "photo": _photo_view(row),
        },
    )


@app.get("/api/v1/photos")
def list_photos(request: Request):
    """照片列表（新的在前）。

    ?device_id=（可选，按设备过滤）· ?limit=（可选，默认 20，范围 1-200）
    返回 photos 元数据数组（含 url，Web 画廊直接用它做 <img src>）。
    """
    qp = request.query_params
    device_id = (qp.get("device_id") or "").strip()
    raw_limit = qp.get("limit")
    try:
        limit = int(raw_limit) if raw_limit not in (None, "") else PHOTO_DEFAULT_LIMIT
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": "invalid 'limit'"},
        )
    if limit < 1 or limit > PHOTO_MAX_LIMIT:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": f"invalid 'limit' (expect 1-{PHOTO_MAX_LIMIT})"},
        )

    conn = _connect()
    try:
        where, args = "", []
        if device_id:
            where, args = " WHERE device_id=?", [device_id]
        total = conn.execute(
            "SELECT COUNT(*) FROM photos" + where, args
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM photos" + where + " ORDER BY id DESC LIMIT ?",
            args + [limit],
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse(
        status_code=200,
        content={"code": "OK", "ok": True, "count": len(rows), "total": total,
                 "device_id": device_id or None,
                 "photos": [_photo_view(r) for r in rows]},
    )


@app.get("/api/v1/photos/{photo_id}")
def get_photo(photo_id: str):
    """取一张照片的 JPEG 字节（Web 画廊的 <img src> 直接指向本接口）。

    photo_id 非法/不存在 → 404；库里有行但文件被手工删掉 → 410
    （meta 仍在，便于发现磁盘异常，而不是伪装成 404 说照片不存在）。
    """
    if not PHOTO_ID_RE.match(photo_id or ""):
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "ok": False,
                     "error": f"unknown photo id: {photo_id}"},
        )
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM photos WHERE id=?", (int(photo_id),)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "ok": False,
                     "error": f"unknown photo id: {photo_id}"},
        )
    abs_path = os.path.join(PHOTO_DIR, row["path"])
    if not os.path.isfile(abs_path):
        return JSONResponse(
            status_code=410,
            content={"code": "FILE_MISSING", "ok": False,
                     "error": f"photo {photo_id} metadata exists but the JPEG file"
                              " is missing on disk",
                     "photo": _photo_view(row)},
        )
    # no-store：画廊里点删除后必须立刻看不到旧图（与 /ui 的轮询口径一致）
    return FileResponse(abs_path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.delete("/api/v1/photos/{photo_id}")
def delete_photo(photo_id: str):
    """删除一张照片：先删库行（提交事务）再删磁盘文件。

    文件已丢失不算失败（file_deleted=False），因为「库里没有这张照片了」
    才是用户看到的结果。重复删除同一个 id 返回 404：该照片确实已不存在。
    """
    if not PHOTO_ID_RE.match(photo_id or ""):
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "ok": False,
                     "error": f"unknown photo id: {photo_id}"},
        )
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM photos WHERE id=?", (int(photo_id),)
        ).fetchone()
        if row is None:
            conn.rollback()
            return JSONResponse(
                status_code=404,
                content={"code": "NOT_FOUND", "ok": False,
                         "error": f"unknown photo id: {photo_id} (already deleted?)"},
            )
        photo = _photo_view(row)
        conn.execute("DELETE FROM photos WHERE id=?", (int(photo_id),))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        return JSONResponse(
            status_code=500,
            content={"code": "DB_ERROR", "ok": False,
                     "error": f"delete photo failed: {e}"},
        )
    finally:
        conn.close()

    abs_path = os.path.join(PHOTO_DIR, row["path"])
    file_deleted = False
    warning = None
    try:
        if os.path.isfile(abs_path):
            os.remove(abs_path)
            file_deleted = True
    except OSError as e:
        # 库行已删，文件残留只是磁盘垃圾：不返回错误，但要告诉调用方
        warning = f"file not removed: {e}"
    return JSONResponse(
        status_code=200,
        content={"code": "OK", "ok": True, "deleted": True,
                 "photo_id": int(photo_id), "file_deleted": file_deleted,
                 "warning": warning, "photo": photo},
    )


# ---------------------------------------------------------------------------
# 摄像头实时直播路由（板端长按 Button A：内存态最新帧，不落盘 / 不入库 / 不建任务）
#
#   POST /api/v1/live       板端推一帧 JPEG（body 即图片字节，Content-Type: image/jpeg）
#   GET  /api/v1/live       推流状态（active / seq / age_ms / 分辨率 / 已知设备列表）
#   GET  /api/v1/live/frame 最新一帧 JPEG 字节（Web 的 <img src> 直接指向本接口）
#
# 与 /api/v1/photos 的区别：照片是「一次事件」，要入库、要幂等、要能删；
# 直播是「连续流」，只保留最新一帧 —— 丢帧无所谓（下一帧 250 ms 后就到），
# 因此这里刻意不写数据库（否则 1-2 fps × 多设备会瞬间把库和磁盘刷爆）。
# ---------------------------------------------------------------------------
def _live_entry_view(entry, now=None):
    """内存帧 → 对外状态结构（active 由帧龄判定：超过 LIVE_TIMEOUT_S 视为已停止）。"""
    if entry is None:
        return None
    now = time.time() if now is None else now
    age = max(0.0, now - float(entry["received_at"]))
    return {
        "active": age <= LIVE_TIMEOUT_S,
        "seq": entry["seq"],
        "age_ms": int(round(age * 1000)),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "bytes": len(entry.get("jpeg") or b""),
        "ts_ms": entry.get("ts_ms"),
        "ts_ms_str": _fmt_epoch_ms(entry["ts_ms"]) if entry.get("ts_ms") else None,
        "received_at": entry["received_at"],
        "received_at_str": _fmt_time(entry["received_at"]),
        "ip": entry.get("ip"),
    }


def _live_int_param(qp, name):
    """可选整数查询参数：缺失/空 → None；非法 → 'INVALID'（调用方返回 400）。"""
    value = qp.get(name)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return "INVALID"


def _live_device_id_error(device_id):
    """直播接口的 device_id 校验（与 upload/photos/tasks/events 同一套白名单）。

    为什么直播也要卡：device_id 会被原样回吐给 Web（GET /api/v1/live 的 devices[]）。
    前端目前一律走 textContent，所以这不构成 XSS；但「只剩一层前端防线」不该是设计 ——
    服务端卡一次才是纵深防御，顺带避免同一台板子因拼写差异在内存态里被记成两台。
    """
    if len(device_id) > MAX_DEVICE_ID_LEN:
        return f"'device_id' too long (max {MAX_DEVICE_ID_LEN})"
    if not _device_id_ok(device_id):
        return ("invalid 'device_id' (only letters, digits, '.', '-' and '_'"
                " are accepted)")
    return None


def _live_prune_locked(now):
    """丢掉超龄设备（长时间没推流 ⇒ 连最后一帧也释放）。调用方必须已持有 _live_lock。"""
    for dev in [d for d, e in _live_frames.items()
                if now - float(e["received_at"]) > LIVE_STALE_EVICT_S]:
        _live_frames.pop(dev, None)
        print(f"[sensor-server] live: {dev!r} 已 {LIVE_STALE_EVICT_S}s 无新帧，释放其帧")


def _live_make_room_locked(device_id):
    """给新设备腾位置：仅当「设备是新的」且已达上限时，淘汰最久未推流的那台。

    只在写路径调用，读路径刻意不淘汰 —— 板端停推流后页面还能显示最后一帧
    （`active=false` 的「已停止（保留最后一帧）」），这是有意的 UX，见 LIVE_STALE_EVICT_S。
    """
    while device_id not in _live_frames and len(_live_frames) >= LIVE_MAX_DEVICES:
        oldest = min(_live_frames, key=lambda d: _live_frames[d]["received_at"])
        _live_frames.pop(oldest, None)
        print(f"[sensor-server] live: 设备数达上限 {LIVE_MAX_DEVICES}，淘汰最久未推流的"
              f" {oldest!r}（内存上界 ≈ {LIVE_MAX_DEVICES * LIVE_MAX_FRAME_BYTES // 1024} KiB）")


@app.post("/api/v1/live")
async def upload_live_frame(request: Request):
    """接收板端直播推流的一帧 JPEG，覆盖该设备在内存里的上一帧。

    查询参数：device_id(必填) · ts_ms(板端拍摄时刻 epoch ms, 可选) ·
    w / h(帧尺寸, 可选)。返回 201 + 全局单调 seq（Web 侧靠它判断「有新帧了」）。
    不落盘、不写库：直播状态只活在服务端进程的内存里。
    """
    if not _device_auth_ok(request):
        return _unauthorized()

    # 同上：先用 Content-Length 挡掉超大 body 再读（见 _read_body_limited）
    raw, too_large = await _read_body_limited(request, LIVE_MAX_FRAME_BYTES, "live frame")
    if too_large is not None:
        return too_large

    qp = request.query_params
    device_id = (qp.get("device_id") or "").strip()
    if not device_id:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "missing or empty 'device_id'"},
        )
    dev_err = _live_device_id_error(device_id)
    if dev_err is not None:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": dev_err},
        )
    if not raw:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "empty body (expect raw JPEG bytes)"},
        )
    if not raw.startswith(LIVE_JPEG_MAGIC):
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "body is not a JPEG (expect SOI marker FF D8 FF)"},
        )

    ts_ms = _live_int_param(qp, "ts_ms")
    width = _live_int_param(qp, "w")
    height = _live_int_param(qp, "h")
    if ts_ms == "INVALID" or width == "INVALID" or height == "INVALID":
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "ts_ms / w / h must be integers when present"},
        )
    # 范围与 /api/v1/photos 对齐（ts_ms>=0、宽高 1..8192）：这些值会原样进内存态、
    # 原样回吐给页面，负数/天文数字最终会显示成「1970-01-01」之类的东西。
    if ts_ms is not None and ts_ms < 0:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "invalid 'ts_ms' (expect a non-negative epoch ms)"},
        )
    if width is not None and not 1 <= width <= 8192:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "invalid 'w' (expect 1-8192)"},
        )
    if height is not None and not 1 <= height <= 8192:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "invalid 'h' (expect 1-8192)"},
        )

    global _live_seq
    with _live_lock:
        # 写路径是唯一腾内存的地方（读路径刻意不淘汰，见 _live_make_room_locked）：
        # 先释放超龄设备，再在设备数达上限时给新设备挤掉最久未推流的那台。
        _live_prune_locked(time.time())
        _live_make_room_locked(device_id)
        _live_seq += 1
        _live_frames[device_id] = {
            "jpeg": raw,
            "width": width,
            "height": height,
            "ts_ms": int(ts_ms) if ts_ms else int(time.time() * 1000),
            "received_at": time.time(),
            "seq": _live_seq,
            "ip": request.client.host if request.client else None,
        }
        seq = _live_seq
        entry = dict(_live_frames[device_id])

    return JSONResponse(
        status_code=201,
        content={
            "code": "OK",
            "ok": True,
            "device_id": device_id,
            "seq": seq,
            "bytes": len(raw),
            "received_at": entry["received_at"],
            "received_at_str": _fmt_time(entry["received_at"]),
        },
    )


@app.get("/api/v1/live")
def live_status(request: Request):
    """直播状态查询（Web 每轮轮询一次，决定是否刷新 <img> 与显示什么徽章）。

    ?device_id=（可选）：指定设备时，顶层字段即该设备状态（从未推流 → active=false，
    仍返回 200，页面据此显示「未推流」而不是报接口错误）。
    无论是否指定设备，devices 总列出已知设备（新帧在前），便于页面/脚本排查多设备。
    """
    qp = request.query_params
    device_id = (qp.get("device_id") or "").strip()
    if device_id:
        # 指定设备时同样走白名单（与写路径一致）：非法 device_id 不可能存在于内存态，
        # 直接 400 比回一个「从未推流」的 200 更准确。
        dev_err = _live_device_id_error(device_id)
        if dev_err is not None:
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID", "ok": False, "error": dev_err},
            )

    now = time.time()
    with _live_lock:
        entries = {dev: dict(e) for dev, e in _live_frames.items()}

    view = _live_entry_view(entries.get(device_id), now) if device_id else None
    devices = []
    for dev, entry in sorted(entries.items(),
                             key=lambda kv: kv[1]["received_at"], reverse=True):
        devices.append({"device_id": dev, **_live_entry_view(entry, now)})

    return JSONResponse(
        status_code=200,
        content={
            "code": "OK",
            "ok": True,
            "device_id": device_id or None,
            "timeout_s": LIVE_TIMEOUT_S,
            "nominal_fps": LIVE_DEFAULT_FPS,
            "active": bool(view and view["active"]),
            "seq": view["seq"] if view else None,
            "age_ms": view["age_ms"] if view else None,
            "width": view["width"] if view else None,
            "height": view["height"] if view else None,
            "bytes": view["bytes"] if view else None,
            "ts_ms": view["ts_ms"] if view else None,
            "ts_ms_str": view["ts_ms_str"] if view else None,
            "received_at": view["received_at"] if view else None,
            "received_at_str": view["received_at_str"] if view else None,
            "ip": view["ip"] if view else None,
            "devices": devices,
        },
    )


@app.get("/api/v1/live/frame")
def live_frame(request: Request):
    """最新一帧 JPEG 字节（device_id 缺失/从未推流 → 400 / 404）。

    no-store：画面每帧都要重新取，浏览器缓存会让「实时画面」冻住。
    Web 侧额外带 &seq=（帧序号）做缓存兜底，服务端忽略该参数。
    """
    device_id = (request.query_params.get("device_id") or "").strip()
    if not device_id:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "missing or empty 'device_id'"},
        )
    dev_err = _live_device_id_error(device_id)
    if dev_err is not None:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": dev_err},
        )

    with _live_lock:
        entry = _live_frames.get(device_id)
        jpeg = entry["jpeg"] if entry else None
    if not jpeg:
        return JSONResponse(
            status_code=404,
            content={"code": "NO_FRAME", "ok": False,
                     "error": f"no live frame received from '{device_id}' yet"},
        )
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# 按需采集任务路由（manual capture task）
#
# 注意：/api/v1/tasks/next 必须声明在 /api/v1/tasks/{request_id} 之前，
# 否则 "next" 会被当成 request_id 匹配掉（FastAPI 按声明顺序匹配路径）。
# ---------------------------------------------------------------------------
@app.post("/api/v1/tasks")
async def create_task(request: Request):
    """Web 下发任务：按需采集（capture）/ 暂停周期（pause）/ 恢复周期（resume）。

    去重：同 device + source + kind 若已有未终态任务，则不再新建，直接返回已有任务
    （duplicate=True），避免连点产生多个并行任务。去重键必须带 kind：否则一条待执行
    的 pause 会把后续的 capture 一直挡住。

    互斥：pause 与 resume 方向相反，两条同时排队会让板端按 created_at 顺序来回抖动，
    因此新建控制任务时把反方向那条未终态控制任务置 failed（superseded by newer
    control task）。
    """
    # 上限 + 解析合成一步（见 _read_json_limited）：Web 侧创建任务也没有过 body 上限。
    payload, bad = await _read_json_limited(request)
    if bad is not None:
        return bad

    ok, result = validate_task_request(payload)
    if not ok:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": result},
        )

    kind = result["kind"]
    is_control = kind in TASK_CONTROL_KINDS

    conn = _connect()
    try:
        # 先清超时，避免一个已过期任务把新任务挡在去重逻辑外
        _expire_stale_tasks(conn, result["device_id"])
        existing = conn.execute(
            "SELECT * FROM tasks WHERE device_id=? AND source=? AND kind=?"
            " AND status NOT IN ('completed','failed','timeout')"
            " ORDER BY created_at ASC LIMIT 1",
            (result["device_id"], result["source"], kind),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return JSONResponse(
                status_code=200,
                content={"code": "DUPLICATE", "ok": True, "duplicate": True,
                         "task": _task_view(existing)},
            )

        now = time.time()
        superseded = 0
        # 互斥只对 pause/resume 这对「方向相反」的控制任务生效；preview 是独立
        # 开关，不取代任何其它控制任务，也不该被它们取代。
        if kind in (TASK_KIND_PAUSE, TASK_KIND_RESUME):
            other = (TASK_KIND_RESUME if kind == TASK_KIND_PAUSE
                     else TASK_KIND_PAUSE)
            cur = conn.execute(
                "UPDATE tasks SET status='failed', completed_at=?,"
                " error='superseded by newer control task'"
                " WHERE device_id=? AND source=? AND kind=?"
                " AND status NOT IN ('completed','failed','timeout')",
                (now, result["device_id"], result["source"], other),
            )
            superseded = cur.rowcount

        request_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO tasks (request_id, device_id, source, unit, sample_rate_hz,"
            " sample_count, trigger, kind, duration_s, status, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                result["device_id"],
                result["source"],
                result["unit"],
                result["sample_rate_hz"],
                result["sample_count"],
                # trigger 供 uploads 侧统一口径：控制任务不产生批次，标记为 control
                "manual" if not is_control else "control",
                kind,
                result["duration_s"],
                "submitted",
                now,
                now + result["timeout_s"],
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
    except sqlite3.Error as e:
        conn.rollback()
        return JSONResponse(
            status_code=500,
            content={"code": "DB_ERROR", "ok": False,
                     "error": f"create task failed: {e}"},
        )
    finally:
        conn.close()

    return JSONResponse(
        status_code=201,
        content={"code": "OK", "ok": True, "duplicate": False,
                 "superseded": superseded,
                 "task": _task_view(row)},
    )


@app.get("/api/v1/tasks/next")
def next_task(request: Request):
    """板端轮询待执行任务：只返回本设备「submitted 且未过期」的最早一条。

    领取是原子的（UPDATE … WHERE status='submitted'），多板并发时也只会有一条
    成功进入 dispatched；过期任务在此既不触达也不会被下发。
    """
    if not _device_auth_ok(request):
        return _unauthorized()

    device_id = (request.query_params.get("device_id") or "").strip()
    if not device_id:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": "missing 'device_id'"},
        )

    conn = _connect()
    try:
        _expire_stale_tasks(conn, device_id)
        row = conn.execute(
            "SELECT request_id FROM tasks WHERE device_id=? AND status='submitted'"
            " AND expires_at >= ? ORDER BY created_at ASC LIMIT 1",
            (device_id, time.time()),
        ).fetchone()
        if row is None:
            conn.commit()
            return JSONResponse(status_code=200,
                                content={"ok": True, "found": False})
        cur = conn.execute(
            "UPDATE tasks SET status='dispatched', dispatched_at=?"
            " WHERE request_id=? AND status='submitted'",
            (time.time(), row["request_id"]),
        )
        conn.commit()
        if cur.rowcount != 1:
            # 已被其它请求领走
            return JSONResponse(status_code=200,
                                content={"ok": True, "found": False})
        task = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (row["request_id"],)
        ).fetchone()
    finally:
        conn.close()

    return JSONResponse(
        status_code=200,
        content={"ok": True, "found": True, "task": _task_view(task)},
    )


@app.get("/api/v1/tasks")
def list_tasks(request: Request):
    """任务列表（默认最近 20 条，可按 device_id 过滤），供 Web 追踪任务。"""
    device_id = (request.query_params.get("device_id") or "").strip()
    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(100, limit))

    conn = _connect()
    try:
        _expire_stale_tasks(conn, device_id or None)
        sql = "SELECT * FROM tasks"
        args = []
        if device_id:
            sql += " WHERE device_id=?"
            args.append(device_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
    finally:
        conn.close()

    return JSONResponse(
        status_code=200,
        content={"ok": True, "device_id": device_id or None,
                 "count": len(rows), "tasks": [_task_view(r) for r in rows]},
    )


@app.get("/api/v1/tasks/{request_id}")
def get_task(request_id: str):
    """按 request_id 查询任务状态（含关联上传摘要），Web 轮询任务进度用。"""
    conn = _connect()
    try:
        _expire_stale_tasks(conn)
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        upload = None
        if row is not None and row["upload_id"]:
            up = conn.execute(
                "SELECT id, device_id, request_id, trigger, sample_count, ts_ms,"
                " received_at FROM uploads WHERE id=?",
                (row["upload_id"],),
            ).fetchone()
            if up is not None:
                upload = dict(up)
                upload["received_at_str"] = _fmt_time(up["received_at"])
        # camera 任务没有 upload，但有照片：把最近一张带上，页面就能直接显示缩略图
        photo = None
        if row is not None:
            ph = conn.execute(
                "SELECT id, ts_ms, received_at, bytes, width, height, note"
                " FROM photos WHERE request_id=? ORDER BY id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            if ph is not None:
                photo = _photo_view(ph)
        conn.commit()
    finally:
        conn.close()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "ok": False,
                     "error": f"unknown request_id: {request_id}"},
        )
    return JSONResponse(status_code=200,
                        content={"ok": True, "task": _task_view(row),
                                 "upload": upload, "photo": photo})


@app.post("/api/v1/tasks/{request_id}/ack")
def ack_task(request_id: str, request: Request):
    """板端回执「已收到任务」（可选步骤；不回执也能靠上传完成）。"""
    if not _device_auth_ok(request):
        return _unauthorized()

    conn = _connect()
    try:
        _expire_stale_tasks(conn)
        cur = conn.execute(
            "UPDATE tasks SET status='acked', acked_at=?"
            " WHERE request_id=? AND status IN ('submitted','dispatched')",
            (time.time(), request_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "ok": False,
                     "error": f"unknown request_id: {request_id}"},
        )
    if cur.rowcount != 1:
        return JSONResponse(
            status_code=409,
            content={"code": "CONFLICT", "ok": False,
                     "error": f"task is already {row['status']}",
                     "task": _task_view(row)},
        )
    return JSONResponse(status_code=200,
                        content={"ok": True, "updated": True,
                                 "task": _task_view(row)})


@app.post("/api/v1/tasks/{request_id}/fail")
async def fail_task(request_id: str, request: Request):
    """板端上报任务失败（达不到采集节拍 / 上传交接失败 / 任务已过期等）。"""
    if not _device_auth_ok(request):
        return _unauthorized()

    # 先卡上限再解析：body 非法（或不是对象）沿用原来的「空 reason」语义，
    # 但超大 body 直接 413 —— 这个接口过去完全没有上限（见 _read_body_limited）。
    body_raw, too_large = await _read_body_limited(request, MAX_JSON_BODY_BYTES)
    if too_large is not None:
        return too_large
    reason = ""
    try:
        body = json.loads(body_raw.decode("utf-8"))
        if isinstance(body, dict):
            reason = str(body.get("error") or body.get("reason") or "").strip()
    except Exception:
        reason = ""
    if not reason:
        reason = "device reported failure"
    reason = reason[:256]

    conn = _connect()
    try:
        _expire_stale_tasks(conn)
        cur = conn.execute(
            "UPDATE tasks SET status='failed', completed_at=?, error=?"
            " WHERE request_id=? AND status NOT IN ('completed','failed','timeout')",
            (time.time(), reason, request_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "ok": False,
                     "error": f"unknown request_id: {request_id}"},
        )
    if cur.rowcount != 1 and row["status"] not in TASK_TERMINAL_STATES:
        return JSONResponse(
            status_code=409,
            content={"code": "CONFLICT", "ok": False,
                     "error": f"task is already {row['status']}",
                     "task": _task_view(row)},
        )
    return JSONResponse(status_code=200,
                        content={"ok": True, "updated": bool(cur.rowcount),
                                 "task": _task_view(row)})


@app.post("/api/v1/tasks/{request_id}/applied")
async def applied_task(request_id: str, request: Request):
    """板端确认「控制任务已生效」（pause / resume / preview）。

    控制任务不产生观测数据，所以无法像采集任务那样靠带 request_id 的上传收尾，
    改由板端在真的切换了周期上报开关之后调用本接口。服务端在同一事务里：
      1) 把任务置 completed（终态）；
      2) 写 device_control —— 这是「当前是否真的暂停」的真相源，页面直接查它，
         而不是靠「periodic 批次数为 0」这种间接信号反推。

    可选 body: {"paused_until_ms": <epoch ms>}：板端带上自己定时器的终点，服务端
    在校验合理后优先采用，这样页面倒计时与板端自愈恢复时刻一致；不合理则回退到
    now + duration_s。

    采集任务（kind=capture）调本接口一律 409 WRONG_KIND：它必须靠上传收尾，
    否则会出现「任务 completed 但一个样本都没有」的假完成。
    """
    if not _device_auth_ok(request):
        return _unauthorized()

    # 先卡上限再解析（见 _read_body_limited）：解析失败沿用原来的「无可选 body」语义。
    body_raw, too_large = await _read_body_limited(request, MAX_JSON_BODY_BYTES)
    if too_large is not None:
        return too_large
    reported_until_ms = None
    try:
        body = json.loads(body_raw.decode("utf-8"))
        if isinstance(body, dict):
            raw = body.get("paused_until_ms")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                reported_until_ms = float(raw)
    except Exception:
        reported_until_ms = None

    now = time.time()
    conn = _connect()
    try:
        _expire_stale_tasks(conn)
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return JSONResponse(
                status_code=404,
                content={"code": "NOT_FOUND", "ok": False,
                         "error": f"unknown request_id: {request_id}"},
            )

        task = dict(row)
        kind = task.get("kind") or TASK_KIND_CAPTURE
        if kind not in TASK_CONTROL_KINDS:
            conn.rollback()
            return JSONResponse(
                status_code=409,
                content={"code": "WRONG_KIND", "ok": False,
                         "error": (f"task {request_id} is kind='{kind}':采集任务由带"
                                   " request_id 的上传收尾（POST /api/v1/upload）"),
                         "task": _task_view(row)},
            )

        cur = conn.execute(
            "UPDATE tasks SET status='completed', completed_at=?"
            " WHERE request_id=? AND status NOT IN ('completed','failed','timeout')",
            (now, request_id),
        )
        updated = cur.rowcount == 1

        prev = conn.execute(
            "SELECT * FROM device_control WHERE device_id=? AND source=?",
            (task["device_id"], task["source"]),
        ).fetchone()
        # 幂等重放保护：板端 POST 成功但响应丢失时会重试一次同一 request_id。
        # 任务已是 completed 且控制行就是这条任务写的 → 不再重写，否则每次重试都会
        # 把 paused_until 往后推，页面倒计时会比板端真实的自动恢复时刻更晚。
        replay = (not updated
                  and prev is not None
                  and prev["request_id"] == request_id)

        # 迟到的 applied 仍然要写控制状态：例如任务在板端已生效后，回执因网络
        # 重试而在 expires_at 之后才到（任务已被判 timeout）。此时板端确实暂停了，
        # 若不记录，页面就会显示「上报中」而实际静默 120 s —— 与真相源矛盾。
        # 但比它更新的控制任务已存在时不再覆盖（避免旧回执翻掉新决定）。
        newer = conn.execute(
            "SELECT request_id FROM tasks WHERE device_id=? AND source=? AND kind IN"
            " ('pause','resume') AND created_at > ? LIMIT 1",
            (task["device_id"], task["source"], task["created_at"]),
        ).fetchone()
        stale = newer is not None

        # preview 只切换 LCD 预览开关，不写 periodic_paused（也不该翻掉暂停状态）
        if kind in (TASK_KIND_PAUSE, TASK_KIND_RESUME) and not stale and not replay:
            if kind == TASK_KIND_PAUSE:
                duration = float(task["duration_s"] or PAUSE_DEFAULT_S)
                until = now + duration
                if reported_until_ms is not None:
                    cand = reported_until_ms / 1000.0
                    # 只接受「不早于现在 5 s、不晚于服务端预期 + 60 s」的报告值
                    if (now - 5.0) <= cand <= (until + 60.0):
                        until = cand
                new_paused, new_until = 1, until
            else:
                new_paused, new_until = 0, None

            cur2 = conn.execute(
                "UPDATE device_control SET periodic_paused=?, paused_until=?,"
                " request_id=?, updated_at=? WHERE device_id=? AND source=?",
                (new_paused, new_until, request_id, now,
                 task["device_id"], task["source"]),
            )
            if cur2.rowcount == 0:
                conn.execute(
                    "INSERT INTO device_control (device_id, source, periodic_paused,"
                    " paused_until, request_id, updated_at) VALUES (?,?,?,?,?,?)",
                    (task["device_id"], task["source"], new_paused, new_until,
                     request_id, now),
                )
        # commit 必须放在 _control_view() 之后：它内部的惰性归零（device_control
        # 里到点的行置回未暂停）也是一次 UPDATE，若在它之前提交，这次归零会随
        # conn.close() 一起被回滚，等于白做。
        control = _control_view(conn, task["device_id"], task["source"])
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        conn.commit()
    finally:
        conn.close()

    return JSONResponse(
        status_code=200,
        content={"code": "OK", "ok": True, "applied": True, "updated": updated,
                 "stale": stale, "replay": replay, "kind": kind,
                 "control": control, "task": _task_view(row)},
    )


@app.get("/api/v1/control")
def get_control(request: Request):
    """查询板端周期上报的暂停状态（Web「周期上报控制」卡片的真相源）。

    ?device_id=（必填）·&source=（可选，默认 qma6100p）

    paused_until 到点即自动恢复：本接口先做惰性归零再返回，因此不需要后台线程，
    也不怕服务重启。`remaining_s` 为剩余秒数（停止中才非空）。
    """
    device_id = (request.query_params.get("device_id") or "").strip()
    if not device_id:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "missing 'device_id'"},
        )
    if len(device_id) > MAX_DEVICE_ID_LEN:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": f"'device_id' too long (max {MAX_DEVICE_ID_LEN})"},
        )
    source = (request.query_params.get("source") or TASK_DEFAULT_SOURCE)
    source = source.strip()[:MAX_SOURCE_LEN] or TASK_DEFAULT_SOURCE

    conn = _connect()
    try:
        control = _control_view(conn, device_id, source)
        conn.commit()          # 惰性归零可能改了行，提交以免下次读到旧值
    finally:
        conn.close()

    return JSONResponse(
        status_code=200,
        content={"code": "OK", "ok": True, "control": control},
    )


@app.post("/api/v1/events/trigger")
async def trigger_event(request: Request):
    """板端按键「触发」上报 —— 闭环的第一跳。

    方向与 tasks 相反：**板端发起、Web 接收**。板端在发这条请求之前就已经把
    LED 闪了、LCD 也改了（本地反馈不等网络），所以这里只负责「把事件登记到
    服务端」，让远端页面立刻显示出来。

    request_id 由板端生成并作为主键：同一次按键重试只会命中一条事件
    （返回 200 + idempotent=true），不会在页面上刷出两条。
    """
    if not _device_auth_ok(request):
        return _unauthorized()

    # 上限 + 解析合成一步（见 _read_json_limited）：旧写法直接 await request.json()，
    # 这个接口没有任何 body 上限。
    payload, bad = await _read_json_limited(request)
    if bad is not None:
        return bad

    ok, result = validate_event_trigger(payload)
    if not ok:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": result},
        )

    ip = request.client.host if request.client else None
    now = time.time()
    conn = _connect()
    try:
        _expire_stale_events(conn, result["device_id"])
        existing = conn.execute(
            "SELECT * FROM events WHERE request_id=?", (result["request_id"],)
        ).fetchone()
        if existing is not None:
            conn.commit()
            # 板端重试（同一 request_id 再发一次）：不新建、不改状态，
            # 直接把当前状态回给板端，让它自行对齐。
            return JSONResponse(
                status_code=200,
                content={"code": "DUPLICATE", "ok": True, "idempotent": True,
                         "event": _event_view(existing)},
            )

        conn.execute(
            "INSERT INTO events (request_id, device_id, source, kind, status,"
            " created_at, device_ts_ms, expires_at, ip, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                result["request_id"],
                result["device_id"],
                result["source"],
                result["kind"],
                EVENT_STATUS_PENDING,
                now,
                result["device_ts_ms"],
                now + EVENT_DEFAULT_TTL_S,
                ip,
                result["note"],
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM events WHERE request_id=?", (result["request_id"],)
        ).fetchone()
    except sqlite3.Error as e:
        conn.rollback()
        return JSONResponse(
            status_code=500,
            content={"code": "DB_ERROR", "ok": False,
                     "error": f"create event failed: {e}"},
        )
    finally:
        conn.close()

    return JSONResponse(
        status_code=201,
        content={"code": "OK", "ok": True, "idempotent": False,
                 "event": _event_view(row)},
    )


@app.post("/api/v1/events/respond")
async def respond_event(request: Request):
    """回应 / 取消 / 确认完成 —— 闭环的收口，板端与 Web 共用这一条接口。

    body: {"request_id": "…", "action": "accept|cancel|confirm", "by": "device|web"}

      accept  → ack       （板端单击「回应键」，或页面点「回应」）
      cancel  → cancelled （板端长按「回应键」，或页面点「取消」）
      confirm → completed （页面点「确认完成」；板端也能发，语义是「我这边结束了」）

    转移规则刻意收紧：终态不可再变（重复发同一动作幂等返回 200，
    发冲突动作返回 409），避免板端连点把状态来回翻。
    """
    # 上限 + 解析合成一步（见 _read_json_limited）：这个接口过去也没有 body 上限。
    payload, bad = await _read_json_limited(request)
    if bad is not None:
        return bad

    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "missing or empty 'request_id'"},
        )
    request_id = request_id.strip()

    action = payload.get("action")
    if not isinstance(action, str) or action.strip().lower() not in EVENT_ACTION_TO_STATUS:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "'action' must be one of accept / cancel / confirm"},
        )
    action = action.strip().lower()
    target = EVENT_ACTION_TO_STATUS[action]

    by = payload.get("by")
    if not isinstance(by, str) or by.strip().lower() not in ("device", "web"):
        # 默认当成 Web 侧（板端会显式带 by=device）
        by = "web"
    else:
        by = by.strip().lower()

    note = payload.get("note")
    note = note.strip()[:EVENT_MAX_NOTE_LEN] if isinstance(note, str) else None

    conn = _connect()
    try:
        _expire_stale_events(conn)
        row = conn.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return JSONResponse(
                status_code=404,
                content={"code": "NOT_FOUND", "ok": False,
                         "error": f"unknown request_id '{request_id}'"},
            )

        current = row["status"]

        # 幂等：重复发同一个动作直接返回当前状态，不改任何时间戳。
        if current == target:
            conn.commit()
            return JSONResponse(
                status_code=200,
                content={"code": "DUPLICATE", "ok": True, "idempotent": True,
                         "event": _event_view(row)},
            )

        # 终态不可回退（expired 也不允许被改写）。
        if current in EVENT_TERMINAL_STATES:
            conn.commit()
            return JSONResponse(
                status_code=409,
                content={"code": "CONFLICT", "ok": False,
                         "error": f"event is already '{current}' and cannot become '{target}'",
                         "event": _event_view(row)},
            )

        now = time.time()
        terminal = target in EVENT_TERMINAL_STATES
        conn.execute(
            "UPDATE events SET status=?, responded_at=?, response=?, responded_by=?,"
            " closed_at=?, note=COALESCE(?, note) WHERE request_id=?",
            (target, now, action, by,
             now if terminal else None,
             note, request_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
    except sqlite3.Error as e:
        conn.rollback()
        return JSONResponse(
            status_code=500,
            content={"code": "DB_ERROR", "ok": False,
                     "error": f"respond to event failed: {e}"},
        )
    finally:
        conn.close()

    return JSONResponse(
        status_code=200,
        content={"code": "OK", "ok": True, "idempotent": False,
                 "event": _event_view(row)},
    )


@app.get("/api/v1/events/status")
def event_status(request: Request):
    """板端轮询闭环事件的最新状态 —— 闭环的最后一跳。

    两种用法：
      ?device_id=…&request_id=…  查指定事件（板端持号轮询，最常用）
      ?device_id=…               查该设备最近一条事件（便于联调时确认板端与
                                 服务端看到的是同一条）

    为了让板端只做最简解析，响应里除了完整 event 还额外给一个顶层 `status`。
    未知 request_id 返回 200 + found=false（而不是 404）：板端每 1 s 轮询一次，
    这类「还没登记上」是正常过渡态，不该在板端日志里刷成错误。
    """
    if not _device_auth_ok(request):
        return _unauthorized()

    device_id = (request.query_params.get("device_id") or "").strip()
    if not device_id:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False, "error": "missing 'device_id'"},
        )
    request_id = (request.query_params.get("request_id") or "").strip()

    conn = _connect()
    try:
        # 惰性过期改了行就必须提交：sqlite 在 conn.close() 时会回滚未提交事务，
        # 只靠同连接内的 SELECT 是"看得见、留不下"（响应看着对，磁盘上还是 pending）。
        _expire_stale_events(conn, device_id)
        conn.commit()
        if request_id:
            row = conn.execute(
                "SELECT * FROM events WHERE request_id=? AND device_id=?",
                (request_id, device_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM events WHERE device_id=?"
                " ORDER BY created_at DESC LIMIT 1",
                (device_id,),
            ).fetchone()
    finally:
        conn.close()

    if row is None:
        return JSONResponse(
            status_code=200,
            content={"ok": True, "found": False, "status": None,
                     "event": None},
        )

    view = _event_view(row)
    return JSONResponse(
        status_code=200,
        content={"ok": True, "found": True, "status": view["status"],
                 "event": view},
    )


@app.get("/api/v1/events")
def list_events(request: Request):
    """闭环事件列表（新的在前）—— 供 Web「闭环事件」卡片渲染。

    ?device_id=…  只看某台设备；?limit=n（1-200，默认 20）
    ?active=1     只看未终态的（待处理/已回应），用于页面顶部「有事件待处理」提示
    """
    device_id = (request.query_params.get("device_id") or "").strip()
    try:
        limit = int(request.query_params.get("limit", EVENT_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = EVENT_DEFAULT_LIMIT
    limit = max(1, min(EVENT_MAX_LIMIT, limit))

    only_active = (request.query_params.get("active") or "").strip() in ("1", "true", "yes")

    sql = "SELECT * FROM events WHERE 1=1"
    args = []
    if device_id:
        sql += " AND device_id = ?"
        args.append(device_id)
    if only_active:
        sql += " AND status IN ('pending','ack')"
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)

    conn = _connect()
    try:
        _expire_stale_events(conn, device_id or None)
        conn.commit()          # 同上：不提交则过期状态不落库
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    events = [_event_view(r) for r in rows]
    return JSONResponse(
        status_code=200,
        content={"code": "OK", "ok": True, "count": len(events),
                 "pending": sum(1 for e in events
                                if e["status"] == EVENT_STATUS_PENDING),
                 "events": events},
    )


@app.get("/api/v1/health")
def health():
    """服务健康状态 + 汇总，便于验证/排错。"""
    try:
        conn = _connect()
        try:
            # 所有统计型 SELECT 之前先做惰性过期（README 注意事项 14），且必须
            # 提交：tasks 与 events 各扫一次，否则 health 的汇总口径会和
            # GET /api/v1/tasks、GET /api/v1/events 打架（显示"待处理 2 条"、
            # 列表只返回 1 条）。不 commit 的话过期 UPDATE 会被 conn.close() 回滚，
            # 于是每条读路径都重算一遍、状态永远落不了库。
            _expire_stale_tasks(conn)
            _expire_stale_events(conn)
            conn.commit()
            n_uploads = conn.execute(
                "SELECT COUNT(*) FROM uploads"
            ).fetchone()[0]
            n_samples = conn.execute(
                "SELECT COUNT(*) FROM samples"
            ).fetchone()[0]
            dev_rows = conn.execute(
                "SELECT DISTINCT device_id FROM uploads"
            ).fetchall()
            task_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            n_photos = conn.execute(
                "SELECT COUNT(*) FROM photos"
            ).fetchone()[0]
            last_photo = conn.execute(
                "SELECT id, device_id, ts_ms, received_at, bytes, width, height"
                " FROM photos ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last = conn.execute(
                "SELECT device_id, ts_ms, received_at, sample_count, source, unit"
                " FROM uploads ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
            # 事件/任务的惰性过期已在函数开头统一做完（见那里的注释）。
            event_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
            ).fetchall()
            last_event = conn.execute(
                "SELECT * FROM events ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return JSONResponse(
            status_code=500, content={"status": "error", "error": str(e)}
        )

    latest = None
    if last:
        latest = {
            "device_id": last["device_id"],
            "source": last["source"],
            "unit": last["unit"],
            "ts_ms": last["ts_ms"],
            "received_at": last["received_at"],
            "received_at_str": _fmt_time(last["received_at"]),
            "sample_count": last["sample_count"],
        }
    latest_photo = None
    if last_photo:
        latest_photo = _photo_view(last_photo)
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "db": DB_PATH,
            "total_uploads": n_uploads,
            "total_samples": n_samples,
            "devices": [r["device_id"] for r in dev_rows],
            "total_tasks": sum(r["n"] for r in task_rows),
            "tasks_by_status": {r["status"]: r["n"] for r in task_rows},
            "total_photos": n_photos,
            "latest_photo": latest_photo,
            "total_events": sum(r["n"] for r in event_rows),
            "events_by_status": {r["status"]: r["n"] for r in event_rows},
            "latest_event": _event_view(last_event) if last_event else None,
            "latest": latest,
        },
    )


@app.get("/api/v1/devices")
def devices():
    conn = _connect()
    try:
        # pending_tasks 属于统计口径：必须先惰性判超时，否则早已 timeout 的任务
        # 仍被算成待办，与 GET /api/v1/tasks 的 status=timeout 互相矛盾。
        _expire_stale_tasks(conn)
        rows = conn.execute(
            # 设备集合 = 上传过样本的设备 ∪ 拍过照片的设备（只有照片没样本的设备
            # 也必须出现在下拉里，否则画廊里会看不到它自己的照片）。
            "SELECT d.device_id AS device_id,"
            " (SELECT COUNT(*) FROM uploads u WHERE u.device_id = d.device_id) AS uploads,"
            " (SELECT MAX(u.received_at) FROM uploads u WHERE u.device_id = d.device_id)"
            "  AS last_seen,"
            " (SELECT COUNT(*) FROM photos p WHERE p.device_id = d.device_id) AS photos,"
            " (SELECT COUNT(*) FROM tasks t WHERE t.device_id = d.device_id"
            "  AND t.status NOT IN ('completed','failed','timeout')) AS pending_tasks"
            " FROM (SELECT device_id FROM uploads UNION SELECT device_id FROM photos) d"
            " ORDER BY d.device_id"
        ).fetchall()
        # 暂停状态真相源：device_control 里到点的行先惰性归零，避免页面显示过期状态
        paused = _paused_devices(conn)
        conn.commit()
    finally:
        conn.close()
    return JSONResponse(
        status_code=200,
        content={
            "devices": [
                {
                    "device_id": r["device_id"],
                    "uploads": r["uploads"],
                    "photos": r["photos"],
                    "last_seen": _fmt_time(r["last_seen"]),
                    "pending_tasks": r["pending_tasks"],
                    "periodic_paused": r["device_id"] in paused,
                }
                for r in rows
            ]
        },
    )


@app.get("/api/v1/latest")
def latest(request: Request):
    """返回最近一次上报（含首末样本），用于核对采集值是否来自本组设备。

    可选 `trigger=manual|periodic`：只看按需采集批次或只看周期上报批次，
    供监控页的「手动 vs 周期」对照区各取一行。缺省时不加该条件，
    与旧调用方行为完全一致（向后兼容）。
    """
    device_id = (request.query_params.get("device_id") or "").strip()
    trigger = (request.query_params.get("trigger") or "").strip()
    if trigger and trigger not in ("manual", "periodic"):
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID", "ok": False,
                     "error": "invalid 'trigger' (expected manual or periodic)"},
        )
    conn = _connect()
    try:
        sql = "SELECT * FROM uploads"
        conds, args = [], []
        if device_id:
            conds.append("device_id=?")
            args.append(device_id)
        if trigger:
            conds.append("trigger=?")
            args.append(trigger)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY received_at DESC LIMIT 1"
        row = conn.execute(sql, args).fetchone()
        if row is None:
            return JSONResponse(
                status_code=200, content={"ok": True, "found": False}
            )
        up = dict(row)
        up.pop("payload", None)
        head = conn.execute(
            "SELECT seq, t_ms, ax, ay, az FROM samples"
            " WHERE upload_id=? ORDER BY seq ASC LIMIT 5",
            (up["id"],),
        ).fetchall()
        tail = conn.execute(
            "SELECT seq, t_ms, ax, ay, az FROM samples"
            " WHERE upload_id=? ORDER BY seq DESC LIMIT 5",
            (up["id"],),
        ).fetchall()
        up["received_at_str"] = _fmt_time(up.get("received_at"))
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "found": True,
                "upload": up,
                "sample_head": [dict(s) for s in head],
                "sample_tail": [dict(s) for s in reversed(tail)],
            },
        )
    finally:
        conn.close()


@app.get("/api/v1/window")
def window(request: Request):
    """返回某设备最近 N 秒的**连续**样本序列，供监控页画实时波形。

    与 /api/v1/latest 的区别：latest 只给最近 1 批的首末各 5 条，
    这里跨批次拼出连续时间轴（abs_ms = 批次起点 ts_ms + 批内偏移 t_ms）。

    说明：过滤条件用**服务端**接收时间 received_at 而非板端 ts_ms ——
    板端 SNTP 失败时 ts_ms 可能不可信，但数据本身仍应展示。
    """
    device_id = (request.query_params.get("device_id") or "").strip()
    try:
        seconds = int(request.query_params.get("seconds", 30))
    except (TypeError, ValueError):
        seconds = 30
    seconds = max(1, min(MAX_WINDOW_SECONDS, seconds))

    conn = _connect()
    try:
        since = time.time() - seconds
        # 用索引把「最近 N 秒」换算成主键下界：uploads.id 与 received_at 都是入库时
        # 递增的，加上 id >= ? 可让 SQLite 走主键范围扫描，避免库变大后扫描该设备的
        # 全部历史批次。received_at 条件仍然保留，查询语义不变，这里纯属性能优化。
        row = conn.execute(
            "SELECT MIN(id) FROM uploads WHERE received_at >= ?", (since,)
        ).fetchone()
        min_id = row[0] if row is not None else None

        rows = []
        if min_id is not None:
            sql = (
                "SELECT u.ts_ms + s.t_ms AS abs_ms, s.ax, s.ay, s.az"
                " FROM samples s JOIN uploads u ON u.id = s.upload_id"
                " WHERE u.received_at >= ? AND u.id >= ?"
            )
            args = [since, min_id]
            if device_id:
                sql += " AND u.device_id = ?"
                args.append(device_id)
            # 先按时间倒序取上限条，再在内存里翻正，保证拿到的是"最近"的 N 条
            sql += " ORDER BY u.received_at DESC, u.id DESC, s.seq DESC LIMIT ?"
            args.append(MAX_WINDOW_POINTS)
            rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    points = [
        {"t": r["abs_ms"], "ax": r["ax"], "ay": r["ay"], "az": r["az"]}
        for r in reversed(rows)
    ]
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "device_id": device_id or None,
            "seconds": seconds,
            "count": len(points),
            "points": points,
        },
    )


def _fmt_epoch_ms(ts_ms):
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return str(ts_ms)


@app.get("/", response_class=HTMLResponse)
def index():
    """最小 Web 页面：实时读库，数值不写死；展示无数据 / 未更新状态。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT device_id, COUNT(*) AS uploads,"
            " MAX(received_at) AS last_seen, MAX(ts_ms) AS last_ts,"
            " (SELECT az FROM samples s WHERE s.upload_id = "
            "  (SELECT id FROM uploads u2 WHERE u2.device_id = u.device_id"
            "   ORDER BY u2.received_at DESC LIMIT 1)"
            "  ORDER BY seq DESC LIMIT 1) AS latest_az"
            " FROM uploads u GROUP BY device_id ORDER BY device_id"
        ).fetchall()
    finally:
        conn.close()

    stale_s = 30  # 超过该秒数视为"未更新"
    now = time.time()
    items = []
    for r in rows:
        last = r["last_seen"]
        age = (now - last) if last else None
        state = "NO_DATA" if age is None else ("FRESH" if age <= stale_s else "STALE")
        items.append(
            {
                "device_id": r["device_id"],
                "uploads": r["uploads"],
                "last_seen": _fmt_time(last),
                "last_ts": _fmt_epoch_ms(r["last_ts"]) if r["last_ts"] else None,
                "age_s": None if age is None else round(age, 1),
                "state": state,
                "latest_az": r["latest_az"],
            }
        )

    if not items:
        body = "<h2>尚无传感数据（NO DATA）</h2><p>等待开发板首次上传...</p>"
    else:
        body = "<h2>各设备最近上报</h2>"
        state_cn = {"FRESH": "更新中", "STALE": "未更新", "NO_DATA": "无数据"}
        color = {"FRESH": "#1a7f37", "STALE": "#d29922", "NO_DATA": "#cf222e"}
        for it in items:
            az = (
                f"{it['latest_az']:.3f} m/s^2"
                if it["latest_az"] is not None
                else "n/a"
            )
            body += (
                "<p><b>%s</b> · <span style='color:%s'>%s</span><br>"
                "最近板端采样时间：%s<br>"
                "服务端收到：%s（%s 秒前）<br>"
                "末样本 az ≈ %s<br>累计批次：%s</p>"
            ) % (
                # device_id 已由 validate_payload/_device_id_ok 白名单卡住字符集（不含
                # '<'），这里再转义一次是纵深防御：模板拼接一旦被改动，输出侧也不会
                # 直接执行板端可控的标记。
                html.escape(str(it["device_id"])),
                color[it["state"]],
                state_cn[it["state"]],
                it["last_ts"],
                it["last_seen"],
                it["age_s"],
                az,
                it["uploads"],
            )

    return (
        "<html lang='zh'><head><meta charset='utf-8'>"
        "<title>传感数据接收平台</title></head><body>"
        f"{body}<hr><p>刷新本页查看最新。接口：/api/v1/health · "
        "/api/v1/devices · /api/v1/latest?device_id=...</p>"
        "<p>实时监控面板（设备选择 / 自动刷新）："
        '<a href="/ui/">/ui/</a></p>'
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def serve():
    """供 python server/main.py 直接启动使用。"""
    import uvicorn

    init_db()
    print(f"[sensor-server] listening on http://{HOST}:{PORT}  db={DB_PATH}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    serve()




