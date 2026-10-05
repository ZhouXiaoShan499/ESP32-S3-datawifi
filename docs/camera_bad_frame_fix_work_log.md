# 相机坏帧 / 拍照失败治理：真机一轮基线 → 定位改写 → 修复 → 再跑一轮验证

> - **记录对象**：板端 `main/main.c`（OV2640 DVP JPEG，`/dev/video2`）；分支 `feature/loop-trigger-callback`
> - **记录日期**：2026-09-30
> - **一句话结论**：把真机验证做成「免按键、可复现的一轮」（`serial_round.py`），**先跑基线**再用
>   数据定位 —— 结论与前一版假设相反：`flags=0x41 used=0` 在这块板 + OV2640 JPEG 配置下是**常态而非残留**，
>   而自校验兜底**只给直播**，于是拍照 **8/8 失败**。据此把兜底扩到全模式并加新鲜度闸门，同 workload
>   再跑一轮：`no usable frame` **8 → 0**，照片**服务端 0 张 → 6 张**（`http=201`，`request_id` 一一对应），
>   全程 `no heap for` / `[csv] write failed` / 崩溃**均为 0**。
> - **证据文件**：板端串口原日志、每笔 `POST /api/v1/tasks` 的响应、机械归一结果、服务端
>   `GET /api/v1/photos` 快照都是**本机一次性产物**，已随 2026-10-05 的仓库整理移出仓库；
>   生成它们的三支本机脚本（`serial_round.py` / `analyze_round.py` / `photos_check.py`）
>   按 `.gitignore` 的约定**不进交付物**，本文保留的是判据与数字。
> - **配套文档**：机制与判读细则见 `docs/realtime_photo_capture.md` §三·补二 / §四；
>   直播侧同一套会话代码见 `docs/camera_live_stream.md`；内存侧（`dma_min` 与 SD `errno=5`）见
>   `docs/psram_upload_fix.md`。本文档是「做了什么 · 验证了什么 · 还差什么」的过程记录。

---

## 一、问题现象（要修什么）

修复前的串口症状（旧版固件）：

| # | 现象 | 影响 |
|---|------|------|
| 1 | 偶发 `[cam] no heap for 4294959519 B JPEG frame` | 长度野值而不是 OOM（4294959519 = 0xFFFFE19F = **−7777**），会让后续夹取/memcpy 全部失效 |
| 2 | **每一帧**都是 `dropping bad frame … flags=0x41 used=0 owner=y first4=FF D8 FF E0` | 驱动交回的帧一律 0 字节，即使映射缓冲里明明有完整 JPEG |
| 3 | SD 卡片写失败 `[csv] write failed errno=5`（EIO） | 录制/落盘链路被打断，出现在「相机会话反复 open/close」之后 |

前两版针对「跨会话残留元素」做了 app 侧自愈（长度笼子、软失败不拆会话、非阻塞重同步），
把自持循环掐掉了一部分，但**拍照仍然失败**——于是这次不再先改代码，而是先做「能重复跑一轮」的验证。

## 二、验证方法：免按键「跑一轮」工具链

### 2.1 为什么可以免按键

直播那条路径的实质是「持续 `DQBUF`」，而 Web 下发的 **`kind=preview`** 任务（本地 LCD 预览）
用的是**同一套会话代码**（同一 `camera_grab_locked()`），所以**不按 Button A 也能把相机路径跑满**。
整轮由脚本驱动，两轮 workload 完全一致，可直接机械对比。

### 2.2 一轮的时序（两轮相同，共 250 s）

```
t=0     RTS 复位（抓到完整启动日志）
  │
  ├─ 4 × kind=camera（间隔 15 s）
  │     每个 = 完整会话 churn：open → REQBUFS → STREAMON → DQBUF → STREAMOFF
  │            →（空闲 2 s 回收，重来一遍 16 KiB 内部 DMA + 3×307200 B PSRAM 分配）→ 上传 JPEG
  ├─ kind=preview ON（90 s，~50 ms 一帧，等价于直播负载，会话全程不关）
  ├─ kind=preview OFF + 10 s 空闲（会话再次被回收）
  └─ 4 × kind=camera（间隔 15 s）
```

