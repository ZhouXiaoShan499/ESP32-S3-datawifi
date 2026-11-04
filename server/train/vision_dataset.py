# -*- coding: utf-8 -*-
"""视觉数据集的筛选、合成与 Dataset 实现（离线训练用，服务端运行时不导入）。

设计要点
--------
1. **退化样本在 __getitem__ 里现场合成**，不落盘：8297 张底图哪怕只扩 3 倍也是
   两万多张 JPEG，既占磁盘又让「每轮看到的遮挡长得一样」。现场合成的好处是每个
   epoch 的退化参数都不同，模型学的是「画面被挡」这件事，而不是某几张固定图。
2. **按底图分 split**：合成样本继承底图的 split，避免同一张图的 clear/blocked
   两个版本分别落进训练集和验证集（那就是泄漏，验证准确率会虚高）。
3. 三类可用性标签固定成 `LABELS` 的顺序，服务端读 `labels.json` 对齐，不允许
   在别处重排。
"""
import os
import random

import numpy as np
from PIL import Image, ImageFilter

# 可用性三分类。顺序即模型输出维度顺序，改动必须同步 models/vision/labels.json。
LABELS = ("clear", "blocked", "degraded")
SCENE_LABELS = ("indoor", "outdoor")
SCENE_IGNORE = -1          # blocked 样本没有场景语义，不参与 scene 头的损失

IMG_SIZE = 224
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

# 数据集根目录：本机已有 8297 张已标注的室内/室外图（indoor 3799 / outdoor 4498）。
DEFAULT_DATA_ROOT = r"D:\Datasets\house\indoor_out\indoor_outdoor_set"
# 项目自己采的照片：留作「真实域」验证，不参与训练。
DEFAULT_REAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "photos")

# 每个 split 的底图数量。8000 张全用上在 CPU 上要跑很久，抽样即可 —— 底图本身
# 同质化很高（荷兰国家档案馆的老照片），再多也不会带来新信息。
BASE_PER_SPLIT = {"train": 800, "val": 200, "test": 200}

# 一个底图能派生几条样本：三类各 2 条，保持类别均衡（真实场景里 clear 占多数，
# 但这里要的是「三类都学得会」，部署侧的分布偏差交给拒识阈值去兜）。
DERIVE = {"clear": 2, "blocked": 2, "degraded": 2}


def _list_bases(data_root):
    """返回 [(abs_path, scene_label)]，scene 取目录名。"""
    out = []
    for scene in ("indoor", "outdoor"):
        d = os.path.join(data_root, scene)
        if not os.path.isdir(d):
            continue
        label = SCENE_LABELS.index(scene)
        for name in os.listdir(d):
            if name.lower().endswith((".jpg", ".jpeg", ".png")):
                out.append((os.path.join(d, name), label))
    out.sort()                     # 固定顺序，seed 才能复现同一份划分
    return out


def build_splits(data_root=DEFAULT_DATA_ROOT, seed=20261104):
    """把底图划分成 train/val/test，返回 {split: [(path, scene), ...]}。

    先整体 shuffle 再按 BASE_PER_SPLIT 切片，同一张底图只出现在一个 split 里。
    """
    bases = _list_bases(data_root)
    rng = random.Random(seed)
    rng.shuffle(bases)
    splits, cursor = {}, 0
    for name in ("train", "val", "test"):
        n = BASE_PER_SPLIT[name]
        splits[name] = bases[cursor:cursor + n]
        cursor += n
    return splits


# ---------------------------------------------------------------------------
# 退化合成
# ---------------------------------------------------------------------------
def _to_array(img):
    return np.asarray(img, dtype=np.float32)


def _to_image(arr):
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGB")


