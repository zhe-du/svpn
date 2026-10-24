"""svpn.netutil 的离线单元测试。

原则：
    * 标准库 only（unittest / mock / socket / ssl / threading），不装任何依赖；
    * 绝不联网：DNS 走 monkeypatch 的假 DoH，TCP 只连 127.0.0.1 上的假服务，
      TLS 全部用假 socket / 假 SSLContext，系统解析也用假 getaddrinfo；
    * 每个用例自带清理（缓存、假服务线程），互不影响，整体 < 5 秒。

运行：
    python tests/test_netutil.py            # 退出码 0 = 全绿
    python tests/test_netutil.py -v         # 看每个用例
    python -m unittest tests.test_netutil   # 从仓库根跑也行
"""
from __future__ import annotations

import socket
import ssl
import struct
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn import netutil                      # noqa: E402
from svpn.netutil import Node, parse_node_url  # noqa: E402


# ---------------------------------------------------------------- 测试替身
class FakeSock:
    """最小 socket 替身：记录写入、按脚本吐数据、可观察关闭。"""

    def __init__(self, payload: bytes = b"") -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self.timeouts: list[float] = []
        self._payload = payload

    # 写
    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    # 读：一次返回一段（模拟分片到达）
    def recv(self, n: int) -> bytes:
        out, self._payload = self._payload[:n], self._payload[n:]
        return out

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return -1

    @property
    def request_text(self) -> str:
        return b"".join(self.sent).decode("latin-1")


class FakeTLS:
    """wrap_socket 的返回值：只需 getpeercert / version / close。"""

    def __init__(self, cn: str = "steamcdn-a.akamaihd.net",
                 version: str = "TLSv1.3") -> None:
        self._cn = cn
        self._version = version
        self.closed = False

    def getpeercert(self) -> dict:
        return {"subject": ((("commonName", self._cn),),)}

    def version(self) -> str:
        return self._version

    def close(self) -> None:
        self.closed = True


