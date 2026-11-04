# 视觉推理与反馈闭环（第 8 周）

> 对应课程第 8 周「让视觉推理结果变成可理解、可追溯的反馈」，并顺带补上第 7 周
> 唯一没做的一项：**镜头遮挡 / 画面不可用判别**。
>
> 目标：板端拍回的照片在**入库那一刻**就有结构化结论（能不能用、为什么不能用、
> 该做什么），页面上能按「正常 / 低分 / 错误 / 未知」四类看样例，人能一键纠正，
> 纠正结果反过来量化「模型有多自信地犯错」。
>
> 相关代码：`server/vision/`（推理引擎）、`server/train/`（数据合成 + 训练）、
> `server/models/vision/`（模型产物）、`server/main.py`（7 个接口 + 两张表）、
> `server/static/*`（第 8 个 tab）、`server/test_vision.py`（15 项自测）。

---

## 一、结论先行

| 要回答的问题 | 结论 | 证据 |
|---|---|---|
| 照片入库能不能自动出结论？ | 能。`store_photo` 落库后同事务外立刻推理，幂等命中不重跑 | `test_vision.py` 第 3/6 项 |
| 三种状态分得开吗？ | 合成测试集 1200 张全对（`availability_acc=1.0`） | `models/vision/train_report.json` |
| 结论人能看懂吗？ | 三档话术由服务端唯一一份 `describe()` 产出，页面直接显示，不自己拼 | `server/vision/decision.py` |
| 人能不能纠正？ | 能，`verdict × corrected_label` UPSERT，一照片只留最新一条 | `test_vision.py` 第 9/10 项 |
| 纠正能不能量化模型？ | 能，「高分误判率」只统计 `decision=ok` 里被人工判错的比例 | `GET /api/v1/vision/summary` |
| 不可用能不能闭环？ | 能，`/vision/recapture` 建 `kind=camera` 任务，板端领到即重拍，回传后自动重推理 | `test_vision.py` 第 13 项 |

**一句话**：不是「加了个分类器」，而是把「判定 → 解释 → 人工纠正 → 重拍 → 再判定」
接成了一条能自己转起来的闭环，并且环上每一段都有可核对的数字。

---

## 二、链路总览

```
板端 POST /api/v1/photos (JPEG)
        │
        ▼
   store_photo()  ── 落盘 + photos 行（原逻辑，不动）
        │
        ▼  落库成功后（异常只打印，绝不影响照片本身入库）
   analyze_photo(photo_id)
        │  vision.predict(jpeg_bytes) → label / score / decision
        │                              / scene / probabilities / 版本 / 耗时
        ▼
   vision_results 表（photo_id 主键 ⇒ 天然幂等）
        │
        ├──▶ GET  /api/v1/vision/results       列表（device / decision / label 过滤）
        ├──▶ GET  /api/v1/vision/results/{id}  单条详情（含人话结论 feedback）
        ├──▶ GET  /api/v1/vision/summary    四类计数 + 混淆矩阵 + 高分误判率
        │
        ├──▶ POST /api/v1/vision/results/{id}/feedback   人工纠正（UPSERT）
        └──▶ POST /api/v1/vision/recapture               不满意 → 建重拍任务 → 回到最上面
```

关键设计：**推理永远不阻塞、不失败上传**。模型缺失、JPEG 解不开、torch 抛异常，
都只写进 `vision_results.error`，照片照样 201 入库（`test_vision.py` 第 5/14 项）。

---

## 三、模型

| 项 | 值 |
|---|---|
| 网络 | MobileNetV3-Small（ImageNet 预训练）+ 两个头 |
| 头 1 | `head_avail`：3 类可用性 `clear / blocked / degraded` |
| 头 2 | `head_scene`：2 类场景 `indoor / outdoor`（`blocked` 样本用 `-1` 掩掉，不参与损失） |
| 输入 | 224×224，ImageNet 均值方差，`resize(short=256) → center_crop(224)`（训练/推理同一份实现） |
| 损失 | `CE(avail) + 0.5 × CE(scene)`，blocked 的 scene 损失被掩码剔除 |
| 训练 | AdamW lr=3e-4 wd=1e-4，CosineAnnealing，6 epoch，batch=32，**CPU 约 78 s/epoch** |
| 运行时格式 | **TorchScript**（`trace` + `freeze`，3.56 MB），不用 ONNX |
| 版本 | `vision-mnv3-v1.2.0`（`labels.json` 记录，随每次推理写进结果行） |

