# ESP32-S3-EYE 上传链路失效分析与修复总结（PSRAM 未启用）

## 问题概述

仿真器（`data_capture_sim/sim/main.py`）推流后，服务端 SQLite 数据库计数会持续增长；
但**真实板卡采集后数据始终无法送达服务端**：

- 仿真器推流 → `total_uploads` 从 1 涨到 4 ✅
- 板卡采集 → `total_uploads` 长时间停在 4，**0 个新 upload** ❌

现象表现为：显示屏与串口都显示采集正常进行，但服务端毫无反应。

---

## 排查过程：被逐一排除的错误假设

定位过程中排除了多个看似合理的方向，记录如下以免重复踩坑：

| 假设 | 验证方法 | 结论 |
|------|----------|------|
| WiFi 未连上 / IP 未获取 | 串口日志 + 监视器脚本抓取 | **排除**：`WiFi connected, IP=192.0.2.11` |
| SNTP 未同步导致时间戳非法 | 串口日志 | **排除**：`System time set via SNTP` |
| 板卡与电脑不在同一网段 | 解析 IP + 子网计算 | **排除**：板卡 `192.0.2.11/24`，服务端 `192.0.2.10`，同属 `192.0.2.0/24` |
| 服务端地址配置错误 | 打印 `CONFIG_SENSOR_SERVER_URL` | **排除**：值为 `http://192.0.2.10:8000/api/v1/upload`，正确 |
| Windows 防火墙拦截入站 | 临时禁用防火墙后重测 | **排除**：行为无变化，随即恢复防火墙 |
| 服务端 `/api/v1/upload` 路由异常 | `curl` 直接 POST 100 条样本 | **排除**：返回 `201 Created` |
| 鉴权失败（401/403） | 核对 token 配置 | **排除**：板卡与服务端 token 均为空串，`verify_token()` 走 no-token 放行分支 |
| 网络不通 | 板卡侧 `esp_ping` 探测服务端 IP | **排除**：`ping 192.0.2.10 success` |

**关键转折**：上述全部排除后，抓取串口日志统计发现——
**347 秒内 `[upload] attempt=` 出现次数为 0**。

也就是说，问题不在网络、不在服务端、不在鉴权，而是**板卡根本没有发起过任何一次 HTTP 请求**。
排查方向由此从「网络层」转向「固件内部为何不进入上传分支」。

---

## 根因分析

**真正根因：8 MB Octal PSRAM 被配置漏掉，导致内部 SRAM 耗尽。**

`sdkconfig` 中只有 `# CONFIG_SPIRAM is not set`，而 ESP32-S3-EYE 硬件配备 8 MB Octal PSRAM。
PSRAM 未启用时，LVGL 内存池与显示双缓冲全部挤在内部 SRAM：

```
boot:      free=60747  min_free=43303  int_largest=43636
display后: free=36355  min_free=35955  int_largest=20480   ← 单块直接跌到 20 KB
```

稳态下仅剩 `free≈36 KB / int_largest≈20 KB`。而一次上传需要把 100 条样本序列化成
cJSON 树（`samples` 数组含 100 个对象 × 4 个字段），所需连续内存远超 20 KB。

### 完整失败链条

```
内部堆耗尽（int_largest 仅 20 KB）
  ↓
wifi: fail to alloc timer                              ← 约 4~6 次/秒
i2c: i2c command link malloc error                     ← 约 10~15 次/秒
  ↓
app_accel_read() 返回非 ESP_OK
  ↓
main.c 采样循环触发 stop_collection_locked("IMU read failed")
  ↓
采集被强制中止，g_sample_count 清零
  ↓
批次永远凑不满 100 条 → upload_task 阻塞在 xQueueReceive → 从不发起 HTTP
  ↓
服务端收不到任何数据
```

### 一个具有误导性的旁证

串口曾输出：

```
Frequency measured: 11214 Hz (Target: 100 Hz, Time: 89 s)
```

`Time: 89 s` 大于当时设备 uptime（仅 12 秒），且 11214 Hz 与目标 100 Hz 相差百倍。
这**不是**独立的频率测量 bug，而是采集被反复「中止 → 重启」后时间基准错乱的产物。
根因修复后该现象随之消失。

### 协议模式为何报 `(0 samples)`

