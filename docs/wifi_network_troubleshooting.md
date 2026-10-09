# WiFi / DHCP 故障排查与自愈（板子「突然没网」怎么办）

> 对应代码：`main/main.c`（`wifi_event_handler()` / `wifi_init_sta()` / `wifi_health_check()`）、
> `sdkconfig.defaults`（`CONFIG_LWIP_DHCP_DOES_NOT_CHECK_OFFERED_IP`、`CONFIG_LWIP_LOCAL_HOSTNAME`）。
> 适用症状：**板子看起来活着（LCD 有画面、COM 口在），但服务端 `last_seen` 不再刷新、
> 网页全部功能失效、串口日志停在 WiFi 关联之后不再往下走。**

---

## 一、先做 3 个零成本判定（30 秒内定性）

| 观察 | 结论 |
|------|------|
| 服务端 `/api/v1/devices` 里 `last_seen` 还在刷新 | 板子和服务端都正常，问题在别处（页面/浏览器缓存） |
| 主机 `ping <板子IP>` 通、`arp -a` 里能看到板子 MAC | 网络层正常，问题在服务端或防火墙 |
| **主机 `ping` 不通、ARP 里找不到板子 MAC** | 板子没有 IP（L3）或没关联（L2），看第二节 |
| **LCD 底部状态栏（`SD:OK 100Hz:OK Live:OFF`）是空白** | `refresh_ui()` 从未执行 ⇒ **卡在 `app_main()` 的启动早期**（WiFi/SD/IMU/相机，这些仍会阻塞启动），看第三节 |
| 状态行 `Idle  NET:--`、`Time: … NTP:--` | 连 AP 都没关联上（L2 没建立）⇒ 查 SSID/密码/AP，看第二节的 `Retrying WiFi connection` |
| 状态行 `Idle  NET:assoc`、`Time: … NTP:--` | **已关联、但一直没有 IP**（DHCP 没发租约 —— 本仓库实测到的故障）⇒ SNTP 连请求都发不出去，看第三节 |
| 状态行 `Idle  NET:192.0.2.x`、`Time: -- (up 00:03:12) NTP:try` | **有 IP，SNTP 在退避重试** ⇒ 网段里 DNS/NTP 不可达，改 `CONFIG_SNTP_SERVER`（可填 IP）或看第五节 |
| `Time: -- (up 00:03:12)` 且秒数在走 | 启动流程走完了，只是没联网（SNTP 没同步）。这个字段每秒刷新一次，**秒数还在走 = 板子没卡死**，只是对不上时间 |
| LCD 状态栏正常、`Time:` 是 `2026-09-23 10:19:59` 这样的日期 | 对时成功（DNS + NTP 都可达）；若时间不刷新则看第五节的对时日志 |

> 状态行右侧的 `NET:` token 由 `refresh_ui()` 维护（`sampler_task` 每秒刷新一次，
> 另有状态变化时的即时刷新）：`--` = 未关联，`assoc` = 已关联但无 IP，其余为当前 IP；
> `NTP:` token 只在未同步时出现（`--` = 还没有 IP 发不出请求，`try` = 有 IP 在重试）。
> 另外 2026-09 修了一个纯坐标 bug：底部状态栏原来在 y=255、协议行在 y=230，
> 而 BSP 的屏只有 **240×240** —— 状态栏整行在屏外、协议行只剩半行，
> 所以「LCD 上看不到状态栏」并不等于 `refresh_ui()` 没跑过。现在 10 行重排进
> 6~210，全部可见。

> 状态栏（`SD:… 100Hz:… Live:…`）由 `sampler_task` 每秒刷新一次，而 `sampler_task`
> 是**最后一个**被创建的任务，所以「状态栏空白」= 启动流程没走完，这是最快的现场判据
> （旧文档写「每 100 ms」，实际采样循环里是 1 s 一次 + 状态变化时的即时刷新）。

## 二、串口日志关键字 → 结论对照表