### 为什么运行时不用 ONNX

试过，装不上：`onnx` 会把 numpy 顶到 2.x，拖垮本机 scipy / streamlit；降级到
`onnx==1.16.2` 撞 `DLL load failed`，`onnx==1.23.2` 撞 protobuf 版本、再撞段错误。
训练脚本里 ONNX 导出**保留为「尽力而为」**（缺 `onnxscript` 就跳过并记进报告），
运行时一律走 TorchScript：单文件、无额外依赖、`torch.jit.load` 直接吃。

### 三档口径（`server/vision/decision.py`）

| decision | 条件 | 页面文案 | 建议动作 |
|---|---|---|---|
| `ok` | score ≥ **0.75** | 「画面可用」 | 无需处理 |
| `low` | 0.55 ≤ score < 0.75 | 「把握不足」 | 建议重拍 |
| `rejected` | score < **0.55** | 「未知」 | 重拍或人工标注 |

**拒识时 `label` 一律为 `NULL`**——宁可不给答案，也不给一个 51% 的答案让人误会。
阈值是闭区间下界，写死在 `labels.json` 里，换模型即换阈值，服务端不硬编码。

---

## 四、数据集与四类样例

底图是 8297 张已标注的室内/室外老照片（`D:\Datasets\house\indoor_out`），
**按底图分 split**（同一张底图的 clear/blocked 版本绝不会跨 train/val），
抽样 800/200/200 张底图，每张派生 6 条（三类各 2 条）⇒ 4800/1200/1200 条样本。

退化样本**在 `__getitem__` 里现场合成**，不落盘：每个 epoch 的遮挡形状、模糊半径、
噪声强度都不同，模型学的是「画面被挡」这件事，而不是记住某几张固定图。

| 类别 | 合成方式 | 真实对应 |
|---|---|---|
| `clear` | 亮度抖动 + 轻微失焦(≤1.2) + 暗角 + 传感器噪声 + JPEG 有损(质量 28–62) | 正常拍到内容，但受板端成像质量限制 |
| `blocked` | ①纯色大面积遮挡 ②失焦压暗 ③强暗角 ④半遮挡留缝 ⑤**全平场**（含纯黑） | 手掌/衣物/桌面盖住镜头 |
| `degraded` | ①重度模糊 ②过曝 ③欠曝 ④强噪声 ⑤低对比雾化 | 对错方向、逆光、脏镜头 |

`clear` 里那些「低质量但可用」的增广是**第二版才加的**：第一版只做亮度抖动，结果
模型把真机 10 张里的 5 张判成 `degraded` —— 训练域是档案馆扫描件，部署域是
ESP32 的 640×480 JPEG，模型以为「不锐利 = 不可用」。加入成像质量模拟后，clear 与
degraded 的分界从「是否锐利」变成「还能不能看清内容」，那才是业务真正关心的。

`blocked` 的「全平场」分支是第三版补的，而且是分两步走错的：

| 版本 | 改了什么 | 真机表现 | 结论 |
|---|---|---|---|
| v1.0.0 | clear 只做亮度抖动 | 10 张里 5 张判 `degraded` | 域不一致：把「糊」当成「不可用」 |
| v1.1.0 | clear 加板端成像质量；blocked 加**暗**平场 | 暗场灯管帧 → `blocked 0.83`（对）；亮绿平场帧 → `clear 0.958`（**自信地漏检**） | 只补暗平场，等于教模型「平且暗 = 遮挡」 |
| v1.2.0 | 平场帧补全**亮场**（120–215 灰阶，含偏色与亮带） | 亮绿平场帧 → `degraded 0.769`；纯黑图 → `blocked 0.994` | 平场两档都要有，否则漏检方向最危险 |

第一版训练集里没有「value≈常数、方差≈0」的帧，一张纯黑图会让三类概率打平、落进拒识；
第二版只补了暗平场，结果亮平场（手掌被灯照亮，正是最常见的盖镜头姿势）被当成 clear。
教训是：**数据的缺口只会以「自信地错」的形式暴露，不会以「不知道」的形式暴露。**

---

## 五、服务端接口

新增两张表（`init_db()` 里建，老库升级安全，只 `CREATE TABLE IF NOT EXISTS`）：

```sql
vision_results(photo_id INTEGER PRIMARY KEY, device_id, label, score, decision,
               scene_label, scene_score, probabilities, model_name, model_version,
               backend, latency_ms, error, created_at)
vision_feedback(photo_id INTEGER PRIMARY KEY, verdict, corrected_label, note,
                created_by, updated_at)
```

