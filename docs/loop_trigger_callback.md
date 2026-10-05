# 闭环事件：触发 → 本地反馈 → 远端显示 → 回应/取消

> 复用既有的「板端上传 + 服务端任务接口 + Web 展示」三件套，**不新增协议栈**，
> 增加按键、屏幕与灯光，做出一条方向与「按需采集」完全相反的链路：
> 前面所有功能是 **Web 下发、板端执行**；本功能是 **板端发起、Web 接收**。

---

## 一、为什么方向反过来就值得单做一条链路

已有的 `tasks` 表语义是「服务端派发的任务」：`GET /api/v1/tasks/next` 会把
`submitted` 的任务下发给板子。如果把板端发起的事件也塞进 `tasks`，
板子自己创建的事件会被自己的轮询领回来 —— 形成回环，越跑越乱。

所以闭环事件独立成 `events` 表 + 三个设备侧接口，`loop_task` 与
`task_poll_task` 并存但互不干扰：

```
                  task_poll_task  （服务端 → 板端：采集/拍照/暂停/恢复）
Web  ……下发任务……▶  GET /api/v1/tasks/next  ◀── 板端轮询领取

                  loop_task       （板端 → 服务端：闭环事件）
板端按键触发 ……▶  POST /api/v1/events/trigger
板端按键回应 ……▶  POST /api/v1/events/respond
板端轮询收口 ……▶  GET  /api/v1/events/status
Web 远端显示 ……▶  GET  /api/v1/events
```

**为什么闭环轮询不塞进 task_poll_task**：那里的每次 HTTP 最长阻塞
`TASK_HTTP_TIMEOUT_MS`（5 s），而它每 3 s 就要领一次任务。网络差时多塞两个请求
会把它拖垮，直接连累「采集一次最新数据」「拍一张照片」。独立任务 + 独立周期，
闭环再慢也不影响既有链路 —— 这也是"不要触碰核心采样与推流逻辑"的落实方式。

---

## 二、板端状态机

```
                    按 B3 触发键
   LOOP_IDLE ───────────────────────────▶ LOOP_TRIGGERED
       ▲                                      │        │
       │                                      │        └── 按 B4 单击回应 / 长按取消
       │                                      │                    │
       │                                      │                    ▼
       │                                      │           LOOP_WAITING_ACK
       │                                      │                    │
       │        轮询到远端终态（闪灯 + DONE）   ▼                    ▼
       └──────── LOOP_COMPLETED ◀─────────────────────────────────┘
                  （停留 LOOP_DONE_DWELL_MS = 3 s 后自动回 IDLE）
```

| 状态 | 含义 | LCD 文本 | 触发它的动作 |
|---|---|---|---|
| `LOOP_IDLE` | 空闲，没有进行中的闭环 | `LOOP:IDLE` | 上电 / DONE 停留结束 |
| `LOOP_TRIGGERED` | 已触发，等远端处理 | `LOOP:TRIG <rid前4位>` | 按 B3 |
| `LOOP_WAITING_ACK` | 已回应/取消，等远端收口 | `LOOP:WAIT` / `LOOP:CANCEL` | 按 B4（单击 / 长按） |
| `LOOP_COMPLETED` | 远端已回来 | `LOOP:DONE` / `CANCEL` / `TIMEOUT` / `FAIL` / `NO NET` | 轮询到终态 / 本地超时 / 上报失败 |

### 「远端已回来」的判据（刻意保守，否则会自激）

这是整个设计里最容易写错的一处。`status` 从服务端读回来的取值有五种，
但**不能一看到非 pending 就收尾**：

| 板端所处状态 | `pending` | `ack` | `cancelled` | `completed` | `expired` |
|---|---|---|---|---|---|
| `TRIGGERED`（板端还没回应） | 继续等 | **收尾**（远端已受理） | **收尾** | **收尾** | **收尾** |
| `WAITING_ACK`（板端已回应） | 继续等 | 继续等 ← 这是**板端自己**刚发的 accept 的回声 | **收尾** | **收尾** | **收尾** |

`WAITING_ACK` 状态下把 `ack` 判成"远端已回来"是错的：板端按一下「回应」，
服务端立刻把状态写成 `ack`，板端下一轮就轮询到 `ack` —— 于是**自己把自己判为完成**，
闭环形同虚设。所以 `ack` 只有在 `TRIGGERED` 状态下才算远端的动作。

---

## 三、本地反馈：按键 / 灯光 / 屏幕

### 3.1 按键分配