`run_protocol()` 中每个 Face 阶段调用 `start_collection_locked()`，但因 I2C 读失败
立刻被 `stop_collection_locked()` 中止，计数器归零，故所有阶段统一输出：

```
Face +X complete (0 samples)
```

---

## 修复措施

### 1. 启用 Octal PSRAM（核心修复）

**修改位置**：`sdkconfig`（本地生效）+ `sdkconfig.defaults`（版本控制）

将 `# CONFIG_SPIRAM is not set` 替换为完整配置块。其中大部分项由 `kconfgen`
根据 `CONFIG_SPIRAM=y` 自动解析，但 `CONFIG_SPIRAM_IGNORE_NOTFOUND` **不会被自动推导**，需手工加入：

```ini
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y            # ESP32-S3-EYE 为 Octal PSRAM
CONFIG_SPIRAM_TYPE_AUTO=y
CONFIG_SPIRAM_SPEED_80M=y
CONFIG_SPIRAM_BOOT_INIT=y
# 安全网：PSRAM 未检测到时仍可用内部 RAM 启动，避免变砖
CONFIG_SPIRAM_IGNORE_NOTFOUND=y
CONFIG_SPIRAM_USE_MALLOC=y
CONFIG_SPIRAM_MEMTEST=y
# >=16 KiB 的分配优先走 PSRAM（LVGL 内存池、显示双缓冲自动移出内部 SRAM）
CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=16384
CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP=y
# 为必须使用 DMA 内部内存的分配保留 32 KiB（I2C command link、SDMMC、esp_timer）
CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL=32768
```

**为何 `MALLOC_RESERVE_INTERNAL` 是关键**：I2C command link、SDMMC 描述符、
esp_timer 这类分配**必须**位于内部 SRAM（DMA 无法访问 PSRAM）。若不保留专用余量，
大块分配涌入 PSRAM 后仍可能被内部内存压力波及。设为 32768 后，实测
`int_largest` 从「随 SD 挂载/任务创建一路掉到 20,480」变为**恒定 31,744**。

### 2. 将修复同步到 `sdkconfig.defaults`（可复现性）

**原因**：`.gitignore` 第 3 行忽略了 `sdkconfig`，而 `sdkconfig.defaults` 是被 git 跟踪的。
若只改 `sdkconfig`，**该修复不会进入版本库**——换台机器或重新 clone 就会退回原来
内存耗尽的坏状态。

已用 ASCII 追加方式写入（不重写既有字节，避免破坏文件中原有的 GBK 编码中文注释）。

### 3. 新增永久堆遥测（诊断基础设施）

**修改位置**：`main/main.c`

新增 `log_heap()`，同时打印三项指标——只看 `esp_get_free_heap_size()` 会**严重误导**，
因为真正卡住 I2C/WiFi 的是「最大连续内部块」：

```c
static void log_heap(const char *tag) {
    size_t free_total = esp_get_free_heap_size();
    size_t free_int   = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    size_t min_int    = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    size_t largest    = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ESP_LOGI(TAG, "[HEAP] %-22s free=%7u  min_free=%7u  int_free=%6u  int_largest=%6u",
             tag, (unsigned)free_total, (unsigned)min_int,
             (unsigned)free_int, (unsigned)largest);
}
```

> **2026-09-30 起 `[HEAP]` 行的当前格式**（`main/main.c` 的 `log_heap()`）：
> `[HEAP] <where>  free=… min=… int_largest=… | dma_free=… dma_largest=… dma_min=… | psram=…`。
> 新增的 DMA 口径是给 SD 写失败用的：ESP32-S3 的 SDMMC **不能对 PSRAM DMA**，
> FATFS 的 512 B 缓冲落在 PSRAM（`CONFIG_FATFS_ALLOC_PREFER_EXTRAM=y`）时要临时申请
> 512 B `MALLOC_CAP_DMA` 做 bounce buffer，申请失败 → `sdmmc_write_blocks` 返回
> `ESP_ERR_NO_MEM` → `[csv] write failed errno=5`（EIO）。`dma_free`/`dma_largest` 是
> 瞬时值（两次采样之间也会塌），`dma_min` 是历史谷底 —— 2026-09-30 的现场
> `dma_largest` 曾掉到 96 B，而当时内部堆总空闲还有 7 MB。

