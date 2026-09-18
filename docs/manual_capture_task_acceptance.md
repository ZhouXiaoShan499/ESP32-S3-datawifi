# 按需采集任务（manual capture task）验收手册

本文只回答一个问题：**怎么证明这套「网页点一下 → 板端采一批 → 数据带任务号入库」
是真的可用，而不是只在纸面上成立。** 每条结论都对应一个可复跑的命令或可核对的现象。

---

## 一、验收对象与边界

| 项 | 内容 |
|----|------|
| 需求 | 服务端下发一次性采样参数 → 板端暂停周期上报、按参数采一批 → 带 `request_id` 回传 → 服务端在写库的同一事务里把任务置 `completed` |
| 交付面 | 板端固件（`main/main.c`）、服务端（`server/main.py`）、监控页（`server/static/*`）、文档与自检脚本 |
| 不在本次范围 | 掉电续传（SD 卡落盘队列）、公网鉴权/TLS、真实姿态融合（仅加速度可视化） |

---

## 二、自动化验收（4 个脚本，按依赖从轻到重）

| 顺序 | 命令 | 依赖 | 通过判据 |
|------|------|------|----------|
| 1 | `python server/test_receive.py` | 只需 Python（临时库，不起服务） | 末尾 `ALL CHECKS PASSED`（27 项） |
| 2 | `python server/e2e_server_check.py` | 需 `fastapi` + `uvicorn`（自动起子进程 8011） | 末尾 `E2E ALL CHECKS PASSED`（8 项） |
| 3 | `python server/e2e_ui_check.py` | 需本机 Edge/Chrome（无则打印 `[SKIP]`） | 末尾 `ALL 41 UI E2E CHECKS PASSED` |
| 4 | `python server/e2e_device_check.py` | **需一块正在周期上报的真板**（无则打印 `[SKIP]` 并退出 0） | 末尾 `DEVICE ACCEPTANCE PASSED (11 checks)` |
| 5 | `build_idf.bat` | 需 ESP-IDF 环境 | `build_log.txt` 末尾 `BUILD_EXIT=0` |

### 2.1 三个「无板也能跑」的脚本各自证明什么

| 脚本 | 证明的事 |
|------|----------|
| `test_receive.py` | 协议与状态机：参数校验（越界/缺失 400）、`submitted → dispatched → acked → completed`、幂等重传（`200 idempotent`，样本不重复写）、连点去重（`duplicate=true`）、不匹配上传（400 + 任务 `failed`）、惰性 `timeout` 且不再下发、板端 `/fail` 上报、未知 `request_id` 404、`/latest?trigger=manual|periodic` 过滤与非法值 400、`/tasks` 历史倒序 + 字段齐全 + `limit` 夹取 1..100 |
| `e2e_server_check.py` | 上面这套在**真实 HTTP + 真实 uvicorn 子进程**上同样成立（不是 TestClient 的假传输） |
| `e2e_ui_check.py` | 页面 JS **真的执行到底**：设备下拉/波形/三维视图、参数表单带上板端上限并用该参数建任务、任务卡片渲染 `upload_id` 与 `*_at_str`、任务历史按倒序列出、`manual` vs `periodic` 对照区各一行、下发 12 s 仍无回执时出现「未收到设备回执」提示、回执后提示消失且状态文本含 `acked` |

### 2.2 真机脚本 `e2e_device_check.py` 的 11 项断言

前置：板端已烧录并连上同一 WiFi，服务端地址指向本机 `IP:8000`，板端正在周期上报
（最近一批周期数据 ≤ 15 s，可用 `DEVICE_FRESH_S` 调整）。

1. `server up`：本机服务起来并响应 `/api/v1/health`；
2. `task created`：建任务返回 201 + `submitted`；
3. `board claimed the task`：板端 3 s 内轮询领取（状态离开 `submitted`）；
4. `board acked the task`：`acked_at` 非空（板端回执链路通）；
5. `task completed`：有效期内走到 `completed`（非 `failed` / `timeout`）；
6. `upload linked to task`：`upload_id` 非空且该批次的 `request_id` 等于任务号；
7. `upload is manual and requested size`：批次 `trigger=manual` 且 `sample_count` 等于下发值；
8. `latest?trigger=manual is this task`：页面对照区查到的 manual 批次就是这次任务；
9. `periodic uploads paused during capture`：**采集窗口内 `periodic` 批次数为 0**（真的暂停了周期上报）；
10. `periodic uploads resumed`：任务结束后 15 s 内出现新的周期批次（暂停是可恢复的）；
11. `task history lists it`：`/tasks?limit=20` 能查到该任务与其 `upload_id`（页面任务历史的数据源）。

常用覆盖：`DEVICE_ID=esp32s3-eye-0001`（指定设备）、`TASK_SAMPLES=600 TASK_RATE_HZ=100`
（打满板端上限）、`TASK_WAIT_S=120`（板端采集较慢时放宽等待）。

---

## 三、Web 侧可观测点核对表（人工点一遍即可确认）

打开 `http://<PC_IP>:8000/ui/`，先确认设备下拉里有板端。