class Socks5Server:
    """单连接的最小 SOCKS5 服务端（127.0.0.1:0），记录客户端发来的每一段字节。

    auth=None      选择 无认证（0x00）
    auth=(u, p)    选择 用户名/密码（0x02）
    认证或 CONNECT 之后把数据 echo 回去，方便验证「返回的 socket 能收发」。
    """

    def __init__(self, *, auth: tuple[bytes, bytes] | None = None,
                 connect_code: int = 0, reply_atyp: int = 1,
                 fake_ver: int | None = None) -> None:
        self.auth = auth
        self.connect_code = connect_code
        self.reply_atyp = reply_atyp
        self.fake_ver = fake_ver

        self.hello = b""          # VER NMETHODS METHODS...
        self.auth_bytes = b""     # VER ULEN UNAME PLEN PASSWD
        self.request = b""        # VER CMD RSV ATYP ADDR PORT

        self.error: BaseException | None = None
        self._done = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(5.0)
        self.port: int = self._srv.getsockname()[1]
        self._thr = threading.Thread(target=self._run, daemon=True,
                                     name=f"fake-socks5-{self.port}")
        self._thr.start()

    # -- 服务端逻辑 ------------------------------------------------
    def _exact(self, conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client 提前关闭")
            buf += chunk
        return buf

    def _run(self) -> None:
        try:
            conn, _addr = self._srv.accept()
            with conn:
                conn.settimeout(5.0)
                self.hello = self._exact(conn, 2)
                self.hello += self._exact(conn, self.hello[1])

                if self.fake_ver is not None:              # 冒充非 SOCKS5 服务
                    conn.sendall(bytes([self.fake_ver, 0x00]))
                    return

                if self.auth is None:
                    conn.sendall(b"\x05\x00")
                else:
                    conn.sendall(b"\x05\x02")
                    head = self._exact(conn, 2)             # VER ULEN
                    ulen = head[1]
                    uname = self._exact(conn, ulen)
                    plen_b = self._exact(conn, 1)
                    plen = plen_b[0]
                    passwd = self._exact(conn, plen)
                    self.auth_bytes = head + uname + plen_b + passwd
                    if (uname, passwd) != self.auth:
                        conn.sendall(b"\x01\x01")
                        return
                    conn.sendall(b"\x01\x00")

                head = self._exact(conn, 4)                 # VER CMD RSV ATYP
                atyp = head[3]
                if atyp == 1:
                    addr = self._exact(conn, 4)
                elif atyp == 3:
                    ln = self._exact(conn, 1)[0]
                    addr = bytes([ln]) + self._exact(conn, ln)
                elif atyp == 4:
                    addr = self._exact(conn, 16)
                else:
                    raise ValueError(f"非法 ATYP {atyp}")
                port_b = self._exact(conn, 2)
                self.request = head + addr + port_b

                if self.connect_code:
                    conn.sendall(b"\x05" + bytes([self.connect_code]) + b"\x00\x01"
                                 + b"\x00" * 6)
                    return

                if self.reply_atyp == 1:
                    tail = b"\x00\x00\x00\x00" + b"\x00\x00"
                elif self.reply_atyp == 4:
                    tail = b"\x00" * 16 + b"\x00\x00"
                else:
                    tail = b"\x09localhost" + b"\x00\x00"
                conn.sendall(b"\x05\x00\x00" + bytes([self.reply_atyp]) + tail)

                while True:                                  # echo 模式
                    data = conn.recv(4096)
                    if not data:
                        return
                    conn.sendall(b"echo:" + data)
        except BaseException as exc:  # noqa: BLE001 —— 线程里不能抛
            self.error = exc
        finally:
            self._done.set()

    # -- 生命周期 --------------------------------------------------
    def wait(self, timeout: float = 5.0) -> None:
        self._done.wait(timeout)
        if self.error is not None:
            raise AssertionError(f"假 SOCKS5 服务出错: {self.error!r}")

    def close(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass
        self._thr.join(5.0)


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ---------------------------------------------------------------- 解析基础
class TestHostHelpers(unittest.TestCase):
    def test_norm_host_lower_strip_dot(self) -> None:
        self.assertEqual(netutil.norm_host("Store.SteamPowered.COM."),
                         "store.steampowered.com")

    def test_norm_host_brackets(self) -> None:
        self.assertEqual(netutil.norm_host("[::1]"), "::1")
        self.assertEqual(netutil.norm_host("[2408::1]"), "2408::1")

    def test_norm_host_empty(self) -> None:
        self.assertEqual(netutil.norm_host(""), "")
        self.assertEqual(netutil.norm_host("   "), "")

    def test_is_ip(self) -> None:
        self.assertTrue(netutil.is_ip("1.2.3.4"))
        self.assertTrue(netutil.is_ip("[::1]"))
        self.assertTrue(netutil.is_ip("[2408::1]"))
        self.assertFalse(netutil.is_ip("store.steampowered.com"))
        self.assertFalse(netutil.is_ip(""))
        self.assertFalse(netutil.is_ip("999.1.1.1"))

    def test_is_private(self) -> None:
        self.assertTrue(netutil.is_private("[::1]"), "IPv6 回环应判私有")
        self.assertTrue(netutil.is_private("127.0.0.1"))
        self.assertTrue(netutil.is_private("192.168.1.10"))
        self.assertTrue(netutil.is_private("10.1.2.3"))
        self.assertTrue(netutil.is_private("fe80::1"), "链路本地应判私有")
        self.assertFalse(netutil.is_private("1.2.3.4"))
        self.assertFalse(netutil.is_private("[2408::1]"), "公网 IPv6 不是私有")
        self.assertFalse(netutil.is_private("not-a-host"))


# ---------------------------------------------------------------- 节点 URL
class TestParseNodeUrl(unittest.TestCase):
    def test_socks5_with_auth(self) -> None:
        n = parse_node_url("socks5://u:p@1.2.3.4:1080")
        self.assertEqual((n.kind, n.host, n.port, n.username, n.password),
                         ("socks5", "1.2.3.4", 1080, "u", "p"))
        self.assertTrue(n.usable)

    def test_socks_alias(self) -> None:
        n = parse_node_url("socks://u:p@1.2.3.4:1080")
        self.assertEqual(n.kind, "socks5")

    def test_socks5h_alias(self) -> None:
        self.assertEqual(parse_node_url("socks5h://1.2.3.4:1080").kind, "socks5")

    def test_http(self) -> None:
        n = parse_node_url("http://1.2.3.4:7890")
        self.assertEqual((n.kind, n.host, n.port), ("http", "1.2.3.4", 7890))
        self.assertTrue(n.usable)

    def test_https_folds_to_http(self) -> None:
        n = parse_node_url("HTTPS://u:p@9.9.9.9:8443")
        self.assertEqual((n.kind, n.host, n.port), ("http", "9.9.9.9", 8443))

    def test_bare_host_port_defaults_to_socks5(self) -> None:
        n = parse_node_url("1.2.3.4:1080")
        self.assertEqual((n.kind, n.host, n.port), ("socks5", "1.2.3.4", 1080))

    def test_missing_port_defaults(self) -> None:
        self.assertEqual(parse_node_url("socks5://1.2.3.4").port, 1080)
        self.assertEqual(parse_node_url("http://1.2.3.4").port, 7890)
        self.assertEqual(parse_node_url("https://1.2.3.4").port, 7890)

    def test_user_without_password(self) -> None:
        n = parse_node_url("socks5://onlyuser@1.2.3.4:1080")
        self.assertEqual((n.username, n.password), ("onlyuser", ""))

    def test_password_with_at_sign(self) -> None:
        n = parse_node_url("socks5://u:p@ss@1.2.3.4:1080")
        self.assertEqual((n.username, n.password, n.host), ("u", "p@ss", "1.2.3.4"))

    def test_bad_scheme_raises(self) -> None:
        for bad in ("vmess://1.2.3.4:443", "ftp://1.2.3.4:21", "ss://x"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    parse_node_url(bad)

    def test_empty_raises(self) -> None:
        for bad in ("", "   "):
            with self.subTest(raw=repr(bad)):
                with self.assertRaises(ValueError):
                    parse_node_url(bad)

    def test_non_numeric_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_node_url("socks5://1.2.3.4:abc")

    def test_missing_host_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_node_url("socks5://:1080")

    def test_ipv6_url(self) -> None:
        """方括号 IPv6 节点 URL（实装已修：不再按第一个冒号切）。"""
        node = parse_node_url("socks5://u:p@[::1]:1080")
        self.assertEqual((node.host, node.port, node.username, node.password),
                         ("::1", 1080, "u", "p"))
        node = parse_node_url("socks5://[2408:8207::1]:1080")
        self.assertEqual((node.host, node.port), ("2408:8207::1", 1080))


# ---------------------------------------------------------------- DoH / 缓存
class TestResolve(unittest.TestCase):
    def setUp(self) -> None:
        netutil.clear_dns_cache()
        self.addCleanup(netutil.clear_dns_cache)

    def test_literal_ip_short_circuits(self) -> None:
        with mock.patch.object(netutil, "_doh_query",
                               side_effect=AssertionError("不该查 DoH")) as doh:
            self.assertEqual(netutil.resolve("1.2.3.4"), (["1.2.3.4"], "literal"))
            self.assertEqual(netutil.resolve("[::1]"), (["::1"], "literal"))
        doh.assert_not_called()

    def test_empty_name(self) -> None:
        with mock.patch.object(netutil, "_doh_query",
                               side_effect=AssertionError("不该查 DoH")) as doh:
            self.assertEqual(netutil.resolve(""), ([], "empty"))
        doh.assert_not_called()

    def test_doh_hit_then_cache(self) -> None:
        calls: list[tuple] = []

        def fake_doh(url: str, name: str, rtype: str, timeout: float) -> list[str]:
            calls.append((url, name, rtype, timeout))
            return ["23.45.64.5"]

        with mock.patch.object(netutil, "_doh_query", side_effect=fake_doh):
            ips1, src1 = netutil.resolve("store.steampowered.com", ttl=300)
            ips2, src2 = netutil.resolve("store.steampowered.com", ttl=300)

        self.assertEqual(ips1, ["23.45.64.5"])
        self.assertEqual(src1, "DoH:doh.pub")
        self.assertEqual((ips2, src2), (ips1, src1), "第二次应命中缓存返回同样结果")
        self.assertIsNot(ips1, ips2, "返回值应是缓存副本，不能被调用方改坏")
        # 首轮会问 2 家 DoH 合并候选（单家只回 1 条 A，failover 不够）；
        # 第二轮必须全部命中缓存，一次 provider 都不该再问。
        self.assertEqual(len(calls), 2, f"第二轮应命中缓存，实际 {calls}")
        self.assertEqual(calls[0][1:3], ("store.steampowered.com", "A"))
        self.assertEqual({c[0] for c in calls},
                         {"https://doh.pub/dns-query", "https://dns.alidns.com/resolve"})

    def test_cache_expires_by_ttl(self) -> None:
        calls: list[str] = []

        def fake_doh(url, name, rtype, timeout):
            calls.append(name)
            return ["1.1.1.1"]

        with mock.patch.object(netutil, "_doh_query", side_effect=fake_doh):
            netutil.resolve("a.example.com", ttl=0)     # 第一次查（2 家 provider）
            calls.clear()
            netutil.resolve("a.example.com", ttl=0)     # ttl=0 → 必然过期，重新查
        self.assertTrue(calls, "ttl=0 必须重新解析")
        self.assertLessEqual(len(calls), 2)

    def test_doh_all_fail_falls_back_to_system(self) -> None:
        fake_infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 0)),
        ]
        doh_calls: list[str] = []

        def boom(url, name, rtype, timeout):
            doh_calls.append(url)
            raise OSError("DoH 不可达")

        with mock.patch.object(netutil, "_doh_query", side_effect=boom), \
                mock.patch.object(socket, "getaddrinfo",
                                  return_value=fake_infos) as gai:
            ips, src = netutil.resolve("steamcommunity.com")

        self.assertEqual(src, "system")
        self.assertEqual(ips, ["93.184.216.34", "93.184.216.35"], "结果应去重且保序")
        self.assertEqual(len(doh_calls), len(netutil.DOH_PROVIDERS),
                         "应把每个 provider 都试一遍再兜底")
        gai.assert_called_once()

    def test_everything_fails_returns_fail(self) -> None:
        with mock.patch.object(netutil, "_doh_query", side_effect=OSError("x")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=OSError("no dns")):
            self.assertEqual(netutil.resolve("nope.invalid"), ([], "fail"))

    def test_explicit_providers_respected(self) -> None:
        seen: list[str] = []

        def fake_doh(url, name, rtype, timeout):
            seen.append(url)
            if "second" in url:
                return ["5.6.7.8"]
            return []

        with mock.patch.object(netutil, "_doh_query", side_effect=fake_doh):
            ips, src = netutil.resolve("x.example", providers=("https://first/dns-query",
                                                               "https://second/dns-query"))
        self.assertEqual(ips, ["5.6.7.8"])
        self.assertEqual(src, "DoH:second")
        self.assertEqual(seen, ["https://first/dns-query", "https://second/dns-query"])


