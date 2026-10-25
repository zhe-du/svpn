"""svpn.netutil 健壮性补充测试（P4-netutil 专有）。

本文件只覆盖本轮加固点，与基线 tests/test_netutil.py（57 用例，不许动）互补：

    1. http_head 的畸形状态行 / 畸形响应头：一律 (0, {})，不抛 IndexError/ValueError；
    2. parse_node_url 的端口范围（1-65535）与裸 IPv6 歧义；
    3. DoH 各家 provider 的降级：截断 JSON / 非 200 / Answer 为 null / Answer 非列表；
    4. open_via / tls_over / tls_probe 在异常路径上必须关掉自己开的 socket；
    5. 域名边界（空、超长、带尾点）与 sni_block_test 的 True/False/None 三态。

原则：标准库 only（unittest/mock），**绝不联网** —— DoH 用假 urlopen，
socket 用假替身，DNS 一律 patch。整体 < 5 秒。

运行：
    python tests/test_netutil_extra.py
"""
from __future__ import annotations

import json
import socket
import ssl
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn import netutil                      # noqa: E402
from svpn.netutil import Node, parse_node_url  # noqa: E402


# ---------------------------------------------------------------- 测试替身
class FakeSock:
    """最小 socket 替身：记录发送、按脚本吐数据、统计 close 次数。"""

    def __init__(self, payload: bytes = b"", fail: str = "") -> None:
        self.sent: list[bytes] = []
        self.closed = 0
        self.timeouts: list[float] = []
        self._payload = payload
        self.fail = fail            # "recv" / "send" / "settimeout"

    def sendall(self, data: bytes) -> None:
        if self.fail == "send":
            raise ConnectionResetError("写入即被重置")
        self.sent.append(bytes(data))

    def recv(self, n: int) -> bytes:
        if self.fail == "recv":
            raise ConnectionResetError("读取即被重置")
        out, self._payload = self._payload[:n], self._payload[n:]
        return out

    def settimeout(self, value: float) -> None:
        if self.fail == "settimeout":
            raise OSError("settimeout 失败")
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed += 1