埋点位置：

| 埋点 | 作用 |
|------|------|
| NVS + WiFi 初始化后 | 基线 |
| `bsp_display_start()` 后 | 量化 LVGL + 显示双缓冲的内存开销 |
| `create_ui()` 后 | 量化 UI 对象开销 |
| `esp_wifi_connect()` 后 | 量化 WiFi 栈开销 |
| WiFi Got-IP 事件内 | 联网完成时的状态 |
| `sampler_task` 每 2 秒 | 长时间运行的泄漏检测 |
| `sampler_task` SD 挂载后 | 量化 SD 栈开销（内部内存的主要消耗者之一） |
| `upload_task` 队列创建后 | 打印 `sizeof(upload_batch_t) × 深度` 最坏占用 |
| `upload_task` cJSON 构建前/后 | **直接量化单次上传的内存峰值**（本次定位的决定性埋点） |

---

## 修改清单

本次修复**仅涉及固件配置与诊断代码，未改动任何业务逻辑、协议或服务端代码**。

| 文件 | 改动 | 是否入库 |
|------|------|----------|
| `sdkconfig` | 启用 Octal PSRAM 全套配置 | ❌ 被 `.gitignore` 忽略 |
| `sdkconfig.defaults` | 同步 PSRAM 配置块（21 行新增） | ✅ **必须入库，否则修复不可复现** |
| `main/main.c` | 新增 `log_heap()` 及 9 处埋点 | ✅ |

诊断辅助脚本（位于 `%TEMP%`，未提交）：

| 脚本 | 用途 |
|------|------|
| `serial_cap.ps1` | 打开 COM5、RTS 脉冲复位板卡、抓取 N 秒启动日志 |
| `monitor.ps1` | 串口 + `/api/v1/health` 双通道并行监控 |
| `dbq.py` | 直接查询 SQLite，绕过 API 层验证数据落库 |

> 注：`git status` 中 `server/main.py`、`server/static/app.js`、`README.md`、
> `PROJECT_STRUCTURE.md`、`main/Kconfig.projbuild` 的改动来自**上一阶段**的
> `/api/v1/window` 端点与 UI 波形工作，不属于本次修复。

---

## 验证结果

### PSRAM 初始化

```
esp_psram: Found 8 MB PSRAM device
esp_psram: Speed: 80MHz
esp_psram: PSRAM initialized, cache is in normal mode.
esp_psram: SPI SRAM memory test OK
esp_psram: PSRAM size: 8388608 bytes (8192 KB)
esp_psram: Memory test: 7615 free aligned bytes
esp_psram: Memory test passed, took 1555 ms
esp_psram: Total available SPI PSRAM size: 8388608 bytes
esp_psram: WiFi/LWIP prefer SPI RAM: yes
```

> 日志中若出现若干条 `PSRAM ID read error`，那是 `TYPE_AUTO` 下探测芯片 ID 的正常输出；
> 紧随其后的 `Memory test passed` 与 `PSRAM size: 8 MB` 证明 PSRAM 已正常工作。

### 堆指标对比

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| `free`（总空闲） | 36,463 | **8,387,731** |
| `int_largest`（最大连续内部块） | **20,480** | **31,744**（恒定） |
| `min_free`（历史最低） | 35,955 | 8,336,987 |
| 上传 cJSON 前后差值 | 无法执行 | 约 9 KB，瞬时即回收 |
| `i2c command link malloc error` | 约 10~15 次/秒 | **0** |
| `wifi: fail to alloc timer` | 约 4~6 次/秒 | **0** |

`int_largest` 的稳定性是最关键的改善：修复前它会随 SD 挂载、任务创建持续下滑；
修复后在 17 分钟连续运行中始终稳定在 31,744 附近。

### 串口统计（连续采集 17 分钟）

| 项目 | 计数 |
|------|------|
| `http=201`（上传成功） | **327** |
| `attempt=[2-9]`（重试） | **0** |
| 上传丢包 / 队列满 | **0** |
| I2C 错误 | **0** |
| WiFi 分配错误 | **0** |
| E 级错误日志 | **0** |
| 重启（`rst:0x`） | **0** |

### 服务端落库对账

