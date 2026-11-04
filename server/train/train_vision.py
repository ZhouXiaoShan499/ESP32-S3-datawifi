# -*- coding: utf-8 -*-
"""训练「画面可用性 + 场景」双头小模型，并导出服务端用的 ONNX。

    python train_vision.py                 # 全流程：训练 → 评估 → 导出 → 写报告
    python train_vision.py --epochs 3      # 快速冒烟
    python train_vision.py --skip-train    # 只用已有 .pt 重新导出/评估

产物（默认落在 server/models/vision/）：
    vision-mnv3-v1.0.0.onnx     推理用模型，两个输出都是 softmax 概率
    vision-mnv3-v1.0.0.pt       训练检查点（便于复训，不进运行时）
    labels.json                 标签顺序 + 阈值 + 归一化参数（服务端唯一口径）
    train_report.json           验证集指标、混淆矩阵、真机照片抽样结果
"""
import argparse
import json
import os
import random
import sys
import time

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from vision_dataset import (  # noqa: E402
    IMG_SIZE, IMAGENET_MEAN, IMAGENET_STD, LABELS, SCENE_IGNORE, SCENE_LABELS,
    VisionDataset, build_splits, real_photos,
)

MODEL_NAME = "vision-mnv3"
DEFAULT_VERSION = "vision-mnv3-v1.2.0"   # v1.2.0：blocked 的平场帧补全亮场（v1.1.0 只补了暗场，亮平场会漏检）
OUT_DIR = os.path.join(os.path.dirname(_HERE), "models", "vision")

# 分档阈值：服务端读 labels.json 里的同一份数，训练脚本只负责写进去。
THRESHOLDS = {"ok": 0.75, "low": 0.55}


class _ListDataset(Dataset):
    """把 VisionDataset 包成 torch 能用的 Dataset（VisionDataset 自己不需要 torch）。"""

    def __init__(self, inner):
        self.inner = inner

    def __len__(self):
        return len(self.inner)

    def __getitem__(self, i):
        x, avail, scene = self.inner[i]
        return torch.from_numpy(x), avail, scene


class MultiHeadNet(nn.Module):
    """共享 MobileNetV3-Small 主干 + 两个头。

    一个 forward 同时给「画面可用性」（3 类，保底任务）和「室内/室外」（2 类，
    拓展任务）。共享主干比训两个模型省一半时间，也让两个任务互相当正则。
    """

    def __init__(self, n_avail=len(LABELS), n_scene=len(SCENE_LABELS), pretrained=True):
        super().__init__()
        weights = MobileNet_V3_Small_Weights.DEFAULT if pretrained else None
        try:
            net = mobilenet_v3_small(weights=weights)
        except Exception as e:                      # noqa: BLE001 - 没网就从头训
            print(f"[train] 预训练权重不可用（{e}），改为随机初始化")
            net = mobilenet_v3_small(weights=None)
        feat = net.classifier[0].in_features
        net.classifier = nn.Identity()               # 保留 features + avgpool
        self.backbone = net
        self.head_avail = nn.Linear(feat, n_avail)
        self.head_scene = nn.Linear(feat, n_scene)

    def forward(self, x):
        f = self.backbone(x)
        return self.head_avail(f), self.head_scene(f)


class _ExportWrapper(nn.Module):
    """导出用：直接吐概率，服务端拿到就能用，不用自己实现 softmax。"""

    def __init__(self, net):
        super().__init__()
        self.net = net

    def forward(self, x):
        a, s = self.net(x)
        return torch.softmax(a, dim=1), torch.softmax(s, dim=1)


