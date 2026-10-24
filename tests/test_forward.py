"""svpn.forward 的离线单元测试。

原则：
    * 标准库 only，不起任何第三方依赖；
    * 绝不联网：只在 127.0.0.1 上开随机端口的假上游/假客户端，DNS 不参与；
    * ClientHello 手工拼字节，不依赖 ssl 握手（更快、更可控）；
    * 每个用例自带清理，整体 < 5 秒。

覆盖：
    * `_allowed()` 白名单语义（空表全拒 / 后缀匹配 / 不误命中 / 大小写 / 前导点）；
    * `extract_sni()` 的解析与畸形输入；
    * 端到端转发 + 上游 writer 关闭（socket 泄漏回归）；
    * 白名单外 SNI 被立刻关闭；
    * `listen_port=0` 时端口回写、绑定冲突抛 OSError、连接结束后无残留 task。

运行：
    python tests/test_forward.py            # 退出码 0 = 全绿
    python tests/test_forward.py -v         # 看每个用例
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn.forward import SniForwarder, extract_sni            # noqa: E402
from svpn.proxy import Upstream                               # noqa: E402

# 测试里会故意打「SNI 拒绝」警告，别让它污染 stderr
logging.getLogger("svpn.forward").addHandler(logging.NullHandler())
logging.getLogger("svpn.forward").propagate = False


# ---------------------------------------------------------------- 手工 ClientHello
def build_client_hello(sni: str, with_sni_ext: bool = True) -> bytes:
    """拼一个最小可解析的 TLS 1.2 ClientHello（record + handshake + SNI 扩展）。"""
    exts = b""
    if with_sni_ext:
        name = sni.encode("ascii")
        sni_data = struct.pack(">HBH", len(name) + 3, 0, len(name)) + name
        exts += struct.pack(">HH", 0x0000, len(sni_data)) + sni_data
    exts += struct.pack(">HH", 0x000A, 4) + b"\x00\x1d\x00\x17"   # supported_groups

    body = (
        b"\x03\x03"                              # client_version TLS1.2
        + b"\x2a" * 32                           # random
        + b"\x00"                                # session_id 长度 0
        + struct.pack(">H", 2) + b"\x13\x01"     # cipher_suites: TLS_AES_128_GCM_SHA256
        + b"\x01\x00"                            # compression_methods: null
        + struct.pack(">H", len(exts)) + exts
    )
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


# ---------------------------------------------------------------- 假上游
class FakeUpstream:
    """本地 TCP 回显服务：收到什么回 `echo:` + 原数据，并记录关闭次数。"""

    def __init__(self) -> None:
        self.port = 0
        self.received = b""
        self.closed = 0
        self._srv: asyncio.AbstractServer | None = None

    async def start(self) -> "FakeUpstream":
        async def handler(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    self.received += data
                    writer.write(b"echo:" + data)
                    await writer.drain()
            finally:
                self.closed += 1
                writer.close()

        self._srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.port = self._srv.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv:
            srv.close()
            await srv.wait_closed()


def _direct_up() -> Upstream:
    """直连上游（不走节点），保证测试不需要任何外部网络。"""
    return Upstream(kind="direct")


# ---------------------------------------------------------------- 白名单
class TestAllowed(unittest.TestCase):
    """`_allowed()` 是「本机 443 会不会变成开放出口」的唯一闸门。"""

    def test_empty_allowlist_denies_everything(self) -> None:
        for arg in (None, [], [""], ["  "]):
            fwd = SniForwarder(_direct_up(), allow_suffixes=arg)
            self.assertEqual(fwd.allow, [], f"allow_suffixes={arg!r} 应归一成空表")
            self.assertIs(fwd._allowed("a.com"), False)
            self.assertIs(fwd._allowed("cdn.akamai.steamstatic.com"), False)

    def test_suffix_match(self) -> None:
        fwd = SniForwarder(_direct_up(), allow_suffixes=["akamai.steamstatic.com"])
        self.assertTrue(fwd._allowed("akamai.steamstatic.com"))       # 精确
        self.assertTrue(fwd._allowed("cdn.akamai.steamstatic.com"))   # 子域
        self.assertTrue(fwd._allowed("a.b.akamai.steamstatic.com"))   # 多级子域

    def test_no_partial_label_match(self) -> None:
        fwd = SniForwarder(_direct_up(), allow_suffixes=["akamai.steamstatic.com"])
        self.assertFalse(fwd._allowed("evil-akamai.steamstatic.com"))
        self.assertFalse(fwd._allowed("akamai.steamstatic.com.evil.com"))
        self.assertFalse(fwd._allowed("xakamai.steamstatic.com"))
        self.assertFalse(fwd._allowed(""))

    def test_case_and_leading_dot(self) -> None:
        fwd = SniForwarder(_direct_up(),
                           allow_suffixes=[".Akamai.SteamStatic.com", " steamcommunity.COM "])
        self.assertEqual(fwd.allow, ["akamai.steamstatic.com", "steamcommunity.com"])
        self.assertTrue(fwd._allowed("CDN.Akamai.SteamStatic.COM"))
        self.assertTrue(fwd._allowed("store.steamcommunity.com"))
        self.assertTrue(fwd._allowed("akamai.steamstatic.com."))       # 结尾的根点
        self.assertFalse(fwd._allowed("notsteamcommunity.com"))


# ---------------------------------------------------------------- SNI 解析
class TestExtractSni(unittest.TestCase):
    def test_normal(self) -> None:
        hello = build_client_hello("cdn.akamai.steamstatic.com")
        self.assertEqual(extract_sni(hello), "cdn.akamai.steamstatic.com")

    def test_lowercased(self) -> None:
        self.assertEqual(extract_sni(build_client_hello("SteamCDN-A.Akamaihd.NET")),
                         "steamcdn-a.akamaihd.net")

    def test_trailing_dot_stripped(self) -> None:
        self.assertEqual(extract_sni(build_client_hello("cdn.example.com.")),
                         "cdn.example.com")

    def test_hello_with_extra_payload(self) -> None:
        hello = build_client_hello("cdn.example.com")
        self.assertEqual(extract_sni(hello + b"\x17\x03\x03\x00\x05hello"),
                         "cdn.example.com")

    def test_no_sni_extension(self) -> None:
        self.assertIsNone(extract_sni(build_client_hello("x.com", with_sni_ext=False)))

    def test_garbage(self) -> None:
        for bad in (b"", b"GET / HTTP/1.1\r\n\r\n", b"\x16", b"\x16\x03\x01\x00\x40",
                    b"\x16\x03\x01\xff\xffsh", b"\x00" * 64,
                    build_client_hello("a.com")[:20]):
            self.assertIsNone(extract_sni(bad), repr(bad[:16]))


# ---------------------------------------------------------------- 端到端
class TestForwarderEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.up_srv = await FakeUpstream().start()
        self.fwd: SniForwarder | None = None

    async def asyncTearDown(self) -> None:
        if self.fwd is not None:
            await self.fwd.close()
        await self.up_srv.stop()

    async def _start(self, allow: list[str] | None, **kw) -> SniForwarder:
        fwd = SniForwarder(_direct_up(), listen_port=0, allow_suffixes=allow,
                           target_port=self.up_srv.port,
                           ip_map={"cdn.example.com": "127.0.0.1"}, **kw)
        await fwd.start()
        self.fwd = fwd
        return fwd

    async def test_data_forwarded_and_upstream_closed(self) -> None:
        fwd = await self._start(["example.com"])
        self.assertGreater(fwd.listen_port, 0, "listen_port=0 必须回写真实端口")

        hello = build_client_hello("cdn.example.com")
        client_r, client_w = await asyncio.open_connection("127.0.0.1", fwd.listen_port)

        # 只在这之后挂 spy：拦住 SniForwarder 拨上游拿到的那条 writer
        upstream_writers: list[asyncio.StreamWriter] = []
        orig = asyncio.open_connection

        async def spy(*a, **kw):
            r, w = await orig(*a, **kw)
            upstream_writers.append(w)
            return r, w

        asyncio.open_connection = spy
        try:
            client_w.write(hello + b"PING")
            await client_w.drain()
            got = await asyncio.wait_for(client_r.read(65535), 3)
            self.assertEqual(got, b"echo:" + hello + b"PING")
            self.assertTrue(self.up_srv.received.startswith(hello),
                            "ClientHello 必须原样转发给上游")

            client_w.close()
            for _ in range(200):                     # 等 finally 走完
                if upstream_writers and upstream_writers[0].is_closing():
                    break
                await asyncio.sleep(0.01)
        finally:
            asyncio.open_connection = orig
            client_w.close()

        self.assertEqual(len(upstream_writers), 1)
        self.assertTrue(upstream_writers[0].is_closing(),
                        "成功路径必须关闭上游 socket，否则每条连接漏一个 fd")

    async def test_reject_sni_outside_allowlist(self) -> None:
        fwd = await self._start(["example.com"])
        client_r, client_w = await asyncio.open_connection("127.0.0.1", fwd.listen_port)
        try:
            client_w.write(build_client_hello("evil.example.net") + b"x")
            await client_w.drain()
            got = await asyncio.wait_for(client_r.read(1024), 3)
        finally:
            client_w.close()
        self.assertEqual(got, b"", "白名单外 SNI 必须被立刻关闭")
        self.assertEqual(self.up_srv.received, b"", "拒绝的连接不能碰到上游")
        self.assertEqual(fwd.stats.errors, 1)

    async def test_reject_non_tls_payload(self) -> None:
        fwd = await self._start(["example.com"])
        client_r, client_w = await asyncio.open_connection("127.0.0.1", fwd.listen_port)
        try:
            client_w.write(b"GET / HTTP/1.1\r\nHost: cdn.example.com\r\n\r\n")
            await client_w.drain()
            got = await asyncio.wait_for(client_r.read(1024), 3)
        finally:
            client_w.close()
        self.assertEqual(got, b"")

    async def test_empty_allowlist_starts_but_rejects_all(self) -> None:
        fwd = await self._start(None)
        self.assertEqual(fwd.allow, [])
        self.assertGreater(fwd.listen_port, 0, "空白名单也要能起监听（只是全拒）")
        client_r, client_w = await asyncio.open_connection("127.0.0.1", fwd.listen_port)
        try:
            client_w.write(build_client_hello("cdn.example.com"))
            await client_w.drain()
            got = await asyncio.wait_for(client_r.read(1024), 3)
        finally:
            client_w.close()
        self.assertEqual(got, b"")
        self.assertEqual(self.up_srv.received, b"")

    async def test_bind_conflict_raises_oserror(self) -> None:
        fwd = await self._start(["example.com"])
        dup = SniForwarder(_direct_up(), listen_port=fwd.listen_port,
                           allow_suffixes=["example.com"])
        with self.assertRaises(OSError):
            await dup.start()

    async def test_no_leaked_tasks(self) -> None:
        fwd = await self._start(["example.com"])
        for _ in range(3):
            hello = build_client_hello("cdn.example.com")
            r, w = await asyncio.open_connection("127.0.0.1", fwd.listen_port)
            w.write(hello)
            await w.drain()
            await asyncio.wait_for(r.read(65535), 3)
            w.close()

        me = asyncio.current_task()

        def is_accept(t: "asyncio.Task") -> bool:
            # 监听 socket 的 accept() 协程本来就常驻，不算泄漏
            return "accept_coro" in getattr(t.get_coro(), "__qualname__", "")

        pending: list[asyncio.Task] = []
        for _ in range(400):
            pending = [t for t in asyncio.all_tasks()
                       if t is not me and not t.done() and not is_accept(t)]
            if not pending:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(pending, [], f"连接结束后残留 task: {pending}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
