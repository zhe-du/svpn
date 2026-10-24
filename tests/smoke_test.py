"""svpn 冒烟测试：不联网也能验证 链路 / 分流 / 鉴权 / SNI 解析。

运行：python tests/smoke_test.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import asyncio
import base64
import socket
import ssl
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn.forward import extract_sni          # noqa: E402
from svpn.proxy import ProxyServer            # noqa: E402
from svpn.rules import Router                 # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ echo server
async def echo_server(port_holder: list[int]):
    async def handler(r, w):
        try:
            while True:
                data = await r.read(4096)
                if not data:
                    break
                w.write(b"echo:" + data)
                await w.drain()
        finally:
            w.close()

    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port_holder.append(srv.sockets[0].getsockname()[1])
    return srv


# ------------------------------------------------------------------ clients
async def socks5_roundtrip(proxy_port: int, target_host: str, target_port: int,
                           payload: bytes, auth: tuple[str, str] | None = None) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
    if auth:
        w.write(b"\x05\x02\x00\x02")
    else:
        w.write(b"\x05\x01\x00")
    await w.drain()
    ver, method = await r.readexactly(2)
    assert ver == 5, ver
    if method == 2:
        u, p = (auth or ("", ""))
        w.write(bytes([1, len(u)]) + u.encode() + bytes([len(p)]) + p.encode())
        await w.drain()
        _, st = await r.readexactly(2)
        assert st == 0, "auth failed"
    hb = target_host.encode()
    w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", target_port))
    await w.drain()
    head = await r.readexactly(4)
    assert head[1] == 0, f"connect code {head[1]}"
    atyp = head[3]
    if atyp == 1:
        await r.readexactly(6)
    elif atyp == 3:
        ln = (await r.readexactly(1))[0]
        await r.readexactly(ln + 2)
    else:
        await r.readexactly(18)
    w.write(payload)
    await w.drain()
    got = await asyncio.wait_for(r.read(65535), 5)
    w.close()
    return got


async def http_connect_roundtrip(proxy_port: int, target: str, payload: bytes,
                                 auth: tuple[str, str] | None = None) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
    hdr = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if auth:
        tok = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        hdr += f"Proxy-Authorization: Basic {tok}\r\n"
    hdr += "\r\n"
    w.write(hdr.encode())
    await w.drain()
    line = await asyncio.wait_for(r.readline(), 5)
    assert b"200" in line, line
    while (await r.readline()) not in (b"\r\n", b""):
        pass
    w.write(payload)
    await w.drain()
    got = await asyncio.wait_for(r.read(65535), 5)
    w.close()
    return got


# ------------------------------------------------------------------ TLS hello
def build_client_hello(sni: str) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(inc, out, server_hostname=sni)
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return out.read()


# ------------------------------------------------------------------ main
async def run() -> None:
    print("== 1. 规则分流 ==")
    r = Router()
    check("shop -> upstream", r.decide("store.steampowered.com") == "upstream")
    check("cm  -> upstream", r.decide("cm-01-iad1.cm.steampowered.com") == "upstream")
    check("cdn -> upstream", r.decide("steamcdn-a.akamaihd.net") == "upstream")
    check("baidu -> direct", r.decide("www.baidu.com") == "direct")
    check("local -> direct", r.decide("127.0.0.1") == "direct")
    check("all 模式", Router(mode="all").decide("github.com") == "upstream")
    check("blacklist 模式", Router(mode="blacklist").decide("www.qq.com") == "direct")

    print("== 2. SNI 解析 ==")
    hello = build_client_hello("steamcdn-a.akamaihd.net")
    check("提取 SNI", extract_sni(hello) == "steamcdn-a.akamaihd.net",
          repr(extract_sni(hello)))
    check("非 TLS 返回 None", extract_sni(b"GET / HTTP/1.1\r\n") is None)

    print("== 3. 本地直连 SOCKS5 ==")
    eport: list[int] = []
    esrv = await echo_server(eport)
    direct_port = free_port()
    srv_direct = ProxyServer({"listen_host": "127.0.0.1", "listen_port": direct_port,
                              "mode": "whitelist", "upstream": {"kind": "direct"}})
    await srv_direct.start()
    got = await socks5_roundtrip(direct_port, "127.0.0.1", eport[0], b"hi")
    check("直连回环", got == b"echo:hi", repr(got))

    print("== 4. 双层链路：client -> socks5 -> socks5 -> echo ==")
    up_port = free_port()
    srv_up = ProxyServer({"listen_host": "127.0.0.1", "listen_port": up_port,
                          "mode": "all", "upstream": {"kind": "direct"}})
    await srv_up.start()
    chain_port = free_port()
    srv_chain = ProxyServer({"listen_host": "127.0.0.1", "listen_port": chain_port,
                             "mode": "all",
                             "proxy_hosts": ["127.0.0.1"],
                             "upstream": {"kind": "socks5", "host": "127.0.0.1",
                                          "port": up_port}})
    await srv_chain.start()
    got = await socks5_roundtrip(chain_port, "127.0.0.1", eport[0], b"chain")
    check("链式回环", got == b"echo:chain", repr(got))
    check("上游计数", srv_chain.stats.via_upstream == 1,
          str(srv_chain.stats.snapshot()))

    print("== 5. HTTP CONNECT + 本地鉴权 ==")
    hp = free_port()
    srv_http = ProxyServer({"listen_host": "127.0.0.1", "listen_port": free_port(),
                            "http_port": hp, "mode": "all",
                            "auth": {"username": "u1", "password": "p1"},
                            "upstream": {"kind": "direct"}})
    await srv_http.start()
    got = await http_connect_roundtrip(hp, f"127.0.0.1:{eport[0]}", b"http",
                                       auth=("u1", "p1"))
    check("CONNECT 隧道", got == b"echo:http", repr(got))
    try:
        await http_connect_roundtrip(hp, f"127.0.0.1:{eport[0]}", b"x")
        check("未授权被拒", False, "居然通了")
    except AssertionError:
        check("未授权被拒", True)

    print("== 6. SOCKS5 鉴权 ==")
    got = await socks5_roundtrip(srv_http.port, "127.0.0.1", eport[0], b"au",
                                 auth=("u1", "p1"))
    check("SOCKS5 带账号", got == b"echo:au", repr(got))

    for s in (srv_direct, srv_up, srv_chain, srv_http):
        await s.close()
    esrv.close()
    await esrv.wait_closed()


def main() -> int:
    asyncio.run(run())
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