class TestSystemResolve(unittest.TestCase):
    """system_resolve / resolve_merged：系统解析一律用假 getaddrinfo。"""

    @staticmethod
    def _infos(*ips: str) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    def setUp(self) -> None:
        netutil.clear_dns_cache()
        self.addCleanup(netutil.clear_dns_cache)

    def test_dedup_and_order(self) -> None:
        infos = self._infos("1.1.1.1", "1.1.1.1", "2.2.2.2")
        with mock.patch.object(socket, "getaddrinfo", return_value=infos) as gai:
            self.assertEqual(netutil.system_resolve("a.example"), ["1.1.1.1", "2.2.2.2"])
        gai.assert_called_once()

    def test_skips_literal_and_empty(self) -> None:
        with mock.patch.object(socket, "getaddrinfo",
                               side_effect=AssertionError("不该去解析字面 IP")):
            self.assertEqual(netutil.system_resolve("1.2.3.4"), [])
            self.assertEqual(netutil.system_resolve(""), [])

    def test_oserror_returns_empty(self) -> None:
        with mock.patch.object(socket, "getaddrinfo", side_effect=OSError("nope")):
            self.assertEqual(netutil.system_resolve("a.example"), [])

    def test_resolve_merged_marks_both_sources(self) -> None:
        infos = self._infos("2.2.2.2", "1.1.1.1")
        with mock.patch.object(netutil, "_doh_query", return_value=["1.1.1.1"]), \
                mock.patch.object(socket, "getaddrinfo", return_value=infos):
            ips, src = netutil.resolve_merged("cdn.example")
        self.assertEqual(ips, ["1.1.1.1", "2.2.2.2"], "DoH 在前，系统结果补后，去重")
        self.assertEqual(src, "DoH:doh.pub+system")

    def test_resolve_merged_system_only(self) -> None:
        infos = self._infos("2.2.2.2")
        with mock.patch.object(netutil, "_doh_query", side_effect=OSError("x")), \
                mock.patch.object(socket, "getaddrinfo", return_value=infos):
            ips, src = netutil.resolve_merged("cdn.example")
        self.assertEqual((ips, src), (["2.2.2.2"], "system"))


