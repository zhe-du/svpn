"""本地代理内核：SOCKS5 + HTTP CONNECT + 绝对 URI 转发，支持上游链。

设计要点
- 纯 asyncio 标准库，零依赖，单进程同时监听 SOCKS5 / HTTP。
- 分流：按域名后缀决定 direct / upstream，域名原样交给上游解析（远端 DNS）。
- 上游支持 socks5(含账号密码) / http(CONNECT, 含 Basic 认证) / direct。
- 统计：连接数、上行下行字节、按域名聚合，SIGINT 时落盘。
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from .rules import Router
from . import netutil

log = logging.getLogger("svpn.proxy")

MAX_HEAD = 64 * 1024
BUF = 65536

DEFAULT_LISTEN_PORT = 7899
DEFAULT_HTTP_PORT = 7900
DEFAULT_TIMEOUT = 12.0


# --------------------------------------------------------------- 配置容错
def _as_port(value: Any, default: int) -> int:
    """容错端口转换：None / 空串 / 非法值 / 越界一律回落默认值，绝不抛。

    `config.json` 里手改出 `"listen_port": null` 或 `"7900 "` 这类值很常见，
    以前 `int(None)` 会直接 TypeError 把整个进程打死。
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        port = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        log.debug("非法端口值 %r，回落默认 %s", value, default)
        return default
    if not 0 <= port <= 65535:
        log.debug("端口越界 %r，回落默认 %s", value, default)
        return default
    return port


