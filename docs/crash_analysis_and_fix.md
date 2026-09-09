# ESP32-S3-EYE IMU Logger 崩溃分析与修复总结

## 问题概述

固件在运行过程中出现 **Guru Meditation Error: LoadProhibited** 崩溃，崩溃发生在 `lv_strnlen()` 函数中，调用栈指向 `refresh_ui()` → `lv_label_set_text_fmt()`。

---

## 崩溃回溯分析

### 原始 Backtrace
```
Guru Meditation Error: LoadProhibited
  PC: 0x420d38ca (lv_strnlen)
  Cause: Load from 0x00000000 (NULL pointer dereference)

Backtrace:
  lv_strnlen(p, ...)           ← p = 0x00000000 (NULL!)
    → lv_vsnprintf_inner(...)
      → lv_text_set_text_vfmt(...)
        → lv_label_set_text_fmt(s_status_bar, ...)  ← line 199
          → refresh_ui()                              ← line 195
            → wifi_event_handler()                    ← line 281
```

### 根因分析

**真正根因：栈溢出导致的参数破坏**

| 因素 | 说明 |
|------|------|
| **esp_event 默认任务栈小** | `wifi_event_handler` 运行在 esp_event 默认事件循环任务中，默认栈大小仅 **4096 字节** |
| **LVGL 格式化函数耗栈** | `lv_label_set_text_fmt()` → `lv_vsnprintf_inner()` 内部使用 `va_copy` 和多次格式化，消耗大量栈空间 |
| **栈溢出破坏参数** | 栈溢出导致函数调用栈上的参数区域被破坏，`%s` 对应的字符串指针变为 **NULL** |
| **NULL 指针解引用崩溃** | `lv_strnlen(NULL)` 触发 `LoadProhibited` 异常 |

---

## 修复措施

### 1. 从 `wifi_event_handler` 移除 `refresh_ui()` 调用

**原因**：WiFi 事件处理函数运行在 esp_event 小栈任务中，不适合调用 LVGL。

**修改位置**：`main.c` 第 285-287 行

```c
// 原代码
refresh_ui();

// 修改后
/* Do NOT call refresh_ui() here — this runs in the esp_event task (stack ~4KB),
 * while lv_label_set_text_fmt() needs much more stack. The sampler_task
 * (runs every 10ms) will pick up the state change automatically. */
```

**原理**：`sampler_task` 每 10ms 运行一次并调用 `refresh_ui()`，WiFi 状态变化会在下一个采样周期自动反映到 UI 上。

---

### 2. 添加 `s_ui_ready` 标志保护

**原因**：防止在 UI 对象创建完成前调用 `refresh_ui()` 导致空指针访问。

**修改位置**：`main.c` 第 90 行、第 147-149 行、第 267 行

```c
// 全局变量
static bool s_ui_ready = false;

// refresh_ui() 开头检查
if (!s_ui_ready) {
    return;
}

// create_ui() 末尾设置
s_ui_ready = true;
```

---

### 3. 修正 `bsp_display_lock()` 超时参数

**原因**：`0` 表示无限等待（根据 API 文档），之前错误地改成了有限超时。

**修改位置**：`main.c` 第 172 行、第 220 行

```c
// 错误写法
if (!bsp_display_lock(pdMS_TO_TICKS(50))) {  // 可能因锁获取失败导致跳过后面的 UI 创建
    return;
}

// 正确写法
if (!bsp_display_lock(0)) {  // 0 = 无限等待，保证总能拿到锁
    return;
}
```

---

### 4. 优化采样定时精度

**问题**：原始定时方案使用 `delay_us / 1000` 整数截断，导致每周期损失 0~1ms，频率漂移至 ~95Hz。

**改进方案**：相位累加器（phase accumulator）算法

```c
// 使用绝对时间累加，避免累积漂移
int64_t next_wake_us = esp_timer_get_time() + (SAMPLE_PERIOD_MS * 1000);

while (true) {
    // ...采样工作...
    
    // 等待到绝对目标时间
    int64_t delay_us = next_wake_us - esp_timer_get_time();
    if (delay_us > 0) {
        if (delay_us < 2000)
            esp_rom_delay_us((uint32_t)delay_us);  // 短延时用微秒级精确等待
        else
            vTaskDelay(pdMS_TO_TICKS((delay_us + 500) / 1000));  // 长延时四舍五入
    }
    
    next_wake_us += (SAMPLE_PERIOD_MS * 1000);  // 累加固定步长，永不漂移
}
```

**其他优化**：
- UI 刷新频率降低到每 10 个采样周期（~100ms）一次，减少采样路径上的 jitter
- 采样任务锁超时从 200ms 降低到 5ms，提高响应速度

---

## 修改清单

| 修改项 | 行号 | 说明 |
|--------|------|------|
| 新增 `s_ui_ready` 标志 | 第 90 行 | UI 创建完成前阻止刷新操作 |
| `refresh_ui()` 增加 `s_ui_ready` 检查 | 第 147-149 行 | UI 未就绪时安全跳过 |
| `create_ui()` 末尾设置 `s_ui_ready = true` | 第 267 行 | 对象全部创建完成后才允许刷新 |
| 从 `wifi_event_handler` 移除 `refresh_ui()` | 第 285-287 行 | 防止在 esp_event 小栈任务中调用 LVGL |
| `bsp_display_lock(50)` → `bsp_display_lock(0)` | 第 172 行 | 0=无限等待，保证总能拿到锁 |
| `bsp_display_lock(pdMS_TO_TICKS(50))` → `bsp_display_lock(0)` | 第 220 行 | 同上 |
| 采样定时改为相位累加器 | 第 653-746 行 | 无漂移、微秒级精度 |
| UI 刷新降频 | 第 749-751 行 | 每 10 个采样周期刷新一次 |

---

## 验证建议

1. **长时间运行测试**：连续运行数小时，观察是否再次崩溃
2. **采样频率测量**：查看串口日志中的频率测量输出，应稳定在 100Hz ± 1Hz
3. **WiFi 重连测试**：断开 WiFi 热点，观察设备是否能自动重连且 UI 正常更新
4. **SD 卡压力测试**：长时间采集数据，确认 SD 卡写入不会导致采样周期丢失

---

## 技术要点总结

### ESP32 任务栈大小

| 任务类型 | 默认栈大小 | 注意事项 |
|----------|------------|----------|
| esp_event 默认循环 | 4096 字节 | 不适合调用 LVGL、复杂格式化函数 |
| 用户创建任务 | 由 `xTaskCreate` 指定 | 建议 ≥ 4096 字节，含 LVGL 操作建议 ≥ 6144 字节 |
| LVGL 任务 | 由 `lvgl_port` 配置 | 通常较大，适合 LVGL 操作 |

### LVGL 线程安全

- LVGL 对象操作必须在 LVGL 上下文中进行（通过 `bsp_display_lock()` 获取锁）
- 从其他任务调用 LVGL 函数时，必须先获取显示锁
- 避免在 interrupt 或 small-stack 任务中调用 LVGL

### FreeRTOS 定时精度

- `vTaskDelay()` 精度受 FreeRTOS tick 频率限制（默认 10ms）
- 需要高精度定时使用 `esp_timer` + 忙等 (`esp_rom_delay_us()`)
- 相位累加器算法可避免累积漂移