### 2.3 三个工具（均已入库）

| 文件 | 作用 |
|------|------|
| `serial_round.py` | **单进程**同时抓 COM5 + 用 `POST /api/v1/tasks` 驱动相机；输出串口原日志与触发日志 |
| `analyze_round.py` | 串口日志 → 「marker 计数 + 消息直方图」，输出 `analysis_round*.txt` |
| `photos_check.py` | 查服务端 `GET /api/v1/photos` → 端到端证明「照片真的落库了」 |
| `round2.ps1` | 「烧录 + 停 10 s + 跑一轮」的封装 |

运行环境：`D:\Espressif\python_env\idf5.4_py3.11_env`（自带 pyserial）。

### 2.4 操作要点（踩过的坑，别再踩）

1. **长任务必须起独立进程**（`Start-Process -WindowStyle Hidden`）：本工具里下一条前台命令会
   杀掉终端里正在跑的命令——第一次 build 就这样被 `^C` 掉，`build_log.txt` 里留下了中断痕迹。
2. **`.bat` 必须是 CRLF**：用 LF 写的批处理会让 cmd 在 `start /b` 那行之后提前退出，
   子脚本根本没跑，还留下一个人占着 COM5 的孤儿进程（这也是最后改成 python 单进程的原因）。
3. **关键数字写文件再读文件**：终端回显不可靠，且 `read_files` 可能给旧快照（同一文件先读到
   748 行/75.8 s，稍后再读才是 2040 行/217 s）。
4. **cmd 里 `&` 与嵌套双引号会被拆**（`findstr /C:"a b"`、`curl "...&limit=30"` 都翻过车）：
   过滤日志用 `powershell -Command "... 'pattern'"` 或干脆用 python。
5. **一轮的 workload 要固定**，否则两轮数字不可比；`kind=camera` 的间隔不要短于板端任务完成周期
   （见 §6.4 的那个 `DUPLICATE`）。

---

## 三、round1（修复前基线）：证据推翻了旧根因

一次 250 s 一轮，`serial_round1.txt` 共 1042 行，`analyze_round.py` 归一结果见 `analysis_round1.txt`。

### 3.1 关键读数

| 读数 | 值 | 判读 |
|------|----|------|
| `no usable frame in … attempts` | **8** | 8 个拍照任务**全部失败** |
| 8 个拍照会话的 `session closed` | **`frames=0 bad=11 salvaged=0`** ×8 | 会话里一帧都没通过，11 次尝试（约 2.7 s × ~4 fps）后放弃 |
| preview 会话 | **`frames=205 bad=2 salvaged=205`**，uptime 87.7 s | 同样的坏帧，但**靠兜底全救回来** |
| `[cam] first frame: … SOI=yes` | **1**（只有 preview 那次） | 驱动真正「可用」的帧只出现过 1 次 |
| `NO-SOI` / `NO-EOI` / `JPEG size` / `RX-DA OVF` | **0 / 0 / 0 / 0** | DVP 控制器自身一条报错都没有 |
| `no heap for` | **0** | 野长度没再出现（长度笼子有效） |
| `[csv] write failed` / `errno=` / Guru / assert | 0 / 0 / 0 / 0 | 本轮无 SD 写失败、无崩溃 |
| `session open` / `closed` | 9 / 9 | 8 个拍照会话 + 1 个 preview；会话约每 2.7 s 就空闲回收一次 |
| `[HEAP]` 行 | 79 + 41（两档 `dma_largest` 10240 / 4096） | preview 开着时 16 KiB DVP DMA 缓冲被占着，内部 DMA 堆变小 |

### 3.2 三条决定性证据

