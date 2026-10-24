"""TCP 转发层。

两种用法：
1. Forwarder      —— 固定目标端口转发（例如某个游戏登录服）。
2. SniForwarder    —— 监听 127.0.0.1:443，按 TLS ClientHello 里的 SNI 决定真实目标。
   Steam 的下载/更新是裸 TCP，HTTP 代理管不到；把 CDN 域名 hosts 到 127.0.0.1 后，
   由这里读 SNI 还原出真实域名，再从节点拨号，等于给下载流量做了个透明隧道。
"""
from __future__ import annotations

import asyncio
import logging

from .proxy import Upstream, dial, pipe, Stats

log = logging.getLogger("svpn.forward")


class Forwarder:
    """listen 127.0.0.1:port -> upstream(target_host:target_port)"""

    def __init__(self, name: str, listen_port: int, target_host: str,
                 target_port: int, up: Upstream, stats: Stats | None = None) -> None:
        self.name = name
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.up = up
        self.stats = stats or Stats()
        self._server: asyncio.AbstractServer | None = None

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self.stats.conns += 1
        try:
            sr, sw = await dial(self.up, self.target_host, self.target_port,
                                self.up.timeout)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            log.warning("[%s] 转发失败 %s:%s %s", self.name, self.target_host,
                        self.target_port, exc)
            writer.close()
            return
        log.debug("[%s] 接通 %s:%s", self.name, self.target_host, self.target_port)
        try:
            # return_exceptions=True：一侧抛错时另一侧不会被卡死，也不会把
            # 异常抛给 client_connected_cb（asyncio 会打 Unhandled exception）
            await asyncio.gather(pipe(reader, sw), pipe(sr, writer),
                                 return_exceptions=True)
        finally:
            for w in (writer, sw):
                try:
                    w.close()
                except OSError:
                    pass

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.listen_port, backlog=256)
        self.listen_port = _bound_port(self._server, self.listen_port)
        log.info("[%s] 127.0.0.1:%s -> %s:%s", self.name, self.listen_port,
                 self.target_host, self.target_port)

    async def close(self) -> None:
        srv, self._server = self._server, None
        if srv:
            srv.close()
            await srv.wait_closed()


# ------------------------------------------------------------------ SNI 解析
def _bound_port(server: asyncio.AbstractServer, default: int) -> int:
    """取实际绑定端口（listen_port=0 时由内核分配）。"""
    socks = server.sockets or ()
    if socks:
        try:
            return int(socks[0].getsockname()[1])
        except OSError:
            pass
    return default


def extract_sni(data: bytes) -> str | None:
    """从 TLS ClientHello 里取 server_name。拿不到就返回 None。"""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        rec_len = int.from_bytes(data[3:5], "big")
        if len(data) < 5 + rec_len:
            return None
        hs = data[5:5 + rec_len]
        if not hs or hs[0] != 0x01:
            return None
        p = 4                                   # hs type(1)+len(3)
        p += 2 + 32                             # client_version + random
        sid_len = hs[p]
        p += 1 + sid_len
        cs_len = int.from_bytes(hs[p:p + 2], "big")
        p += 2 + cs_len
        comp_len = hs[p]
        p += 1 + comp_len
        if p + 2 > len(hs):
            return None
        ext_total = int.from_bytes(hs[p:p + 2], "big")
        p += 2
        end = min(p + ext_total, len(hs))
        while p + 4 <= end:
            etype = int.from_bytes(hs[p:p + 2], "big")
            elen = int.from_bytes(hs[p + 2:p + 4], "big")
            p += 4
            if etype == 0x0000:                 # server_name
                seg = hs[p:p + elen]
                if len(seg) < 5:
                    return None
                nlen = int.from_bytes(seg[3:5], "big")
                raw = seg[5:5 + nlen]
                # 注意：bytes.decode("idna", "replace") 会抛 UnicodeError（idna
                # 编码器不支持 error handler），这里只做 ascii → punycode 还原
                try:
                    name = raw.decode("ascii").lower()
                except UnicodeDecodeError:
                    name = raw.decode("latin-1").lower()
                name = name.rstrip(".")
                if name.endswith(".xn--") or ".xn--" in name:
                    try:
                        name = ".".join(
                            lbl.encode().decode("idna") if lbl.startswith("xn--") else lbl
                            for lbl in name.split("."))
                    except (UnicodeError, ValueError):
                        pass
                return name or None
            p += elen
        return None
    except (IndexError, ValueError):
        return None


