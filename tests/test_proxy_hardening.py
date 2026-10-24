"""proxy.py 健壮性回归（P1）。

覆盖 WORKLOG §7 静态扫描列出的 5 类真实炸点：
1. `_bridge()` 无兜底 → 异常穿出 handler、另一半 task 悬挂；
2. `pipe()` 异常白名单过窄；
3. `listen_port` / `http_port` / `connect_timeout` 容错（null / "" / "abc"）；
4. `start()` 在 listen_port == http_port 时抛裸 OSError 10048；
5. `handle_http` 的 `CONNECT host:abc` / 绝对 URI 非法端口 → 400 而不是 ValueError。

全部离线：只用 127.0.0.1 的临时监听端口，不发外部请求。
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn.proxy import (  # noqa: E402
    DEFAULT_HTTP_PORT,
    DEFAULT_LISTEN_PORT,
    DEFAULT_TIMEOUT,
    ProxyServer,
    Stats,
    _bridge,
    pipe,
)


# ---------------------------------------------------------------- 假对象
class FakeReader:
    """按脚本吐数据 / 抛异常的 StreamReader 替身。"""

    def __init__(self, chunks: list[bytes] | None = None,
                 exc: BaseException | None = None) -> None:
        self._chunks = list(chunks or [])
        self._exc = exc

    async def read(self, _n: int = -1) -> bytes:
        if self._exc is not None:
            raise self._exc
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class BlockingReader:
    """永远读不到数据，用来验证取消路径。"""

    async def read(self, _n: int = -1) -> bytes:
        await asyncio.Future()
        return b""      # pragma: no cover


class FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False
        self.eof = False

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.eof = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, _name: str, default=None):
        return default


def _pending_tasks() -> list[asyncio.Task]:
    cur = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not cur and not t.done()]


# ---------------------------------------------------------------- 1. 配置容错
class TestConfigTolerance(unittest.TestCase):
    def test_listen_port_falls_back(self) -> None:
        for bad in (None, "", "   ", "abc", "7900abc", True, 70000, -1, [1]):
            with self.subTest(value=bad):
                srv = ProxyServer({"listen_port": bad})
                self.assertEqual(srv.port, DEFAULT_LISTEN_PORT)
                self.assertEqual(srv.listen_port, DEFAULT_LISTEN_PORT)

    def test_http_port_falls_back(self) -> None:
        for bad in (None, "", "abc", True, 99999, -3):
            with self.subTest(value=bad):
                srv = ProxyServer({"http_port": bad})
                self.assertEqual(srv.http_port, DEFAULT_HTTP_PORT)

    def test_connect_timeout_falls_back(self) -> None:
        for bad in (None, "", "abc", True, 0, -2):
            with self.subTest(value=bad):
                srv = ProxyServer({"connect_timeout": bad})
                self.assertEqual(srv.timeout, DEFAULT_TIMEOUT)
                self.assertEqual(srv.upstream.timeout, DEFAULT_TIMEOUT)

    def test_valid_values_are_kept(self) -> None:
        srv = ProxyServer({"listen_port": "8100", "http_port": 8101,
                           "connect_timeout": "5"})
        self.assertEqual((srv.port, srv.http_port), (8100, 8101))
        self.assertEqual(srv.timeout, 5.0)

    def test_http_port_zero_still_means_off(self) -> None:
        """显式 0 = 不监听 HTTP，不能被容错逻辑吃掉。"""
        self.assertEqual(ProxyServer({"http_port": 0}).http_port, 0)

    def test_missing_http_port_means_off(self) -> None:
        """键整个不写 = 只要 SOCKS5（smoke_test 依赖这条历史语义）。"""
        self.assertEqual(ProxyServer({}).http_port, 0)
        self.assertEqual(ProxyServer({"listen_port": 7899}).http_port, 0)

    def test_bad_upstream_port_does_not_raise(self) -> None:
        srv = ProxyServer({"upstream": {"kind": "socks5", "host": "127.0.0.1",
                                        "port": "abc"}})
        self.assertEqual(srv.upstream.port, 0)

    def test_listen_host_never_falls_back_to_all_interfaces(self) -> None:
        """listen_host 为 null/空白时 asyncio 会绑 0.0.0.0（开放代理），必须回落回环。"""
        for bad in (None, "", "   ", 123):
            with self.subTest(value=bad):
                self.assertEqual(ProxyServer({"listen_host": bad}).host,
                                 "127.0.0.1")

    def test_listen_port_is_read_only_alias(self) -> None:
        srv = ProxyServer({"listen_port": 8123})
        self.assertEqual(srv.listen_port, srv.port)
        with self.assertRaises(AttributeError):
            srv.listen_port = 1      # type: ignore[misc]


# ---------------------------------------------------------------- 2. start 预检
class TestStartGuards(unittest.TestCase):
    def test_same_port_raises_value_error(self) -> None:
        srv = ProxyServer({"listen_host": "127.0.0.1",
                           "listen_port": 7900, "http_port": 7900})

        async def scenario() -> None:
            await srv.start()

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(scenario())
        self.assertIn("7900", str(ctx.exception))
        self.assertIn("相同", str(ctx.exception))
        self.assertEqual(srv._servers, [], "预检失败时不该 bind 成功一半")

    def test_start_is_idempotent(self) -> None:
        """listen_port=0 + http_port=0：只绑一个临时端口，二次调用直接返回。"""
        srv = ProxyServer({"listen_host": "127.0.0.1",
                           "listen_port": 0, "http_port": 0})

        async def scenario() -> None:
            await srv.start()
            n = len(srv._servers)
            await srv.start()
            self.assertEqual(len(srv._servers), n)
            await srv.close()

        asyncio.run(scenario())


# ---------------------------------------------------------------- 3. HTTP 400
class TestHttpBadPort(unittest.TestCase):
    """用真实回环 socket 打 handle_http，比假对象更接近线上。"""

    def _roundtrip(self, request: bytes) -> tuple[asyncio.AbstractEventLoop, bytes, list]:
        """起一个一次性 HTTP 代理，喂一段请求，返回 (loop, 响应, 异常处理器记录)。"""
        srv = ProxyServer({"listen_host": "127.0.0.1"})
        seen: list = []
        box: dict = {}

        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
            box["loop"] = loop
            server = await asyncio.start_server(srv.handle_http, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(request)
                await writer.drain()
                box["resp"] = await asyncio.wait_for(reader.read(), 3)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()
            await asyncio.sleep(0.05)      # 让回调里的异常有机会冒到 handler

        asyncio.run(scenario())
        return box["loop"], box["resp"], seen

    def _assert_400(self, request: bytes) -> None:
        _loop, resp, seen = self._roundtrip(request)
        self.assertTrue(resp.startswith(b"HTTP/1.1 400 Bad Request"),
                        f"应回 400，实际 {resp[:80]!r}")
        self.assertIn(b"Content-Length: 0", resp)
        self.assertEqual(seen, [], f"不该有未处理异常：{seen}")

    def test_connect_non_numeric_port(self) -> None:
        self._assert_400(b"CONNECT example.com:abc HTTP/1.1\r\n"
                         b"Host: example.com:abc\r\n\r\n")

    def test_connect_port_out_of_range(self) -> None:
        self._assert_400(b"CONNECT example.com:99999 HTTP/1.1\r\n\r\n")

    def test_absolute_uri_non_numeric_port(self) -> None:
        self._assert_400(b"GET http://example.com:abc/index.html HTTP/1.1\r\n"
                         b"Host: example.com\r\n\r\n")

    def test_absolute_uri_empty_host(self) -> None:
        self._assert_400(b"GET http://:80/ HTTP/1.1\r\n\r\n")

    def test_garbage_request_line_is_silent(self) -> None:
        _loop, resp, seen = self._roundtrip(b"HELLO\r\n\r\n")
        self.assertEqual(resp, b"", "畸形请求应直接关连接")
        self.assertEqual(seen, [], f"不该有未处理异常：{seen}")


# ---------------------------------------------------------------- 4. _bridge
class TestBridge(unittest.TestCase):
    def test_connection_reset_does_not_escape(self) -> None:
        stats = Stats()

        async def scenario() -> None:
            cr = FakeReader([b"hello"])
            sr = FakeReader(exc=ConnectionResetError("64"))
            cw, sw = FakeWriter(), FakeWriter()
            await _bridge(cr, cw, sr, sw, stats)     # 不许抛
            self.assertTrue(cw.closed, "客户端 writer 必须关")
            self.assertTrue(sw.closed, "上游 writer 必须关")
            self.assertEqual(stats.up_bytes, 5)     # 客户端 -> 上游
            self.assertEqual(stats.down_bytes, 0)
            self.assertEqual(_pending_tasks(), [])

        asyncio.run(scenario())

    def test_normal_eof_both_sides(self) -> None:
        stats = Stats()

        async def scenario() -> None:
            cr = FakeReader([b"abc"])
            sr = FakeReader([b"defg"])
            cw, sw = FakeWriter(), FakeWriter()
            await _bridge(cr, cw, sr, sw, stats)
            self.assertEqual((stats.up_bytes, stats.down_bytes), (3, 4))
            self.assertTrue(cw.closed and sw.closed)
            self.assertEqual(_pending_tasks(), [])

        asyncio.run(scenario())

    def test_runtime_error_side_is_contained(self) -> None:
        """旧日志里的 cannot reuse already awaited coroutine 属于 RuntimeError。"""
        stats = Stats()

        async def scenario() -> None:
            cr = FakeReader(exc=RuntimeError("cannot reuse already awaited coroutine"))
            sr = FakeReader([b"x"])
            cw, sw = FakeWriter(), FakeWriter()
            await _bridge(cr, cw, sr, sw, stats)
            self.assertTrue(cw.closed and sw.closed)
            self.assertEqual(stats.down_bytes, 1)

        asyncio.run(scenario())

    def test_outer_cancel_reaps_both_tasks(self) -> None:
        stats = Stats()

        async def scenario() -> None:
            cw, sw = FakeWriter(), FakeWriter()
            task = asyncio.create_task(
                _bridge(BlockingReader(), cw, BlockingReader(), sw, stats))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.assertTrue(cw.closed and sw.closed, "取消也必须关两侧 writer")
            self.assertEqual(_pending_tasks(), [], "取消后不许留 pending task")

        asyncio.run(scenario())


# ---------------------------------------------------------------- 5. pipe
class TestPipe(unittest.TestCase):
    def _run(self, src, counter=None):
        dst = FakeWriter()

        async def scenario() -> FakeWriter:
            await pipe(src, dst, counter)
            return dst

        return asyncio.run(scenario())

    def test_runtime_error_is_swallowed(self) -> None:
        dst = self._run(FakeReader(exc=RuntimeError("boom")))
        self.assertTrue(dst.eof, "退出时应该 write_eof")

    def test_os_error_family_is_swallowed(self) -> None:
        for exc in (ConnectionResetError("reset"), BrokenPipeError("broken"),
                    TimeoutError("timeout"), asyncio.IncompleteReadError(b"", 4)):
            with self.subTest(exc=type(exc).__name__):
                dst = self._run(FakeReader(exc=exc))
                self.assertTrue(dst.eof)

    def test_cancelled_error_propagates(self) -> None:
        async def scenario() -> None:
            dst = FakeWriter()
            with self.assertRaises(asyncio.CancelledError):
                await pipe(FakeReader(exc=asyncio.CancelledError()), dst)

        asyncio.run(scenario())

    def test_bytes_are_counted(self) -> None:
        counter = [0]
        src = FakeReader([b"ab", b"cde"])
        self._run(src, counter)
        self.assertEqual(counter[0], 5)

    def test_write_eof_failure_is_silent(self) -> None:
        class BadEofWriter(FakeWriter):
            def write_eof(self) -> None:
                raise OSError("already closed")

        async def scenario() -> None:
            await pipe(FakeReader([b"x"]), BadEofWriter())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
