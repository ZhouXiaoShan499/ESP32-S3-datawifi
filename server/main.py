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
    "samples": [
        {"i": 0, "t_ms": 0,    "ax": 0.02, "ay": -0.15, "az": 9.81},
        {"i": 1, "t_ms": 10,   "ax": 0.03, "ay": -0.14, "az": 9.80}
    ]
  }

运行（本地电脑）：
  python server/main.py          # 默认 http://127.0.0.1:8000
可用环境变量覆盖：SENSOR_HOST / SENSOR_PORT / SENSOR_DB
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
HOST = os.environ.get("SENSOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("SENSOR_PORT", "8000"))

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
DB_PATH = os.environ.get(
    "SENSOR_DB", os.path.join(_DATA_DIR, "upload.db")
)

# 限制与常量
MAX_DEVICE_ID_LEN = 64
MAX_SOURCE_LEN = 32
MAX_SAMPLES_PER_BATCH = 2000          # 单批最多样本数（板端约 100 Hz，分批上传）
MAX_BODY_BYTES = 2 * 1024 * 1024      # 单次请求体上限 2 MB
SUPPORTED_AXES = ("ax", "ay", "az")

app = FastAPI(
    title="ESP32-S3 IMU Sensor Receiver",
    description="接收并存储开发板 IMU 传感数据，提供查询接口。",
    version="1.0.0",
)

# sqlite 连接：多线程下为每个请求单独建连接，见 get_db()
_db_init_lock = threading.Lock()
_initialized = False


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def _connect():
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表：uploads(每个批次) + samples(每批内的每个采样点)。"""
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
                    payload       TEXT NOT NULL       -- 原始 JSON，便于追溯原始记录
                );
                CREATE INDEX IF NOT EXISTS idx_uploads_device_ts
                    ON uploads (device_id, ts_ms);
                CREATE TABLE IF NOT EXISTS samples (
                    upload_id INTEGER NOT NULL REFERENCES uploads(id),
                    device_id TEXT NOT NULL,
                    seq       INTEGER NOT NULL,       -- 板内序号 i
                    t_ms      INTEGER NOT NULL,       -- 相对批次起点的偏移(ms)
                    ax REAL, ay REAL, az REAL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_upload
                    ON samples (upload_id);
                """
            )
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
        cleaned.append({"i": seq, "t_ms": t_ms, **vals})

    return True, {
        "device_id": device_id,
        "source": source,
        "unit": unit,
        "ts_ms": ts_ms,
        "samples": cleaned,
    }


# ---------------------------------------------------------------------------
# 业务
# ---------------------------------------------------------------------------
def store_upload(data, ip=None):
    conn = _connect()
    try:
        samples = data["samples"]
        payload_json = json.dumps(
            {
                "device_id": data["device_id"],
                "source": data["source"],
                "unit": data["unit"],
                "ts_ms": data["ts_ms"],
                "samples": samples,
            },
            ensure_ascii=False,
        )
        cur = conn.execute(
            "INSERT INTO uploads (device_id, source, unit, ts_ms, received_at,"
            " sample_count, ip, payload) VALUES (?,?,?,?,?,?,?,?)",
            (
                data["device_id"],
                data["source"],
                data["unit"],
                data["ts_ms"],
                time.time(),
                len(samples),
                ip,
                payload_json,
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
        conn.commit()
        return upload_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _fmt_time(epoch_s):
    if not epoch_s:
        return None
    return datetime.fromtimestamp(epoch_s).strftime("%Y-%m-%d %H:%M:%S")


@app.post("/api/v1/upload")
async def upload(request: Request):
    """接收开发板上传的传感数据批次。"""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"code": "TOO_LARGE", "ok": False,
                     "error": "request body too large"},
        )
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
    try:
        upload_id = store_upload(result, ip=ip)
    except sqlite3.Error as e:
        return JSONResponse(
            status_code=500,
            content={"code": "DB_ERROR", "ok": False,
                     "error": f"store failed: {e}"},
        )
    return JSONResponse(
        status_code=201,
        content={
            "code": "OK",
            "ok": True,
            "upload_id": upload_id,
            "device_id": result["device_id"],
            "sample_count": len(result["samples"]),
            "unit": result["unit"],
            "ts_ms": result["ts_ms"],
        },
    )


@app.get("/api/v1/health")
def health():
    """服务健康状态 + 汇总，便于验证/排错。"""
    try:
        conn = _connect()
        try:
            n_uploads = conn.execute(
                "SELECT COUNT(*) FROM uploads"
            ).fetchone()[0]
            n_samples = conn.execute(
                "SELECT COUNT(*) FROM samples"
            ).fetchone()[0]
            dev_rows = conn.execute(
                "SELECT DISTINCT device_id FROM uploads"
            ).fetchall()
            last = conn.execute(
                "SELECT device_id, ts_ms, received_at, sample_count, source, unit"
                " FROM uploads ORDER BY received_at DESC LIMIT 1"
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
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "db": DB_PATH,
            "total_uploads": n_uploads,
            "total_samples": n_samples,
            "devices": [r["device_id"] for r in dev_rows],
            "latest": latest,
        },
    )


@app.get("/api/v1/devices")
def devices():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT device_id, COUNT(*) AS uploads,"
            " MAX(received_at) AS last_seen"
            " FROM uploads GROUP BY device_id ORDER BY device_id"
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse(
        status_code=200,
        content={
            "devices": [
                {
                    "device_id": r["device_id"],
                    "uploads": r["uploads"],
                    "last_seen": _fmt_time(r["last_seen"]),
                }
                for r in rows
            ]
        },
    )


@app.get("/api/v1/latest")
def latest(request: Request):
    """返回最近一次上报（含首末样本），用于核对采集值是否来自本组设备。"""
    device_id = (request.query_params.get("device_id") or "").strip()
    conn = _connect()
    try:
        if device_id:
            row = conn.execute(
                "SELECT * FROM uploads WHERE device_id=? "
                "ORDER BY received_at DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM uploads ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
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
                it["device_id"],
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




