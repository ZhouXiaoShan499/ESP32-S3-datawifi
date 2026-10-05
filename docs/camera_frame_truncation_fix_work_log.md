# 直播/预览帧「下半幅灰带 + 中段横向撕裂」：根因与修复（工作记录）

> - **记录对象**：板端 `main/main.c`（OV2640 DVP JPEG，`/dev/video2`）；
>   受影响端点 `POST /api/v1/live`（长按 Button A 的直播）、本地预览、`kind=camera` 拍照落盘
> - **记录日期**：2026-10-05
> - **一句话结论**：花屏不是「渲染问题」也不是「时序/信号问题」，而是**帧尾被截断**。
>   链路有两环：① 本工程链接的是 **ESP-IDF 5.4.3 自带**的 DVP 驱动，它把 VSYNC 当帧边界、
>   收到的字节数少算了帧尾 → `bytesused=0` + `V4L2_BUF_FLAG_ERROR`(0x41) 成为**每帧常态**；
>   ② app 的兜底自校验 `camera_jpeg_span()` 扫的是**整块复用缓冲**，于是扫到的是**上一帧残留的
>   EOI**，把「本帧前缀 + 旧帧尾巴」当成一整帧上传 ⇒ 下半幅灰带、中段错位色带。
>   **修法**：每次把 mmap 缓冲 QBUF 还给驱动之前，把整块缓冲写 0（PSRAM ⇒ `esp_cache_msync` C2M）。
>   真机验证：`diag … span == written` 完全相等（9787/10543/10565），新照片
>   **2400/2400 MCU** 且肉眼干净。
> - **证据文件**：修复前后的帧样本、熵解码输出、构建与烧录日志都是**本机一次性产物**，
>   已随 2026-10-05 的仓库整理移出仓库（`.gitignore` 现已忽略 `/tmp_*` 与 `/flash_log*.txt`）；
>   本文只保留可复现的做法与判据数字，复跑方法见第七节。
> - **配套文档**：直播侧设计见 `docs/camera_live_stream.md`（文末已追加同一结论）；
>   坏帧/拍照治理的历史过程见 `docs/camera_bad_frame_fix_work_log.md`（**其「done 列表残留元素」
>   的根因段落已被本文推翻**）；拍照链路设计见 `docs/realtime_photo_capture.md`。

---

## 一、问题现象（要修什么）

| # | 现象 | 观测面 |
|---|------|--------|
| 1 | 直播/预览每帧都是「顶部几十像素正常 → 往下一条条**横向色带**与零星错位小块 → 最底部约 25 px **纯灰**」 | Web 页面「摄像头实时画面」、板端 LCD 本地预览 |
| 2 | 同样形态的坏图被**落盘**：`server/photos/25.jpg`（红黑条带 + 底部灰块） | 照片画廊 |
| 3 | 串口**没有任何** `NO-EOI` / `NO-SOI` / `JPEG size` 报错，`flags=0x41 used=0` 是常态 | 串口日志 |
| 4 | 「下半幅灰带」在 JPEG 与 PNG（导出的同一帧）里**逐行统计完全一致** ⇒ 坏在 JPEG 字节里，不是渲染 | 逐行 RGB 统计 |

第 4 条是本次定性的起点：既然 JPEG 和 PNG 一样坏，就往**字节层**查，而不是查渲染/时序。

## 二、离线取证：把「花屏」量化成「少了多少个 MCU」

关键工具是**自己写的 baseline JPEG 熵解码器**（一次性本地脚本，用完已清理）：
它按 JPEG 语法走 Huffman 解码到 MCU 级，能回答一个决定性的问题 ——
**这张 JPEG 里到底有多少个 MCU 是「真数据」，从哪个 MCU 开始变成垃圾/结束**。
（只看文件大小或肉眼比对分辨率是分不出来的：截断的 JPEG 依然是合法文件。）

| 检查项 | 做法 | 结论 |
|--------|------|------|
| 逐行统计 | 对 JPEG 与 PNG 逐行统计 RGB | 完全一致 ⇒ **坏在 JPEG 字节，不是渲染** |
| 熵解码数 MCU | 自写 baseline 熵解码器（不依赖 libjpeg），数 MCU | 直播坏帧只解出 **2273/2400 MCU（94.7%）**，位预算 **99.99%** 用光，最后一个 MCU **中途 EOF**；`25.jpg` 只 2034/2400（84.8%）⇒ **帧尾那截（约几百字节）不在文件里** |
| 字节级审计 | 扫熵段里的非法 `FF`、找是否有第二个 EOI、看尾部结构 | 熵段**没有非法 FF**、**没有第二个 EOI**、`suffix` 只有 2~21 B ⇒ **不是「新帧头+旧帧尾」式的错位拼接**，也不是位错误/传输损坏 |
| 拼接点平滑度 | 逐 MCU 行算 `mean abs(dDC)` | 熵流本身**合法且连续**（0.2~5）⇒ 「数据是好的，只是**少了尾巴**」 |
| 16 张历史照片批量扫一遍 | 同上去数 MCU | 16 张 **2400/2400**，`25.jpg` 只 2034/2400 ⇒ **拍照路径也会坏**，不是直播特有 |