| 时刻 | `total_uploads` | `total_samples` | DB 大小 |
|------|-----------------|-----------------|---------|
| 基线 | 4 | 4 | — |
| 16:39:59 | 197 | 19,304 | — |
| 16:43:44 | 393 | 38,904 | 7,577,600 B |
| 16:46:23 | **527** | **52,304** | **10,203,136 B** |

**523 批新增 × 100 样本 = 52,300 样本新增，与 `samples` 表增量严格 1:1 对账，零丢包零重复。**

### 查询与 UI 性能

| 接口 | 结果 |
|------|------|
| `/api/v1/window?seconds=30` | HTTP 200，**113 ms**，返回 2900 点 |
| `/api/v1/health` | HTTP 200，45~47 ms |
| `/ui/` | HTTP 200，6818 B（= `index.html` 磁盘字节数，StaticFiles 无缓存陈旧问题） |

UI 前端逻辑核对（`server/static/app.js`）：

| 行号 | 内容 |
|------|------|
| 12 | `const POLL_MS = 800;` |
| 15 | `const CHART_WINDOW_S = 30;` |
| 212-213 | `cv.getContext('2d')` |
| 370-372 | `'/api/v1/window?device_id=…&seconds=' + CHART_WINDOW_S` |
| 418 | `window.addEventListener('resize', () => drawChart(...))` |
| 425 | `setInterval(poll, POLL_MS)` |

113 ms 的服务端响应远快于 800 ms 轮询周期，不会成为实时性瓶颈。

---

## 遗留问题

以下各项**均未擅自改动**，需按实际情况决定：

### 1. Flash 大小配置不符

```
CONFIG_ESPTOOLPY_FLASHSIZE="2MB"
spi_flash: Detected size(8192k) larger than the size in the binary image header(2048k).
```

芯片实测 8 MB，配置仍为 2 MB。当前 app 分区 1.5 MB，余量仅约 9%。

> **注意**：单纯把 `FLASHSIZE` 改成 `8MB` **不会**扩大 app 分区——
> `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE` 使用固定 CSV，`factory` 分区恒为 `0x177000`。
> 要真正利用额外空间需改用自定义分区表。建议在新增功能前处理。

### 2. WiFi 凭据与服务器地址存在同样的「只存在于 gitignore 文件」问题

`CONFIG_WIFI_SSID` / `CONFIG_WIFI_PASSWORD` / `CONFIG_SENSOR_SERVER_URL` 仅存在于
被忽略的 `sdkconfig` 中，`sdkconfig.defaults` 里没有对应项 →
**重新 clone 后板卡将连不上网、也找不到服务端**（与本次 PSRAM 修复前完全相同的隐患）。

未擅自将密码写入版本库。可选方案：

- 写入 `sdkconfig.defaults`（简单，但密码入库）
- 改用环境变量 / 本地覆盖文件（需自建加载机制）
- 保留手工 `idf.py menuconfig` 配置，并在 README 中明确记录

### 3. 堆遥测输出频率

`log_heap()` 每 2 秒输出一条，串口略显嘈杂。这是**有意保留**的——正是它定位了本次 bug。
如需降噪，可改为每 10 秒，或仅在数值显著变化时输出。

### 4. 其他

- `build_log.txt` 为 `build_idf.bat` 产生的临时文件，建议加入 `.gitignore`
- 协议模式（标定 / 静置等）此前因同一根因输出 `(0 samples)`，**建议重跑一次验证**
- 采集需物理按下 Button A 启停；如需无人值守测试，可考虑增加串口/控制台命令

---

## 诊断方法备忘

### 抓取启动日志（含 PSRAM 初始化）

串口被占用时无法复位板卡，用 RTS 脉冲触发重启：

```powershell
$sp = New-Object System.IO.Ports.SerialPort 'COM5',115200,'None',8,'One'
$sp.DtrEnable = $false; $sp.RtsEnable = $false
$sp.Open()
Start-Sleep -Milliseconds 200
$sp.RtsEnable = $true            # 拉高 RTS → EN 复位
Start-Sleep -Milliseconds 200
$sp.RtsEnable = $false           # 释放 → 板卡重启
```

> **务必设 `DtrEnable = $false`**：DTR 控制的是 GPIO0（boot 模式），
> 拉高会让板卡进入下载模式而非正常启动。