| 日志 | 含义 | 处理 |
|------|------|------|
| `wifi:connected with 431, aid = 1` 之后再无输出 | 关联成功但 **DHCP 没发租约**（本仓库实测到的故障），旧固件会在这里死等 | 见第三节；新固件会打印 `WiFi: no IP after 20 s …` 并继续启动 |
| `Got IP: 192.0.2.x` | 拿到 IP，网络层 OK | 查服务端/防火墙 |
| `Retrying WiFi connection... (n/5) reason=…` | 关联失败在重试，`reason` 是 `wifi_err_reason_t`（`201`=NO_AP_FOUND，`205`=CONNECTION_FAIL，`15`=4WAY_HANDSHAKE_TIMEOUT…） | 检查 SSID/密码/AP 是否在；`205` 多为密码错或 AP 拒绝 |
| `WiFi still down after 6 attempts (reason=…) - background retry every 5 s` | 超过 5 次，转入后台慢重连（**新固件不会放弃**） | 不用管，等网络恢复即可自愈 |
| `WiFi lost IP (DHCP lease lost)` | 租约被路由器收回 | 自动重新申请 |
| `No IP 15 s after association - restarting DHCP client (n/3)` | 自愈逻辑在重启 DHCP 客户端 | 若反复出现，见第三节根因 |
| `DHCP gave no lease - forcing re-association` | 重启 DHCP 3 次无效，强制断开重连重走一遍 | 同上 |
| `Static IP fallback applied: 192.0.2.200/255.255.255.0 gw 192.0.2.254 dns 223.5.5.5` | 本网段没有 DHCP 服务应答，Kconfig 打开了静态兜底，固件停掉 DHCP 客户端改用固定地址（下一步就是 `Got IP:` + 对时成功） | 正常；换网段/地址冲突时改 Kconfig 的 `Static IP Fallback` 四项（第八节） |
| `Static IP fallback: invalid Kconfig values …` | Kconfig 里填的地址不是合法 IPv4 ⇒ 兜底本次开机不生效（只报一次） | 改 `CONFIG_STATIC_IP_ADDR/NETMASK/GATEWAY` |
| `Wifi: no IP after 20 s (link_up=1 …) - booting offline` | 启动阶段没等到 IP，但**继续启动**了（板子离线可用） | 板子会在后台自愈 |
| `SNTP task started: server=pool.ntp.org, retry 5000 ms -> 60000 ms` | 后台对时任务已启动（**不再阻塞启动流程**） | 正常 |
| `SNTP no sync yet (ESP_ERR_TIMEOUT) - next try in 5000 ms (link_up=1 ip=1)` | 本轮没对上时间（网段里 DNS/NTP 不可达），会自动退避重试（5 s→60 s 上限，永不放弃） | 确认网段能访问 NTP；把 `CONFIG_SNTP_SERVER` 改成网内 NTP 服务器或**直接填 IP**（DNS 坏掉也能对时） |
| `SNTP time synchronized: 2026-09-23 10:19:59 (UTC+8)` | 对时成功，LCD 的 `Time:` 随即变成北京时间 | 正常；此后 lwIP 每小时自动再同步一次 |
| `Guru Meditation` / `Backtrace` | 固件崩溃 | 与网络无关，抓 backtrace 用 `addr2line` 解 |
| `ClearCommError failed (PermissionError...)` | **主机侧**串口瞬断（多开监视器 / USB 线松），并把板子复位了一次 | 只开一个 monitor，换 USB 线/口；不是网络问题 |

## 三、根因

### 0. 现场实测：本网段当前**没有 DHCP 服务在应答**（网络侧，不是固件缺陷）
用 `server/dhcp_probe.py`（纯 Python，不需要管理员权限）在这台 PC 的网卡上直接发
DHCP DISCOVER，三种客户端 MAC 全部 8 s 内零回应：

| 探测用的 chaddr | 结果 |
|---|---|
| `94-a9-90-1c-6f-b4`（本板 STA） | no reply |
| `94-a9-90-1c-70-9c`（同网段另一块**在线**的 ESP32，192.0.2.103） | no reply |
| `54-01-4a-5e-26-e5`（本机网卡） | no reply |

结论：`192.0.2.0/24` 这个二层段现在收不到任何 DHCP OFFER，**任何新接入的客户端都拿不到 IP**；
网段里还能 ping 通的设备靠**长租约或静态 IP** 硬撑（本机以太网也是静态 `192.0.2.14/24`，
`arp -a` 里 192.0.2.x 全部显示「静态」）。板子上午 10:22 还能正常上报（服务端
`/api/v1/devices` 里 `last_seen=2026-09-23 10:22:40`），说明当时 DHCP 是好的 —— 故障点在
AP / 路由器 / DHCP 服务侧，**固件改不掉它**。

