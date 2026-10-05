# 静态 IP 兜底复盘：条件编译缺陷与 sdkconfig 回写陷阱

> 记录两件事：`main/main.c` 里 `STATIC_IP_GIVEUP_LIMIT` 的**作用域缺陷**
> （`#if` 内定义的宏被运行期 `if` 引用 → 开关关闭时必然编译失败），
> 以及 ESP-IDF confgen **按 default 回写新 Kconfig 符号**、把开关悄悄改成 `n`
> 的陷阱。两个问题叠加才表现出「现场构建报未声明标识符」。
> 排查与验证方法见 `docs/wifi_network_troubleshooting.md`。

## 一、报错的直接原因：条件编译的"作用域"写错了一行

问题的核心在 `main/main.c` 里一段很短的预处理代码。**修复前**的结构是这样的（示意）：

```c
#if CONFIG_USE_STATIC_IP_FALLBACK          /* ① 只有打开开关时，才进入这块 */
#define STATIC_IP_GIVEUP_LIMIT      2      /* ② 宏在这里面定义 */
#define STATIC_IP_FALLBACK_ENABLED  1
#else
#define STATIC_IP_FALLBACK_ENABLED  0
#endif
```

然后在**普通函数体**里（`wifi_health_check()`，不是 `#if` 区域）这样用：

```c
if (STATIC_IP_FALLBACK_ENABLED && s_dhcp_giveup_count >= STATIC_IP_GIVEUP_LIMIT) {
    ... /* 放弃 DHCP，切静态 IP */
}
```

**关键点在这里：** 预处理阶段（`#if`）和编译阶段（`if`）是两套完全不同的机制：

- `STATIC_IP_FALLBACK_ENABLED` 是预处理常量，当开关关闭时它被展开成 `0`；
- 但 `&&` 的"短路"是**运行期语义**，编译器在编译 `if` 语句时必须先把**两个操作数都当成合法的 C 表达式解析**——即使左边是常量 `0`，它也要求右边的 `s_dhcp_giveup_count >= STATIC_IP_GIVEUP_LIMIT` 里的每个标识符都有定义；
- 开关关闭时宏 `STATIC_IP_GIVEUP_LIMIT` 从未被定义 → 编译器报"未声明的标识符" → `-Werror` 下直接失败。

**结论：只要开关是关的，这份代码永远编译不过。** 这是真正意义上的代码缺陷，不是环境问题。

## 二、为什么这个缺陷一直没被发现？—— 两层"欺骗"

**第一层：以前的构建都是绿的。** 因为我之前构建时开关是**打开**的，`#if` 分支被编译，宏存在，一切正常。也就是说，**"构建通过"只能证明我当时验证的那种配置是对的**，并不能证明"仓库默认配置"是对的。这是单点验证的典型盲区。

**第二层：你的 `sdkconfig` 被悄悄改回了默认值。** 这才是把你推向失败配置的"触发器"。

## 三、`sdkconfig` 为什么会被改回去？（这是一个很隐蔽的坑）

你的 `sdkconfig` 原来被我改成了打开开关 + 实验室网段地址，但你构建时它变成了：

```
# CONFIG_USE_STATIC_IP_FALLBACK is not set
CONFIG_STATIC_IP_ADDR="192.168.4.200"     ← 占位默认值
```

排查的关键线索：**`CONFIG_WIFI_SSID="<你的SSID>"`、服务器 URL `…192.0.2.10…` 这些人工改过的老配置项全都还在**（本文里的真实 SSID 与网段已按仓库脱敏规则换成占位值，`192.0.2.0/24` 是 RFC 5737 文档网段）。如果 `sdkconfig` 是被整体重新生成的，它们也会丢。所以不是"整体重置"，而是：

> **ESP-IDF 的 CMake 配置生成（confgen）在发现 `sdkconfig` 中缺少新加入的 Kconfig 符号时，会按 `default` 值补写进去；而我手改的那次发生在"新符号第一次经过 confgen"之前，于是就被默认值覆盖了。**

已经存在的旧符号不会被重写（因为它们已在 `sdkconfig` 里），所以现象表现得像"只丢了我加的那几项"。这个坑很阴：看起来像"随机丢失配置"。

**于是链路完整了：** 新符号被回写成 `n` → 你的构建走到"关闭开关"的配置 → 撞上第一节里那个真实的代码缺陷 → 构建失败。**两个问题叠加才表现出你看到的报错。**

## 四、"静态 IP 兜底"这个功能本身在做什么（背景）

现场实测的形态是：板子 WiFi **关联成功**，但所在网段**完全没有 DHCP 服务器应答**。后果是一条必然失败的链条：

```
关联成功 → 永远拿不到租约 → 没有 IP → SNTP 连 pool.ntp.org 的域名都解析不了
        → 时间永远不同步 → LCD 上永远显示 "-- (up …)"
```

重启 DHCP 客户端、强制重关联都救不了。因此这个功能的策略是：