def _seed_all(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def _loader(inner, batch, shuffle):
    return DataLoader(_ListDataset(inner), batch_size=batch, shuffle=shuffle,
                      num_workers=0, drop_last=False)


def _run_epoch(net, loader, criterion, optimizer=None):
    training = optimizer is not None
    net.train(training)
    total, correct, loss_sum = 0, 0, 0.0
    for x, avail, scene in loader:
        with torch.set_grad_enabled(training):
            logits_a, logits_s = net(x)
            loss_a = criterion(logits_a, avail)
            # blocked 样本没有场景语义：用掩码把它们的 scene 损失剔掉，
            # 否则模型会被迫在「一片漆黑」上猜室内/室外，把主干学歪。
            mask = scene != SCENE_IGNORE
            if mask.any():
                loss_s = criterion(logits_s[mask], scene[mask])
                loss = loss_a + 0.5 * loss_s
            else:
                loss = loss_a
        if training:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        loss_sum += float(loss.detach()) * x.size(0)
        correct += int((logits_a.argmax(1) == avail).sum())
        total += x.size(0)
    return loss_sum / max(1, total), correct / max(1, total)


@torch.no_grad()
def evaluate(net, loader):
    """返回可用性混淆矩阵、各类召回、二分类（clear vs 非 clear）指标、场景准确率。"""
    net.eval()
    n = len(LABELS)
    cm = [[0] * n for _ in range(n)]
    scene_ok = scene_total = 0
    probs_clear, clear_truth = [], []
    for x, avail, scene in loader:
        logits_a, logits_s = net(x)
        prob = torch.softmax(logits_a, 1)
        pred = prob.argmax(1)
        for t, p in zip(avail.tolist(), pred.tolist()):
            cm[t][p] += 1
        keep = scene != SCENE_IGNORE
        if keep.any():
            scene_ok += int((logits_s[keep].argmax(1) == scene[keep]).sum())
            scene_total += int(keep.sum())
        probs_clear.extend(prob[:, LABELS.index("clear")].tolist())
        clear_truth.extend([1 if v == LABELS.index("clear") else 0 for v in avail.tolist()])

    total = sum(sum(r) for r in cm) or 1
    acc = sum(cm[i][i] for i in range(n)) / total
    recall = {LABELS[i]: round(cm[i][i] / max(1, sum(cm[i])), 4) for i in range(n)}
    # clear 当正类：这条二分类口径比三分类整体准确率更能说明「保底任务」是否成立
    tp = sum(1 for p, t in zip(probs_clear, clear_truth) if t == 1 and p >= 0.5)
    fp = sum(1 for p, t in zip(probs_clear, clear_truth) if t == 0 and p >= 0.5)
    tn = sum(1 for p, t in zip(probs_clear, clear_truth) if t == 0 and p < 0.5)
    fn = sum(1 for p, t in zip(probs_clear, clear_truth) if t == 1 and p < 0.5)
    return {
        "availability_acc": round(acc, 4),
        "per_class_recall": recall,
        "confusion": cm,
        "clear_binary_acc": round((tp + tn) / max(1, tp + tn + fp + fn), 4),
        "clear_false_positive": fp,      # 非 clear 被判成 clear（漏检，最危险）
        "clear_false_negative": fn,      # clear 被判成有问题（误报，浪费一次重拍）
        "scene_acc": round(scene_ok / max(1, scene_total), 4),
    }


def _export_runtime(net, stem, out_dir):
    """导出运行时模型：TorchScript 为主，ONNX 为「有干净 onnx 环境时的加分项」。

    原计划是 ONNX 单一路径，但这台机器的 Anaconda 里 onnx 与 numpy 2.4 的 ABI
    对不上（`import onnx` 直接段错误），而为了降 numpy 又要动整个 base 环境。
    TorchScript 不需要额外依赖（训练环境本来就有 torch），trace 一次即可，
    服务端 `torch.jit.load()` 就能跑。ONNX 仍然尝试导出，成功就一起交付 ——
    引擎侧优先用 ONNX，读不到再回退 TorchScript。
    """
    wrapper = _ExportWrapper(net).eval()
    dummy = torch.randn(1, 3, IMG_SIZE, IMG_SIZE)

    ts_path = os.path.join(out_dir, stem + ".ts.pt")
    with torch.no_grad():
        traced = torch.jit.trace(wrapper, dummy)
        traced = torch.jit.freeze(traced)
    traced.save(ts_path)
    print(f"[train] TorchScript 导出 {ts_path} "
          f"({os.path.getsize(ts_path) / 1048576:.2f} MB)")

    onnx_path = os.path.join(out_dir, stem + ".onnx")
    onnx_note = None
    try:
        torch.onnx.export(
            wrapper, dummy, onnx_path,
            input_names=["input"], output_names=["availability", "scene"],
            dynamic_axes={"input": {0: "batch"}, "availability": {0: "batch"},
                          "scene": {0: "batch"}},
            opset_version=17,
        )
        print(f"[train] ONNX 导出 {onnx_path} "
              f"({os.path.getsize(onnx_path) / 1048576:.2f} MB)")
    except Exception as e:                          # noqa: BLE001
        onnx_path = None
        onnx_note = f"{type(e).__name__}: {e}"
        print(f"[train] ONNX 导出跳过（{onnx_note}），运行时会用 TorchScript")
    return ts_path, onnx_path, onnx_note


@torch.no_grad()
def predict_file(net, path):
    from PIL import Image

    from vision_dataset import _center_crop, _normalize, _resize_short_side

    with Image.open(path) as im:
        img = _center_crop(_resize_short_side(im.convert("RGB"), IMG_SIZE + 32), IMG_SIZE)
    x = torch.from_numpy(_normalize(img)).unsqueeze(0)
    pa, ps = torch.softmax(net(x)[0], 1)[0], torch.softmax(net(x)[1], 1)[0]
    idx = int(pa.argmax())
    return {
        "label": LABELS[idx],
        "score": round(float(pa[idx]), 4),
        "scene": SCENE_LABELS[int(ps.argmax())],
        "scene_score": round(float(ps.max()), 4),
        "probs": {LABELS[i]: round(float(pa[i]), 4) for i in range(len(LABELS))},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=20261104)
    ap.add_argument("--no-pretrained", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    args = ap.parse_args()

    _seed_all(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)
    stem = args.version

    splits = build_splits(args.data_root) if args.data_root else build_splits()
    print(f"[train] 底图 train={len(splits['train'])} val={len(splits['val'])} "
          f"test={len(splits['test'])}")

    ds_train = VisionDataset(splits["train"], train=True, seed=args.seed)
    ds_val = VisionDataset(splits["val"], train=False, seed=args.seed + 1)
    ds_test = VisionDataset(splits["test"], train=False, seed=args.seed + 2)
    print(f"[train] 样本 train={len(ds_train)} val={len(ds_val)} test={len(ds_test)}")

    net = MultiHeadNet(pretrained=not args.no_pretrained)
    ckpt_path = os.path.join(args.out_dir, stem + ".pt")
    history = []

    if args.skip_train and os.path.isfile(ckpt_path):
        net.load_state_dict(torch.load(ckpt_path, map_location="cpu"))
        print(f"[train] 载入已有检查点 {ckpt_path}")
    else:
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
        train_loader = _loader(ds_train, args.batch, True)
        val_loader = _loader(ds_val, args.batch, False)
        best = -1.0
        for epoch in range(1, args.epochs + 1):
            t0 = time.time()
            loss, acc = _run_epoch(net, train_loader, criterion, optimizer)
            val_loss, val_acc = _run_epoch(net, val_loader, criterion)
            scheduler.step()
            history.append({"epoch": epoch, "loss": round(loss, 4),
                            "train_acc": round(acc, 4),
                            "val_loss": round(val_loss, 4),
                            "val_acc": round(val_acc, 4),
                            "sec": round(time.time() - t0, 1)})
            print(f"[train] epoch {epoch}/{args.epochs} loss={loss:.4f} "
                  f"train_acc={acc:.3f} val_acc={val_acc:.3f} "
                  f"({time.time() - t0:.0f}s)")
            if val_acc > best:
                best = val_acc
                torch.save(net.state_dict(), ckpt_path)
        net.load_state_dict(torch.load(ckpt_path, map_location="cpu"))

    metrics = evaluate(net, _loader(ds_test, args.batch, False))
    print(f"[train] test: {json.dumps(metrics, ensure_ascii=False)}")

    ts_path, onnx_path, onnx_note = _export_runtime(net, stem, args.out_dir)
    size_mb = round(os.path.getsize(ts_path) / 1024.0 / 1024.0, 2)

    # 真机照片：训练分布之外的域，最能说明模型是不是只会认合成数据
    real = []
    for path in real_photos():
        try:
            item = predict_file(net, path)
        except Exception as e:                      # noqa: BLE001
            item = {"error": str(e)}
        item["file"] = os.path.relpath(path, os.path.dirname(_HERE)).replace("\\", "/")
        real.append(item)
    print(f"[train] 真机照片 {len(real)} 张已完成抽样推理")

    labels_doc = {
        "model_name": MODEL_NAME,
        "model_version": args.version,
        "labels": list(LABELS),
        "scene_labels": list(SCENE_LABELS),
        "input_size": IMG_SIZE,
        "mean": list(IMAGENET_MEAN),
        "std": list(IMAGENET_STD),
        "thresholds": THRESHOLDS,
        "runtime": {"format": "torchscript", "file": os.path.basename(ts_path)},
        "onnx": os.path.basename(onnx_path) if onnx_path else None,
    }
    with open(os.path.join(args.out_dir, "labels.json"), "w", encoding="utf-8") as f:
        json.dump(labels_doc, f, ensure_ascii=False, indent=2)

    report = {
        "model_name": MODEL_NAME,
        "model_version": args.version,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runtime": {"format": "torchscript", "file": os.path.basename(ts_path),
                    "size_mb": size_mb},
        "onnx_export": None if onnx_path else onnx_note,
        "labels": list(LABELS),
        "scene_labels": list(SCENE_LABELS),
        "thresholds": THRESHOLDS,
        "dataset": {
            "source": args.data_root or "D:/Datasets/house/indoor_out/indoor_outdoor_set",
            "base_images": {k: len(v) for k, v in splits.items()},
            "samples": {"train": len(ds_train), "val": len(ds_val), "test": len(ds_test)},
            "synth": "退化样本在 __getitem__ 现场合成（blocked 4 种 / degraded 5 种 / clear 轻微抖动）",
        },
        "train": {"epochs": args.epochs, "batch": args.batch, "lr": args.lr,
                  "pretrained": not args.no_pretrained, "history": history},
        "metrics": metrics,
        "real_photos": real,
    }
    with open(os.path.join(args.out_dir, "train_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[train] 报告已写入 {os.path.join(args.out_dir, 'train_report.json')}")


if __name__ == "__main__":
    main()