由此确定固件必须满足的两条底线：**不能把「等到 IP」当成启动的前置条件；不能假设一次连上
就永远在线。** 这正是下面 4 个缺陷要修的原因。

### 1. DHCP 客户端默认先做地址冲突探测 → 拿到租约也可能被静默丢弃（加固）
LWIP 默认在收到 DHCP ACK 后会先对这个地址发 ARP 探测；**只要网段里有任何设备（含陈旧 ARP
条目、手工静态 IP）应答该地址，客户端就丢弃租约并重新 DISCOVER**，日志上一片安静：
既没有 `Got IP`，也没有 `DISCONNECTED`。多台 ESP 共存 + 静态 IP 混用的实验网里很容易踩到，
所以这里按「加固」处理（本次实测的主因是上一节的「根本没有 DHCP 应答」）。

修复：`sdkconfig` + `sdkconfig.defaults` 里选中 **`CONFIG_LWIP_DHCP_DOES_NOT_CHECK_OFFERED_IP=y`**。
⚠ IDF 5.4 用 choice `LWIP_DHCP_CHECKS_OFFERED_ADDRESS` 三选一（`ARP_CHECK` 默认 /
`ACD_CHECK` / `NOT_CHECK_OFFERED_IP`），**不能写 `CONFIG_LWIP_DHCP_DOES_ARP_CHECK=n`**：
CMake 会打印 `Trying to set symbol … to n, but it is currently selected by choice …`
然后把设置丢掉（改完看构建日志前几行就能发现）。
同时把主机名从默认的 `espressif` 改成 `esp32s3-eye-0001`（本网段实测有 3 台 `94:a9:90:*` 设备同名）。

### 2. `wifi_init_sta()` 用 `portMAX_DELAY` 死等 → 一个 DHCP 卡住，整机卡死
`app_main()` 的顺序是 `create_ui → wifi_init_sta → SNTP → SD → IMU → 相机 → 按键 → HTTP 服务 → 各任务`，
所以只要 WiFi 那一步不返回，**相机、按键、页面全都不会初始化**：按 Button A 没有反应、
网页打不开、LCD 状态栏永远空白 —— 现场表现就是「板子死了」，其实只是卡在等 IP。

修复：等待上限 `WIFI_CONNECT_TIMEOUT_MS`（20 s），超时打印
`WiFi: no IP after 20 s … - booting offline` 后继续启动；联网交给 `wifi_health_check()` 在后台完成。

### 3. `s_wifi_connected` 在断连后永不恢复 → 表面在线、功能全废
旧代码只在 `wifi_init_sta()` 里把它置 `true`（开机一次），`DISCONNECTED` 事件里置 `false`，
而 `GOT_IP` 分支**没有恢复**。后果：一次几秒的 WiFi 抖动之后，即使重连成功，
`[upload] skip: WiFi not connected`、`periodic` 上报停摆、`task_poll` 直接 `continue`
（网页拍照全废）、直播 `[live] WiFi not connected`、LCD 永远显示 `WiFi disconnected`。

修复：`IP_EVENT_STA_GOT_IP` 分支里恢复 `s_wifi_connected = true` + `set_status_locked("WiFi connected")`
（在 `s_state_mutex` 保护下；不在事件回调里调 `refresh_ui()`，它栈需求大，`sampler_task` 会兜底刷新）。

### 4. 重连 5 次后彻底放弃 → 一次抖动 = 永久离线
旧代码 `if (s_retry_num < EXAMPLE_ESP_MAXIMUM_RETRY)` 才 `esp_wifi_connect()`，第 6 次之后
只置 `WIFI_FAIL_BIT` 就再没有任何重连调用（全工程只有 2 处 `esp_wifi_connect()`）。

修复：5 次以内立刻重连；超过后只降噪 + 用 `WIFI_FAIL_BIT` 解除启动阶段的等待，
之后由 `wifi_health_check()` **每 5 s 继续重连，永不放弃**。