def _as_timeout(value: Any, default: float = DEFAULT_TIMEOUT) -> float:
    """容错超时转换：非正数 / 非法值回落默认，绝不抛。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        secs = float(value)
    except (TypeError, ValueError, OverflowError):
        log.debug("非法超时值 %r，回落默认 %s", value, default)
        return default
    if not secs > 0:
        log.debug("超时值非正 %r，回落默认 %s", value, default)
        return default
    return secs


def _close_writer(writer: Any) -> None:
    """关 writer：已经关了、传输层已炸都不许抛。"""
    try:
        writer.close()
    except (OSError, RuntimeError):
        pass


# --------------------------------------------------------------- upstream
@dataclass
class Upstream:
    kind: str = "direct"          # direct | socks5 | http
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    timeout: float = 12.0

    @property
    def enabled(self) -> bool:
        if self.kind not in ("socks5", "http") or not self.host:
            return False
        # 只排掉真正的占位符；127.0.0.1 是本机客户端（Clash/v2rayN/sing-box）的常态
        return self.host.upper() not in ("NODE_HOST", "NODE", "HOST", "EXAMPLE.COM",
                                         "CHANGE_ME")


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    conns: int = 0
    errors: int = 0
    up_bytes: int = 0
    down_bytes: int = 0
    via_upstream: int = 0
    via_direct: int = 0
    hosts: dict[str, int] = field(default_factory=dict)

    def hit(self, host: str) -> None:
        self.hosts[host] = self.hosts.get(host, 0) + 1
        if len(self.hosts) > 4096:          # 长跑也别把内存吃掉
            keep = sorted(self.hosts.items(), key=lambda kv: -kv[1])[:1024]
            self.hosts = dict(keep)

    def snapshot(self) -> dict[str, Any]:
        top = sorted(self.hosts.items(), key=lambda kv: -kv[1])[:15]
        return {
            "uptime_s": round(time.time() - self.started, 1),
            "connections": self.conns,
            "errors": self.errors,
            "via_upstream": self.via_upstream,
            "via_direct": self.via_direct,
            "up_mb": round(self.up_bytes / 1048576, 2),
            "down_mb": round(self.down_bytes / 1048576, 2),
            "top_hosts": top,
        }


# --------------------------------------------------------------- dialing
async def _open_tcp(host: str, port: int, timeout: float):
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)


async def dial_direct(host: str, port: int, timeout: float):
    return await _open_tcp(host, port, timeout)


async def _race_connect(ips: list[str], port: int, timeout: float,
                        stagger: float = 0.12):
    """多 IP 并发的 happy-eyeballs：谁先连上就用谁，其余取消。

    Windows 上 asyncio.open_connection(域名) 会按 getaddrinfo 顺序串行尝试，
    第一个 IP 被墙就能卡满整个超时（store.steampowered.com 经常这样）。
    """
    async def attempt(ip: str, delay: float):
        if delay:
            await asyncio.sleep(delay)
        return await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)

    tasks = [asyncio.create_task(attempt(ip, i * stagger))
             for i, ip in enumerate(ips[:4])]
    pending = set(tasks)
    errors: list[BaseException] = []
    winner: asyncio.Task | None = None
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    # 竞速的其它分支可能同时连上：第一个当赢家，剩下的必须关掉，
                    # 否则每次拨号都漏一个 fd（长跑几十万连接会耗尽句柄）
                    if winner is None:
                        winner = task
                    else:
                        _close_result(task)
                else:
                    errors.append(task.exception())
            if winner is not None:
                break
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # 取消与「刚好连上」之间存在窗口，这里统一兜底回收非赢家的连接
        for task in tasks:
            if task is winner:
                continue
            _close_result(task)
    if winner is not None:
        return winner.result()
    raise OSError(f"全部 {len(ips)} 个 IP 连接失败: {errors[:2]}")


def _close_result(task: asyncio.Task) -> None:
    """关掉一个已完成「竞速任务」拿到的连接；失败/取消/未完成时静默。"""
    if task.cancelled():
        return
    try:
        if task.exception() is not None:
            return
        _reader, writer = task.result()
    except (asyncio.CancelledError, asyncio.InvalidStateError, OSError):
        return
    try:
        writer.close()
    except OSError:
        pass


async def dial_direct_smart(host: str, port: int, timeout: float):
    """域名先自己做 DoH+系统解析，再多 IP 竞速；IP 直接连。"""
    if netutil.is_ip(host):
        return await dial_direct(host, port, timeout)
    ips, _source = await asyncio.to_thread(netutil.resolve_merged, host, timeout)
    if not ips:
        raise OSError(f"解析失败: {host}")
    try:
        return await _race_connect(ips, port, timeout)
    except OSError:
        # 兜底交给系统解析器（可能有我们没拿到的 IPv6/就近记录）
        return await _open_tcp(host, port, timeout)


async def dial_socks5(up: Upstream, host: str, port: int):
    reader, writer = await _open_tcp(up.host, up.port, up.timeout)
    try:
        if up.username:
            writer.write(b"\x05\x02\x00\x02")
        else:
            writer.write(b"\x05\x01\x00")
        await writer.drain()
        ver, method = await asyncio.wait_for(reader.readexactly(2), up.timeout)
        if ver != 0x05:
            raise ConnectionError("上游不是 SOCKS5")
        if method == 0x02:
            u = up.username.encode()[:255]
            p = up.password.encode()[:255]
            writer.write(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
            await writer.drain()
            _, status = await asyncio.wait_for(reader.readexactly(2), up.timeout)
            if status != 0x00:
                raise ConnectionError("上游 SOCKS5 认证失败")
        elif method != 0x00:
            raise ConnectionError(f"上游拒绝认证方式: {method}")

        # IP 字面量直接打包，域名交给上游解析（远端 DNS）
        # 注意必须区分 v4/v6：老实现只认 "全数字+点"，IPv6 会掉进域名分支用 ATYP=3
        # 发出去，上游按域名去解一个 ::1 必然失败（v6-only 站点直接连不上）
        atyp = 0x03
        payload = b""
        try:
            addr = ipaddress.ip_address(host.strip("[]")) if host else None
        except ValueError:
            addr = None
        if isinstance(addr, ipaddress.IPv4Address):
            atyp, payload = 0x01, addr.packed
        elif isinstance(addr, ipaddress.IPv6Address):
            atyp, payload = 0x04, addr.packed
        if atyp == 0x03:
            payload = host.encode("idna") if not host.isascii() else host.encode()[:255]
            req = (b"\x05\x01\x00\x03" + bytes([len(payload)]) + payload
                   + struct.pack(">H", port))
        else:
            req = b"\x05\x01\x00" + bytes([atyp]) + payload + struct.pack(">H", port)
        writer.write(req)
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), up.timeout)
        if head[1] != 0x00:
            raise ConnectionError(f"上游 CONNECT 失败 code={head[1]}")
        atyp = head[3]
        if atyp == 0x01:
            await reader.readexactly(4 + 2)
        elif atyp == 0x03:
            ln = await reader.readexactly(1)
            await reader.readexactly(ln[0] + 2)
        elif atyp == 0x04:
            await reader.readexactly(16 + 2)
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def dial_http_connect(up: Upstream, host: str, port: int):
    reader, writer = await _open_tcp(up.host, up.port, up.timeout)
    try:
        auth = ""
        if up.username:
            token = base64.b64encode(
                f"{up.username}:{up.password}".encode()
            ).decode()
            auth = f"Proxy-Authorization: Basic {token}\r\n"
        target = f"[{host}]" if ":" in host else host
        req = (
            f"CONNECT {target}:{port} HTTP/1.1\r\n"
            f"Host: {target}:{port}\r\n"
            f"{auth}"
            "Proxy-Connection: keep-alive\r\n\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), up.timeout)
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 2 or not parts[1].startswith("2"):
            raise ConnectionError(f"上游 HTTP CONNECT 失败: {line}")
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def dial(up: Upstream, host: str, port: int, timeout: float = 12.0):
    if up.kind == "socks5" and up.enabled:
        return await dial_socks5(up, host, port)
    if up.kind == "http" and up.enabled:
        return await dial_http_connect(up, host, port)
    return await dial_direct(host, port, timeout)


# --------------------------------------------------------------- piping
# 单向拷贝可以「静默结束」的异常：对端断开 / 超时 / 流被提前复用 / 传输层已关。
# CancelledError 是 BaseException，不在此列，必须照常上抛给 gather 回收。
PIPE_SILENT = (
    OSError,                      # 含 ConnectionResetError / BrokenPipeError / ConnectionError
    asyncio.IncompleteReadError,
    TimeoutError,
    RuntimeError,                 # cannot reuse already awaited coroutine 之类
)


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
               counter: list[int] | None = None) -> None:
    try:
        while True:
            data = await src.read(BUF)
            if not data:
                break
            dst.write(data)
            await dst.drain()
            if counter is not None:
                counter[0] += len(data)
    except PIPE_SILENT:
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (OSError, RuntimeError):
            pass


async def _bridge(cr, cw, sr, sw, stats: Stats) -> None:
    """双向摆渡。**任何情况下都不许把异常抛给 handler**，且不留 pending task。

    旧实现 `await gather(t1, t2)` 在 t1 抛异常时直接向外传播，t2 还挂着；
    `finally` 里 cancel 了却从不 await，于是日志里出现
    `Task was destroyed but it is pending` / `cannot reuse already awaited coroutine`。
    """
    c2s: list[int] = [0]
    s2c: list[int] = [0]
    t1 = asyncio.create_task(pipe(cr, sw, c2s))
    t2 = asyncio.create_task(pipe(sr, cw, s2c))
    tasks = (t1, t2)
    try:
        results = await asyncio.gather(t1, t2, return_exceptions=True)
        for res in results:
            if isinstance(res, BaseException) and not isinstance(
                    res, asyncio.CancelledError):
                log.debug("bridge 单向退出: %r", res)
    finally:
        # 另一半还挂着就先取消，再回收（await 掉），否则解释器关 loop 时会骂人
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # 外层（handler 所在 task）被取消：吞掉这一次，保证下面照样收尾
            pass
        stats.up_bytes += c2s[0]
        stats.down_bytes += s2c[0]
        for w in (cw, sw):
            _close_writer(w)


def _reply_socks(w: asyncio.StreamWriter, code: int = 0) -> None:
    w.write(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00\x00\x00\x00" + b"\x00\x00")


async def _bad_request(w: asyncio.StreamWriter) -> None:
    """回 400 并收工：目标端口写错（CONNECT host:abc）时不许 ValueError 穿出去。"""
    try:
        w.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
    except (OSError, RuntimeError):
        pass


# --------------------------------------------------------------- servers
class ProxyServer:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.router = Router(
            mode=cfg.get("mode", "whitelist"),
            proxy_suffixes=cfg.get("proxy_suffixes"),
            direct_suffixes=cfg.get("direct_suffixes"),
            direct_hosts=cfg.get("direct_hosts"),
            proxy_hosts=cfg.get("proxy_hosts"),
        )
        u = cfg.get("upstream") or {}
        self.timeout = _as_timeout(cfg.get("connect_timeout"), DEFAULT_TIMEOUT)
        self.upstream = Upstream(
            kind=(u.get("kind") or "direct").lower(),
            host=u.get("host", "") or "",
            port=_as_port(u.get("port"), 0),
            username=u.get("username", "") or "",
            password=u.get("password", "") or "",
            timeout=self.timeout,
        )
        self.stats = Stats()
        # domain -> 已知最优 IP（CDN 选边结果），直连时优先用它拨号
        self.ip_map: dict[str, str] = dict(cfg.get("ip_map") or {})
        self.auth = cfg.get("auth") or {}          # 本地鉴权，防止局域网白嫖
        # listen_host 为 null/空串时绝不能落到 asyncio 的「双栈全网卡」语义上
        _h = cfg.get("listen_host")
        self.host = _h.strip() if isinstance(_h, str) and _h.strip() else "127.0.0.1"
        # 容错：config.json 里写 null / "" / "abc" 只回落默认，不许 TypeError。
        # 注意「键整个不写」= 不要 HTTP 监听（历史语义，冒烟测试与单测依赖），
        # 而「写了但值是 null/空/非法」才回落默认 7900。
        self.port = _as_port(cfg.get("listen_port"), DEFAULT_LISTEN_PORT)
        if "http_port" in cfg:
            self.http_port = _as_port(cfg["http_port"], DEFAULT_HTTP_PORT)
        else:
            self.http_port = 0
        self._servers: list[asyncio.AbstractServer] = []

    @property
    def listen_port(self) -> int:
        """只读别名：对外统一用 listen_port，内部历史字段 self.port 保留。"""
        return self.port

    # ---- 本地鉴权
    def _check_auth(self, user: str, pwd: str) -> bool:
        a = self.auth
        return bool(a.get("username")) and user == a["username"] and pwd == a["password"]

    @property
    def auth_required(self) -> bool:
        return bool((self.auth or {}).get("username"))

    # ---- 路由 + 拨号
    async def _route(self, host: str, port: int):
        decision = self.router.decide(host)
        if decision == "upstream" and self.upstream.enabled:
            self.stats.via_upstream += 1
        else:
            decision = "direct"
            self.stats.via_direct += 1
        log.debug("route %s:%s -> %s", host, port, decision)
        if decision == "upstream":
            return await dial(self.upstream, host, port, self.timeout), decision
        hint = self.ip_map.get(host.lower())
        if hint:
            try:
                return await dial_direct(hint, port, self.timeout), f"direct[{hint}]"
            except OSError as exc:      # 缓存 IP 失效就退回正常解析
                log.debug("ip_map %s->%s 失败(%s)，回退解析", host, hint, exc)
        return await dial_direct_smart(host, port, self.timeout), decision

    # ---- SOCKS5
    async def handle_socks(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        """client_connected_cb 入口：任何异常都在这里终结，不许穿到 asyncio。"""
        try:
            await self._socks_session(reader, writer)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            log.debug("socks 兜底：%r", exc)
        finally:
            _close_writer(writer)

    async def _socks_session(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            ver, n = await asyncio.wait_for(reader.readexactly(2), 10)
            if ver != 0x05:
                return
            methods = await reader.readexactly(n)
            need = self.auth_required
            if need and 0x02 in methods:
                writer.write(b"\x05\x02")
            elif not need and 0x00 in methods:
                writer.write(b"\x05\x00")
            else:
                writer.write(b"\x05\xff")
                return
            await writer.drain()

            if need:
                await reader.readexactly(1)
                ulen = (await reader.readexactly(1))[0]
                user = (await reader.readexactly(ulen)).decode("utf-8", "replace")
                plen = (await reader.readexactly(1))[0]
                pwd = (await reader.readexactly(plen)).decode("utf-8", "replace")
                if not self._check_auth(user, pwd):
                    writer.write(b"\x01\x01")
                    await writer.drain()
                    return
                writer.write(b"\x01\x00")
                await writer.drain()

            head = await asyncio.wait_for(reader.readexactly(4), 10)
            if head[0] != 0x05:
                return
            atyp = head[3]
            if atyp == 0x01:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif atyp == 0x03:
                ln = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(ln)).decode("ascii", "replace")
            elif atyp == 0x04:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            else:
                _reply_socks(writer, 0x08)
                await writer.drain()
                return
            port = struct.unpack(">H", await reader.readexactly(2))[0]

            self.stats.conns += 1
            self.stats.hit(host)
            try:
                (sr, sw), decision = await self._route(host, port)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                log.warning("socks dial fail %s:%s %s", host, port, exc)
                _reply_socks(writer, 0x05 if "refused" in str(exc).lower() else 0x04)
                try:
                    await writer.drain()
                except (OSError, RuntimeError):
                    pass
                return
            _reply_socks(writer, 0x00)
            await writer.drain()
            await _bridge(reader, writer, sr, sw, self.stats)
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            log.debug("socks err %s %s", peer, exc)

    # ---- HTTP proxy
    async def handle_http(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        """client_connected_cb 入口：任何异常都在这里终结，不许穿到 asyncio。"""
        try:
            await self._http_session(reader, writer)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            log.debug("http 兜底：%r", exc)
        finally:
            _close_writer(writer)

    async def _http_session(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
        except (asyncio.IncompleteReadError, TimeoutError, asyncio.LimitOverrunError,
                ConnectionError, OSError):
            return
        try:
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if self.auth_required:
            want = "Basic " + base64.b64encode(
                f"{self.auth['username']}:{self.auth['password']}".encode()
            ).decode()
            if headers.get("proxy-authorization", "") != want:
                body = b"proxy auth required"
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Proxy-Authenticate: Basic realm=\"svpn\"\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
                return

        # CONNECT 隧道
        if method.upper() == "CONNECT":
            host, _, p = target.rpartition(":")
            host = host.strip("[]")
            try:
                port = int(p or 443)
            except ValueError:
                await _bad_request(writer)      # CONNECT host:abc
                return
            if not 0 < port < 65536:
                await _bad_request(writer)
                return
            self.stats.conns += 1
            self.stats.hit(host)
            try:
                (sr, sw), _ = await self._route(host, port)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                log.warning("http connect fail %s:%s %s", host, port, exc)
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await _bridge(reader, writer, sr, sw, self.stats)
            return

        # 普通代理请求（绝对 URI）
        if not target.lower().startswith("http://"):
            await _bad_request(writer)
            return
        rest = target[7:]
        hostport, _, path = rest.partition("/")
        host, _, port_s = hostport.partition(":")
        host = host.strip("[]")
        try:
            port = int(port_s or 80)
        except ValueError:
            await _bad_request(writer)          # http://host:abc/
            return
        if not 0 < port < 65536:
            await _bad_request(writer)
            return
        if not host:
            await _bad_request(writer)
            return
        self.stats.conns += 1
        self.stats.hit(host)
        try:
            (sr, sw), _ = await self._route(host, port)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            log.warning("http get fail %s:%s %s", host, port, exc)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        out = [f"{method} /{path} {version}"]
        for k, v in headers.items():
            if k in ("proxy-authorization", "proxy-connection", "connection"):
                continue
            out.append(f"{k.title()}: {v}")
        out.append("Connection: close")
        sw.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
        await sw.drain()
        await _bridge(reader, writer, sr, sw, self.stats)

    # ---- 生命周期
    async def start(self) -> None:
        if self._servers:          # 幂等，避免二次 bind 10048
            return
        if self.http_port and self.http_port == self.port:
            # 提前拦住：否则第二次 bind 抛裸 OSError 10048，用户看不懂
            raise ValueError(f"listen_port 与 http_port 相同: {self.port}")
        s1 = await self._bind(self.handle_socks, self.port)
        self._servers.append(s1)
        log.info("SOCKS5   listening %s:%s", self.host, self.port)
        if self.http_port:
            s2 = await self._bind(self.handle_http, self.http_port)
            self._servers.append(s2)
            log.info("HTTP     listening %s:%s", self.host, self.http_port)

    async def _bind(self, handler, port: int) -> asyncio.AbstractServer:
        # Windows 上偶发 WinError 121（信号灯超时），重试一次即可
        last: BaseException | None = None
        for attempt in range(3):
            try:
                return await asyncio.start_server(
                    handler, self.host, port, limit=MAX_HEAD, backlog=512)
            except OSError as exc:
                last = exc
                if getattr(exc, "winerror", None) != 121:
                    raise
                await asyncio.sleep(0.3 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _dump_stats(self, path: str) -> None:
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.stats.snapshot(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except (OSError, ValueError, TypeError):
            pass

    async def _stats_loop(self, path: str, interval: float = 5.0) -> None:
        """周期落盘：`svpn down` 是 taskkill /F，进程没有机会走 finally。"""
        try:
            while True:
                await asyncio.sleep(interval)
                self._dump_stats(path)
        except asyncio.CancelledError:
            pass

    async def serve_forever(self, stats_path: str | None = None) -> None:
        await self.start()
        writer = asyncio.create_task(self._stats_loop(stats_path)) if stats_path else None
        try:
            await asyncio.gather(*(s.serve_forever() for s in self._servers))
        except asyncio.CancelledError:
            raise
        finally:
            if writer:
                writer.cancel()
            if stats_path:
                self._dump_stats(stats_path)

    async def close(self) -> None:
        servers, self._servers = self._servers, []
        for s in servers:
            s.close()
            await s.wait_closed()