> 判读口诀：**MCU 数不足 + 位预算用光 + 尾部偏移平滑 = 尾部截断**；
> 若是「错位拼接」则会在熵流里出现非法 `FF`/第二个 EOI，或 `splice` 点有明显跳变（本次都没有）。

## 三、根因（真机 + 源码对账，两环缺一不可）

### 3.1 驱动侧：`used=0 / flags=0x41` 是「每帧如此」的机制

1. **本工程实际链接的是 ESP-IDF 5.4.3 自带的 DVP 驱动**
   `components/esp_driver_cam/dvp/src/esp_cam_ctlr_dvp_cam.c`，
   **不是** `managed_components/espressif__esp_cam_sensor/src/driver_dvp/esp_cam_ctlr_dvp_cam.c`
   —— 后者被 `esp_cam_ctlr_dvp_ext.h` 的
   `#if (ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5,5,2))` 整个挡掉：在 5.4.3 上
   `ESP_CAM_CTLR_DVP_ENABLE` 未定义，该文件编译成**空对象**，
   `esp_cam_new_dvp_ctlr_ext()` 只是宏别名到 IDF 的实现。
   **核对方法（别再靠行号猜）**：`build/config.env` 里 `IDF_VERSION=5.4.3`；
   `build/compile_commands.json` 里能找到该 `.c` 但**没有任何** `ESP_CAM_CTLR_DVP_ENABLE` 定义。
   ⇒ 之前文档/记忆里所有「`managed_components/…/esp_cam_ctlr_dvp_cam.c:754-758`」式的引用**全部作废**。
2. IDF 5.4.3 的 HAL 在 `cam_hal_init()` 里执行 `cam_ll_enable_vsync_generate_eof(hw, 1)`
   （`components/hal/cam_hal.c:67`）：**VSYNC 直接产生 GDMA 的 EOF**，即「一帧」的边界完全由
   VSYNC 决定。对 JPEG 这种**变长、与行时序无关**的数据流，帧尾那部分字节会落在边界之外，
   于是 `esp_cam_ctlr_dvp_dma_get_recv_size()`（把描述符 `dw0.length` 相加）算出来的长度
   **短于整帧**。
3. 于是 `esp_cam_ctlr_dvp_get_jpeg_size()`（在收到的区域里往回找 `FF D9`）**找不到 EOI** ⇒
   返回 0 ⇒ `trans.received_size = 0` ⇒（5.4.3 的 `esp_cam_ctlr_recv_frame_done_isr()`
   **无条件**回调 `on_trans_finished`，没有 `if (received_size)` 判断，这一点与 ext 版相反）
   ⇒ `esp_video_done_buffer(n=0)` ⇒ `element->valid_size = 0` ⇒
   `esp_video_ioctl.c:198` 填 `bytesused = 0` 并置 `V4L2_BUF_FLAG_ERROR`(0x41)。
   **⇒ 这就是历史上「`used=0/flags=0x41` 是常态」的真正机制**；该驱动**没有** NO-EOI/NO-SOI 日志，
   所以串口上「一条错都不报」，此前据此判断「没有证据说明是驱动问题」是不成立的。

### 3.2 app 侧：真正吃掉画面的是「兜底扫描扫到了上一帧的残留」

- `camera_jpeg_span()`（app 的自校验兜底）扫的是**整块 307200 B 的 mmap 缓冲**，
  而不是「本帧 DMA 实际写入的长度」——因为驱动交回的 `bytesused` 常年是 0，只能自己扫。
- 缓冲是**复用的**（3 个 mmap 缓冲轮转）：没被本帧 DMA 覆盖的部分**还是上一帧的数据**，
  里面自然有上一帧的 `FF D9`。
- 于是自校验「找到」的其实是**旧帧的结尾**，上传/落盘的是
  **「本帧前缀 + 旧帧尾巴」** ⇒ 下半幅灰带 + 中段错位色带/宏块。
