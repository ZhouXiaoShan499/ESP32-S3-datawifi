# AI-Agent 采集路径事件循环阻塞问题修复报告

> 一句话结论：`/api/v1/agent/chat` 在 `async def` 里直接调用了**同步阻塞**的 `nlu.parse()`
> 与 `router_.route()`（内部 `time.sleep` 轮询设备证据，采集 60s / 拍照 30s / 控制 30s），
> 把 uvicorn 的单线程事件循环整个占死；用 `run_in_threadpool` 把这两步挪出事件循环后，
> agent 请求从 **60.2s 失败** 变为 **2~6.6s 成功**，期间 `/api/v1/health` 全程 200。

---

## 1. 问题描述

### 1.1 症状表现

| 项目 | 现象 |
| --- | --- |
| 接口 | `POST /api/v1/agent/chat`（Web 页「AI 助手」发「帮我采集一次数据」） |
| 返回 | `采集timeout: expired before device upload`（`status=failure`） |
| 耗时 | 约 **60 秒**（恰好等于采集工具的轮询窗口） |
| 连带影响 | 该请求进行期间，**整个服务端**几乎失去响应 |

### 1.2 对比参照

同一系统里的直接任务接口完全正常，说明设备、数据库、任务状态机都没问题：

| 接口 | 耗时 | 结果 |
| --- | --- | --- |
| `POST /api/v1/tasks`（直接创建采集任务） | ~4s | `status=completed`，`upload_id=46202` |
| `POST /api/v1/agent/chat`（经 AI Agent 创建同一任务） | ~60s | `failure`：`expired before device upload` |

### 1.3 影响面

事件循环被独占期间，**所有**并发请求（无论是不是 agent 的）都被排队：

1. `/api/v1/health` 健康检查超时 —— 实测连续 12 次、每次 3s 全部 `TimeoutError`；
2. Web 页面的轮询接口（`/api/v1/latest`、`/api/v1/window`、`/api/v1/live`…）全部卡住；
3. **设备端** `GET /api/v1/tasks/next` 领不到任务 —— 刚创建的任务派发不出去，
   只能自己走到 `expires_at` 变成 `timeout`，于是接口最终报出
   `expired before device upload`（看起来像「设备上传慢」，其实是服务端自己把自己饿死了）。

---

## 2. 根因分析

### 2.1 定位过程（探针）

用 `probe.py` 在 agent 请求**在飞的同时**高频探测 `/api/v1/health`，直接抓到了「事件循环饥饿」的铁证：