| 按键 | 单击 | 长按 2 s |
|---|---|---|
| `BSP_BUTTON_1`（Button A） | 启动/停止采集 | 摄像头三态：关 → Web 直播 → 本地预览 |
| `BSP_BUTTON_2`（Button B） | 六面标定启停 | 循环切换 5 种动作协议 |
| **`BSP_BUTTON_3`（新增）** | **触发闭环事件** | — |
| **`BSP_BUTTON_4`（新增）** | **回应（accept）** | **取消（cancel）** |

⚠️ **ESP32-S3-EYE 的 BUTTON_1..4 不是独立 GPIO 按键，而是同一个 ADC 通道上的
电阻梯按键**（`managed_components/espressif__esp32_s3_eye/src/bsp_button.c`：
4 档都挂在 `ADC_CHANNEL_0`，靠电压档位 2410 / 1980 / 820 / 380 区分）；
`BSP_BUTTON_5` 才是 GPIO0 的 BOOT 键。板子上共 6 个功能键（含 RST）。
`iot_button` 已经把 4 档封装成 4 个独立设备，回调注册方式与 GPIO 按键完全一致，
所以 1/2 归 A/B 之后，3/4 可以放心用。**不需要**退化成"双击 A 触发"。

### 3.2 灯光（板载绿色 LED，GPIO3）

| 事件 | 闪烁次数 |
|---|---|
| 触发成功 | 2 |
| 已回应（已上报） | 1 |
| 远端确认完成 | 3 |
| 取消 / 过期 | 4 |
| 上报失败 / 离线 / 本地超时 | 6 |

⚠️ **GPIO3 必须配成开漏输出（`GPIO_MODE_OUTPUT_OD`）**。乐鑫官方硬件文档原文：
> "Software can configure GPIO3 to set different LED statuses … Note that GPIO3
> must be set up in open-drain mode. **Pulling GPIO3 up may burn the LED.**"

因此**不能用** `bsp_led_indicator_create()` / `bsp_led_set()`：那条路径走的是
`led_indicator_gpio`，内部是 `GPIO_MODE_OUTPUT`（推挽，见
`managed_components/espressif__led_indicator/src/led_indicator_gpio.c`），
正是官方警告要避免的配置。本功能直接 `gpio_config()` 成开漏。

开漏下"释放(level=1)"与"拉低(level=0)"哪个是亮，取决于板子 LED 的接法，
所以给了两个编译期开关（`main/main.c` 的「闭环事件 config」）：

| 宏 | 默认 | 作用 |
|---|---|---|
| `LOOP_LED_ON_LEVEL` | `1` | 哪个电平算"亮"。**上板若发现亮灭相反，改成 0 即可** |
| `LOOP_LED_REST_ON` | `0` | 闪烁结束后是否常亮。改 `1` 可当电源指示灯用（闪烁表现为短暂熄灭） |

**非阻塞**：按键回调只往长度为 1 的队列里 `xQueueOverwrite` 一个 `uint8_t`
（还要闪几下），真正的 `vTaskDelay` 循环在独立的 `loop_led` 任务里。
`xQueueOverwrite` 永不阻塞、永不失败，连按只会保留最后一次。

### 3.3 屏幕

闭环状态显示在 LCD 那条**黄色状态行**（`s_stand_label_ui`，y=188），
优先级为：**动作协议 > 闭环状态 > `s_status_text`**。

选这条行而不是新开一行，是因为屏幕只有 240×240，状态栏（y=210）已经贴底，
硬塞第四行会被裁掉。代价是**5 种动作协议运行时闭环文本会被盖住** ——
协议显示本身也是"正在进行的事"，且两者同时跑的场景很少，故接受这个取舍。

---

## 四、接口契约（本地电脑当服务端时同样适用）

没有 VPS 时，板端与服务端同处一个局域网，服务地址就是电脑的局域网 IP，
下面的接口与 `SENSOR_SERVER_URL` 里配的 `/api/v1/upload` 自动同源
（板端用 `server_api_url()` 从上传 URL 推导 base，不需要额外配置；
**前提是该 URL 以 `/api/v1/upload` 结尾**——反向代理前缀会被保留，
但后缀之后不能再有查询串等内容，否则板端会禁用这些接口并打
`cannot derive API base ...` 警告，见 README 注意事项 15）。

### 4.1 `POST /api/v1/events/trigger` （板端 → 服务端）

```json
{
  "device_id": "esp32s3-eye-0001",
  "source":    "qma6100p",
  "kind":      "alert",
  "request_id": "3f7a1c9e5b2d4a8f9c0e1d2b3a4f5e6d",
  "ts_ms":     1790516305071
}
```