class SniForwarder:
    """127.0.0.1:port 上按 SNI 透明转发；白名单外一律拒绝，避免变成开放代理。"""

    def __init__(self, up: Upstream, listen_port: int = 443,
                 allow_suffixes: list[str] | None = None,
                 target_port: int = 443, stats: Stats | None = None,
                 max_head: int = 16384, ip_map: dict[str, str] | None = None) -> None:
        self.up = up
        self.listen_port = listen_port
        # 前导点吃掉、统一小写；空列表 = 谁都不放行（见 _allowed）
        self.allow = [str(s).strip().lower().lstrip(".") for s in (allow_suffixes or [])
                      if str(s).strip()]
        self.target_port = target_port
        self.stats = stats or Stats()
        self.max_head = max_head
        # domain -> 最优边缘 IP（CDN 选边结果）；命中就直连该 IP，不走节点
        self.ip_map = {k.lower(): v for k, v in (ip_map or {}).items()}
        self._server: asyncio.AbstractServer | None = None

    def _allowed(self, host: str) -> bool:
        """精确相等或子域后缀命中白名单。

        白名单为空时一律拒绝：SniForwarder 只绑 127.0.0.1，如果空表放行，
        本机任意进程都能把它当通用 443 出口用。
        """
        host = (host or "").strip().lower().rstrip(".")
        if not host or not self.allow:
            return False
        return any(host == s or host.endswith("." + s) for s in self.allow)

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self.stats.conns += 1
        sw: asyncio.StreamWriter | None = None       # 必须在 try 外，finally 要关它
        try:
            head = b""
            while len(head) < self.max_head:
                chunk = await asyncio.wait_for(reader.read(4096), 10)
                if not chunk:
                    break
                head += chunk
                if len(head) >= 5:
                    if head[0] != 0x16:         # 不是 TLS 握手，别等满 10 秒
                        break
                    need_len = 5 + int.from_bytes(head[3:5], "big")
                    if len(head) >= need_len:
                        break
            sni = extract_sni(head)
            if not sni or not self._allowed(sni):
                log.warning("SNI 拒绝: %r", sni)
                self.stats.errors += 1
                return
            hint = self.ip_map.get(sni)
            if hint:
                try:
                    sr, sw = await asyncio.wait_for(
                        asyncio.open_connection(hint, self.target_port),
                        self.up.timeout)
                    log.debug("SNI %s -> 直连最优边缘 %s", sni, hint)
                except (OSError, asyncio.TimeoutError):
                    sr, sw = await dial(self.up, sni, self.target_port, self.up.timeout)
            else:
                sr, sw = await dial(self.up, sni, self.target_port, self.up.timeout)
            sw.write(head)                      # 把 ClientHello 原样送给目标
            await sw.drain()
            log.debug("SNI %s 已接通", sni)
            # return_exceptions=True：一侧出错时另一侧照常收尾，异常不外抛，
            # 避免 asyncio 打印 "Unhandled exception in client_connected_cb"
            await asyncio.gather(pipe(reader, sw), pipe(sr, writer),
                                 return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            log.debug("SNI 转发异常: %s", exc)
        finally:
            # 两侧都要关：漏掉 sw 就是每条 SNI 连接泄一个上游 socket
            for w in (writer, sw):
                if w is None:
                    continue
                try:
                    w.close()
                except OSError:
                    pass

    async def start(self) -> None:
        if not self.allow:
            log.warning("SNI 白名单为空，全部拒绝（检查 cdn.hosts 配置）")
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.listen_port, backlog=512)
        # port=0 时把内核分配的真实端口写回，调用方（cli）要拿它打日志
        self.listen_port = _bound_port(self._server, self.listen_port)
        log.info("[sni] 127.0.0.1:%s 按 SNI 转发（白名单 %d 条）",
                 self.listen_port, len(self.allow))

    async def close(self) -> None:
        srv, self._server = self._server, None
        if srv:
            srv.close()
            await srv.wait_closed()
