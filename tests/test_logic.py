"""svpn 逻辑层离线单测：结论判定 / CDN 排序与缓存 / 节点发现 / 分流规则。

原则：
    * 标准库 only，不联网（环境变量用 patch.dict，注册表用假 winreg 模块，
      连接全部是假 socket）；
    * 每个用例独立清理，总耗时 < 5 秒。

运行：
    python tests/test_logic.py            # 退出码 0 = 全绿
    python tests/test_logic.py -v
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn import cdn, discover, netutil, probe, rules   # noqa: E402
from svpn.cdn import DomainResult, Edge                 # noqa: E402
from svpn.netutil import Node                           # noqa: E402
from svpn.rules import Router                           # noqa: E402


# ---------------------------------------------------------------- 测试替身
class FakeSock:
    """够 http_head / verify 用的假连接：写进 sent，读从 payload 分片吐。"""

    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.sent = b""
        self.closed = False

    def settimeout(self, _value: float) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent += bytes(data)

    def recv(self, n: int) -> bytes:
        out, self.payload = self.payload[:n], self.payload[n:]
        return out

    def close(self) -> None:
        self.closed = True


def http_response(code: int = 200) -> bytes:
    return (f"HTTP/1.1 {code} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode("latin-1")


class _FakeRegKey:
    def __enter__(self) -> "_FakeRegKey":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def fake_winreg_module(enable: object, server: object) -> types.ModuleType:
    """假 winreg：只在内存里回答两个值，绝不碰真实注册表。"""
    mod = types.ModuleType("winreg")
    mod.HKEY_CURRENT_USER = object()

    def OpenKey(_root, _path):
        return _FakeRegKey()

    def QueryValueEx(_key, name):
        if name == "ProxyEnable":
            return (enable, 1)
        if name == "ProxyServer":
            return (server, 1)
        raise FileNotFoundError(name)

    mod.OpenKey = OpenKey
    mod.QueryValueEx = QueryValueEx
    return mod


# ---------------------------------------------------------------- probe._verdict
class TestVerdict(unittest.TestCase):
    def test_direct_ok(self) -> None:
        self.assertEqual(probe._verdict({"ok": True}, None), "直连可用")
        self.assertEqual(probe._verdict({"ok": True}, {"ok": False}), "直连可用")

    def test_sni_rst_needs_node(self) -> None:
        self.assertEqual(probe._verdict({"ok": False, "reason": "sni_rst"}, {"ok": True}),
                         "需节点 · SNI 阻断")

    def test_dns_pollution_needs_node(self) -> None:
        self.assertEqual(probe._verdict({"ok": False, "reason": "dns"}, {"ok": True}),
                         "需节点 · DNS 污染")

    def test_ip_unreachable_needs_node(self) -> None:
        self.assertEqual(probe._verdict({"ok": False, "reason": "tcp"}, {"ok": True}),
                         "需节点 · IP 不可达")

    def test_unknown_reason_defaults_to_unreachable(self) -> None:
        self.assertEqual(probe._verdict({"ok": False, "reason": ""}, None),
                         "不通 · 待排查")
        self.assertEqual(probe._verdict({"ok": False, "reason": "weird"}, {"ok": True}),
                         "不通 · 待排查")

    def test_node_dead_wins_over_reason(self) -> None:
        for reason in ("sni_rst", "dns", "tcp"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    probe._verdict({"ok": False, "reason": reason}, {"ok": False}),
                    "节点也不通 · 换节点")

    def test_no_node_still_names_the_fault_layer(self) -> None:
        """没配节点时也要说清是 DNS 还是 SNI，只是没法给「需节点」的建议。"""
        self.assertEqual(probe._verdict({"ok": False, "reason": "dns"}, None),
                         "需节点 · DNS 污染")
        self.assertEqual(probe._verdict({"ok": False, "reason": "sni_rst"}, None),
                         "需节点 · SNI 阻断")

    def test_row_needs_node_flag(self) -> None:
        row = probe.Row(host="store.steampowered.com", note="商店",
                        verdict="需节点 · SNI 阻断")
        self.assertTrue(row.needs_node)
        row2 = probe.Row(host="x", note="", verdict="直连可用")
        self.assertFalse(row2.needs_node)

    def test_summarize_buckets(self) -> None:
        rows = [
            probe.Row(host="a", note="", verdict="直连可用"),
            probe.Row(host="b", note="", verdict="需节点 · DNS 污染"),
            probe.Row(host="c", note="", verdict="不通 · 待排查"),
        ]
        s = probe.summarize(rows, Node("socks5", "1.2.3.4", 1080))
        self.assertEqual(s["direct_ok"], ["a"])
        self.assertEqual(s["need_node"], ["b"])
        self.assertEqual(s["dead"], ["c"])
        self.assertFalse(s["ok"])
        self.assertIn("1.2.3.4:1080", s["node"])


# ---------------------------------------------------------------- cdn 排序
class TestEdgeScore(unittest.TestCase):
    def test_throughput_beats_latency(self) -> None:
        slow_but_works = Edge(ip="1.1.1.1", ms=5.0, mbps=0.0)
        fast_pipe = Edge(ip="2.2.2.2", ms=200.0, mbps=12.5)
        medium = Edge(ip="3.3.3.3", ms=50.0, mbps=3.5)
        order = sorted([slow_but_works, fast_pipe, medium], key=lambda e: e.score)
        self.assertEqual([e.ip for e in order], ["2.2.2.2", "3.3.3.3", "1.1.1.1"])

    def test_same_throughput_prefers_lower_latency(self) -> None:
        a = Edge(ip="a", ms=120.0, mbps=8.0)
        b = Edge(ip="b", ms=40.0, mbps=8.0)
        self.assertEqual([e.ip for e in sorted([a, b], key=lambda e: e.score)], ["b", "a"])

    def test_no_throughput_ordered_by_latency(self) -> None:
        a = Edge(ip="a", ms=300.0)
        b = Edge(ip="b", ms=20.0)
        self.assertEqual([e.ip for e in sorted([a, b], key=lambda e: e.score)], ["b", "a"])

    def test_domain_result_best_and_dict(self) -> None:
        best = Edge(ip="9.9.9.9", ms=30.0, mbps=20.0, code=206, bytes_got=1048576)
        res = DomainResult(domain="steamcdn-a.akamaihd.net", source="DoH:doh.pub",
                           candidates=["1.1.1.1", "2.2.2.2"], edges=[best,
                                                                    Edge(ip="8.8.8.8")])
        self.assertIs(res.best, best)
        info = res.to_dict()
        self.assertEqual(info["best_ip"], "9.9.9.9")
        self.assertEqual(info["best_mbps"], 20.0)
        self.assertEqual(info["candidates"], 2)
        self.assertEqual(info["tested"], 2)
        self.assertEqual(len(info["top"]), 2)

    def test_domain_result_empty(self) -> None:
        info = DomainResult(domain="x").to_dict()
        self.assertIsNone(DomainResult(domain="x").best)
        self.assertEqual((info["best_ip"], info["best_ms"], info["best_mbps"]),
                         ("", 0, 0))


# ---------------------------------------------------------------- cdn 缓存
class TestCdnCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "sub" / "cdn-cache.json"

    def test_roundtrip_creates_parent(self) -> None:
        cache = {"steamcdn-a.akamaihd.net": {"best_ip": "23.45.64.5", "best_mbps": 12.3}}
        out = cdn.save_cache(cache, self.path)
        self.assertEqual(out, self.path)
        self.assertTrue(self.path.exists(), "父目录应被自动创建")
        self.assertEqual(cdn.load_cache(self.path), cache)

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("updated", raw)
        self.assertEqual(raw["domains"], cache)

    def test_missing_file(self) -> None:
        self.assertEqual(cdn.load_cache(self.path), {})

    def test_bad_json(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(cdn.load_cache(self.path), {})

    def test_expired_returns_empty(self) -> None:
        cdn.save_cache({"a.com": {"best_ip": "1.2.3.4"}}, self.path)
        with mock.patch("time.time", return_value=time.time() + 7 * 3600):
            self.assertEqual(cdn.load_cache(self.path), {},
                             "超过默认 6 小时应视为过期")
            self.assertEqual(cdn.load_cache(self.path, max_age=8 * 3600)["a.com"]
                             ["best_ip"], "1.2.3.4")

    def test_max_age_zero_returns_empty(self) -> None:
        cdn.save_cache({"a.com": {"best_ip": "1.2.3.4"}}, self.path)
        with mock.patch("time.time", return_value=time.time() + 1):
            self.assertEqual(cdn.load_cache(self.path, max_age=0), {})

    def test_best_ip_map(self) -> None:
        cache = {
            "good.steamstatic.com": {"best_ip": "23.45.64.5", "best_ms": 30},
            "v6.steamstatic.com": {"best_ip": "2408::1"},
            "junk.steamstatic.com": {"best_ip": "not-an-ip"},
            "empty.steamstatic.com": {"best_ms": 10},
            "null.steamstatic.com": None,
        }
        cdn.save_cache(cache, self.path)
        got = cdn.best_ip_map(self.path)
        self.assertEqual(got, {"good.steamstatic.com": "23.45.64.5",
                               "v6.steamstatic.com": "2408::1"})

    def test_best_ip_map_expired(self) -> None:
        cdn.save_cache({"good.steamstatic.com": {"best_ip": "23.45.64.5"}}, self.path)
        # Windows 的 time.time() 粒度约 15ms，同一 tick 内 max_age=0 未必过期，
        # 这里把时钟推后 1 秒保证确定性。
        with mock.patch("time.time", return_value=time.time() + 1):
            self.assertEqual(cdn.best_ip_map(self.path, max_age=0), {})


# ---------------------------------------------------------------- discover: env
class TestFromEnv(unittest.TestCase):
    def _env(self, **values: str):
        clean = {k: v for k, v in values.items()}
        return mock.patch.dict(os.environ, clean, clear=True)

    def test_empty(self) -> None:
        with self._env():
            self.assertEqual(discover._from_env(), [])

    def test_all_proxy_socks5(self) -> None:
        with self._env(ALL_PROXY="socks5://u:p@1.2.3.4:1080"):
            found = discover._from_env()
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual((f.node.kind, f.node.host, f.node.port), ("socks5", "1.2.3.4", 1080))
        self.assertEqual((f.node.username, f.node.password), ("u", "p"))
        self.assertEqual(f.source, "env")
        self.assertEqual(f.node.label, "env ALL_PROXY")

    def test_dedup_same_endpoint(self) -> None:
        with self._env(ALL_PROXY="socks5://1.2.3.4:1080",
                       all_proxy="socks5://1.2.3.4:1080",
                       HTTP_PROXY="socks5://1.2.3.4:1080"):
            found = discover._from_env()
        self.assertEqual(len(found), 1, "同 kind/host/port 只留一条")
        self.assertEqual(found[0].node.label, "env ALL_PROXY", "先出现的那条胜出")

    def test_multiple_distinct(self) -> None:
        with self._env(ALL_PROXY="socks5://1.2.3.4:1080",
                       HTTPS_PROXY="http://5.6.7.8:7890"):
            found = discover._from_env()
        self.assertEqual([(f.node.kind, f.node.host, f.node.port) for f in found],
                         [("socks5", "1.2.3.4", 1080), ("http", "5.6.7.8", 7890)])

    def test_invalid_is_skipped(self) -> None:
        with self._env(ALL_PROXY="vmess://whatever", HTTP_PROXY=""):
            self.assertEqual(discover._from_env(), [])


# ---------------------------------------------------------------- discover: wininet
class TestFromWininet(unittest.TestCase):
    def test_non_windows_returns_empty(self) -> None:
        with mock.patch.object(discover, "IS_WIN", False):
            with mock.patch.dict(sys.modules, {"winreg": fake_winreg_module(1, "1.1.1.1:1")}):
                self.assertEqual(discover._from_wininet(), [])

    def _call(self, enable: object, server: object) -> list:
        mod = fake_winreg_module(enable, server)
        with mock.patch.object(discover, "IS_WIN", True), \
                mock.patch.dict(sys.modules, {"winreg": mod}):
            return discover._from_wininet()

    def test_simple_server(self) -> None:
        found = self._call(1, "127.0.0.1:7890")
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].node.kind, found[0].node.host, found[0].node.port),
                         ("http", "127.0.0.1", 7890))
        self.assertEqual(found[0].source, "wininet")
        self.assertEqual(found[0].node.label, "Windows 系统代理")

    def test_protocol_prefixed_server(self) -> None:
        found = self._call(1, "http=127.0.0.1:7890;https=1.2.3.4:7891")
        self.assertEqual([(f.node.host, f.node.port) for f in found],
                         [("127.0.0.1", 7890), ("1.2.3.4", 7891)])

    def test_at_most_two_picks(self) -> None:
        found = self._call(1, "a:1;b:2;c:3")
        self.assertEqual(len(found), 2, "只取前两个，避免重复探测")

    def test_disabled_or_empty(self) -> None:
        self.assertEqual(self._call(0, "127.0.0.1:7890"), [])
        self.assertEqual(self._call(1, ""), [])

    def test_portless_entries_skipped(self) -> None:
        self.assertEqual(self._call(1, "127.0.0.1"), [])
        self.assertEqual(self._call(1, "http=127.0.0.1"), [])

    def test_query_error_returns_empty(self) -> None:
        mod = types.ModuleType("winreg")
        mod.HKEY_CURRENT_USER = object()

        def boom(*_a, **_k):
            raise OSError("no key")

        mod.OpenKey = boom
        mod.QueryValueEx = boom
        with mock.patch.object(discover, "IS_WIN", True), \
                mock.patch.dict(sys.modules, {"winreg": mod}):
            self.assertEqual(discover._from_wininet(), [])


# ---------------------------------------------------------------- discover: verify
class TestVerify(unittest.TestCase):
    def _found(self, **node_kw) -> discover.Found:
        return discover.Found(node=Node(kind="socks5", host="127.0.0.1",
                                        port=7899, **node_kw), source="test")

    def test_ok_and_steam_ok(self) -> None:
        sock = FakeSock(http_response(200))
        with mock.patch.object(netutil, "open_via", return_value=sock), \
                mock.patch.object(netutil, "tls_over", return_value=sock):
            f = discover.verify(self._found(), timeout=1.0)

        self.assertTrue(f.ok)
        self.assertTrue(f.steam_ok)
        self.assertEqual(f.http_code, 200)
        self.assertEqual(f.err, "")
        self.assertGreaterEqual(f.ms, 0.0)
        self.assertTrue(sock.closed, "verify 收尾必须关 socket")
        self.assertIn("HEAD / HTTP/1.1", sock.sent.decode("latin-1"))

    def test_403_counts_as_reachable(self) -> None:
        sock = FakeSock(http_response(403))
        with mock.patch.object(netutil, "open_via", return_value=sock), \
                mock.patch.object(netutil, "tls_over", return_value=sock):
            f = discover.verify(self._found(), timeout=1.0)
        self.assertTrue(f.ok)
        self.assertTrue(f.steam_ok, "403 是地区限制，链路是通的")

    def test_500_is_proxy_ok_but_steam_bad(self) -> None:
        sock = FakeSock(http_response(500))
        with mock.patch.object(netutil, "open_via", return_value=sock), \
                mock.patch.object(netutil, "tls_over", return_value=sock):
            f = discover.verify(self._found(), timeout=1.0)
        self.assertTrue(f.ok)
        self.assertFalse(f.steam_ok)
        self.assertEqual(f.http_code, 500)

    def test_proxy_failure_sets_false(self) -> None:
        with mock.patch.object(netutil, "open_via",
                               side_effect=ConnectionResetError("boom")):
            f = discover.verify(self._found(), timeout=1.0)
        self.assertFalse(f.ok)
        self.assertFalse(f.steam_ok)
        self.assertTrue(f.err.startswith("ConnectionResetError"), f.err)

    def test_tls_failure_sets_false(self) -> None:
        sock = FakeSock(http_response(200))
        with mock.patch.object(netutil, "open_via", return_value=sock), \
                mock.patch.object(netutil, "tls_over", side_effect=ssl_error()):
            f = discover.verify(self._found(), timeout=1.0)
        self.assertFalse(f.ok)
        self.assertFalse(f.steam_ok)
        self.assertTrue(f.err.startswith("SSLError"), f.err)

    def test_shallow_mode_skips_http(self) -> None:
        sock = FakeSock(http_response(200))
        with mock.patch.object(netutil, "open_via", return_value=sock), \
                mock.patch.object(netutil, "http_head",
                                  side_effect=AssertionError("deep=False 不该发 HTTP")) as hh:
            f = discover.verify(self._found(), timeout=1.0, deep=False)
        self.assertTrue(f.ok)
        self.assertTrue(f.steam_ok)
        hh.assert_not_called()

    def test_score_orders_steam_first(self) -> None:
        a = discover.Found(node=Node("socks5", "h", 1), ok=True, steam_ok=True, ms=90)
        b = discover.Found(node=Node("socks5", "h", 2), ok=True, steam_ok=False, ms=10)
        self.assertEqual([f.node.port for f in sorted([b, a], key=lambda f: f.score)],
                         [1, 2])


def ssl_error() -> Exception:
    import ssl
    return ssl.SSLError("handshake failure")


# ---------------------------------------------------------------- Router 边界
class TestRouterBoundaries(unittest.TestCase):
    def test_case_and_trailing_dot(self) -> None:
        r = Router()
        self.assertEqual(r.decide("STORE.STEAMPOWERED.COM"), "upstream")
        self.assertEqual(r.decide("store.steampowered.com."), "upstream")
        self.assertEqual(r.decide("  Store.SteamPowered.Com.  "), "upstream")

    def test_subdomain_matches_but_lookalike_does_not(self) -> None:
        r = Router()
        self.assertEqual(r.decide("cm-01-iad1.cm.steampowered.com"), "upstream")
        self.assertEqual(r.decide("notsteampowered.com"), "direct")
        self.assertEqual(r.decide("steampowered.com.evil.example"), "direct")

    def test_ipv6_and_loopback(self) -> None:
        r = Router()
        self.assertEqual(r.decide("[::1]"), "direct")
        self.assertEqual(r.decide("::1"), "direct")
        self.assertEqual(r.decide("localhost"), "direct")
        self.assertEqual(r.decide("printer.localhost"), "direct")
        self.assertEqual(r.decide("[2408::1]"), "direct", "whitelist 下公网 IPv6 也直连")

    def test_private_ranges_never_go_upstream_in_all_mode(self) -> None:
        r = Router(mode="all")
        for host in ("10.0.0.1", "192.168.1.5", "127.0.0.1", "169.254.10.10",
                     "172.16.0.1"):
            with self.subTest(host=host):
                self.assertEqual(r.decide(host), "direct")

    def test_private_172_16_12_fully_direct(self) -> None:
        """172.16.0.0/12 整段直连（Docker 172.17 / WSL 172.20 不能漏进上游）。"""
        r = Router(mode="all")
        for host in ("172.16.0.1", "172.17.0.2", "172.20.0.5", "172.31.255.254"):
            with self.subTest(host=host):
                self.assertEqual(r.decide(host), "direct")

    def test_steam_cidr_goes_upstream(self) -> None:
        r = Router()
        self.assertEqual(r.decide("23.45.64.5"), "upstream")
        self.assertEqual(r.decide("155.133.240.10"), "upstream")
        self.assertEqual(r.decide("1.2.3.4"), "direct")

    def test_explicit_hosts_override(self) -> None:
        r = Router(direct_hosts=["Store.SteamPowered.com."],
                   proxy_hosts=["Example.COM"])
        self.assertEqual(r.decide("store.steampowered.com"), "direct")
        self.assertEqual(r.decide("EXAMPLE.com."), "upstream")

    def test_modes(self) -> None:
        self.assertEqual(Router().decide("github.com"), "direct")
        self.assertEqual(Router(mode="all").decide("github.com"), "upstream")
        bl = Router(mode="blacklist")
        self.assertEqual(bl.decide("github.com"), "upstream")
        self.assertEqual(bl.decide("www.qq.com"), "direct")
        self.assertEqual(bl.decide("example.cn"), "direct", "顶级后缀 cn 也要匹配")

    def test_empty_host(self) -> None:
        self.assertEqual(Router().decide(""), "direct")
        self.assertEqual(Router(mode="all").decide(""), "upstream")

    def test_helpers(self) -> None:
        self.assertTrue(rules.match_suffix("a.b.steamstatic.com", ("steamstatic.com",)))
        self.assertFalse(rules.match_suffix("", ("steamstatic.com",)))
        self.assertTrue(rules.in_cidrs("[23.45.64.5]", ("23.45.64.0/18",)))
        self.assertFalse(rules.in_cidrs("store.steampowered.com", ("23.45.64.0/18",)))
        self.assertFalse(rules.in_cidrs("1.2.3.4", ("bogus-cidr",)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