### 5. SNTP 只在开机同步一次 + 死等 60 s → 网络恢复后时间永远不对
`initialize_sntp()` 旧实现：`esp_netif_sntp_init/start` → `esp_netif_sntp_sync_wait()` 循环
最多 30×2 s（60 s）→ 成功写时间、失败把 LCD 的 `Time:` 写成固定的 `SNTP FAILED` 就结束。
两个后果：
- **开机瞬间没有 IP（DHCP 慢 / 本网段没有 DHCP 应答）就注定失败，之后网络恢复也不再同步**，
  时间一直停在 1970，只能重启 —— 而重启还是同样的顺序，永远失败。服务端随后会拒绝这类
  板子上报（`ts_ms` 早于任务创建时刻），手动采集任务在网页上直接失败。
- 离线开机时白白卡住启动流程 60 s：SD/IMU/相机/HTTP 全部晚起 60 s，而
  `wifi_health_check()` 在 `sampler_task` 里、`sampler_task` 又是最后一个被创建的任务，
  自愈也跟着被推迟。

修复（`main.c` 的「SNTP 对时」一节）：
- `initialize_sntp()` 只 `setenv("TZ","CST-8")` + 建 `sntp_sync_task` 后**立刻返回**（不再阻塞）。
- `sntp_sync_task` 只在 `s_wifi_connected && s_wifi_link_up`（有 IP）时才尝试；
  失败按 5 s→10 s→…→60 s 退避**一直重试**；成功则刷新 LCD 时间并每 30 s 复查。
- 重试用 `esp_netif_sntp_start()`（内部 `sntp_stop()+sntp_init()`），保证每次都是**真正的新请求**：
  lwIP 的 `sntp_init()` 只在 PCB 为空时才发首次请求，`sntp_request()` 则每次都会重新解析域名
  （见 `components/lwip/lwip/src/apps/sntp/sntp.c`）。
- 判定「已同步」只看 `time(NULL) >= 1600000000`（2020-09-13）。未同步时 `time()` 是从 1970
  起算的上电时长，所以这个判据与 lwIP 内部的同步信号量无关，重启/时钟被改都能正确识别。
- LCD 的 `Time:` 字段改由采样循环**每秒**刷新（`refresh_time_str()`）：未同步时显示
  `-- (up 00:03:12)`（uptime，秒数在动）。这样「时间不对」和「板子卡死」在 LCD 上就能直接区分。
- NTP 服务器地址移到 Kconfig（`CONFIG_SNTP_SERVER`，默认 `pool.ntp.org`）：网内 DNS 坏掉时
  直接填 IP 也能对时，不必改代码。

```powershell
# 复现/验证（板子先离线开机，再接网络）
# 期望：LCD 的 Time: 立刻显示 "-- (up 00:00:0x)" 并在动 → 串口出现
#   SNTP task started: server=pool.ntp.org, retry 5000 ms -> 60000 ms
#   SNTP no sync yet (ESP_ERR_TIMEOUT) - next try in 5000 ms (link_up=0 ip=0)
# 网络接上后（不重启板子）：
#   Got IP: 192.0.2.x → SNTP time synchronized: 2026-09-23 10:19:59 (UTC+8)
#   ← LCD 的 Time: 变成同一时刻（说明时间在运行中补上了）
```

### 附：状态现在是两层
- `s_wifi_link_up`（L2，`STA_CONNECTED`/`DISCONNECTED` 维护）
- `s_wifi_connected`（L3，有 IP）

只有这样才可能发现「关联上了但一直没有 IP」这种此前无法观测的中间态。

## 四、自愈逻辑一览（`wifi_health_check()`，`sampler_task` 每 5 s 调用）

```
s_wifi_connected == true              → 正常，清零计数
未关联（s_wifi_link_up == false）      → 立刻 esp_wifi_connect()（永远不放弃）
已关联但 15 s 仍无 IP                  → esp_netif_dhcpc_stop/start 重启 DHCP（最多 3 次）
再 15 s 仍无 IP（第 2 次放弃 ≈ 100 s）  → Kconfig 打开 Static IP Fallback 时：停 DHCP 客户端、
                                        套固定 IP + DNS（第八节），状态置「有 IP」
                                        没打开时：esp_wifi_disconnect() → 事件里立刻重连
                                        （重走 DISCOVER，永远不会放弃）
已进入静态模式后又丢了地址（重连）      → 直接把固定地址补回去（lwIP 会在链路断开时清空 IP，
                                        而 DHCP 客户端一直是停着的）
```