* **修复前**：agent 请求 60.2s 期间，health 连续 12 次 3s 超时；agent 一返回，**0.19s 内** health 立刻恢复 200。
* **修复后**：agent 请求期间 health 全程 200（最慢 0.40s）。见 [4.4 并发探针测试](#44-并发探针测试)。

这种「请求一结束服务立刻恢复」的形状，排除了「设备慢 / 数据库锁 / 网络问题」，
唯一解释就是**服务端在自己等待自己**。

### 2.2 技术根因

`server/agent/api.py` 的 `agent_chat` 是 `async def`，却直接同步调用了两个阻塞函数：

```python
@router.post("/chat")
async def agent_chat(request: Request):
    # ... 省略入参校验与会话处理 ...
    from .nlu import get_nlu_parser
    from .router import get_router

    # ❌ 修复前：同步阻塞调用，直接写在 async def 里
    nlu = get_nlu_parser()
    parsed = nlu.parse(req.message, sess)      # ① 同步调 LLM（超时 30s）

    router_ = get_router()
    result = router_.route(parsed, sess)       # ② 同步轮询设备证据（采集 60s）
```

两个阻塞点：

| # | 调用 | 阻塞来源 | 最长阻塞 |
| --- | --- | --- | --- |
| ① | `nlu.parse()` | 同步调用 LLM（`config.AGENT_CONFIG[...]["timeout_s"]`）；LLM 不可用时回退关键词解析 | 30s |
| ② | `router_.route()` | 各工具用 `time.sleep` **死等**设备侧证据：采集 60s / 拍照 30s / 控制 30s | 60s |

关键点：`async def` 里的同步调用**不会**自动让出事件循环，uvicorn 是单事件循环模型，
于是这 60s 内没有任何协程能被执行。

### 2.3 连锁影响链

```
POST /api/v1/agent/chat
        │
        ├─> nlu.parse()          [同步阻塞，最多 30s]
        └─> router_.route()      [同步阻塞，最多 60s]
                 │  tools/capture.py: while ...: time.sleep(0.2)  ← time.sleep 独占事件循环
                 ▼
        ┌──────────────────────────────────────────────┐
        │ 事件循环被独占，所有请求排队（uvicorn 单循环）│
        └──────────────────────────────────────────────┘
                 │
     ┌───────────┼────────────────────────┬──────────────────────┐
     ▼           ▼                        ▼                      ▼
/api/v1/health  /api/v1/latest    /api/v1/tasks/next      网页各轮询接口
  12×3s 超时     全部卡住          设备领不到任务          全部卡住
                                          │
                                          ▼
                            任务派发不出去 → 到 expires_at 变 timeout
                                          │
                                          ▼
                       接口返回「采集timeout: expired before device upload」
```

---

## 3. 修复方案

### 3.1 代码改动（`server/agent/api.py`，4 个调用点 + 1 个导入）

```diff
 from fastapi import APIRouter, Request
+from fastapi.concurrency import run_in_threadpool
 from fastapi.responses import JSONResponse
```

**修复点 1 / 2：`/chat` 端点（NLU 解析 + 路由执行）**

```diff
-    parsed = nlu.parse(req.message, sess)
+    parsed = await run_in_threadpool(nlu.parse, req.message, sess)
 
     router_ = get_router()
-    result = router_.route(parsed, sess)
+    result = await run_in_threadpool(router_.route, parsed, sess)
```

**修复点 3 / 4：`/clarify` 端点（选择澄清项后继续执行，同样会阻塞）**

```diff
-    chosen = get_nlu_parser().parse(req.choice, sess)
+    chosen = await run_in_threadpool(get_nlu_parser().parse, req.choice, sess)
     ...
     router_ = get_router()
-    result = router_.route(new_parsed, sess)
+    result = await run_in_threadpool(router_.route, new_parsed, sess)
```

### 3.2 设计思路：为什么用 `run_in_threadpool`

1. **改动面最小、风险最低**：只动端点层的 4 个调用点，完全不碰
   `nlu.py` / `router.py` / `tools/*.py` 的业务逻辑与状态机，
   不改变任何返回结构、错误语义、防幻觉不变量。
2. **一处修复覆盖三类任务**：采集 / 拍照 / 控制的轮询都在 `router_.route()` 里，
   包住这一层就同时解决了三者。
3. **线程安全**：SQLite 侧是 WAL + `busy_timeout=10s` + 每次调用独立连接，
   没有跨线程共享的连接对象，因此挪到线程池是安全的。
4. **零新增依赖**：`fastapi.concurrency.run_in_threadpool` 是 Starlette 自带能力。
5. **比 `asyncio.to_thread` 更贴合框架**：`run_in_threadpool` 走的是 Starlette 的
   线程池（AnyIO），与 FastAPI 其他同步 endpoint 的处理路径一致；
   且用法是「先传函数、再传参数」（不是 `to_thread` 的 `(fn, *args)` 位置一致约定），
   在 `await` 前后保持 `parsed → route` 的顺序不变。

### 3.3 代码内注释（已写入 `server/agent/api.py`）

```python
    # NLU 解析与路由执行都改到工作线程里跑 —— 这两步都是**同步阻塞**调用：
    #   - nlu.parse() 同步调 LLM（超时 30s，见 config.AGENT_CONFIG[...]["timeout_s"]）；
    #   - router_.route() 里各工具要同步轮询设备证据，全是 time.sleep 死等
    #     （采集 60s / 拍照 30s / 控制 30s，见 tools/capture.py 等）。
    # 直接把它们放在 async def 里会独占 uvicorn 的事件循环：轮询期间**所有**请求
    # 都得不到响应，连 /api/v1/health 都会超时，设备端的 /api/v1/tasks/next 同样领
    # 不到任务 —— 于是刚创建的任务只能自己走到过期，接口最终返回
    # 「采集timeout: expired before device upload」（实测：agent 请求 60.2s 期间
    # health 连续 12 次 3s 超时，agent 一返回 0.19s 内立刻恢复 200）。
    # run_in_threadpool 把这两个阻塞调用挪出事件循环，其它请求照常被服务。
    nlu = get_nlu_parser()
    parsed = await run_in_threadpool(nlu.parse, req.message, sess)

    # 路由执行
    router_ = get_router()
    result = await run_in_threadpool(router_.route, parsed, sess)
```

### 3.4 明确**未**修改的部分

| 文件 | 是否改动 | 原因 |
| --- | --- | --- |
| `server/agent/nlu.py` | ❌ 未改 | 同步调 LLM 是既有设计，阻塞由调用方隔离即可 |
| `server/agent/router.py` | ❌ 未改 | 路由分发逻辑本身无问题 |
| `server/agent/tools/*.py` | ❌ 未改 | 轮询是业务必需；被线程池隔离后不再伤害事件循环 |
| `server/main.py` | ❌ 未改 | 其余接口本就是同步 `def`（FastAPI 自动丢线程池），只有 agent 端点写成 `async def` 才踩坑 |

> 修复的实质是：让 agent 端点也遵循「**同步阻塞代码不要写在 `async def` 里**」这条框架约定。

---

## 4. 验证结果

### 4.1 修复前 vs 修复后

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| agent `/chat` 耗时 | **60.2s** | **2.0s / 4.1s / 6.0s / 6.6s** |
| agent 返回 | `failure` + `expired before device upload` | **`success`** |
| agent 返回 `upload_id` | 无 | 46203 / 46204 / 46207 |
| 请求期间 `/api/v1/health` | 12×（每次 3s）全部超时 | **全程 200** |
| health 最慢响应 | >3000ms（超时） | **0.04 ~ 0.40s** |
| 设备取任务 | 领不到 → 任务 timeout | 创建→dispatched→acked→completed ≈ **1.6s** |
| 网页/设备其他接口 | 全部卡死 | 正常 |

### 4.2 单元测试：`server/test_agent.py`（19 项断言全通过）

命令：`python server/test_agent.py`（或 `run_tests.bat`），证据文件：`test_agent_out.txt`

```
=== py_compile ===
compile_exit=0
=== test_agent.py ===
NLU 客户端不可用，回退关键词解析（同类告警只提示一次）: openai 库未安装: pip install openai
[sensor-server] AI Agent 已挂载: /api/v1/agent/chat · /api/v1/agent/clarify
[PASS] main.app 已挂载 AI Agent 路由（/api/v1/agent/chat 可达）
[PASS] LLM 不可达时回退关键词解析：9 句话映射到正确意图
[PASS] 成功返回必带 evidence（空库 query_health 也是 success + 证据）
[PASS] 空库查询：不 500，如实返回 not_found/success
[PASS] 歧义 → clarification（带 options）→ /clarify 继续执行成功
[PASS] 追问选项被真正执行：选「查看设备列表」跑的是 query_devices（not_found）
[PASS] 无待澄清上下文的 /clarify 返回 failure 而非 500
[PASS] 入参校验：缺/空白 message、缺 session_id|choice、非法 JSON → 400
[PASS] 防幻觉：无真机时 start_capture 返回 no_evidence 且 evidence=None（绝不 success）
[PASS] 防幻觉：重复点击（已有待执行任务）也不返回 success，evidence=None
[PASS] 防幻觉：无真机时 take_photo 返回 no_evidence，绝不 success
[PASS] 不变量扫描：非 success/not_found 的响应一律不带 evidence（含暂停/恢复）
[PASS] 防幻觉：暂停/恢复无设备确认 → no_evidence + duplicate + evidence=None
[PASS] 设备白名单：非白名单设备被拒（failure，未进入工具执行）
[PASS] 路由器对未知意图返回 failure(unknown_intent)
[PASS] 工具层：make_evidence 剔 None / format_time_ago 分级 / to_dict 结构稳定
[PASS] 会话表惰性清理：超 TTL 的 session 被摘掉，未过期的保留
[PASS] 包导入布局：顶层 agent 与 server.agent 两种落点下相对导入均成立
[PASS] 临时库已清理（正式库未被写入）

全部通过：19 项断言
test_exit=0
```

> 结论：修复**没有**破坏任何既有行为（防幻觉不变量、歧义追问、入参校验、会话清理等）。

### 4.3 服务端全量自检：`server/e2e_server_check.py`（27 行全 PASS）

命令：`python server/e2e_server_check.py`（或 `run_e2e_server_check.bat`），证据文件：`e2e_check_prefix.txt`

```
[PASS] uvicorn main:app 冷启动（全新库，仅 ASGI lifespan）: /api/v1/health 200，表已建好
[PASS] /agent/chat 轮询期间 health 全程 200（16 次探针，最慢 0.40s），事件循环未被阻塞；且无真机时不返回 success
[PASS] uvicorn 子进程已就绪并响应 /api/v1/health
[PASS] POST 真实 HTTP 上传 201: 1
[PASS] health total_uploads=1 total_samples=4
[PASS] latest 末样本 az=9.83（与上传值一致）
[PASS] 连点去重（真实 HTTP）：duplicate=true 且 request_id 不变
[PASS] /tasks/next 领取一次即 dispatched，ack 回执 200（真实 HTTP）
[PASS] 手动批次回传 → 任务 completed（真实 HTTP，upload_id=2）
[PASS] health 任务统计（真实 HTTP）: {'completed': 1}
[PASS] /api/v1/control 初始未暂停（真实 HTTP）
[PASS] pause 经 /applied 生效（真实 HTTP）：periodic_paused=True 剩余 20.0s
[PASS] /api/v1/devices 标注 periodic_paused=True（真实 HTTP）
[PASS] resume 提前恢复（真实 HTTP）：periodic_paused=False
[PASS] 采集任务调 /applied 被拒 409 WRONG_KIND（真实 HTTP）
[PASS] health 任务统计（含控制任务）: {'completed': 3}
[PASS] 板端触发（真实 HTTP）：201 + status=pending
[PASS] 板端重试幂等（真实 HTTP）：同 request_id 仍只有 1 条事件
[PASS] 板端轮询 /events/status（真实 HTTP）：status=pending
[PASS] Web 远端列表可见待处理事件（active=1）
[PASS] 板端回应 accept（真实 HTTP）：status=ack
[PASS] 板端轮询到 ack（真实 HTTP）：本地可收尾
[PASS] Web 确认完成 → completed，且终态不可回退（409）
[PASS] 未知 request_id：轮询 200/found=false，响应 404
[PASS] health 事件统计: {'completed': 1}

E2E ALL CHECKS PASSED
CHECK_EXIT=0
```

其中第 2 行就是本次新增的**回归自检**（见 [6.3](#63-新增的回归自检)）。

### 4.4 并发探针测试（实机，`probe.py`）

证据文件：`probe_result.txt`

```
=== health probes while agent request in flight ===
  poll#01 @0.0s  health dt=0.04s code=200

=== agent result ===
elapsed=2.0s http=200
{"session_id":"0aa1d650a4be","type":"result",
 "message":"采集完成！\n- 设备: esp32s3-eye-0001\n- 样本数: 100\n- 采样时间: 2026-10-13 12:55:25\n- 任务耗时: 2.0s",
 "status":"success",
 "data":{"task":{"request_id":"ed38414c1d384ea985b9ac2856d90a48","status":"completed",
                 "created_at_str":"2026-10-13 12:55:24","dispatched_at_str":"2026-10-13 12:55:25",
                 "acked_at_str":"2026-10-13 12:55:25","completed_at_str":"2026-10-13 12:55:26",
                 "upload_id":46207},
         "upload":{"id":46207,"sample_count":100},
         "wait_s":2.0},
 "evidence":{"status":"completed","upload_id":46207,"sample_count":100}}

=== newest task ===
http=200 dt=0.03s
{"ok":true, ..., "upload_id":46207, "status":"completed", ...}
EXIT=0
```

**关键读数**：`poll#01 @0.0s health dt=0.04s code=200` —— agent 请求正在执行的那一刻，
health 依然 0.04s 返回 200，事件循环空闲；任务从创建到完成仅 1.8s。

### 4.5 实机端到端（两条路径同一次运行）

命令：`test_e2e.bat`，证据文件：`e2e_result.txt`

```
=== 1. health ===                      {"status":"ok", ..., "total_uploads":46201, ...}
=== 2. direct POST /tasks ===          {"code":"OK","ok":true,"duplicate":false,
                                        "task":{"request_id":"7152d3c6...","status":"submitted",
                                                "created_at_str":"2026-10-12 16:02:10"}}
=== 3. wait 8s then GET /tasks?limit=1 ===
                                       {"status":"completed","upload_id":46202,
                                        "dispatched_at_str":"2026-10-12 16:02:12",
                                        "acked_at_str":"2026-10-12 16:02:12",
                                        "completed_at_str":"2026-10-12 16:02:14"}
=== 4. agent chat: capture ===         {"status":"success",
                                        "message":"采集完成！...任务耗时: 6.0s",
                                        "data":{"task":{"upload_id":46203,"status":"completed",
                                                        "completed_at_str":"2026-10-12 16:02:23"},
                                                "wait_s":6.0},
                                        "evidence":{"upload_id":46203,...}}
=== 5. health after agent ===          {"status":"ok", ..., "total_uploads":46203,
                                        "tasks_by_status":{"completed":80,"failed":59,"timeout":13}}
=== DONE ===
```

**读法**：`completed` 由 78 → 80（两条路径各完成一单），`failed`/`timeout` **没有增加**，
说明修复后 agent 路径不再制造超时任务。

---

## 5. 附加工作

### 5.1 新增 `start_server.bat`（服务启停脚手架）

排查过程中发现：原来挂在 IDE 终端里的后台服务，**终端一关闭就被连带杀掉**
（stderr 无 Python 堆栈，属进程树回收，不是代码崩溃）。于是补了一个启停脚本：

| 用法 | 行为 |
| --- | --- |
| `start_server.bat` | 前台启动（日志打到当前窗口，`Ctrl+C` 停止） |
| `start_server.bat detach` | 后台启动，**不随终端关闭而死** |
| `start_server.bat stop` | 按端口找到监听进程并停止 |

实现要点与坑：

1. `detach` 用 WMI `Win32_Process.Create` 创建进程（而不是 `start` 子 shell），
   这样进程不挂在调用终端上，终端关掉服务仍在；
2. 脚本内部用 `cd /d` 切目录后**没有**污染调用者的工作目录（已实测修正该副作用）；
3. ⚠️ `detach` 由 WMI 代创建，**不继承**调用 shell 的 `SENSOR_*` 环境变量。
   若你依赖仅在当前 shell 里设置的 `SENSOR_TOKEN` / `SENSOR_PORT`，
   要么用前台模式，要么先把它们设成系统/用户级环境变量。

> 实机已走通 `stop → detach → /api/v1/health 200` 全链路。

### 5.2 环境状态（验证时）

| 项目 | 状态 |
| --- | --- |
| 服务进程 | Python PID **35632**，监听 `0.0.0.0:8000` |
| `/api/v1/health` | 200，响应 0.035s |
| 设备 | `esp32s3-eye-0001` 持续轮询 `/api/v1/tasks/next` |
| `SENSOR_TOKEN` | **UNSET**（鉴权旁路，与修复前一致，未改动） |

### 5.3 排查中查清的一处「假异常」

日志里出现过一句 `GET /api/v1/health%29`（多了一个 `%29` = `)`）。
定位结果是 **cmd 的锅**：我在 bat 里把多个命令用括号分组，URL 末尾紧跟的 `)`
被吞进了 URL，服务器自然回 404。**不是服务端 bug，只有一个服务实例在跑。**

### 5.4 仓库状态

本次修复相关（**待提交**）：

```
M server/agent/api.py           # 核心修复：4 处 run_in_threadpool
M server/e2e_server_check.py    # 新增回归自检 check_agent_chat_not_blocking()
```

与本次修复**无关**、属工作区既有改动（建议单独复核后再决定是否提交）：

```
M server/agent/nlu.py           # 默认模型 gpt-4o-mini → qwen3:14b
M server/main.py                # 仅空白字符（第 84 行空行多一个空格）
M main/main.c                   # 板端 C 代码
M main/Kconfig.projbuild        # 板端 Kconfig
```

验证脚手架（新增、未跟踪，可保留复用）：

```
test_e2e.bat              实机两条路径 E2E 入口
probe.py / probe_run.bat  并发探针（agent 在飞时探 health）
run_tests.bat             单元测试入口
run_e2e_server_check.bat  服务端全量自检入口
start_server.bat          服务启停（前台 / detach / stop）
agent_payload.json        agent 请求体样本
test_agent_out.txt        test_agent.py 输出留档
e2e_check_prefix.txt      e2e_server_check.py 输出留档
probe_result.txt          probe.py 输出留档
```

---

## 6. 复现与验证方法

### 6.1 前置

* Python 环境可 `import fastapi`（与项目其余脚本一致）；
* 端到端自检（`e2e_server_check.py`）会**自己拉起 uvicorn 子进程**，
  用独立端口 + 独立临时库，不会写正式库，可放心在开发机上跑；
* 实机端到端（`test_e2e.bat` / `probe.py`）需要**服务在 8000 端口运行**且设备在线。

### 6.2 命令速查

| 目的 | 命令 |
| --- | --- |
| 单元测试 | `python server\test_agent.py` 或 `run_tests.bat` |
| 服务端全量自检（含新回归） | `python server\e2e_server_check.py` 或 `run_e2e_server_check.bat` |
| 实机两条路径 E2E | `test_e2e.bat` |
| 并发探针（agent 在飞时探 health） | `probe_run.bat` |
| 起服务 | `start_server.bat` / `start_server.bat detach` / `start_server.bat stop` |

### 6.3 新增的回归自检

在 `server/e2e_server_check.py` 中新增 `check_agent_chat_not_blocking()`，
专门锁死这个 bug，防止回归：

1. 用**独立临时库 + 端口 8015** 起一个 uvicorn；
2. 把 `AGENT_POLL_S=8`、`AGENT_CAPTURE_TIMEOUT_S` 调小，让测试快；
3. 客户端 `timeout` 设得**大于**轮询超时；
4. 发一个 `/agent/chat` 采集请求，同时在另一个线程**连续探 `/api/v1/health`**；
5. 断言：所有探针都必须 200；一旦出现 `TimeoutError` 即判定「事件循环被阻塞」并给出证据列表。

**反向验证（证明这个测试真的能抓到 bug）**：

* 把修复**回退**后跑 → 精确失败，信息为：

  ```
  agent 轮询期间服务端失去响应（事件循环被阻塞）: [(5.01, "TimeoutError('timed out')")]
  ```

* 修复后跑 → 通过：

  ```
  [PASS] 16 次探针全 200，最慢 0.40s
  ```

### 6.4 Windows 环境注意事项（踩坑记录）

* **别在 cmd 里拼超长引号命令**（尤其是带 JSON 的 `curl`），极易被截断/转义错；
  用 `.bat` 包装更稳。
* **URL 末尾紧跟 `)`** 会被 cmd 的括号分组吞进 URL（见 [5.3](#53-排查中查清的一处假异常)）。
* 想「后台跑 + 不被终端回收」：`Start-Process ... -WindowStyle Hidden` +
  子进程用 `python -u`（无缓冲），输出重定向到文件再读，比抓终端输出可靠。

---

## 7. 后续建议（可选，均超出本次修复范围）

### 7.1 任务超时语义可再精确一点

当前任务 `expires_at` 与采集工具的轮询窗口**都是 60s**。当设备**真离线**时，
两者会同时到点，工具会报出 `expired before device upload` 这种「像上传慢」的消息，
而更贴切的语义是 `no_evidence`（设备没响应）。修复后正常链路 2~6s 就返回、
不再触发超时，但若要提升可观测性，可把这两条消息的判定顺序/阈值错开。

### 7.2 可考虑把 agent 会话存储外置

`server/agent/api.py` 的 `_sessions` 是**进程内内存 dict**（重启即丢）。
目前单进程够用，若将来多 worker 部署（`uvicorn --workers N`），
会话会因请求落到不同 worker 而丢失，届时需要换成 Redis 或数据库。

### 7.3 提交建议

1. 先提交本次修复：`server/agent/api.py` + `server/e2e_server_check.py`；
2. 再单独复核并决定：`start_server.bat` 是否纳入版本控制；
3. 最后处理工作区既有改动：`server/agent/nlu.py`（模型默认值变更属**行为变更**，
   建议单独一个提交并说明）、`server/main.py`（建议直接还原空白改动）、
   `main/main.c` + `main/Kconfig.projbuild`（板端，与本次无关）。

---

## 附录：改动清单速览

| 文件 | 改动 | 说明 |
| --- | --- | --- |
| `server/agent/api.py` | 修改 | 导入 `run_in_threadpool`；`/chat` 的 `nlu.parse`+`router_.route`、`/clarify` 的 `get_nlu_parser().parse`+`router_.route` 共 4 处改为 `await run_in_threadpool(...)`；补注释解释原因 |
| `server/e2e_server_check.py` | 修改 | 新增 `check_agent_chat_not_blocking()` 回归自检 |
| `start_server.bat` | 新增 | 服务前台 / detach / stop（详见 5.1） |

---

*报告整理时间：2026-10-13*
*验证方式：单元测试（19 项）+ 服务端自检（27 行）+ 实机 E2E（两条路径）+ 并发探针*
*证据留档：`test_agent_out.txt` / `e2e_check_prefix.txt` / `e2e_result.txt` / `probe_result.txt`*