- 旧帧比本帧短多少，就被切掉多少：直播场景约 5%（2273/2400 MCU），
  `25.jpg` 约 15%（2034/2400 MCU）。

### 3.3 与旧结论的对照（重要，避免再走回头路）

| 旧说法（2026-09-29/30 的记忆与文档） | 本次对账 |
|--------------------------------------|----------|
| 坏帧来自「`esp_video_setup_buffer()` 重建缓冲后 done 列表残留元素」 | **推翻**：5.4.3 的驱动**每帧无条件**回调 `on_trans_finished`，`valid_size=0` 是**结构性的**，不需要残留元素 |
| 「链接的是 `esp_cam_sensor` 的 DVP 驱动」 | **推翻**：那是 ≥5.5.2 才编进来的 ext 驱动，5.4.3 上编译成空对象 |
| 「`used=0/flags=0x41` 说明没有证据支持驱动问题」 | **推翻**：该驱动没有任何 NO-EOI/NO-SOI 日志，无日志 ≠ 无问题 |
| 「自校验兜底是可靠的解法」 | **部分保留**：兜底方向对，但**必须先保证残留区不含旧 EOI**，否则兜底就是「拼旧帧尾巴」 |

## 四、修复（只动 app：`main/main.c`）

### 4.1 核心改动：`camera_wipe_map_locked()`

```c
static void camera_wipe_map_locked(uint32_t idx)   /* main/main.c:1859 */
{
    if (idx >= s_cam.buf_count || !s_cam.map[idx] || s_cam.map_len[idx] == 0) {
        return;
    }
    memset(s_cam.map[idx], 0, s_cam.map_len[idx]);
    if (esp_ptr_external_ram(s_cam.map[idx])) {          /* 缓冲在 PSRAM */
        esp_cache_msync(s_cam.map[idx], s_cam.map_len[idx],
                        ESP_CACHE_MSYNC_FLAG_DIR_C2M);   /* 必须写回，见下 */
    }
}
```

**语义**：每次把 mmap 缓冲**还给驱动（QBUF）之前**，把整块缓冲写 0。
于是残留区不可能再出现 `FF D9`，`camera_jpeg_span()` 只可能扫到
**本帧 DMA 真写进去的那一个 EOI** —— 要么拿到完整帧，要么（真被截断时）明确判为坏帧丢弃，
**再也不会把旧帧尾巴拼上来**。

**为什么必须 `esp_cache_msync` C2M**：缓冲在 PSRAM，memset 会留在 cache 里；
如果不主动写回（C2M），脏 cache 行会在 DMA 把新帧写进同一地址**之后**被回写，
把刚到的新帧数据冲掉。（esp_video/驱动对同一块缓冲用的是 M2C 反向同步。）

### 4.2 调用点（6 处，覆盖所有归还路径）

| 位置 | 场景 |
|------|------|
| `main.c:2014` | 新建会话的首轮 QBUF（`REQBUFS`/mmap 之后、`STREAMON` 之前） |
| `main.c:2246` | `camera_resync_locked()` 清 done 列表时，逐个「残留元素」入队前 |
| `main.c:2380` | 暖机帧丢弃（`warmup`） |
| `main.c:2391` | 坏帧回收（`dropping bad frame`） |
| `main.c:2413` | 好帧但拷贝 `malloc` 失败（真 OOM）→ 先还缓冲再报错 |
| `main.c:2424` | **好帧**：`memcpy` 拷走之后、QBUF 之前 |

**代价**：每帧多一次 307200 B 的 memset + cache 写回（直播 2 fps、预览 ~4 fps，可忽略）。

### 4.3 配套诊断（低成本、可复跑）

新增 `camera_zero_run_end()`（`main.c:1880`）+ 常量 `CAMERA_ZERO_RUN_MIN`(64) /
`CAMERA_DIAG_SALVAGE_FRAMES`(12)，在每个会话**头 12 个「兜底帧」**上打印：

```
[cam] diag idx=2 driver_used=0 span=9787 written=9787 head=FF D8 FF E0
```

- `written` = 写 0 之后「已写入区」的边界（第一段连续 ≥64 个 0 之前）= **本帧 DMA 真写进去的长度**
- `span`    = 自校验找到的 EOI 位置（0 = 没找到 ⇒ 本帧没写完）
- **`span == written`** ⇒ 帧完整（EOI 就在 DMA 写入数据的末尾）；
  `span == 0` ⇒ 帧尾（含 EOI）压根没进缓冲 ⇒ 应判为坏帧

