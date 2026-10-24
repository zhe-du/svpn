"""真网验证：走本地代理真实握手、真实取包。需要先 `python -m svpn up -b`。

用法: python tests/live_test.py [socks_port] [http_port]
"""
from __future__ import annotations

import socket
import ssl
import struct
import sys


def socks_connect(host: str, port: int, ph: str = "127.0.0.1", pp: int = 7899,
                  timeout: float = 15.0) -> socket.socket:
    s = socket.create_connection((ph, pp), timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    ver, method = s.recv(2)
    assert ver == 5 and method == 0, f"握手失败 {ver},{method}"
    hb = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    head = s.recv(4)
    assert head[1] == 0, f"CONNECT 被拒 code={head[1]}"
    atyp = head[3]
    if atyp == 1:
        s.recv(6)
    elif atyp == 3:
        ln = s.recv(1)[0]
        s.recv(ln + 2)
    else:
        s.recv(18)
    return s


def https_get(host: str, path: str = "/", pp: int = 7899) -> tuple[int, int]:
    raw = socks_connect(host, 443, pp=pp)
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(raw, server_hostname=host)
    tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                f"Connection: close\r\nUser-Agent: svpn-test\r\n\r\n".encode())
    buf = b""
    while len(buf) < 65536:
        chunk = tls.recv(16384)
        if not chunk:
            break
        buf += chunk
    tls.close()
    line = buf.split(b"\r\n", 1)[0].decode("latin-1")
    code = int(line.split()[1]) if len(line.split()) > 1 else 0
    return code, len(buf)


def http_get_via_proxy(host: str, hp: int) -> tuple[int, int]:
    s = socket.create_connection(("127.0.0.1", hp), 15)
    s.settimeout(15)
    s.sendall(f"GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\n"
              f"Connection: close\r\nUser-Agent: svpn-test\r\n\r\n".encode())
    buf = b""
    while True:
        chunk = s.recv(16384)
        if not chunk:
            break
        buf += chunk
    s.close()
    line = buf.split(b"\r\n", 1)[0].decode("latin-1")
    code = int(line.split()[1]) if len(line.split()) > 1 else 0
    return code, len(buf)


def main() -> int:
    sp = int(sys.argv[1]) if len(sys.argv) > 1 else 7899
    hp = int(sys.argv[2]) if len(sys.argv) > 2 else 7900
    fails = 0

    for host, path in (("www.baidu.com", "/"),
                       ("store.steampowered.com", "/"),
                       ("api.steampowered.com", "/ISteamWebAPIUtil/GetServerInfo/v1/")):
        try:
            code, n = https_get(host, path, pp=sp)
            ok = 200 <= code < 400
            print(f"  {'PASS' if ok else 'WARN'}  SOCKS5+TLS {host}{path} -> {code}, {n}B")
            fails += 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN  SOCKS5+TLS {host} -> {type(exc).__name__}: {exc}")
            fails += 1

    try:
        code, n = http_get_via_proxy("www.baidu.com", hp)
        ok = 200 <= code < 400
        print(f"  {'PASS' if ok else 'WARN'}  HTTP 代理绝对URI -> {code}, {n}B")
        fails += 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN  HTTP 代理 -> {type(exc).__name__}: {exc}")
        fails += 1

    print("\nALL PASS" if fails == 0 else f"\n{fails} 项异常（可能是网络本身不通）")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