### 绕过 API 直接查库

```powershell
& D:\anaconda\python.exe dbq.py     # 输出 uploads/samples 行数、按设备统计、最近 5 条
```

### 判定上传是否真的发出

```powershell
# 关键：统计 attempt 而非只看 OK
$log | Select-String -Pattern '\[upload\] attempt='   # 0 次 → 问题在固件内部
$log | Select-String -Pattern 'http=(\d+)'            # 非 201 → 问题在服务端/网络
```

---

## 技术要点总结

### ESP32 内存模型

| 要点 | 说明 |
|------|------|
| **内部 SRAM 才是稀缺资源** | ESP32-S3 内部 SRAM 约 512 KB，扣除 ROM / 栈 / DMA 后用户可用仅约 300 KB |
| **PSRAM 默认不启用** | 即使硬件配备 8 MB PSRAM，`CONFIG_SPIRAM` 未开启时完全闲置 |
| **DMA 无法访问 PSRAM** | I2C command link、SDMMC 描述符、部分 WiFi 缓冲必须位于内部 SRAM |
| **`MALLOC_ALWAYSINTERNAL` 阈值** | 小于该值的分配优先内部 SRAM，大于则优先 PSRAM。设 16384 可让 LVGL 池与显示缓冲自动移出 |
| **`MALLOC_RESERVE_INTERNAL`** | 为 DMA 类分配保留内部余量，防止被大块分配挤占 |

### 堆诊断的正确指标

| 指标 | 含义 | 为何重要 |
|------|------|----------|
| `esp_get_free_heap_size()` | 总空闲字节 | **会误导**：总量充足不代表有足够大的连续块 |
| `heap_caps_get_largest_free_block(INTERNAL)` | 最大连续内部块 | **决定性指标**：I2C / WiFi / cJSON 分配失败的真正原因 |
| `heap_caps_get_minimum_free_size(INTERNAL)` | 历史最低水位 | 判断是否存在瞬时尖峰或缓慢泄漏 |

本次 bug 中，`free` 看似还有 36 KB「够用」，但 `int_largest` 仅 20 KB，
不足以构建 100 样本的 cJSON 树——**只看总空闲量会完全错过根因**。

### 排查方法论

1. **先证伪，再定位**：本次 8 个假设全部被证伪后，才把方向从网络层转到固件内部
2. **找「不可能」的数据**：`Time: 89 s` > uptime 12 s 这类自相矛盾的输出，
   往往直接指向状态机被异常打断
3. **区分「没成功」与「没尝试」**：统计 `attempt=` 而非 `OK`，
   一步区分出「请求发出但失败」与「请求根本没发出」
4. **配置必须可复现**：修改被 gitignore 的文件后，务必同步到受版本控制的
   `*.defaults`，否则修复只是「本机偶然生效」

---

## 补记 · 2026-10-08：`[csv] write failed … errno=5` 的红字其实是「重试即恢复」被误报

### 现象

带 CSV 录制（Button A）且同时开直播时，串口反复出现成对的两行：

```
E (…) imu_logger: [csv] write failed #1/20 errno=5 ferror=0 file=/sdcard/DATA_….csv samples=…
                  | dma_free=591 dma_largest=432 int_largest=432
I (…) imu_logger: [HEAP] csv write failed  free=… min=… int_largest=432 | dma_free=1519 dma_largest=1472 dma_min=55 | psram=…
W (…) imu_logger: [csv] write recovered after 1 failed tick(s)
```

逐项读：

| 线索 | 含义 |
|------|------|
| `errno=5` | `EIO`。SD 写失败被 newlib 映射成的值 |
| `ferror=0` | **决定性**：错误标志已被 `clearerr()` 清掉 → 失败点在 `fflush()`，不是 `fprintf()` |
| `samples=` 只前进一点点 | 失败节奏与定时刷盘间隔（`CSV_FLUSH_INTERVAL_MS=500`）一致 |
| `dma_free`/`dma_largest` 掉到几百~1.5 KB、`dma_min=55` | 内部 DMA 子堆瞬时见底 |
| 紧随其后的 `recovered` | **数据一条没丢**：同一 tick 内重试就写成功了 |

### 根因链