# ---------------------------------------------------------------- TLS 分层
class TestTlsProbe(unittest.TestCase):
    """用假 socket + 假 SSLContext.wrap_socket 复现三种故障，不碰真网络。"""

    def _probe(self, wrap_side_effect) -> dict:
        fake = FakeSock()
        with mock.patch.object(socket, "create_connection", return_value=fake), \
                mock.patch.object(ssl.SSLContext, "wrap_socket",
                                  side_effect=wrap_side_effect):
            return netutil.tls_probe("1.2.3.4", "store.steampowered.com", timeout=1.0)

    def test_tcp_reset(self) -> None:
        with mock.patch.object(socket, "create_connection",
                               side_effect=ConnectionResetError("rst")):
            r = netutil.tls_probe("1.2.3.4", "store.steampowered.com", timeout=1.0)
        self.assertEqual(r["status"], "tcp")
        self.assertEqual(r["err"], "RST")
        self.assertEqual((r["ip"], r["sni"]), ("1.2.3.4", "store.steampowered.com"))

    def test_tcp_timeout(self) -> None:
        with mock.patch.object(socket, "create_connection",
                               side_effect=socket.timeout("t")):
            r = netutil.tls_probe("1.2.3.4", "x", timeout=1.0)
        self.assertEqual((r["status"], r["err"]), ("tcp", "TIMEOUT"))

    def test_sni_rst(self) -> None:
        r = self._probe(ConnectionResetError("rst"))
        self.assertEqual(r["status"], "sni_rst", r)
        self.assertEqual(r["err"], "RST")
        self.assertIn("tcp_ms", r, "分层结果要带 TCP 段耗时，便于判断是哪一层坏")

    def test_tls_error(self) -> None:
        r = self._probe(ssl.SSLError("handshake failure"))
        self.assertEqual(r["status"], "tls", r)
        self.assertTrue(r["err"].startswith("SSLError"), r["err"])

    def test_tls_timeout(self) -> None:
        r = self._probe(socket.timeout("t"))
        self.assertEqual((r["status"], r["err"]), ("tls", "TIMEOUT"))

    def test_other_oserror_is_tls(self) -> None:
        r = self._probe(OSError("connection aborted"))
        self.assertEqual((r["status"], r["err"]), ("tls", "OSError"))

    def test_ok_when_handshake_succeeds(self) -> None:
        fake_tls = FakeTLS(cn="store.steampowered.com")
        r = self._probe(lambda *a, **k: fake_tls)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["cert_cn"], "store.steampowered.com")
        self.assertEqual(r["version"], "TLSv1.3")
        self.assertTrue(fake_tls.closed, "成功路径也要关掉 TLS 连接")

    def test_tcp_probe_reset_and_ok(self) -> None:
        fake = FakeSock()
        with mock.patch.object(socket, "create_connection", return_value=fake):
            ok, ms, err = netutil.tcp_probe("1.2.3.4", 443)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertGreaterEqual(ms, 0.0)
        self.assertTrue(fake.closed)