另外 `esp_wifi_set_ps(WIFI_PS_NONE)` 关掉了 modem sleep：本项目持续取样 + 周期上传 + 250 ms 推流，
省电没意义，而默认 modem sleep 会让 RTT 抖动到 200 ms 以上（同网段另一台 ESP 实测 ping 225~256 ms）。

## 五、复现/验证步骤

```powershell
# 1) 起服务端（0.0.0.0:8000）
cd server ; python main.py

# 2) 只开一个串口监视器（不要和 VS Code 串口监视器/PuTTY 同时开）
idf.py -p COM5 monitor        # 期望看到 Got IP: 192.0.2.x / WiFi power save / Camera ready

# 3) 主机侧确认
ping <板子IP>                                    # 通
curl http://127.0.0.1:8000/api/v1/devices        # last_seen 在刷新
curl http://192.0.2.14:8000/api/v1/live          # active=False 且 devices=[]

# 3.5) 板子拿不到 IP 时，先用探针判断「这个网段到底有没有 DHCP 服务」
#      （纯 Python、不需要管理员权限；结果与板子无关，是网络侧的事实）
python server/dhcp_probe.py 94-a9-90-1c-6f-b4 8   # 板子 STA 的 MAC
python server/dhcp_probe.py 54-01-4a-5e-26-e5 8   # 本机网卡 MAC（对照）
# 两个都 no reply → 是网段/DHCP 服务的问题，不是板子的问题

# 4) 自愈回归：把 AP 关掉/拔掉路由器 30 s 再恢复
#    期望日志：WiFi still down after 6 attempts … → WiFi offline - reconnecting …
#             → WiFi associated … → Got IP: …
#    恢复后周期上报与网页拍照应自动恢复（不需要重启板子）
```

## 六、现场用过的定位命令（主机侧）

```powershell
ipconfig                                   # 本机 IP/掩码/网关
ping 192.0.2.254                           # 网关是否通（区分「本机没网」和「板子没网」）
arp -a | findstr 94-a9-90                  # 找板子/其它 ESP（板子 STA MAC 见启动日志 wifi:mode : sta (…)）
for /L %i in (1,1,254) do @start /b ping -n 1 -w 300 192.0.2.%i   # 整段扫描后配合 arp -a 用
reg query HKLM\HARDWARE\DEVICEMAP\SERIALCOMM                      # COM 口是否还在（板子有没有被拔/掉电）
netstat -ano -p tcp | findstr :8000                              # 服务端是否在监听
netsh advfirewall firewall show rule name=all dir=in status=enabled   # 允许 python.exe 入站（公用配置文件）
```

## 七、改 `sdkconfig` 的注意事项 & 仍未处理的观察项

- `sdkconfig` 是 **kconfig 生成的**文件：手写的普通注释行会在下一次 CMake 重配置时被清掉
  （只有 `# CONFIG_X is not set` 这种形式会保留）。所以「为什么这么改」的解释要写在
  `sdkconfig.defaults` 里，`sdkconfig` 只留最终值。
- 改完 `sdkconfig` 直接 `idf.py build` 会触发 CMake 重配置 + 几乎整体重编译（几分钟），
  并且**重配置会把无效的设置回滚**——所以第一次构建日志的前 20 行要当检查点看
  （例如 choice 成员写错会有 `Trying to set symbol … is currently selected by choice …`）。
- 每次启动都会打印 `phy_init: Saving new calibration data due to checksum failure or outdated
  calibration data, mode(0)`：PHY 校准数据没有持久化到 `phy_init` 分区（可能与该分区被整片擦写过有关）。
  不影响功能，但每次开机都会重新做 RF 校准，会拖慢启动几百毫秒。

## 八、静态 IP 兜底（网段里根本没有 DHCP 服务）

**症状**：LCD 状态行长期显示 `Idle  NET:assoc`（已关联、永远没有 IP），
`Time:` 停在 `-- (up …) NTP:--`；串口反复出现

```
No IP 15 s after association - restarting DHCP client (1/3) … (3/3)
DHCP gave no lease - forcing re-association
```

**为什么固件救不了**：第二节的 DHCP 重启/重关联只能应对「客户端自己把租约弄丢了」，
而如果整个二层段没有 DHCP 服务应答（本仓库 2026-09 实测 `server/dhcp_probe.py`
对三种客户端 MAC 全部 no reply），任何客户端都拿不到地址 —— 这是网络侧的事。