`photo_id` 做主键 ⇒ **一张照片永远只有一条结论**，重跑推理是 `UPSERT` 而不是堆积。

| 接口 | 作用 | 要点 |
|---|---|---|
| `GET /api/v1/vision/status` | 模型是否就绪、版本、后端、阈值 | 页面靠它区分「未就绪」和「没结果」 |
| `POST /api/v1/vision/analyze` | 补跑一次推理 | 传 `photo_id`，或只传 `device_id` 取最新一张（重拍后立刻复核） |
| `GET /api/v1/vision/results` | 列表 | `device_id` / `decision` / `label` 过滤，`limit` 1–200，非法值 400 |
| `GET /api/v1/vision/results/{id}` | 单条详情 | 带 `feedback{title,text,action}`，人话结论只有服务端一份 |
| `POST /api/v1/vision/results/{id}/feedback` | 人工纠正 | `verdict ∈ correct/wrong/unknown`；`wrong` 必须给 `corrected_label` |
| `POST /api/v1/vision/recapture` | 重拍闭环 | 建 `kind=camera` 任务，板端 `/tasks/next` 能领到 |
| `GET /api/v1/vision/summary` | 汇总 | 四类计数 + 混淆矩阵 + 复核准确率 + **高分误判率** |

列表与汇总都 `JOIN photos`：**照片删了，结论跟着消失**，不留孤儿行。

### 高分误判率

```
只取 decision = ok 的样本（模型自称有把握的那批），
高分误判率 = 其中被人工判 verdict=wrong 的比例
```

比整体准确率更有用：它专门量化「自信地错」。整体准确率 90% 但高分误判率 15%，
说明模型在它最该可信的时候不可信，不能直接采信。

---

## 六、前端（第 8 个 tab）

- **摄像头画廊**：每张缩略图右上角带角标 —— `可用 92%` / `遮挡 88%` / `不可用 61%` / `未知`，
  四档配色（绿 / 红 / 橙 / 灰），不用点进去就知道这批片子能不能用。
- **「视觉」tab**：
  - 顶部汇总条：总数、`ok/low/rejected` 分布、人工复核准确率、高分误判率（>10% 标红警示）；
  - 四类样例切换：`正常 / 低分 / 错误 / 未知`，计数来自汇总（不是当前 50 条，翻页不跳变）；
  - 结果表：缩略图（点开原图）、判定角标、场景、**模型版本**、人话结论、人工反馈、操作；
  - 每行两个操作：「纠正」（弹层选 判对/判错/也判断不了 + 正确答案 + 备注）、
    「重新采集」（下发拍照任务，4 秒后自动刷新）。
- 全部离线可用，无外部 CDN；配色跟随现有深色主题。

---

## 七、复跑

```bash
# 1) 训练（约 8 分钟，CPU）
cd server/train && D:/Anaconda3/python.exe -u train_vision.py
#   产物：models/vision/vision-mnv3-v1.2.0.ts.pt（运行时用，3.56 MB，进仓库）
#        models/vision/vision-mnv3-v1.2.0.pt   （state_dict，3.86 MB，只用于复训/续训）
#        models/vision/labels.json            （标签/阈值/版本，服务端读它）
#        models/vision/train_report.json      （指标 + 真机抽样，结论都在这）

# 2) 自测（15 项断言，独立临时库，不碰正式库）
D:/Anaconda3/python.exe server/test_vision.py

# 3) 起服务看页面
start_server.bat
#   浏览器打开 http://127.0.0.1:8000/ui/ → 「视觉」tab
```

想临时关掉推理（比如换模型前）：`set VISION_ENABLED=0`，页面显示「推理未启用」，
照片照常入库，接口不 500。换模型目录：`set VISION_MODEL_DIR=D:\some\dir`。

---

## 八、已知限制（别当成 bug）

1. **合成测试集已经饱和**（v1.2.0 的 `availability_acc=1.0`、三类召回全 1.0）：
   三类的合成方式差异太明确，这个数字只能证明「管线通、学得会」，
   **不能当作真实场景准确率**。真实水平只能看真机抽样。
