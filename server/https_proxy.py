# -*- coding: utf-8 -*-
"""本地 HTTPS 反向代理。

ESP32 继续通过明文 HTTP 访问 Uvicorn 的 8000 端口；浏览器访问本代理的
8443 端口，因此 Web Speech API 能获得 secure context。代理只监听本地电脑，
不改变设备侧协议，也不需要 VPS。
"""

from __future__ import annotations

import argparse
import http.client
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream_host = "127.0.0.1"
    upstream_port = 8000
    timeout_s = 120

    def do_GET(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_OPTIONS(self):
        self._proxy()

    def _proxy(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        body = self.rfile.read(length) if length else None

        headers = {
            name: value for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP and name.lower() != "host"
        }
        headers["Host"] = f"{self.upstream_host}:{self.upstream_port}"
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")

        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=self.timeout_s)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP and name.lower() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except Exception as exc:                 # noqa: BLE001
            try:
                self.send_error(502, f"upstream unavailable: {exc}")
            except Exception:
                pass
        finally:
            conn.close()

    def log_message(self, fmt, *args):
        # 直播页约 4 fps；逐请求日志会淹没真正有用的错误。
        return


def main():
    cert_dir = Path(__file__).resolve().parent / "certs"
    parser = argparse.ArgumentParser(description="Local HTTPS proxy for /ui/")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8443)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8000)
    parser.add_argument("--certfile", default=str(cert_dir / "dev-cert.pem"))
    parser.add_argument("--keyfile", default=str(cert_dir / "dev-key.pem"))
    args = parser.parse_args()

    ProxyHandler.upstream_host = args.upstream_host
    ProxyHandler.upstream_port = args.upstream_port
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ProxyHandler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.certfile, args.keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(
        f"[https-proxy] https://{args.listen_host}:{args.listen_port}/ui/"
        f" -> http://{args.upstream_host}:{args.upstream_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