**兜底做法**（Kconfig → `Static IP Fallback (subnets where DHCP never answers)`）：

| 配置项 | 作用 |
|---|---|
| `CONFIG_USE_STATIC_IP_FALLBACK` | 总开关（默认 `n`；本地 `sdkconfig` 里按现场打开） |
| `CONFIG_STATIC_IP_ADDR` | 固定地址，**必须是本网段空闲地址**（无人应答其 ARP） |
| `CONFIG_STATIC_IP_NETMASK` / `CONFIG_STATIC_IP_GATEWAY` | 本网段掩码与路由器（板子要靠它访问 NTP 和 PC 上的 FastAPI） |
| `CONFIG_STATIC_IP_DNS` | **必须显式给**：DHCP 死了就没有 DNS 服务器，否则 SNTP 解析不了 `CONFIG_SNTP_SERVER`（把它直接填 IP 也可以） |

行为：`wifi_health_check()` 先给 DHCP 完整窗口（3 次重启客户端 + 1 次强制重关联，
约 100 s），确认没救才 `esp_netif_dhcpc_stop()` + `esp_netif_set_ip_info()` +
`esp_netif_set_dns_info()`，并打印

```
W Static IP fallback applied: 192.0.2.200/255.255.255.0 gw 192.0.2.254 dns 223.5.5.5
I Got IP: 192.0.2.200          ← 由 esp_netif_set_ip_info() 自己 post 的 GOT_IP 事件
I SNTP time synchronized: 2026-09-24 … (UTC+8)
```

要点：
- 地址**只到重启为止**；链路断开重连后 lwIP 会清空 IP，而 DHCP 客户端已停，
  所以兜底逻辑会在「已关联但没 IP」时把地址补回去（不会退回纯 DHCP）。
- 想回到纯 DHCP：把 `CONFIG_USE_STATIC_IP_FALLBACK` 关掉重新编译即可。
- 静态地址和 DHCP 池不要重叠，否则可能出现地址冲突（`arp -a` 里能看到两个 MAC 抢同一个 IP）。
- 板子仍是「离线可用」的：拿不到 IP 也不影响采样、SD 落盘、LCD 与手动采集。

**本地 sdkconfig 里的现场值**（`sdkconfig` 已被 .gitignore 忽略，committed 的
`Kconfig.projbuild` / `sdkconfig.defaults` 只保留占位与说明）：

```
CONFIG_USE_STATIC_IP_FALLBACK=y
CONFIG_STATIC_IP_ADDR="192.0.2.200"        # 同网段另一块 ESP 用 .103、本机 .14，故避开
CONFIG_STATIC_IP_NETMASK="255.255.255.0"
CONFIG_STATIC_IP_GATEWAY="192.0.2.254"
CONFIG_STATIC_IP_DNS="223.5.5.5"
```

### 8.1 改完怎么确认真的生效了

本地踩过一次坑：手改 `sdkconfig` 打开开关后，下一轮 CMake confgen 把**新加进来的符号**
按 Kconfig 默认值重写回 `n`，`string` 四项也回到占位值 `192.168.4.x`；于是固件里
`STATIC_IP_FALLBACK_ENABLED` 是 0，兜底根本不触发（而 `CONFIG_WIFI_SSID` 这类**早就存在**
的手改值不会被重写，所以很难察觉）。

任何重写 `sdkconfig` 的动作都会导致它：VS Code ESP-IDF 扩展的 SDK Configuration 编辑器
保存、删掉 `sdkconfig` 让 CMake 重新生成、以及新加 Kconfig 符号后的首轮 confgen。

所以改完**必须 build 一次**，再确认编译期真正看到的配置：

```
findstr STATIC_IP sdkconfig build\config\sdkconfig.h
```

- `build/config/sdkconfig.h` 里要有 `#define CONFIG_USE_STATIC_IP_FALLBACK 1` 和
  `#define CONFIG_STATIC_IP_ADDR "192.0.2.200"`；只有 `sdkconfig` 里写了 `=y` 不算数。
- `sdkconfig` 里同一项**不要同时留** `# … is not set` 和 `…=y` 两行（confgen 只认最后一行，
  人却容易看错）。
- 开关关掉时（committed 默认）固件行为与以前完全一致：不做任何静态地址尝试。