新增头文件：`#include "esp_cache.h"` / `#include "esp_memory_utils.h"`（`main.c:75-76`）。

---

## 五、真机验证（2026-10-05，COM3）

构建：ESP-IDF 5.4.3 `idf.py build` 通过（`build_log.txt`）；烧录：`flash_log2.txt` 收尾于
`Hard resetting via RTS pin…`，板子复位后重新连上 WiFi 并继续上报。

### 5.1 决定性证据：`span == written`

修复后串口（`kind=camera` + 预览各一次会话）：

| 日志 | 读数 | 判读 |
|------|------|------|
| `diag idx=2 driver_used=0 span=9787 written=9787` | 9787 == 9787 | 帧完整 |
| `diag idx=0 driver_used=0 span=10543 written=10543` | 10543 == 10543 | 帧完整 |
| `diag idx=0 driver_used=0 span=10565 written=10565` | 10565 == 10565 | 帧完整 |

**`span` 与 `written` 逐个字节相等** ⇒ **DMA 其实把整帧（含 EOI）都写进了缓冲**，
只是驱动（§3.1）没把长度算全；写 0 之后 app 拿到的是**完整帧**而不是「旧帧尾巴」。

### 5.2 预览会话

同一个会话连续 35 帧，自校验长度 **10533~10591 B**（波动正常）；
修复前同场景扫到的「旧 EOI 位置」只有约 9000 B ⇒ 那些帧平均被切掉约 1.3 KB。

### 5.3 端到端（服务端落盘 → 离线复检）

| 服务端 | 字节数 | 离线熵解码 | 肉眼 |
|--------|--------|-----------|------|
| `photos/26.jpg` | 10519 B | **OK 2400/2400 MCU**（100.0%），`unread tail=0 B` | 干净，无撕裂、无灰底 |
| `photos/27.jpg` | 9824 B | **OK 2400/2400 MCU**（100.0%），`unread tail=0 B` | 干净，无撕裂、无灰底 |

对照修复前的坏图 `photos/25.jpg`（当时抓到的直播坏帧样本也是同一形态）：
**2034/2400 MCU（84.8%）**、位预算 99.99%、红黑条带 + 底部灰块。

> 附带观察（非证据）：2026-09-30 那轮「成功」的照片是 8.9~9.3 KB，
> 本次同分辨率照片是 9.8~10.5 KB —— 与「当时也一直在拼旧帧尾巴」一致，
> 但场景不同、不能单独作为结论。

### 5.4 未覆盖项

直播（长按 Button A）与预览共用同一个 `camera_grab_locked()`，因此**同一修复**；
但按钮无法远程触发，本轮**没有单独复跑直播上传**（建议现场长按 Button A 后在 `/ui/`
看画面确认一次）。

---

## 六、遗留与下一步

| 优先级 | 事项 | 状态 / 建议 |
|--------|------|-------------|
| P1 | 驱动仍报 `used=0/flags=0x41`（我们没改 IDF） | 已知、可接受；根治见下一行 |
| P1 | 彻底干净：升级到 **IDF ≥ 5.5.2**，让 `esp_cam_ctlr_dvp_ext`（JPEG 专用：`cam_vs_eof_en = 0` + 按 `cam_rec_data_bytelen` 分块）生效 | 升级后 `camera_wipe_map_locked()` 建议**保留**作保险 |
| P2 | 诊断日志 `diag …` 每会话打 12 行 | 成本可忽略；确认稳定后可把 `CAMERA_DIAG_SALVAGE_FRAMES` 调 0/1 |
| P2 | 直播路径未单独跑一轮 | 现场长按 Button A 目视确认（或按第七节抓一段串口 + 页面截图） |
| P3 | 历史文档里的旧根因段落 | 已在 `docs/camera_live_stream.md` 文末追加更正、`main.c` 内注释加「2026-10-05 更正」；`docs/camera_bad_frame_fix_work_log.md` §七 的「残留元素」行按本文理解即可 |

---

## 七、复现与复跑方法

```powershell
# 0) 环境：D:\Espressif\python_env\idf5.4_py3.11_env（自带 pyserial）

# 1) 构建 + 烧录（长任务必须起独立进程，否则会被终端里下一条前台命令 ^C 掉）
Start-Process -FilePath cmd.exe -ArgumentList "/c build_idf.bat > build_log.txt 2>&1" -WindowStyle Hidden
# 等 build_log.txt 出现 "Project build complete." / BUILD_EXIT=0
Start-Process -FilePath cmd.exe -ArgumentList "/c flash_idf.bat COM3 > flash_log2.txt 2>&1" -WindowStyle Hidden
# 等 flash_log2.txt 出现 "Hard resetting via RTS pin..."
# （build_log.txt / flash_log*.txt / tmp_* 都已在 .gitignore 里，放仓库根不会误提交）
```