- `request_id` **由板端生成**（128 bit `esp_random()` → 32 个十六进制字符）。
  必须板端生成：按下按键时灯就闪了、屏幕就改了，本地反馈不能等网络往返。
- 成功 `201`；**同 `request_id` 重发返回 `200` + `idempotent: true`**，
  页面上不会因重试刷出两条。
- 失败 `400`（字段非法）/ `401`（配了 `SENSOR_TOKEN` 但没带 Bearer）。

### 4.2 `POST /api/v1/events/respond` （板端与 Web 共用）

```json
{ "request_id": "…", "action": "accept", "by": "device" }
```

| `action` | 目标状态 | 谁发 |
|---|---|---|
| `accept` | `ack` | 板端单击 B4；Web 点「回应」 |
| `cancel` | `cancelled` | 板端长按 B4；Web 点「取消」 |
| `confirm` | `completed` | Web 点「确认完成」 |

- 重复同一动作 → `200` + `idempotent: true`；目标状态已是当前状态也算幂等。
- **终态不可回退** → `409 CONFLICT`（连点不会把状态来回翻）。
- 未知 `request_id` → `404`。

### 4.3 `GET /api/v1/events/status?device_id=…&request_id=…` （板端轮询）

```json
{ "ok": true, "found": true, "status": "ack", "event": { … } }
```

- 顶层额外给一个 `status`，板端只做最简解析。
- **未知 `request_id` 返回 `200` + `found: false`，不是 404**：板端每 1 s 轮询一次，
  "事件还没登记上"是正常过渡态，不该在板端日志里刷成错误。
- 不带 `request_id` 时返回该设备最近一条事件（联调时用来确认两边看的是同一条）。

### 4.4 `GET /api/v1/events?device_id=…&limit=8&active=1` （Web 用）

事件列表（新的在前）。`active=1` 只看未终态（`pending` / `ack`），
响应里带 `pending` 计数，页面用它渲染"n 条待处理"的红色徽章。

### 4.5 状态与过期

`pending → ack → completed`；`pending/ack → cancelled`。

过期判定与 `tasks` 同一哲学 —— **惰性**，不引入后台线程：
任何读事件的路径先调 `_expire_stale_events()`，把 `pending`/`ack` 超过
`expires_at` 的行置为 `expired`（TTL 由 `SENSOR_EVENT_TTL_S` 控制，默认 300 s）。
板端掉电、断网后事件不会永远挂在"待处理"。`expired` 也是终态，不可被改写。

> ⚠ 统计类接口（`/api/v1/health` 的 `events_by_status` / `latest_event`）必须在
> `SELECT … GROUP BY status` **之前**调用同一个过期函数，否则会把"已超时但行内状态
> 仍是 `pending`"的事件算进待处理，出现 **health 说「待处理 2 条」、而
> `GET /api/v1/events` 只返回 1 条** 的自相矛盾。惰性过期要求**每一条读路径**都先过期，
> 少一条就会在汇总口径上露馅。
> ⚠ 而且这一步是**写操作**：调完必须 `conn.commit()`。sqlite 在 `conn.close()` 时回滚
> 未提交事务，而同一个连接里的 `SELECT` 看得见未提交的 UPDATE —— 于是漏提交时**接口
> 返回值完全正常、磁盘上那一行却仍是 `pending`**，是最难发现的一类"静默失效"。验证时
> 请另开一个连接直读 `events.status`，不要只看响应。

---

## 五、板端本地兜底（网络一直不通时）

| 场景 | 行为 |
|---|---|
| 按键时 WiFi 没连上 | 灯闪 6 下，LCD 显示 `LOOP:NO NET`，直接回 IDLE（不会永远卡住） |
| trigger / respond 上报失败 | 灯闪 6 下，LCD 显示 `LOOP:FAIL`，回 IDLE |
| 上报成功但远端一直不处理 | 超过 `LOOP_LOCAL_TIMEOUT_MS`（60 s）→ 灯闪 6 下，显示 `LOOP:TIMEOUT` |
| 服务端重启 | 事件在库里，板端下一轮轮询照常读到状态 |

关键点：**本地反馈（灯 + 屏）总是先于网络发生**。HTTP 失败只影响"远端能不能看到"，
不影响"现场有没有反馈" —— 这是把 `request_id` 放在板端生成的根本原因。

---

## 六、同伴走查步骤与逐条观测点

