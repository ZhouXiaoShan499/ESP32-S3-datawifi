#!/usr/bin/env python
"""DHCP DISCOVER 探针（宿主侧诊断工具，纯标准库、不需要管理员权限）

用途
----
板子（ESP32-S3）关联上了 WiFi 却一直拿不到 IP 时，需要区分两种可能：

  A. 网段/AP/路由器侧的 DHCP 服务没有应答（网络故障）
  B. 板子自身没把 DHCP 请求发出去 / 收到了 OFFER 却不接受（固件问题）

本脚本在本机网卡上直接广播一个 DHCPDISCOVER，并在 UDP/68 上等 OFFER/ACK：
**只要本机都收不到应答，就说明这个二层段没有可用的 DHCP 服务**，
板子的表现就是正常的、无需再怀疑固件（配合 `docs/wifi_network_troubleshooting.md` 使用）。

用法
----
    python server/dhcp_probe.py <chaddr-mac> [等待秒数]

    # 板子 STA 的 MAC（启动日志 wifi:mode : sta (94:a9:90:1c:6f:b4)）
    python server/dhcp_probe.py 94-a9-90-1c-6f-b4 8
    # 对照组：本机网卡自己的 MAC（ipconfig /all 里的「物理地址」）
    python server/dhcp_probe.py 54-01-4a-5e-26-e5 8

实测结论（2026-09-23，192.0.2.0/24）：板子 MAC / 同网段另一块在线 ESP32 的 MAC /
本机网卡 MAC 三种 chaddr 全部 8 s 内零回应 → 该网段当时没有 DHCP 服务。
"""
import random
import socket
import struct
import sys
import time

MAGIC = b"\x63\x82\x53\x63"
BROADCAST = "255.255.255.255"
DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68
TYPES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE",
         5: "ACK", 6: "NAK", 7: "RELEASE", 8: "INFORM"}


def mac_to_bytes(mac):
    """'94:a9:90:1c:6f:b4' / '94-a9-90-1c-6f-b4' -> b'\\x94...'"""
    return bytes(int(part, 16) for part in mac.replace(":", "-").split("-"))


def build_discover(xid, chaddr, broadcast=True):
    """BOOTP 固定部分(236B) + magic cookie + 最小选项集。"""
    flags = 0x8000 if broadcast else 0x0000
    fixed = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        1,                      # op = BOOTREQUEST
        1,                      # htype = ethernet
        6,                      # hlen
        0,                      # hops
        xid,                    # transaction id
        0,                      # secs
        flags,                  # 广播标志：让服务器把应答也广播出来
        b"\x00" * 4,            # ciaddr
        b"\x00" * 4,            # yiaddr
        b"\x00" * 4,            # siaddr
        b"\x00" * 4,            # giaddr
        chaddr + b"\x00" * 10,  # chaddr（16 字节，后 10 字节补零）
        b"",                    # sname
        b"",                    # file
    )
    options = b""
    options += bytes([53, 1, 1])                  # DHCP 消息类型 = DISCOVER
    options += bytes([61, 7, 1]) + chaddr[:6]     # client identifier（和板端一致）
    options += bytes([12, 8]) + b"probe-pc"       # hostname
    options += bytes([55, 4, 1, 3, 6, 15])        # mask/router/dns/domain
    options += bytes([255])                       # end
    return fixed + MAGIC + options


def parse_reply(data):
    """解析 BOOTP/DHCP 应答，返回 (type, yiaddr, server_id, mask, router)。"""
    if len(data) < 240 or data[236:240] != MAGIC:
        return None
    yiaddr = socket.inet_ntoa(data[16:20])
    msg_type = server_id = mask = router = None
    i = 240
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        length = data[i + 1]
        value = data[i + 2:i + 2 + length]
        if code == 53 and length >= 1:
            msg_type = value[0]
        elif code == 54 and length == 4:
            server_id = socket.inet_ntoa(value)
        elif code == 1 and length == 4:
            mask = socket.inet_ntoa(value)
        elif code == 3 and length >= 4:
            router = socket.inet_ntoa(value[:4])
        i += 2 + length
    return msg_type, yiaddr, server_id, mask, router


def probe(mac, seconds):
    chaddr = mac_to_bytes(mac)
    xid = random.getrandbits(32)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    rx.bind(("", DHCP_CLIENT_PORT))
    rx.settimeout(1.0)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    packet = build_discover(xid, chaddr)
    try:
        tx.sendto(packet, (BROADCAST, DHCP_SERVER_PORT))
        print("[probe] sent DISCOVER chaddr=%s xid=0x%08x (%d bytes)"
              % (mac, xid, len(packet)))
    except OSError as exc:
        print("[probe] send failed: %r" % (exc,))
    finally:
        tx.close()

    replied = False
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data, addr = rx.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError as exc:
            print("[probe] recv failed: %r" % (exc,))
            break
        if len(data) < 8 or struct.unpack("!I", data[4:8])[0] != xid:
            continue                      # 不是本次事务的应答
        parsed = parse_reply(data)
        if not parsed:
            continue
        msg_type, yiaddr, server_id, mask, router = parsed
        print("[probe] REPLY from %s: type=%s yiaddr=%s server=%s mask=%s gw=%s"
              % (addr[0], TYPES.get(msg_type, msg_type),
                 yiaddr, server_id, mask, router))
        replied = True

    rx.close()
    if not replied:
        print("[probe] no reply within %ds for chaddr=%s -> 该网段没有 DHCP 服务应答"
              % (seconds, mac))
    return replied


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "94-a9-90-1c-6f-b4"
    wait_s = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    probe(target, wait_s)