```bat
:: 2) 让相机跑起来（免按键：kind=preview 与直播共用同一套会话代码）
::    这段请在 cmd.exe 里跑（PowerShell 里 curl 是 Invoke-WebRequest 的别名，要写 curl.exe）
curl -s -X POST -H "Content-Type: application/json" -d "{\"device_id\":\"esp32s3-eye-0001\",\"kind\":\"preview\"}" http://127.0.0.1:8000/api/v1/tasks
curl -s -X POST -H "Content-Type: application/json" -d "{\"device_id\":\"esp32s3-eye-0001\",\"kind\":\"camera\"}"  http://127.0.0.1:8000/api/v1/tasks

:: 3) 抓串口看判据：span 是否等于 written（抓串口的小脚本见下）
python tmp_serial.py COM3 20 tmp_serial.txt
findstr /n /c:"diag" tmp_serial.txt

:: 4) 取回落盘照片 + 离线复检（必须 2400/2400 MCU）
curl -s "http://127.0.0.1:8000/api/v1/photos?device_id=esp32s3-eye-0001&limit=2" -o tmp_photos.json
curl -s http://127.0.0.1:8000/api/v1/photos/27 -o tmp_new_photo\27.jpg
```

`tmp_serial.py`（一次性、8 行；`/tmp_*` 已在 `.gitignore` 里）：

```python
import sys, time, serial
port, secs, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
s = serial.Serial(port, 115200, timeout=0.3)
s.dtr = False; s.rts = False
end = time.time() + secs
with open(out, "w", encoding="ascii", errors="replace") as fh:
    while time.time() < end:
        fh.write(s.read(8192).decode("ascii", "replace"))
s.close()
```

**MCU 级复检**：一帧 2400 个 MCU，**填满 2400 个才算完整**。仓库里不带熵解码器（那是本次的
一次性取证脚本，已清理），日常可用任一**严格**解码器粗筛 —— 截断的帧会直接报错：

```bat
python -c "from PIL import Image; Image.open(r'tmp_new_photo\27.jpg').load(); print('OK')"
:: 截断帧 → OSError: image file is truncated（宽松解码器只打 Corrupt JPEG data: premature end）
:: 亦可 ffmpeg -v error -i tmp_new_photo\27.jpg -f null - 看是否在 EOI 附近报错
```

要精确计数，就按第二节的做法自写一个 baseline 熵解码器（约 200 行：Huffman 表 → MCU 循环 → 数 EOF）。

**判读口诀**：
`diag … span == written` ⇒ 帧完整（修复生效）；
`span == 0` ⇒ 本帧真被截断（应看到 `dropping bad frame`，属正确行为，不是回归）；
`salvaged` 仍会增长（兜底仍是主路径，驱动没改）；
照片若出现「与上一张逐字节相同」会被新鲜度闸门当残留丢掉（`dup_drops++`）。

---

## 八、本次变更文件

| 文件 | 变更 |
|------|------|
| `main/main.c` | 新增 `camera_wipe_map_locked()`（6 处调用）、`camera_zero_run_end()` 诊断、`CAMERA_ZERO_RUN_MIN` / `CAMERA_DIAG_SALVAGE_FRAMES`、两个头文件；旧「残留元素」注释段前加「2026-10-05 更正」 |
| `docs/camera_live_stream.md` | 文末追加同一根因/修复/验证结论（本文的精简版）+ 指向本文的链接 |
| `docs/realtime_photo_capture.md` | §四「一轮对照」与 §五「坏帧兜底」处补「2026-10-05 更正/补充」+ 指向本文的链接 |
| `docs/camera_frame_truncation_fix_work_log.md` | **本文（新增；已收录进 `README.md` / `PROJECT_STRUCTURE.md` 的 docs 索引）** |
| `.workbuddy/memory/2026-10-05.md`、`.workbuddy/memory/MEMORY.md` | 记忆索引（非入库文件） |
| `tmp_*.py` / `tmp_*.txt` / `tmp_*.json` / `tmp_live_frames/` 等 | 一次性取证脚本与证据，**未入库**：2026-10-05 整理仓库时已移出，`.gitignore` 新增 `/tmp_*` 与 `/flash_log*.txt`（复核：`git ls-files -i -c --exclude-standard` 应为空） |