> 前置：电脑 `python server/main.py`（监听 `0.0.0.0:8000`）；板端
> `SENSOR_SERVER_URL = http://<电脑局域网IP>:8000/api/v1/upload`；同一 WiFi；
> `idf.py -p <串口> flash monitor`。浏览器开 `http://<电脑局域网IP>:8000/ui/`。

| # | 操作 | 板上应该看到 | 页面上应该看到 | 串口应该有 |
|---|---|---|---|---|
| 1 | 按 **B3** 一次 | 绿灯闪 **2** 下；状态行变 `LOOP:TRIG xxxx` | 「闭环事件」卡片出现该 `request_id`，状态 **待处理**，徽章变红 `1 条待处理` | `[loop] state=TRIGGERED …` / `[loop] trigger OK` |
| 2 | 等 1–2 s 不操作 | 状态行保持 `LOOP:TRIG` | 仍是「待处理」，`有效期剩余` 在倒数 | `[loop] trigger OK` |
| 3 | 按 **B4** 单击 | 绿灯闪 **1** 下；状态行变 `LOOP:WAIT` | 状态转 **已回应**，`最近响应 = 回应 · 板端按键` | `[loop] respond accept (…) -> OK` |
| 4 | 页面点 **确认完成** | 绿灯闪 **3** 下；状态行变 `LOOP:DONE`；3 s 后回 `LOOP:IDLE` | 状态转 **已完成**，`收口时间` 有值 | `[loop] state=COMPLETED …` |
| 5 | 再按 **B3**，然后 **长按 B4** 2 s | 先闪 2 下，再闪 **4** 下；状态行 `LOOP:CANCEL` | 状态转 **已取消** | `[loop] respond cancel (…) -> OK` |
| 6 | 再按 **B3**，然后**直接断电** | —（板子已断电） | 事件停在「待处理」，`SENSOR_EVENT_TTL_S`（默认 300 s）后自动变 **已过期** | — |
| 7 | 拔掉网线/关掉服务端，再按 **B3** | 绿灯闪 **6** 下；状态行 `LOOP:NO NET`，随后回 IDLE | 无新事件（预期） | `[loop] state=COMPLETED text=LOOP:NO NET` |
| 8 | 连续快速按 **B3** 五次 | 只闪一组 2 下；**不会**进入 TRIGGERED 之外的第二个事件 | 列表里只有 **1 条**该设备的事件 | `[loop] trigger ignored: already in TRIGGERED` |
| 9 | 网络恢复后再按 **B3**，1 s 内按 **B4**，随后**什么都不做** 60 s | 闪 2 下 → 闪 1 下 → 60 s 后闪 **6** 下 + `LOOP:TIMEOUT` | 停在「已回应」直到过期 | `[loop] local timeout in state=WAITING_ACK` |

逐条对应关系（"请求—回执—新观测"）：

```
页面 request_id  ←→  串口 [loop] 行里的 rid  ←→  uploads/events 表里的 request_id
   板端 LED 次数  ←→  页面「最近响应」的 回应/取消/确认  ←→  events.response 字段
   板端 LCD 文本  ←→  页面「事件状态」  ←→  events.status 字段
```

---

## 七、自动化核对（无需人工点按键）

```bash
python server/test_events.py        # 23 项：状态机 / 幂等 / 终态不可回退 / 惰性过期 / 鉴权
python server/e2e_server_check.py   # 真实 uvicorn + 真实 HTTP，含 9 项闭环事件断言
python server/e2e_ui_check.py       # 无头浏览器，90 项（含 9 项事件卡片渲染断言）
```

板端固件侧：`idf.py build`（本仓库 `build_idf.bat` 会写 `build_log.txt`，
末尾 `BUILD_EXIT=0` 表示成功）。

---

## 八、已知边界（明确不做的部分）

1. **动作协议运行时，闭环文本被协议文本盖住**（§3.3 的取舍）。协议跑完即恢复。
2. **事件没有推送，只有轮询**：板端 1 s 一轮、页面 0.8 s 一轮。
   局域网下延迟可忽略；若将来要公网部署，再考虑 WebSocket / SSE。
3. **`kind` 目前只有 `alert` 一种**。表结构留了 `kind` 字段，加"求助/呼叫"等
   语义只需扩 `EVENT_DEFAULT_KIND` 与页面文案，不动状态机。
4. **不做事件的多设备广播**：`events` 按 `device_id` 隔离，页面下拉选谁看谁。
5. **Web 端刻意没有"下发触发"按钮**：触发只能来自板子上的物理按键，
   否则"本地反馈先于网络"这条设计（trigger 前灯已闪）就无从验证。