def make_blocked(img, rng):
    """合成「镜头被挡」：接近全黑/纯色/大面积模糊色块，只剩极弱的明暗结构。

    真实现场就是手掌、衣服、桌面上盖住镜头 —— 画面均匀、几乎无边缘。这里刻意
    保留一点点渐变和噪声，否则模型会退化成「方差为 0 就判遮挡」这一条死规则。

    `kind == 4` 是后补的**全平场**分支：训练集里原本没有「value≈常数、方差≈0」
    这种帧，导致一张纯黑图（服务端自测里就有）三类概率打平、被判成拒识。真机
    当然拍不出严格平场（总有传感器噪声和 JPEG 块），但「镜头彻底盖死」时人眼看
    到的就是一片死黑，模型必须能坚定地说 blocked，而不是含糊其辞。
    """
    w, h = img.size
    kind = rng.randrange(5)
    if kind == 0:
        # 纯色大面积遮挡（手掌/衣物）：随机偏暗的肤色/布色 + 轻微渐变
        base = np.array([rng.randint(18, 70), rng.randint(14, 55), rng.randint(12, 50)],
                        dtype=np.float32)
        yy = np.linspace(0.82, 1.12, h, dtype=np.float32)[:, None, None]
        arr = base[None, None, :] * yy
        arr = np.repeat(arr, w, axis=1)
    elif kind == 1:
        # 失焦 + 极暗：还能看见一点轮廓，但完全无法辨认内容
        arr = _to_array(img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(18, 34))))
        arr *= rng.uniform(0.04, 0.12)
    elif kind == 2:
        # 强暗角：中心勉强有一点亮度，四周全黑
        arr = _to_array(img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(10, 20))))
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = w / 2.0, h / 2.0
        r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
        vignette = np.clip(1.35 - r * 1.25, 0.0, 1.0) ** 2
        arr = arr * vignette[:, :, None] * rng.uniform(0.05, 0.15)
    elif kind == 3:
        # 半遮挡：一侧留一条极窄的可见缝隙，其余压黑
        arr = _to_array(img)
        arr *= rng.uniform(0.03, 0.09)
        edge = rng.uniform(0.06, 0.16)
        if rng.random() < 0.5:
            arr[:, int(w * (1 - edge)):, :] = arr[:, int(w * (1 - edge)):, :] * 8
        else:
            arr[int(h * (1 - edge)):, :, :] = arr[int(h * (1 - edge)):, :, :] * 8
    else:
        # 全平场：整幅同一个灰阶。方差≈0 是这类帧的唯一特征。
        # 亮度必须覆盖两档 —— 盖镜头的手掌被灯照着是**亮**平场，深色衣物盖住才是暗平场；
        # 只补暗平场的结果是「亮平场被判 clear 0.96」，那是最糟的错误方向（自信地漏检）。
        if rng.random() < 0.5:
            base, sd = float(rng.randint(0, 46)), rng.uniform(0.2, 1.2)      # 暗平场
        else:
            base, sd = float(rng.randint(120, 215)), rng.uniform(0.5, 2.5)   # 亮平场
        arr = np.full((h, w, 3), base, dtype=np.float32)
        # 轻微偏色（肤色/灯罩的黄绿、白墙的白），别让模型学成「平场 = 中性灰」
        arr += np.array([rng.uniform(-6, 10), rng.uniform(-4, 8), rng.uniform(-10, 6)],
                        dtype=np.float32)
        if rng.random() < 0.35:
            # 极少数情况留一条亮带（灯管/屏幕反光），整体仍是废帧
            band = rng.randint(3, max(4, h // 10))
            y0 = rng.randrange(0, max(1, h - band))
            arr[y0:y0 + band, :, :] = rng.uniform(150, 240)
    if kind == 4:
        arr += np.random.normal(0, sd, arr.shape).astype(np.float32)
        return _to_image(arr)
    # 其余分支的噪声：太大就变成「低照度纹理」，反而教不会「平 = 废」这件事
    arr += np.random.normal(0, rng.uniform(1.5, 5.0), arr.shape).astype(np.float32)
    return _to_image(arr)


def make_degraded(img, rng):
    """合成「画面不可用但不算遮挡」：模糊 / 过曝 / 欠曝 / 噪声 / 低对比。"""
    kind = rng.randrange(5)
    if kind == 0:
        return img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(3.0, 8.0)))
    arr = _to_array(img)
    if kind == 1:                                  # 过曝
        arr = arr * rng.uniform(2.4, 4.2) + rng.uniform(20, 60)
    elif kind == 2:                                # 欠曝
        arr = arr * rng.uniform(0.12, 0.28)
    elif kind == 3:                                # 噪声
        arr = arr + np.random.normal(0, rng.uniform(18, 38), arr.shape)
    else:                                          # 低对比（雾化）
        mean = arr.mean()
        arr = (arr - mean) * rng.uniform(0.18, 0.35) + mean + rng.uniform(-10, 10)
    return _to_image(arr)


