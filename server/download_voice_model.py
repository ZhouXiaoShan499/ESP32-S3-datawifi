# -*- coding: utf-8 -*-
"""下载 Vosk 中文小模型到 server/models/（约 42 MB，供板端语音离线识别）。

用法：
    python server/download_voice_model.py
    python server/download_voice_model.py --zip D:\\downloads\\vosk-model-small-cn-0.22.zip

下载地址（也可以浏览器手动下载后解压到 server/models/）：
    https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip

国内网络慢时，先在浏览器把 zip 下好，再用 --zip 指定本地文件即可。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")
MODEL_NAME = "vosk-model-small-cn-0.22"
DEFAULT_URL = "https://alphacephei.com/vosk/models/%s.zip" % MODEL_NAME


def _human(num):
    return "%.1f MB" % (num / 1048576.0)


def _download(url, dest):
    """带进度地把 url 下到 dest；失败抛异常由调用方给出人工下载指引。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "esp32-s3-eye-voice/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        done = 0
        last_pct = -1
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total > 0:
                    pct = int(done * 100 / total)
                    if pct != last_pct:
                        last_pct = pct
                        sys.stdout.write("\r  下载中 %3d%%  %s / %s"
                                         % (pct, _human(done), _human(total)))
                        sys.stdout.flush()
    sys.stdout.write("\r  下载完成 %s%s\n" % (_human(done), " " * 16))
    sys.stdout.flush()


def _safe_extract(zip_path, dest_dir):
    """解压并拒绝路径穿越（zip 里的绝对路径 / ../ 一律不认）。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for name in names:
            normalized = name.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise RuntimeError("压缩包包含非法路径：%s" % name)
        zf.extractall(dest_dir)


def _looks_like_model(path):
    return os.path.isdir(os.path.join(path, "am"))


def main():
    parser = argparse.ArgumentParser(
        description="下载 Vosk 中文小模型到 server/models/")
    parser.add_argument("--url", default=os.environ.get("VOICE_MODEL_URL", DEFAULT_URL),
                        help="模型 zip 地址（默认 alphacephei 官方地址）")
    parser.add_argument("--zip", default="",
                        help="使用已经下载好的 zip（离线安装）")
    parser.add_argument("--force", action="store_true",
                        help="模型已存在时也重新下载")
    args = parser.parse_args()

    os.makedirs(MODELS_DIR, exist_ok=True)
    target = os.path.join(MODELS_DIR, MODEL_NAME)

    if _looks_like_model(target) and not args.force:
        print("模型已存在，无需下载：%s" % target)
        print("若服务端已在运行，重启后生效。")
        return 0

    zip_path = args.zip
    temp_path = ""
    if not zip_path:
        temp_path = os.path.join(tempfile.gettempdir(), MODEL_NAME + ".zip")
        zip_path = temp_path
        print("下载：%s" % args.url)
        try:
            _download(args.url, zip_path)
        except Exception as exc:              # noqa: BLE001
            print("\n下载失败：%s" % exc)
            print("可以手动下载下面的文件，再用 --zip 指定本地路径：")
            print("  %s" % args.url)
            print("  python server/download_voice_model.py --zip <下载到的 zip>")
            return 1
    elif not os.path.isfile(zip_path):
        print("找不到压缩包：%s" % zip_path)
        return 1

    try:
        print("解压到：%s" % MODELS_DIR)
        _safe_extract(zip_path, MODELS_DIR)
    except Exception as exc:                  # noqa: BLE001
        print("解压失败：%s" % exc)
        return 1
    finally:
        if temp_path and os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    if not _looks_like_model(target):
        print("解压完成，但没有找到 %s\\am —— 请确认压缩包内容。" % target)
        return 1

    print("完成：%s" % target)
    print("重启服务端后生效；页面「板端麦克风」卡片应显示“就绪”。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