定时刷盘交出去的是一段**非 512 B 整数倍**的 CSV 尾巴。FatFs 的直通写只认
「偏移扇区对齐 + 长度 ≥512」，尾巴那一个不满扇区只能走 `FATFS.win` 窗口缓冲；
窗口在 PSRAM（`CONFIG_FATFS_ALLOC_PREFER_EXTRAM=y`），而 ESP32-S3 的 SDMMC
**不能对 PSRAM DMA**，于是 `sdmmc_write_sectors()` 必须临时申请 512 B
`MALLOC_CAP_DMA` 的 bounce buffer。相机（直播/预览，每帧 16 KiB 连续内部 DMA）
正占着内部 DMA 堆时这次申请失败 → `ESP_ERR_NO_MEM` → FatFs `FR_DISK_ERR`
→ `fflush()` 返回 EOF、`errno=5`。

**它是瞬时且自愈的**：`sampler_task` 在同一 tick 内 `clearerr()` + `vTaskDelay(1)` + 重试，
等一拍（相机 DMA 让出）再申请就成功 —— 所以每次红字后面必跟一条 `recovered`。

### 真正的代码缺陷（本次修复）

重试循环**第一次失败就置 `csv_write_failed = true`**，哪怕后续重试成功：

```c
for (int attempt = 0; attempt < CSV_FLUSH_RETRY; attempt++) {
    if (fflush(write_file) == 0) break;
    csv_write_failed = true;      /* ← 重试若成功，这一票"失败"也已经记下了 */
    clearerr(write_file); vTaskDelay(1);
}
```

这与紧邻注释写的「偶发的堆碎片就靠这个挺过去，**只有持续失败才会计入 fail_streak**」
正好相反：一次「重试即恢复」既打了一条 E 级红字、又计了一次 `csv_fail_streak`。

改动（`main/main.c`，`sampler_task`）：

- **只有 `CSV_FLUSH_RETRY` 次重试全部用尽**才置 `csv_write_failed`（真失败，行为不变：
  逐条 ERROR + 连续 `CSV_WRITE_FAIL_LIMIT` 次停采集）；
- 重试成功只累加 `csv_flush_retried`，并在**会话结束**（采样闸门 `gate_open`
  下降沿）时汇总一行 WARN，既不刷屏也不完全静默；
- 失败统计与复位逻辑（`csv_flush_retried = 0`）随新会话一起重置。

### 为什么不直接改 `sdkconfig`

两个「看似对症」的旋钮都被项目现有结论否掉（见 `sdkconfig.defaults` §40-44 / §74-78）：

| 方案 | 否决理由（已实测/已推理） |
|------|--------------------------|
| `CONFIG_FATFS_ALLOC_PREFER_EXTRAM` y → n | 每个打开文件的 4096 B 缓存 + 4096 B 窗口搬到内部 RAM（两个文件 ≈ 12 KiB 常驻），与本板「相机每次 STREAMON 要一块 **16 KiB 连续内部 DMA**」的主要矛盾直接冲突 |
| `CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL` 32768 → 65536 | 实测会把最大连续内部块从 29696 压到 26624，反而更容易让相机 `VIDIOC_STREAMON failed: errno=12` |

也就是说：**主路径本来就是零 bounce**（4096 B 整数倍、扇区对齐的整块下发走直通 DMA），
需要 bounce 的只有定时刷盘的尾巴；而这条尾巴是「按时间刷盘」换来 500 ms 掉电丢失窗口的代价。
用「偶发且自愈的 SD 重试」去换「相机拍照直接失败」不划算，故不动配置。

### 验证

| 项 | 结果 |
|----|------|
| 编译 | `build_idf.bat` → `build_log.txt` 末尾 `BUILD_EXIT=0`；`[4/9] Building … main.c.obj` 正常，仅剩既有的 `_IO/_IOR/_IOW redefined` 告警，无新增 |
| 产物 | `data_capture_sim.bin = 0x172890 / 0x177000`（1% free，未超分区） |
| 真机（待复跑） | 期望：`[csv] write failed … errno=5` 归零；会话结束最多一行 `[csv] session end: N flush(es) needed an in-tick retry …`；`[csv] write recovered …` 也随之消失（同 tick 恢复不再计 streak）。若连汇总行都没有 → 说明该轮 DMA 碎片未到触发点，是更好结果 |