class FakeResp:
    """urllib.request.urlopen 的返回值替身（带上下文管理）。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.code = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def doh_body(*ips: str) -> bytes:
    """标准的 DoH-JSON（doh.pub / cloudflare 的格式）。"""
    return json.dumps({
        "Status": 0,
        "Answer": [{"name": "x.example", "type": 1, "TTL": 60, "data": ip} for ip in ips],
    }).encode()


# ---------------------------------------------------------------- 1. http_head
class TestHttpHeadMalformed(unittest.TestCase):
    """畸形状态行不许抛，统一 (0, 空头)。"""

    CASES = (
        ("空响应", b""),
        ("只有空行", b"\r\n\r\n"),
        ("只有协议版本", b"HTTP/1.1\r\n\r\n"),
        ("状态码粘着原因短语", b"HTTP/1.1 200OK\r\nServer: x\r\n\r\n"),
        ("状态码不是数字", b"HTTP/1.1 abc OK\r\n\r\n"),
        ("状态码位数不对", b"HTTP/1.1 20 OK\r\n\r\n"),
        ("状态码越界", b"HTTP/1.1 999 Weird\r\n\r\n"),
        ("没有协议前缀", b"200 OK\r\n\r\n"),
        ("非 ascii 垃圾字节", b"\xff\xfe\x00garbage\r\n\r\n"),
        ("裸冒号头行", b": just-a-value\r\n\r\n"),
    )

    def test_malformed_returns_zero(self) -> None:
        for name, payload in self.CASES:
            with self.subTest(name=name, payload=payload):
                code, headers = netutil.http_head(FakeSock(payload), "a.example", "/", 1.0)
                self.assertEqual(code, 0, f"{name} 应返回 0")
                self.assertIsInstance(headers, dict)
                self.assertNotIn("", headers, "空名头行不许进字典")

    def test_empty_payloads_give_empty_headers(self) -> None:
        for name, payload in (("空响应", b""), ("只有空行", b"\r\n\r\n"),
                              ("只有协议版本", b"HTTP/1.1\r\n\r\n"),
                              ("非 ascii 垃圾字节", b"\xff\xfe\x00garbage\r\n\r\n")):
            with self.subTest(name=name):
                code, headers = netutil.http_head(FakeSock(payload), "a.example", "/", 1.0)
                self.assertEqual((code, headers), (0, {}))

    def test_normal_status_line(self) -> None:
        payload = (b"HTTP/1.1 200 OK\r\n"
                   b"Content-Length: 0\r\n"
                   b"X-Test:   spaced  \r\n"
                   b"Set-Cookie: a=1\r\n\r\n")
        code, headers = netutil.http_head(FakeSock(payload), "a.example", "/", 1.0)
        self.assertEqual(code, 200)
        self.assertEqual(headers["content-length"], "0")
        self.assertEqual(headers["x-test"], "spaced", "值两侧空白要吃掉")
        self.assertEqual(headers["set-cookie"], "a=1")

    def test_status_code_boundaries(self) -> None:
        for line, want in ((b"HTTP/1.1 100 Continue\r\n\r\n", 100),
                           (b"HTTP/1.1 599 Edge\r\n\r\n", 599),
                           (b"HTTP/1.1 099 Nope\r\n\r\n", 0),
                           (b"HTTP/1.1 600 Nope\r\n\r\n", 0)):
            with self.subTest(line=line):
                code, _ = netutil.http_head(FakeSock(line), "a.example", "/", 1.0)
                self.assertEqual(code, want)

    def test_socket_error_returns_zero(self) -> None:
        for fail in ("recv", "send", "settimeout"):
            with self.subTest(fail=fail):
                code, headers = netutil.http_head(FakeSock(b"", fail), "a.example", "/", 1.0)
                self.assertEqual((code, headers), (0, {}))

    def test_header_without_colon_is_ignored(self) -> None:
        payload = b"HTTP/1.1 204 No Content\r\nthis-line-has-no-colon\r\n\r\n"
        code, headers = netutil.http_head(FakeSock(payload), "a.example", "/", 1.0)
        self.assertEqual(code, 204)
        self.assertEqual(headers, {})


# ---------------------------------------------------------------- 2. parse_node_url
class TestNodeUrlBoundaries(unittest.TestCase):
    def test_port_out_of_range_raises(self) -> None:
        for bad in ("socks5://1.2.3.4:0", "socks5://1.2.3.4:65536",
                    "socks5://1.2.3.4:-1", "socks5://1.2.3.4:99999",
                    "socks5://[::1]:70000", "http://1.2.3.4:0"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError) as cm:
                    parse_node_url(bad)
                self.assertIn("端口", str(cm.exception))

    def test_port_edges_ok(self) -> None:
        self.assertEqual(parse_node_url("socks5://1.2.3.4:1").port, 1)
        self.assertEqual(parse_node_url("socks5://1.2.3.4:65535").port, 65535)

    def test_bare_ipv6_with_scheme_raises_clear_error(self) -> None:
        for bad in ("socks5://::1:1080", "http://2408:8207::1:7890"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError) as cm:
                    parse_node_url(bad)
                msg = str(cm.exception)
                self.assertIn("裸 IPv6", msg)
                self.assertIn("方括号", msg)

    def test_bare_ipv6_without_port_still_works(self) -> None:
        """`2408::1` 是完整地址，不能因为歧义判定被误伤。"""
        n = parse_node_url("2408::1")
        self.assertEqual((n.host, n.port), ("2408::1", 1080))
        n = parse_node_url("socks5://2408:8207::1")
        self.assertEqual((n.host, n.port), ("2408:8207::1", 1080))

    def test_bracketed_ipv6(self) -> None:
        n = parse_node_url("socks5://[::1]:1080")
        self.assertEqual((n.kind, n.host, n.port), ("socks5", "::1", 1080))
        self.assertEqual(parse_node_url("http://[::1]").port, 7890)
        self.assertEqual(parse_node_url("socks5://[2408::1]").port, 1080)

    def test_password_with_at_sign(self) -> None:
        n = parse_node_url("socks5://u:p@ss@1.2.3.4:1080")
        self.assertEqual((n.username, n.password, n.host, n.port),
                         ("u", "p@ss", "1.2.3.4", 1080))

    def test_existing_behaviour_kept(self) -> None:
        self.assertEqual(parse_node_url("1.2.3.4:1080").kind, "socks5")
        self.assertEqual(parse_node_url("https://1.2.3.4:8443").kind, "http")
        self.assertEqual(parse_node_url("proxy.example").port, 1080)
        for bad in ("", "   ", "socks4://1.2.3.4:1080", "vmess://x"):
            with self.subTest(raw=repr(bad)):
                with self.assertRaises(ValueError):
                    parse_node_url(bad)


# ---------------------------------------------------------------- 3. DoH 降级
class TestDohDegrade(unittest.TestCase):
    """一个 provider 出任何问题都只跳过它，绝不向外抛。"""

    P = ("https://bad/dns-query", "https://good/dns-query")

    def setUp(self) -> None:
        netutil.clear_dns_cache()
        self.addCleanup(netutil.clear_dns_cache)

    def _run(self, bad_body, *, bad_status: int = 200,
             good_body: bytes | None = None):
        """第一家返回坏响应，第二家返回正常响应；返回 resolve 的结果。"""
        good = good_body if good_body is not None else doh_body("1.2.3.4", "5.6.7.8")

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            return FakeResp(bad_body, bad_status) if "bad" in url else FakeResp(good)

        with mock.patch.object(netutil._NO_PROXY_OPENER, "open",
                               side_effect=fake_urlopen), \
                mock.patch.object(socket, "getaddrinfo",
                                  side_effect=AssertionError("DoH 能答就不该走系统解析")):
            return netutil.resolve("cdn.example", providers=self.P)

    def test_truncated_json_falls_through(self) -> None:
        ips, src = self._run(b'{"Answer": [{"type": 1, "da')
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])
        self.assertEqual(src, "DoH:good")

    def test_non_200_falls_through(self) -> None:
        ips, src = self._run(b"<html>rate limited</html>", bad_status=503)
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])
        self.assertEqual(src, "DoH:good")

    def test_answer_null_falls_through(self) -> None:
        ips, src = self._run(b'{"Status": 3, "Answer": null}')
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])
        self.assertEqual(src, "DoH:good")

    def test_answer_not_a_list_falls_through(self) -> None:
        ips, src = self._run(b'{"Answer": {"type": 1, "data": "9.9.9.9"}}')
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])

    def test_non_dict_payload_falls_through(self) -> None:
        ips, _ = self._run(b"[\"not\", \"an\", \"object\"]")
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])

    def test_bad_answers_are_filtered(self) -> None:
        """data 缺失 / 非字符串 / 不是 IP / 类型不符 → 丢弃，不抛。"""
        body = json.dumps({"Answer": [
            {"type": 1, "data": "1.2.3.4"},
            {"type": 1},                                  # 缺 data
            {"type": 1, "data": 12345},                    # data 不是字符串
            {"type": 1, "data": "not-an-ip"},              # 不是 IP
            {"type": 28, "data": "2408::1"},               # 类型不符（A 查询里的 AAAA）
            {"type": 5, "data": "alias.example"},          # CNAME
            "垃圾条目",                                     # 不是 dict
            {"type": 1, "data": "  5.6.7.8  "},            # 前后空白
        ]}).encode()
        with mock.patch.object(netutil._NO_PROXY_OPENER, "open",
                               return_value=FakeResp(body)):
            got = netutil._doh_query("https://x/dns-query", "cdn.example", "A", 1.0)
        self.assertEqual(got, ["1.2.3.4", "5.6.7.8"])

    def test_all_providers_broken_is_contained(self) -> None:
        with mock.patch.object(netutil._NO_PROXY_OPENER, "open",
                               side_effect=OSError("DoH 全挂")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=OSError("no dns")):
            self.assertEqual(netutil.resolve("cdn.example"), ([], "fail"))
            self.assertEqual(netutil.resolve_merged("cdn.example"), ([], "none"))

    def test_resolve_merged_never_raises(self) -> None:
        with mock.patch.object(netutil, "resolve",
                               side_effect=RuntimeError("provider 炸了")), \
                mock.patch.object(netutil, "system_resolve",
                                  side_effect=RuntimeError("系统解析也炸了")):
            ips, src = netutil.resolve_merged("cdn.example")
        self.assertEqual(ips, [])
        self.assertEqual(src, "none")

    def test_resolve_merged_partial_source_labels(self) -> None:
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("2.2.2.2", 0))]
        with mock.patch.object(netutil, "_doh_query", return_value=["1.1.1.1"]), \
                mock.patch.object(socket, "getaddrinfo", return_value=infos):
            ips, src = netutil.resolve_merged("cdn.example")
        self.assertEqual(ips, ["1.1.1.1", "2.2.2.2"])
        self.assertEqual(src, "DoH:doh.pub+system")


# ---------------------------------------------------------------- 4. socket 回收
class TestSocketReclaim(unittest.TestCase):
    """异常路径上必须 close —— 这是长跑进程里最贵的泄漏。"""

    def test_tls_over_closes_on_wrap_error(self) -> None:
        sock = FakeSock()
        with mock.patch.object(ssl.SSLContext, "wrap_socket",
                               side_effect=ssl.SSLError("handshake failure")):
            with self.assertRaises(ssl.SSLError):
                netutil.tls_over(sock, "a.example", 1.0)
        self.assertEqual(sock.closed, 1, "wrap 失败必须关掉底层 socket")

    def test_tls_over_closes_on_settimeout_error(self) -> None:
        sock = mock.Mock()
        sock.settimeout.side_effect = OSError("settimeout 失败")
        with self.assertRaises(OSError):
            netutil.tls_over(sock, "a.example", 1.0)
        sock.close.assert_called_once()

    def test_tls_over_returns_wrapped_socket(self) -> None:
        sock = FakeSock()
        wrapped = object()
        with mock.patch.object(ssl.SSLContext, "wrap_socket", return_value=wrapped):
            self.assertIs(netutil.tls_over(sock, "a.example", 1.0), wrapped)
        self.assertEqual(sock.closed, 0, "成功时不许提前关")

    def test_tls_probe_closes_raw_on_handshake_error(self) -> None:
        for exc, status in ((ConnectionResetError("rst"), "sni_rst"),
                            (ssl.SSLError("bad"), "tls"),
                            (socket.timeout("t"), "tls"),
                            (OSError("aborted"), "tls")):
            with self.subTest(exc=type(exc).__name__):
                raw = FakeSock()
                with mock.patch.object(socket, "create_connection", return_value=raw), \
                        mock.patch.object(ssl.SSLContext, "wrap_socket", side_effect=exc):
                    r = netutil.tls_probe("1.2.3.4", "a.example", timeout=1.0)
                self.assertEqual(r["status"], status)
                self.assertEqual(raw.closed, 1, "握手失败的 raw socket 必须关")

    def test_tls_probe_closes_on_success(self) -> None:
        raw = FakeSock()
        tls = mock.Mock()
        tls.getpeercert.return_value = {}
        tls.version.return_value = "TLSv1.3"
        with mock.patch.object(socket, "create_connection", return_value=raw), \
                mock.patch.object(ssl.SSLContext, "wrap_socket", return_value=tls):
            r = netutil.tls_probe("1.2.3.4", "a.example", timeout=1.0)
        self.assertEqual((r["status"], r["version"]), ("ok", "TLSv1.3"))
        tls.close.assert_called_once()

    def test_open_via_socks5_closes_on_protocol_error(self) -> None:
        fake = FakeSock(fail="recv")
        with mock.patch.object(socket, "create_connection", return_value=fake):
            with self.assertRaises(ConnectionError):
                netutil.open_via(Node("socks5", "1.1.1.1", 1080), "a.example", 443, 1.0)
        self.assertEqual(fake.closed, 1)

    def test_open_via_http_closes_on_reject(self) -> None:
        fake = FakeSock(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        with mock.patch.object(socket, "create_connection", return_value=fake):
            with self.assertRaises(ConnectionError):
                netutil.open_via(Node("http", "1.1.1.1", 7890), "a.example", 443, 1.0)
        self.assertEqual(fake.closed, 1)

    def test_open_via_http_closes_on_empty_reply(self) -> None:
        fake = FakeSock(b"")           # 对端一句话不说就关
        with mock.patch.object(socket, "create_connection", return_value=fake):
            with self.assertRaises(ConnectionError):
                netutil.open_via(Node("http", "1.1.1.1", 7890), "a.example", 443, 1.0)
        self.assertEqual(fake.closed, 1)

    def test_open_via_direct_failure_propagates(self) -> None:
        with mock.patch.object(socket, "create_connection",
                               side_effect=ConnectionResetError("rst")):
            with self.assertRaises(OSError):
                netutil.open_via(Node("direct"), "a.example", 443, 1.0)


# ---------------------------------------------------------------- 5. 域名边界 + 三态
class TestDomainBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        netutil.clear_dns_cache()
        self.addCleanup(netutil.clear_dns_cache)

    def test_empty_names(self) -> None:
        for name in ("", "   ", "."):
            with self.subTest(name=repr(name)):
                with mock.patch.object(netutil, "_doh_query",
                                       side_effect=AssertionError("空域名不该查 DoH")):
                    self.assertEqual(netutil.resolve(name), ([], "empty"))

    def test_trailing_dot_is_normalized(self) -> None:
        seen: list[str] = []

        def fake_doh(url, name, rtype, timeout):
            seen.append(name)
            return ["1.1.1.1"]

        with mock.patch.object(netutil, "_doh_query", side_effect=fake_doh), \
                mock.patch.object(socket, "getaddrinfo", return_value=[]):
            ips, _ = netutil.resolve_merged("CDN.Example.COM.")
        self.assertEqual(ips, ["1.1.1.1"])
        self.assertEqual(set(seen), {"cdn.example.com"}, "带尾点/大写的域名必须先归一")

    def test_overlong_domain_short_circuits(self) -> None:
        long_name = "a" * 250 + ".example.com"       # > 253
        with mock.patch.object(netutil, "_doh_query",
                               side_effect=AssertionError("超长域名不该查 DoH")):
            self.assertEqual(netutil.resolve(long_name), ([], "invalid"))
        with mock.patch.object(netutil, "_doh_query",
                               side_effect=AssertionError("超长域名不该查 DoH")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=OSError("no dns")):
            self.assertEqual(netutil.resolve_merged(long_name), ([], "invalid"))

    def test_253_chars_still_queried(self) -> None:
        name = ("a" * 249) + ".com"                  # 恰好 253
        self.assertEqual(len(name), 253)
        with mock.patch.object(netutil, "_doh_query", return_value=["1.1.1.1"]), \
                mock.patch.object(socket, "getaddrinfo", return_value=[]):
            self.assertEqual(netutil.resolve(name)[0], ["1.1.1.1"])

    def test_system_resolve_overlong_returns_empty(self) -> None:
        with mock.patch.object(socket, "getaddrinfo", side_effect=OSError("too long")):
            self.assertEqual(netutil.system_resolve("a" * 300), [])


class TestSniBlockTriState(unittest.TestCase):
    """复验 control_ip 的 5 分钟 TTL / refresh=True，以及 None 三态没被破坏。"""

    def setUp(self) -> None:
        netutil._CONTROL_IP.clear()
        self.addCleanup(netutil._CONTROL_IP.clear)

    def test_control_ip_cache_and_refresh(self) -> None:
        with mock.patch.object(netutil, "resolve_merged",
                               return_value=(["1.1.1.1", "2.2.2.2"], "system")), \
                mock.patch.object(netutil, "race_tls",
                                  return_value=[{"ip": "1.1.1.1", "ms": 12.0}]) as race:
            self.assertEqual(netutil.control_ip("ctl.example"), "1.1.1.1")
            self.assertEqual(netutil.control_ip("ctl.example"), "1.1.1.1")
            self.assertEqual(race.call_count, 1, "TTL 内必须命中缓存")
            self.assertEqual(netutil.control_ip("ctl.example", refresh=True), "1.1.1.1")
            self.assertEqual(race.call_count, 2, "refresh=True 必须真的重算")

    def test_control_ip_unreachable_clears_cache(self) -> None:
        netutil._CONTROL_IP["ctl.example"] = (0.0, "9.9.9.9")   # 塞一条过期缓存
        with mock.patch.object(netutil, "resolve_merged", return_value=(["1.1.1.1"], "system")), \
                mock.patch.object(netutil, "race_tls", return_value=[]):
            self.assertEqual(netutil.control_ip("ctl.example"), "")
        self.assertNotIn("ctl.example", netutil._CONTROL_IP, "找不到对照 IP 要清缓存")

    def test_no_control_ip_returns_none(self) -> None:
        with mock.patch.object(netutil, "control_ip", return_value=""):
            self.assertIsNone(netutil.sni_block_test("store.steampowered.com"))

    def test_blocked_true(self) -> None:
        with mock.patch.object(netutil, "control_ip", return_value="1.1.1.1"), \
                mock.patch.object(netutil, "tls_probe",
                                  return_value={"status": "sni_rst"}):
            self.assertTrue(netutil.sni_block_test("store.steampowered.com"))

    def test_not_blocked_false(self) -> None:
        with mock.patch.object(netutil, "control_ip", return_value="1.1.1.1"), \
                mock.patch.object(netutil, "tls_probe", return_value={"status": "ok"}):
            self.assertIs(netutil.sni_block_test("store.steampowered.com"), False)

    def test_tls_error_is_none(self) -> None:
        with mock.patch.object(netutil, "control_ip", return_value="1.1.1.1"), \
                mock.patch.object(netutil, "tls_probe", return_value={"status": "tls"}):
            self.assertIsNone(netutil.sni_block_test("store.steampowered.com"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