def make_clear(img, rng):
    """正常样本：亮度抖动 + **模拟板端摄像头的成像质量**。

    第一版这里只做亮度/对比抖动，结果模型在真机照片上把 10 张里的 5 张判成
    `degraded` —— 训练集是档案馆的高清扫描件，ESP32 的 640×480 JPEG 在它眼里
    全是"糊的"。这不是模型笨，是训练域和部署域不一致。

    修法是把"低质量但可用"这件事**训进 clear 类**：轻微失焦（radius ≤ 1.2）、
    暗角、传感器噪声、JPEG 有损压缩。这样 clear 与 degraded 的分界就从
    "是否锐利"变成"还能不能看清内容"，那才是这条业务真正关心的。
    """
    arr = _to_array(img) * rng.uniform(0.88, 1.14) + rng.uniform(-8, 8)
    out = _to_image(arr)

    if rng.random() < 0.75:                       # 轻微失焦（不是糊，是镜头素质）
        out = out.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.3, 1.2)))
    if rng.random() < 0.5:                        # 轻微暗角
        w, h = out.size
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = w / 2.0, h / 2.0
        r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
        grid = np.clip(1.08 - r * 0.20, 0.72, 1.0)[:, :, None]
        out = _to_image(_to_array(out) * grid)
    if rng.random() < 0.5:                        # 传感器噪声
        arr2 = _to_array(out) + np.random.normal(0, rng.uniform(2, 7), (out.size[1],
                                                                       out.size[0], 3))
        out = _to_image(arr2)
    if rng.random() < 0.7:                        # JPEG 有损压缩伪影（板端就是 JPEG 直传）
        import io

        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=rng.randint(28, 62))
        buf.seek(0)
        out = Image.open(buf).convert("RGB")
    return out


AUGMENTERS = {"clear": make_clear, "blocked": make_blocked, "degraded": make_degraded}


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------
class VisionDataset:
    """不依赖 torchvision.transforms，纯 PIL + numpy 出张量。

    torchvision 的 transforms 在 DataLoader 多进程里偶尔会因为 PIL 解码器句柄
    出问题；这里只用最基础的 resize/crop/normalize，自己写既确定又少一层版本耦合。
    """

    def __init__(self, bases, train=True, seed=0, derive=None):
        self.samples = []
        self.train = train
        derive = derive or DERIVE
        for path, scene in bases:
            for label, times in derive.items():
                for k in range(times):
                    self.samples.append((path, LABELS.index(label), scene, k))
        self.rng = random.Random(seed)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, avail, scene, variant = self.samples[idx]
        rng = random.Random((self.rng.random() if self.train else idx * 7919) + variant)
        with Image.open(path) as im:
            img = im.convert("RGB")
            img = _resize_short_side(img, IMG_SIZE + 32)
            img = _random_crop(img, IMG_SIZE, rng) if self.train else _center_crop(img, IMG_SIZE)
            label = LABELS[avail]
            img = AUGMENTERS[label](img, rng)
            if self.train and rng.random() < 0.5:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
        x = _normalize(img)
        return x, avail, (scene if avail != LABELS.index("blocked") else SCENE_IGNORE)


def _resize_short_side(img, size):
    w, h = img.size
    scale = size / float(min(w, h))
    return img.resize((max(size, int(round(w * scale))), max(size, int(round(h * scale)))),
                      Image.BILINEAR)


def _center_crop(img, size):
    w, h = img.size
    left, top = (w - size) // 2, (h - size) // 2
    return img.crop((left, top, left + size, top + size))


def _random_crop(img, size, rng):
    w, h = img.size
    left = rng.randrange(0, max(1, w - size + 1))
    top = rng.randrange(0, max(1, h - size + 1))
    return img.crop((left, top, left + size, top + size))


def _normalize(img):
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = (arr - np.array(IMAGENET_MEAN, dtype=np.float32)) / np.array(IMAGENET_STD,
                                                                      dtype=np.float32)
    return np.transpose(arr, (2, 0, 1)).copy()


def real_photos(photo_dir=DEFAULT_REAL_DIR):
    """项目自己采的真机照片（可能有多个设备子目录），只用于真实域验收。"""
    out = []
    if not os.path.isdir(photo_dir):
        return out
    for root, _dirs, files in os.walk(photo_dir):
        for name in sorted(files):
            if name.lower().endswith(".jpg"):
                out.append(os.path.join(root, name))
    return sorted(out)