1. **暖机帧就已经是 0 字节**：每个会话的前两帧（`warmup frame`）就是
   `flags=0x41 used=0 owner=y first4=FF D8 FF E0`，而整轮的**第一个**相机任务就是板子启动后的
   第一个会话（`uptime≈7 s`）——**不存在任何历史会话**，所以「跨会话残留」解释不了基线。
2. **同一块映射缓冲里数据是好的**：`warmup frame` 行里 `first4=FF D8 FF E0`（SOI 在偏移 0），
   兜底路径每次都能扫到完整 JPEG（`self-checked 8927 / 8893 / … / 9028 B`，长度随内容变化）。
3. **坏帧不是「偶发」而是「每次首帧」**，且 `NO-SOI`/`NO-EOI` 各 0 次：
   `dvp_calculate_jpeg_size()` 没有失败，说明驱动交回的**是 done 列表里另一个没有写入
   `valid_size` 的元素**，真正装帧的元素在别的边上。

### 3.3 被推翻的两个假设

| 旧假设 | 反证 |
|--------|------|
| `used=0` 只可能来自 `esp_video_buffer_reset()`（STREAMOFF 残留） | 第一场会话（无任何 STREAMOFF 历史）暖机帧就是 `used=0` |
| 「拍照不做兜底，宁可失败」（2026-09-29 的决定） | 等于拍照 **100% 失败**：8 个任务 0 成功 |

---

## 四、根因模型（当前认识）

```
OV2640(DVP) ──帧──▶ esp_video 元素表 ─┬─▶ done 列表元素 A：video_buffer->info.size 未写/被复用
                                      │      ⇒ DQBUF 交回 bytesused=0（常态）或野长度（−7777）
                                      └─▶ 映射缓冲 map[idx]：完整 JPEG（SOI@0 … EOI）
                                            ⇒ 只能由 app 自己扫 SOI/EOI 取图（唯一可靠来源）
```

- **`used=0` 是这块板 + 该传感器配置下的常态**，不是残留、不是 OOM、也不是 DVP 起振问题；
- 因此 **app 侧自校验 `camera_jpeg_span()` 必须是主路径（全模式）**，而不是直播专用兜底；
- 野长度（`−7777`）仍属残留元素的变体，所以 **`bytesused` 必须继续关在 `(0, map_len]` 的笼子里**；
- 唯一要防的是「刚 STREAMON 时读到的缓冲还装着上一场最后一帧」⇒ 给拍照加**内容哈希新鲜度闸门**；
- 会话 churn（open/close 反复）会周期性重分配 16 KiB 内部 DMA + 3×307200 B PSRAM，把
  `dma_largest` 从 10240 B 压到 4096 B —— 这正是 SD 写 `errno=5` 的前置条件，所以直播/预览期间
  **不**做空闲回收。

---

## 五、修复内容（`main/main.c`，共 4 处）

| # | 位置 | 改动 | 计数/日志 |
|---|------|------|-----------|
| 1 | `camera_grab_locked()` 的 C 层兜底 | 去掉 `mode == CAMERA_MODE_LIVE` 限定：`if (!usable && !warmup && idx_ok)`，**拍照也走自校验**（撤销 2026-09-29 的决定） | 日志加 ` (photo)` 后缀（复用原有 `self-checked %u B%s` 格式，不新增格式符，省 flash） |
| 2 | 同上，拍照分支 | 新增新鲜度闸门 `camera_jpeg_hash()`（FNV-1a32）：与**上一次成功拍照**逐字节相同 ⇒ 判为跨会话残留旧图，`continue` 取下一帧 | `s_cam.dup_drops` → `session closed` 与 `no usable frame …` 两行都新增 `dup=` |
| 3 | `camera_session_idle_close()` | 开头加 `if (s_live_streaming \|\| s_preview_active) return;` —— 直播/预览期间不做空闲回收 | 无新增日志；表现为 `session closed` 次数下降（round1 的 9 次 ≠ round2 的 7 次含任务数差异，见 §6.4） |
| 4 | `log_heap()` | 新增 `dma_min = heap_caps_get_minimum_free_size(MALLOC_CAP_DMA)`（DMA 子堆**历史谷底**） | `[HEAP] … dma_free=N dma_largest=N dma_min=N \| psram=N` |