# ---------------------------------------------------------------- SOCKS5 协议
class TestSocks5Connect(unittest.TestCase):
    """真起一个 127.0.0.1 上的假 SOCKS5 服务，逐字节核对客户端协议实现。"""

    def _server(self, **kwargs) -> Socks5Server:
        srv = Socks5Server(**kwargs)
        self.addCleanup(srv.close)
        return srv

    def test_no_auth_domain_atyp3(self) -> None:
        srv = self._server()
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port)
        sock = netutil.socks5_connect(node, "example.com", 443, timeout=3)
        try:
            sock.sendall(b"ping")
            got = sock.recv(64)
        finally:
            sock.close()
        srv.wait()

        self.assertEqual(srv.hello, b"\x05\x01\x00", "无认证应只提供 method 0x00")
        self.assertEqual(srv.auth_bytes, b"")
        self.assertEqual(srv.request[:-2], b"\x05\x01\x00\x03\x0bexample.com")
        self.assertEqual(srv.request[-2:], struct.pack(">H", 443))
        self.assertEqual(srv.request[3], 0x03, "域名必须走 ATYP=3")
        self.assertEqual(got, b"echo:ping", "返回的 socket 应可直接收发")

    def test_username_password(self) -> None:
        srv = self._server(auth=(b"u1", b"p1"))
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port,
                    username="u1", password="p1")
        sock = netutil.socks5_connect(node, "store.steampowered.com", 444, timeout=3)
        try:
            self.assertIsInstance(sock, socket.socket)
        finally:
            sock.close()
        srv.wait()

        self.assertEqual(srv.hello, b"\x05\x02\x00\x02", "带账号应提供 0x00 和 0x02")
        expect_auth = (bytes([1, len(b"u1")]) + b"u1" + bytes([len(b"p1")]) + b"p1")
        self.assertEqual(srv.auth_bytes, expect_auth,
                         "用户名密码子协商格式 VER ULEN UNAME PLEN PASSWD")
        self.assertEqual(srv.request[:4], b"\x05\x01\x00\x03")
        self.assertEqual(srv.request[4:5], bytes([len(b"store.steampowered.com")]))
        self.assertEqual(srv.request[5:-2], b"store.steampowered.com")
        self.assertEqual(srv.request[-2:], struct.pack(">H", 444))

    def test_literal_ipv4_uses_atyp1(self) -> None:
        srv = self._server()
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port)
        sock = netutil.socks5_connect(node, "1.2.3.4", 80, timeout=3)
        sock.close()
        srv.wait()
        self.assertEqual(srv.request, b"\x05\x01\x00\x01\x01\x02\x03\x04\x00\x50")

    def test_literal_ipv6_uses_atyp4(self) -> None:
        srv = self._server(reply_atyp=1)
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port)
        sock = netutil.socks5_connect(node, "2408::1", 443, timeout=3)
        sock.close()
        srv.wait()
        self.assertEqual(srv.request[:4], b"\x05\x01\x00\x04")
        self.assertEqual(srv.request[4:-2], socket.inet_pton(socket.AF_INET6, "2408::1"))
        self.assertEqual(len(srv.request), 4 + 16 + 2)

    def test_reply_domain_atyp_is_drained(self) -> None:
        srv = self._server(reply_atyp=3)
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port)
        sock = netutil.socks5_connect(node, "example.com", 443, timeout=3)
        try:
            sock.sendall(b"ok")
            self.assertEqual(sock.recv(64), b"echo:ok")
        finally:
            sock.close()
        srv.wait()

    def test_reply_ipv6_atyp_is_drained(self) -> None:
        srv = self._server(reply_atyp=4)
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port)
        sock = netutil.socks5_connect(node, "example.com", 443, timeout=3)
        try:
            sock.sendall(b"v6")
            self.assertEqual(sock.recv(64), b"echo:v6")
        finally:
            sock.close()
        srv.wait()

    def test_connect_refused_raises_and_closes(self) -> None:
        srv = self._server(connect_code=0x05)
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port)
        with self.assertRaises(ConnectionError) as cm:
            netutil.socks5_connect(node, "blocked.example", 443, timeout=3)
        srv.wait()
        self.assertIn("code=5", str(cm.exception))

    def test_auth_rejected_raises(self) -> None:
        srv = self._server(auth=(b"u1", b"p1"))
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port,
                    username="u1", password="wrong")
        with self.assertRaises(ConnectionError) as cm:
            netutil.socks5_connect(node, "example.com", 443, timeout=3)
        srv.wait()
        self.assertIn("认证失败", str(cm.exception))

    def test_not_socks5_raises(self) -> None:
        srv = self._server(fake_ver=4)
        node = Node(kind="socks5", host="127.0.0.1", port=srv.port)
        with self.assertRaises(ConnectionError) as cm:
            netutil.socks5_connect(node, "example.com", 443, timeout=3)
        srv.wait()
        self.assertIn("不是 SOCKS5", str(cm.exception))

    def test_unreachable_upstream_raises(self) -> None:
        node = Node(kind="socks5", host="127.0.0.1", port=free_port())
        with self.assertRaises(OSError):
            netutil.socks5_connect(node, "example.com", 443, timeout=0.5)