1. `wifi_health_check()` 先给 DHCP **完整的机会**（约 3 次重启 DHCP 客户端 + 1 次强制重关联，每次 45~60 s）；
2. 出现 `DHCP gave no lease` 达到 `STATIC_IP_GIVEUP_LIMIT`（2 次）后，才**停掉 DHCP 客户端**，用 `esp_netif_set_ip_info()` 直接套上静态地址；
3. `esp_netif` 在 dhcpc 停止后会**自己补发 `IP_EVENT_STA_GOT_IP`**，所以兜底复用了原有的 GOT_IP 处理路径（`s_wifi_connected` 同步置位，避免 5 秒健康检查把刚拿到的地址拆掉）；
4. 因为 DHCP 死了就**没有 DNS**，静态兜底必须显式配 DNS（`223.5.5.5`）；若该网段出不了外网，就把 `CONFIG_SNTP_SERVER` 改成字面 IP，绕开域名解析。

## 五、我具体改了什么

| 文件 | 改法 | 目的 |
|---|---|---|
| `main/main.c`（209–218 行） | 把 `#define STATIC_IP_GIVEUP_LIMIT 2` **移到 `#if` 外面无条件定义**，只有 `STATIC_IP_FALLBACK_ENABLED` 继续由 Kconfig 控制；注释里解释了"为什么运行期 `if` 的引用逼着它必须无条件定义" | 让**开/关两种配置都能编译**，根除报错 |
| `sdkconfig`（git 忽略） | 重新写回 `CONFIG_USE_STATIC_IP_FALLBACK=y`、`192.0.2.200` / `255.255.255.0` / `192.0.2.254` / `223.5.5.5`，并删掉重复的 `is not set` 行 | 让板子在实验室网段能用静态兜底 |
| `docs/wifi_network_troubleshooting.md` | 新增 **§8.1「改完怎么确认真的生效了」** | 记录这个 confgen 回写陷阱与验证方法，避免下次再踩 |
| `Kconfig.projbuild` / `sdkconfig.defaults` | **保持不变**：`default n` + 注释掉的占位地址 | 不把实验室网段强行烧进默认值（否则别人的板子会出问题） |

## 六、怎么验证"真的修好了、真的生效了"

1. **关掉开关编译**：`main.c` 零错误 → 证明那个缺陷是真修好了，而不是被开启开关掩盖。
2. **打开开关完整构建**：`BUILD_EXIT=0`；`data_capture_sim.bin` = `0x16f110 / 0x177000`（剩余 2%）。
3. **确认编译器真的看到配置**：`build/config/sdkconfig.h` 里是 `#define CONFIG_USE_STATIC_IP_FALLBACK 1` 和 `CONFIG_STATIC_IP_ADDR "192.0.2.200"`（只看 `sdkconfig` 不够，必须看这个生成头文件）。
4. **确认产物真的含兜底代码**：在 `.bin` 里能搜到只属于"开启分支"的字符串——`Static IP fallback applied`、`WiFi connected (static IP)`、`NET:%s`、`NTP:%s`。若这些字符串不在，说明开关没生效。
5. **确认配置稳定**：构建（confgen 跑过）之后 `sdkconfig` 仍是 `=y` → 说明这次是一次性回写，现在编辑是稳定的。
6. 临时文件全部删除，工作区干净。

## 七、经验教训（以后避免同类问题）

- **`#if` 里定义的宏，不要在普通 `if` 里用**（反之亦然）；如果必须在运行期判断，就把它 **无条件定义**，让开关只控制"数值/使能标志"，而不是控制"符号存不存在"。
- **新增 Kconfig 符号后，务必检查编译产物的配置头文件**（`build/config/sdkconfig.h`），不要只信 `sdkconfig` 文本。
- **两种开关状态都要构建一次**（至少让 CI/脚本各跑一次），单配置构建通过不等于代码没问题。
- 不要同时保留 `# CONFIG_X is not set` 和 `CONFIG_X=y` 两行（会看错、也可能被后写的覆盖）。
- 用 VS Code 的 SDK Configuration 编辑器会重写 `sdkconfig`，改完配置要回头复查生成头文件。

## 八、你现在可以直接做的验证

```
flash_idf.bat        （项目约定：COM5，日志写入 build_log.txt）
```

- 烧写前先确认 `192.0.2.200` 空闲（`ping` 无回应）；
- DHCP 死掉时的预期序列：`NET:assoc` → `No IP 15 s (1/3)…(3/3)` → `DHCP gave no lease - forcing re-association` → `Static IP fallback applied: 192.0.2.200/…` → `Got IP: 192.0.2.200` → `SNTP time synchronized: …(UTC+8)`，LCD 1 秒内显示 IP 与日期、不重启；
- 若 LCD 显示 `NET:192.0.2.200` 但 `NTP:try`（日志 `SNTP no sync yet … link_up=1 ip=1`），就把 `sdkconfig` 的 `CONFIG_SNTP_SERVER` 换成字面 IP 重新构建，**不需要改代码**。