**为什么闸门用「与上一张照片逐字节相同」而不是时间戳**：跨会话残留的特点就是**一模一样**；
误判代价只是多丢一帧（~40 ms 后重取），不会让拍照失败。实测两轮 `dup=0`，一次都没误伤。

**构建**：`BUILD_EXIT=0`，`data_capture_sim.bin = 0x1724b0 / 0x177000`（+224 B，1% free），
无新增告警。

---

## 六、round2（修复后，同 workload）：拍照回来了

`serial_round2.txt` 共 1023 行（`analysis_round2.txt`）。

### 6.1 A/B 对照

| 指标 | round1（修复前） | round2（修复后） |
|------|------------------|------------------|
| 拍照任务结果 | **8/8 失败**，`frames=0 bad=11 salvaged=0` ×8 | **0 失败**，`frames=1 bad=2 salvaged=1 dup=0` ×6 |
| `no usable frame in …` | **8** | **0** |
| 服务端落库照片 | **0 张** | **6 张**（id 14–19，全部 `http=201`） |
| `salvaged` | 205（全来自 preview） | 229（其中 6 次带 `(photo)`） |
| `[cam] first frame` | 1（只有 preview） | **7**（7 场会话都有可用帧） |
| preview 会话 | `frames=205 bad=2 salvaged=205`，87.7 s | `frames=223 bad=2 salvaged=223`，93.6 s |
| `session open` / `closed` | 9 / 9 | 7 / 7（任务数本就不同，见 §6.4） |
| `NO-SOI`/`NO-EOI`/`JPEG size`/`RX-DA OVF` | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| `no heap for` / `[csv] write failed` / 崩溃 | 0 / 0 / 0 | 0 / 0 / 0 |

> `salvaged` 数只升不降（205 → 229）**不是回归**：兜底本来是「主路径」，round2 多出的 6 次是
> 拍照成功带来的，而 `bad=11` 那种「一个会话里连丢 11 帧」的形态彻底消失了。

### 6.2 端到端证据（服务端）

`photos_check.py` 查到的 6 张新照片，与板端 `first frame` 字节数**逐字节对应**：

| 服务端 id | 收到时间 | bytes | 板端对应 |
|-----------|----------|-------|----------|
| 14 | 09:23:35 | 8977 | `first frame: 8977 B` |
| 15 | 09:23:43 | 8957 | `first frame: 8957 B` |
| 16 | 09:24:14 | 8997 | `first frame: 8997 B` |
| 17 | 09:26:14 | 9132 | `first frame: 9132 B` |
| 18 | 09:26:28 | 9279 | `first frame: 9279 B` |
| 19 | 09:26:40 | 9127 | `first frame: 9127 B` |

- 6 张全部 **640×480**、8.9–9.3 KB，`request_id` 与任务一一对应（`[photo] upload OK … http=201`）；
- **6 张字节数互不相同** ⇒ 拿到的是**新帧**，不是同一块残留缓冲被重复上传（配合 `dup=0` 双重佐证）。

### 6.3 新遥测：`dma_min`

| 场景 | `dma_min`（DMA 子堆历史谷底） |
|------|------------------------------|
| 拍照会话期间 | 9123 B |
| 跑完 90 s preview 后 | **2611 B** |

→ 这就是 SD 写 `errno=5` 要盯的数字（ESP32-S3 的 SDMMC 不能对 PSRAM DMA：FATFS 缓冲在 PSRAM 时
需要 512 B `MALLOC_CAP_DMA` 做 bounce buffer）。round2 未开 CSV 录制，所以本轮没有 `errno=5`。

### 6.4 两处与修复无关的差异（避免误读）