# ---------------------------------------------------------------- 测速下载
class TestDownloadBench(unittest.TestCase):
    """download_bench 只依赖 sock_factory，直接喂假 socket 即可，无需真 TLS。"""

    HOST = "steamcdn-a.akamaihd.net"
    PATH = "/client/installer/SteamSetup.exe"
    SIZE = 1_048_576

    def _sock(self, head: bytes, body: bytes, chunk: int = 400_000) -> FakeSock:
        payload = head + body
        s = FakeSock(payload)
        # 让 recv 像网络一样分片返回
        chunks: list[bytes] = []
        i = 0
        while i < len(payload):
            chunks.append(payload[i:i + chunk])
            i += chunk
        s._payload = b""
        s._extra = chunks
        orig_recv = s.recv

        def recv(n: int) -> bytes:
            if s._extra:
                data = s._extra.pop(0)
                out, rest = data[:n], data[n:]
                if rest:
                    s._extra.insert(0, rest)
                return out
            return orig_recv(n)

        s.recv = recv  # type: ignore[method-assign]
        return s

    def test_206_and_throughput(self) -> None:
        head = (b"HTTP/1.1 206 Partial Content\r\n"
                b"Content-Range: bytes 0-1048575/5000000\r\n"
                b"Content-Length: 1048576\r\n\r\n")
        sock = self._sock(head, b"x" * self.SIZE)
        got, mbps, err = netutil.download_bench(
            lambda: sock, self.HOST, self.PATH, self.SIZE, timeout=1.0)

        self.assertEqual(err, "", f"不该有错误，实际 {err}")
        self.assertEqual(got, self.SIZE)
        self.assertGreater(mbps, 0.0)
        self.assertTrue(sock.closed, "用完必须关连接")
        req = sock.request_text
        self.assertTrue(req.startswith(f"GET {self.PATH} HTTP/1.1\r\n"), req[:60])
        self.assertIn(f"Host: {self.HOST}\r\n", req)
        self.assertIn("Range: bytes=0-1048575\r\n", req)
        self.assertIn("Valve/Steam HTTP Client 1.0", req)

    def test_custom_size(self) -> None:
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n\r\n"
        sock = self._sock(head, b"y" * 1024)
        got, mbps, err = netutil.download_bench(
            lambda: sock, self.HOST, self.PATH, 1024, timeout=1.0)
        self.assertEqual((got, err), (1024, ""))
        self.assertGreater(mbps, 0.0)
        self.assertIn("Range: bytes=0-1023\r\n", sock.request_text)

    def test_http_error_reported(self) -> None:
        sock = self._sock(b"HTTP/1.1 404 Not Found\r\n\r\n", b"nope")
        got, mbps, err = netutil.download_bench(
            lambda: sock, self.HOST, self.PATH, 1024, timeout=1.0)
        self.assertEqual(err, "HTTP 404")
        self.assertEqual(mbps, 0.0)
        self.assertTrue(sock.closed)

    def test_no_response(self) -> None:
        sock = FakeSock(b"")
        got, mbps, err = netutil.download_bench(
            lambda: sock, self.HOST, self.PATH, 1024, timeout=1.0)
        self.assertEqual((got, mbps, err), (0, 0.0, "无响应"))
        self.assertTrue(sock.closed)

    def test_factory_exception_is_contained(self) -> None:
        def boom():
            raise ConnectionResetError("rst")

        for fn in (boom,):
            got, mbps, err = netutil.download_bench(
                fn, self.HOST, self.PATH, 1024, timeout=1.0)
        self.assertEqual((got, mbps), (0, 0.0))
        self.assertIn("ConnectionResetError", err)


# ---------------------------------------------------------------- 杂项
class TestOpenViaRouting(unittest.TestCase):
    """open_via 只是按 kind 分派，用假连接确认没走错分支。"""

    def test_direct_uses_create_connection(self) -> None:
        fake = FakeSock()
        with mock.patch.object(socket, "create_connection",
                               return_value=fake) as cc:
            got = netutil.open_via(Node("direct"), "example.com", 443, 1.0)
        self.assertIs(got, fake)
        cc.assert_called_once()

    def test_socks5_dispatch(self) -> None:
        with mock.patch.object(netutil, "socks5_connect",
                               return_value=FakeSock()) as s5:
            netutil.open_via(Node("socks5", "1.1.1.1", 1080), "a.example", 443, 1.0)
        s5.assert_called_once()

    def test_http_dispatch(self) -> None:
        with mock.patch.object(netutil, "http_connect",
                               return_value=FakeSock()) as hc:
            netutil.open_via(Node("http", "1.1.1.1", 7890), "a.example", 443, 1.0)
        hc.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