2. **真机抽样只有 10 张，而且全是废片**。现存 `server/photos/esp32s3-eye-0001/`
   里的 10 张，内容是「镜头对着天花板灯管 / 严重失焦 / 对着白墙」，人工看没有一张
   属于「画面可用」。v1.2.0 在这 10 张上的判定：

   | 照片 | 模型判定 | 置信度 | 人工看 | 评价 |
   |---|---|---|---|---|
   | 21 | degraded | 0.510 → **拒识** | 糊掉的暗角 | 保守，安全 |
   | 22 | clear | 0.558 → 低分 | 糊掉的白墙 | 保守，安全 |
   | 24 | clear | 0.586 → 低分 | 糊掉的白墙 | 保守，安全 |
   | 28 | **blocked** | 0.752 → ok | 暗场 + 灯管 | ✅ 判对 |
   | 29 | clear | **0.977 → ok** | 糊掉的白墙角落 | ❌ **自信地漏检** |
   | 31 | clear | 0.511 → **拒识** | 绿墙 + 灯管 | 保守，安全 |
   | 32 | degraded | 0.889 → ok | 严重失焦 | ✅ 判对 |
   | 33 | degraded | 0.986 → ok | 严重失焦 | ✅ 判对 |
   | 34 | clear | 0.563 → 低分 | 糊掉的暗处 | 保守，安全 |
   | 35 | degraded | 0.769 → ok | 亮绿平场 + 灯管 | ✅ 判对 |

   9/10 没有说「可用」，唯一漏检的是 29 号（0.977 判 clear）—— 这正是
   「高分误判率」要抓的样本：整体数字好看，但模型在最该可信的地方错了一次。
   这批样本也**无法验证「正常的 clear 会不会被误判」**，要验证得重新采一组
   「对准场景、能看清内容」的正样本。
3. **场景头只是附带能力**（v1.2.0 的 `scene_acc=0.8113`），页面显示室内/室外，
   但不参与任何决策，也不影响 `decision`。
4. **单帧判定，不看时序**：连续两帧之间的变化（手刚移开、镜头正在对焦）不参与判断。
5. **合成矢量图（纯色块 + 渐变）会被判 `degraded`**：它不在训练分布内。这类误报的
   代价只是多拍一张，属于可接受的保守方向。
6. **训练集是「老照片」，不是「ESP32 拍的房间」**：这是最根本的域差距。要真正解决，
   下一步应该拿板子拍 100–200 张真实场景照（含正常/遮挡/失焦三类），做一次微调
   （`train_vision.py` 已经有 `--skip-train` 载入检查点的路子，加个 fine-tune 开关即可）。
   在那之前，模型的价值是「把明显不可用的片子挡在前面」，而不是「准确率多少」。

---

## 九、变更清单

| 文件 | 变更 |
|---|---|
| `server/vision/engine.py` | 懒加载推理引擎：TorchScript 优先、ONNX 回退、预/后处理与训练一致 |
| `server/vision/decision.py` | 三档阈值 + 人话话术（拒识不给标签） |
| `server/vision/__init__.py` | 对外只暴露 `predict / status / decide / describe …` |
| `server/train/vision_dataset.py` | 底图划分 + 三类合成退化（现场合成，不落盘） |
| `server/train/train_vision.py` | 双头网络、训练/评估、TorchScript 导出、真机抽样、报告 |
| `server/models/vision/*` | v1.2.0 模型 + `labels.json` + `train_report.json`（模型进仓库，clone 即可推理） |
| `server/main.py` | 两张新表、`store_photo` 后挂推理、7 个接口、`vision_policy()` |
| `server/static/index.html` / `app.js` | 第 8 个 tab、四类样例、纠正弹层、画廊角标 |
| `server/test_vision.py` | 15 项断言（推理 / 反馈 / 闭环 / 降级） |
| `server/requirements-vision.txt` | torch / torchvision / numpy / Pillow |

---

## 十、与第 7 周的对应关系

第 7 周要求「按需获取一张环境图像」，成果与证据 6/7 已具备，缺的那一项是
**镜头遮挡 / 画面不可用判别**。本周期把它补上了：

| 第 7 周要求 | 落点 |
|---|---|
| 按需拍照并回传 | `kind=camera` 任务链（已有） |
| 照片落盘可查可删 | `photos` 表 + 画廊（已有） |
| **能识别画面是否可用** | `vision_results.decision` = `ok/low/rejected`，遮挡/不可用各有专门标签 |
| 不可用时给出下一步 | `describe().action` + `/vision/recapture` 重拍闭环 |

第 8 周要求「视觉推理结果变成可理解反馈」，对应本文件的第三～六节。