1. **`DUPLICATE` 不是板端问题**：测试脚本第 4 次 `kind=camera`（09:24:11）返回
   `200 {"code":"DUPLICATE"}`，复用了 09:23:56 那个**尚未终态**任务的 `request_id`
   —— 这是服务端既有设计（重复点击复用未完成任务，见 `docs/manual_capture_task_acceptance.md`），
   只是本轮的 15 s 间隔短于板端「拍摄+上传+收尾」周期。round1 没有这个现象，是因为那时每个
   `camera` 任务都**失败**（task 立即落到 `failed` → 下一次 POST 必然新建任务）。
   ⇒ 所以 8 个 POST 只产生 7 个唯一任务。
2. **第 7 个任务在窗口内没被领取**：09:26:51 创建的 `0ae6c835…` 没有对应会话/照片；同期板端出现
   一次 `[task] GET failed err=ESP_ERR_HTTP_EAGAIN http=-1`（HTTP 轮询超时，206.7 s）。
   推测是「服务端已把它标成 dispatched，而板端没拿到响应」导致该任务卡住（**未验证**，与相机问题无关，
   单独跟踪）。

---

## 七、遗留与下一步

| 优先级 | 事项 | 状态 |
|--------|------|------|
| P2 | 「直播/预览期间不空闲回收」这条改动**本轮没被触发**（预览 ~50 ms 一帧，凑不满 2 s 空闲） | 属防御性改动；要验证需制造 >2 s 取帧空档（例如让 HTTP 上传变慢） |
| P2 | SD `errno=5` 未在**带 CSV 录制**的场景复现（本轮没按 Button A 录制） | 有了 `dma_min` 后，带录制再跑一轮即可判定；必要时抬 `CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL` 32768 → 65536 |
| P1 | `dma_min` 谷底 2611 B 偏低 | 可选：`CONFIG_FATFS_ALLOC_PREFER_EXTRAM` y → n（去掉 bounce buffer 需求），或加大内部保留 |
| P3 | 640×480 JPEG 仅 ~9 KB，`CAMERA_JPEG_MIN_BYTES = 1024` 门限偏松 | 属画质/产品决策，未改动传感器 JPEG 质量档 |
| P3 | 根治在驱动侧：`esp_video_setup_buffer()` 重建缓冲后补 `TAILQ_INIT(&stream->queued_list/done_list)` | 本项目不落库（避免被组件升级覆盖），仅作为对照实验保留 |

## 八、复现方法

```powershell
# 1) 构建 + 烧录
Start-Process -FilePath cmd.exe -ArgumentList "/c build_idf.bat > build_log.txt 2>&1" -WindowStyle Hidden
# 等 build_log.txt 出现 BUILD_EXIT=0 后再烧录
Start-Process -FilePath cmd.exe -ArgumentList "/c flash_idf.bat COM5 > flash_log.txt 2>&1" -WindowStyle Hidden

# 2) 跑一轮（250 s：8 拍照 + 90 s preview）
& D:\Espressif\python_env\idf5.4_py3.11_env\Scripts\python.exe serial_round.py `
    --port COM5 --seconds 250 --reset `
    --out serial_round_A.txt --trigger-out trigger_round_A.txt

# 3) 归一 + 与服务端对账（用不同文件名再跑一次即可 A/B 对比）
& ...\python.exe analyze_round.py serial_round_A.txt analysis_round_A.txt
& ...\python.exe photos_check.py esp32s3-eye-0001 photos_api.txt
```

> `round2.ps1` 就是「烧录 + 停 10 s + 跑一轮」的封装（`powershell -NoProfile
> -ExecutionPolicy Bypass -File round2.ps1`），它把 `%TEMP%` 指回工程目录，
> 所以 `flash_log.txt` / `serial_round2.txt` / `trigger_round2.txt` 都落在工程根下。

**判读口诀**：`no usable frame` 必须为 0；拍照会话应形如 `frames=1 bad=2 salvaged=1 dup=0`；
`dup` 恒为 0（非 0 说明闸门在防跨会话残留）；`NO-SOI`/`NO-EOI` 若 > 0 则是另一类问题
（真正的起振/时序故障）；`errno=`/`csv write failed` 一起看 `dma_min`。