"""D3-e2e 临时测量脚本（只写这一个文件，不碰源码）。

安静环境 10 连测：分别经 SOCKS5(7899) 和 HTTP(7900) 各取 10 次
https://store.steampowered.com/ ，记录状态码 / 总耗时 / 失败原因（rep 码或异常类型）。

    python _crew/_e2e_probe.py --rounds 10 --out _crew/_e2e_probe.json

纯标准库。SOCKS5 手写握手，HTTP 用 CONNECT。TLS 用 CERT_NONE（只验通路，不验证书链）。
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import struct
import sys
import time
from pathlib import Path

HOST = "127.0.0.1"
SOCKS_PORT = 7899
HTTP_PORT = 7900
TARGET = "store.steampowered.com"
CDN = "steamcdn-a.akamaihd.net"
UA = "svpn-e2e/1.0"
CONNECT_TIMEOUT = 10.0
HEAD_TIMEOUT = 15.0
OK_CODES = (200, 301, 302, 403)          # store 对 HEAD 可能回 301/302

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被提前关闭")
        buf += chunk
    return buf


def socks5_open(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    sock = socket.create_connection((HOST, SOCKS_PORT), timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS5 不接受无认证方式")
        hb = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb
                     + struct.pack(">H", port))
        ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if ver != 0x05:
            raise ConnectionError(f"SOCKS5 版本异常 {ver}")
        if rep != 0x00:
            raise ConnectionError(f"SOCKS5 CONNECT 失败 rep={rep}")
        if atyp == 0x01:
            _recv_exact(sock, 6)
        elif atyp == 0x03:
            _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
        elif atyp == 0x04:
            _recv_exact(sock, 18)
        return sock
    except BaseException:
        sock.close()
        raise


def http_connect_open(host: str, port: int,
                      timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    sock = socket.create_connection((HOST, HTTP_PORT), timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall((f"CONNECT {host}:{port} HTTP/1.1\r\n"
                      f"Host: {host}:{port}\r\n"
                      "Proxy-Connection: keep-alive\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("HTTP 代理在 CONNECT 应答前关闭")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("CONNECT 应答过长")
        line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise ConnectionError(f"CONNECT 应答异常: {line}")
        if int(parts[1]) // 100 != 2:
            raise ConnectionError(f"CONNECT 被拒 HTTP {parts[1]}")
        return sock
    except BaseException:
        sock.close()
        raise


def _read_head(sock: socket.socket, timeout: float = HEAD_TIMEOUT) -> tuple[int, str]:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    if not buf:
        raise ConnectionError("没有收到任何 HTTP 应答")
    line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split()
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return code, line


def one(connector, host: str, path: str = "/") -> dict:
    """一次完整测量：连接 → TLS → HEAD。"""
    out = {"code": 0, "line": "", "ms": 0.0, "ok": False, "err": "", "tls": ""}
    t0 = time.perf_counter()
    raw = tls = None
    try:
        raw = connector(host, 443)
        tls = CTX.wrap_socket(raw, server_hostname=host)
        out["tls"] = tls.version() or ""
        tls.sendall((f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\n"
                     f"User-Agent: {UA}\r\nAccept: */*\r\n"
                     "Connection: close\r\n\r\n").encode())
        out["code"], out["line"] = _read_head(tls)
        out["ok"] = out["code"] in OK_CODES
    except Exception as exc:                     # noqa: BLE001
        out["err"] = f"{type(exc).__name__}: {exc}"
    finally:
        out["ms"] = (time.perf_counter() - t0) * 1000
        for s in (tls, raw):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
    return out


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = max(1, min(len(s), int(round(p / 100.0 * len(s) + 0.4999))))
    return s[k - 1]


def summarize(name: str, rows: list[dict]) -> dict:
    ok = [r for r in rows if r["ok"]]
    ms = [r["ms"] for r in ok]
    allms = [r["ms"] for r in rows]
    return {
        "name": name,
        "rounds": len(rows),
        "success": len(ok),
        "rate": round(len(ok) / max(len(rows), 1), 3),
        "p50_ms": round(pct(ms, 50), 1),
        "p95_ms": round(pct(ms, 95), 1),
        "min_ms": round(min(allms), 1) if allms else 0.0,
        "max_ms": round(max(allms), 1) if allms else 0.0,
        "codes": [r["code"] for r in rows],
        "tls": sorted({r["tls"] for r in rows if r["tls"]}),
        "errs": [r["err"] for r in rows if r["err"]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    result: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "rounds": args.rounds,
                    "target": TARGET, "socks": [], "http": [], "cdn": None, "summary": {}}

    for i in range(1, args.rounds + 1):
        r = one(socks5_open, TARGET)
        result["socks"].append(r)
        print(f"SOCKS5 {i:>2}/{args.rounds}  code={r['code'] or '-':<4} "
              f"{r['ms']:7.0f}ms  {'OK' if r['ok'] else 'FAIL'} {r['err']}", flush=True)

    for i in range(1, args.rounds + 1):
        r = one(http_connect_open, TARGET)
        result["http"].append(r)
        print(f"HTTP   {i:>2}/{args.rounds}  code={r['code'] or '-':<4} "
              f"{r['ms']:7.0f}ms  {'OK' if r['ok'] else 'FAIL'} {r['err']}", flush=True)

    cdn = one(socks5_open, CDN)
    result["cdn"] = cdn
    print(f"CDN    socks5  code={cdn['code'] or '-':<4} {cdn['ms']:7.0f}ms "
          f"{'OK' if cdn['ok'] else 'FAIL'} {cdn['err']}", flush=True)

    result["summary"]["socks"] = summarize("socks5", result["socks"])
    result["summary"]["http"] = summarize("http", result["http"])
    for key in ("socks", "http"):
        s = result["summary"][key]
        print(f"== {key}: {s['success']}/{s['rounds']} ok  P50={s['p50_ms']}ms "
              f"P95={s['p95_ms']}ms  range={s['min_ms']}-{s['max_ms']}ms  "
              f"codes={s['codes']}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"written {args.out}")
    return 0 if (result["summary"]["socks"]["success"] == args.rounds
                 and result["summary"]["http"]["success"] == args.rounds) else 1


if __name__ == "__main__":
    sys.exit(main())