| 编号 | 观测点 | 在哪看 | 期望现象 |
|------|--------|--------|----------|
| D1 | 批次可追溯 | 数据卡片「任务号 request_id」「触发方式 trigger」；任务卡片「关联数据 upload_id」「下发/回执/完成时间」 | 手动批次 `trigger=manual` 且 `request_id` 非空；任务卡片四个字段都有值，`upload_id` 与任务详情一致 |
| D2 | 任务历史 | 「任务历史（最近 20 条）」表 | 每次点击新增一行：创建时间 / request_id / 点数@Hz / 状态 / upload_id / 耗时；重复点击不新增非终态任务 |
| D3 | 参数表单 | 顶部控件行 | 样本数默认 100（上限 600）、采样率默认 100 Hz（上限 100）、有效期默认 60 s；`source` 固定 `qma6100p`、无 `unit` 输入；填越界值点按钮 → 状态条提示「任务参数不合法」，不发请求 |
| D4 | 回执未收到 | 任务卡片下的黄色提示行 | 任务 `dispatched` 超过 5 s 没 `acked_at` 时出现；板端回执后自动消失（板端回执失败只写设备串口，服务端状态不会变） |
| D5 | 状态文本 | 任务卡片「任务状态」 | 中文状态 + 状态码 + 该阶段说明，例如 `设备接收（状态码 acked）· 板端已回执，采集/上传进行中`；失败时追加 `error` 原文 |
| D6 | 手动 vs 周期对照 | 「手动批次 vs 周期批次」表 | 两行分别来自 `?trigger=manual` / `?trigger=periodic`：手动行 `request_id` 非空、周期行为 `—`；缺某类批次时该行给出显式说明而不是空表 |

> 想不点按钮也做同样的核对：`/ui/?autocapture=1&samples=250&rate=50` 会用表单参数
> 自动建一次任务（与点按钮完全同一条代码路径）。

---

## 四、真机手工场景（板端在场时的补充确认）

| 场景 | 操作 | 期望 |
|------|------|------|
| 打满上限 | 表单填 600 点 @ 100 Hz | 任务 `completed`，批次 600 点；板端日志 `captured 600 samples` |
| 超上限 | 绕开表单限制、直接 `POST /api/v1/tasks`（如 2000 点或 200 Hz） | 板端领取后 `POST /tasks/{id}/fail` → 任务 `failed`，`error` 形如 `sample_count or sample_rate_hz beyond device capability` |
| 掉电/超时 | 点按钮后给板端断电 | 有效期（默认 60 s）后任务 `timeout`、无关联批次；重新上电后可正常建新任务，旧任务不会被再次下发 |
| 重复回传 | 用同一 `request_id` 再 POST 一次上传 | `200 idempotent=true`，`samples` 行数不变 |
| 连点 5 次 | 快速点按钮 5 次 | 只产生 1 条非终态任务，页面提示「已复用未完成任务」；若已 `dispatched`，则下发 12 s 后能看到 D4 的提示（无板时用来验证该提示） |
| 时间戳来源 | 展开板端 `expires_at` 兼容日志 | 板端接受秒/毫秒两种 `expires_at`（`< 1e12` 视为秒），不会被误判成立即过期 |

---

## 五、边界与已知限制（明确不验收的内容）

1. **掉电不续传**：采集中途断电，这一批数据丢失；任务靠惰性 `timeout` 收尾，不做补采。
2. **失败批次不重放**：周期批次上传失败只重试当前批次，不做磁盘队列。
3. **参数只到板端上限**：服务端允许 2000 点 / 200 Hz，但板端上限是 600 点 / 100 Hz，
   页面表单按交集（≤600 / ≤100）限制；超出交集的请求由服务端 400 或板端 `/fail` 兜底。
4. **无回执提示是前端推断**：判定条件是「`status=dispatched` 且 `dispatched_at` 超过 5 s
   且无 `acked_at`」，服务端状态机没有任何额外状态；板端若跳过 `ack` 直接回传，
   任务仍会正常 `completed`。
5. **单设备串行**：同一设备同一时刻只允许一个非终态任务（连点复用），不支持排队。
6. **安全**：默认无鉴权（局域网内网使用）；需要公网请自行加反向代理 + Token。

---

## 六、结论记录（每次验收填一行）

| 日期 | 提交/分支 | test_receive | e2e_server | e2e_ui | e2e_device | 备注 |
|------|-----------|--------------|------------|--------|------------|------|
| 2026-09-18 | `feature/manual-capture-task-web-3d` | `ALL CHECKS PASSED`（27 项） | `E2E ALL CHECKS PASSED`（8 项） | `ALL 41 UI E2E CHECKS PASSED` | `[SKIP]`（当时无板在线） | 真机项按 §2.2 前置条件在有板环境复跑 |

> 板端不在场时，第 4 个脚本打印 `[SKIP]` 且退出码为 0；只有真板在场才会给出
> `DEVICE ACCEPTANCE PASSED` 或 `[FAIL]`。这样它可以放进日常流水线而不因缺硬件变红。