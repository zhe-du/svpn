"""本轮修复的回归测试（只覆盖「新语义」，实现细节见各用例 docstring）。

对应的改动：
    1. resolve 首轮问最多 2 家 DoH 并合并候选
    2. sni_block_test 的判定
    3. control_ip 的缓存（成功才缓存）
    4. probe_many 的并发 + 顺序
    5. display_width / pad 的中英混排宽度
    6. ProxyServer._dump_stats 的原子落盘 + 静默失败
    7. ProxyServer._stats_loop 的周期落盘
    8. Upstream.enabled 不再把 127.0.0.1 当占位符（P0）
    9. parse_node_url 支持 [IPv6]:port
   10. Router 把 172.16/12 整段（Docker/WSL）判为直连
   11. system.apply_hosts / clear_hosts 的受管块（幂等、保留手写条目）

原则：标准库 only、零联网（DNS/探测全 monkeypatch，落盘用 tempfile）。
运行：python tests/test_regression.py      # 退出码 0 = 全绿
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn import netutil, rules, system          # noqa: E402
from svpn.proxy import ProxyServer, Upstream  # noqa: E402
from svpn.rules import Router            # noqa: E402


def free_path(tmp: Path, name: str = "stats.json") -> Path:
    return tmp / name


# ---------------------------------------------------------------- 1. resolve 合并
class TestResolveMergesTwoProviders(unittest.TestCase):
    """单家 DoH 常只回一条 A（failover 不够），所以首轮要问 2 家并合并。"""

    P1 = "https://one.example/dns-query"
    P2 = "https://two.example/dns-query"
    P3 = "https://three.example/dns-query"

    def setUp(self) -> None:
        netutil.clear_dns_cache()
        self.addCleanup(netutil.clear_dns_cache)

    def _resolve(self, answers: dict[str, list[str]], **kwargs):
        calls: list[str] = []

        def fake_doh(url, name, rtype, timeout):
            calls.append(url)
            if isinstance(answers.get(url), Exception):
                raise answers[url]
            return list(answers.get(url, []))

        with mock.patch.object(netutil, "_doh_query", side_effect=fake_doh):
            ips, source = netutil.resolve("store.steampowered.com",
                                          providers=(self.P1, self.P2, self.P3),
                                          **kwargs)
        return ips, source, calls

    def test_one_ip_each_merges_two_providers(self) -> None:
        ips, source, calls = self._resolve({self.P1: ["1.1.1.1"], self.P2: ["2.2.2.2"]})
        self.assertEqual(ips, ["1.1.1.1", "2.2.2.2"], "两家各一条应合并保留顺序")
        self.assertEqual(calls, [self.P1, self.P2], "拿到 2 条即停，不该问第三家")
        self.assertEqual(source, "DoH:one.example", "source 只记首个 provider")

    def test_source_never_has_plus_marker(self) -> None:
        _ips, source, _calls = self._resolve({self.P1: ["1.1.1.1"], self.P2: ["2.2.2.2"]})
        self.assertNotIn("+", source, f"合并细节不该污染 source：{source!r}")

    def test_duplicate_across_providers_is_deduped(self) -> None:
        ips, _source, calls = self._resolve({self.P1: ["1.1.1.1"], self.P2: ["1.1.1.1"]})
        self.assertEqual(ips, ["1.1.1.1"], "重复 IP 只留一条")
        self.assertEqual(len(calls), 2, "去重后仍不足 2 条，但问满 2 家就停")

    def test_two_ips_from_first_provider_stops_immediately(self) -> None:
        ips, source, calls = self._resolve({self.P1: ["1.1.1.1", "2.2.2.2"]})
        self.assertEqual(ips, ["1.1.1.1", "2.2.2.2"])
        self.assertEqual(calls, [self.P1], "首家的 2 条已够，不该再问第二家")
        self.assertEqual(source, "DoH:one.example")

    def test_failing_provider_does_not_consume_budget(self) -> None:
        ips, _source, calls = self._resolve({self.P1: OSError("down"),
                                             self.P2: ["1.1.1.1"],
                                             self.P3: ["2.2.2.2"]})
        self.assertEqual(ips, ["1.1.1.1", "2.2.2.2"])
        self.assertEqual(calls, [self.P1, self.P2, self.P3],
                         "挂掉的不算「问过一家」，要继续问到 2 家成功")

    def test_merged_result_is_cached(self) -> None:
        answers = {self.P1: ["1.1.1.1"], self.P2: ["2.2.2.2"]}
        calls: list[str] = []

        def fake_doh(url, name, rtype, timeout):
            calls.append(url)
            return list(answers.get(url, []))

        with mock.patch.object(netutil, "_doh_query", side_effect=fake_doh):
            netutil.resolve("cdn.example", providers=(self.P1, self.P2, self.P3))
            ips2, src2 = netutil.resolve("cdn.example", providers=(self.P1, self.P2))
        self.assertEqual(len(calls), 2, "第二次应命中缓存，不再问 provider")
        self.assertEqual((ips2, src2), (["1.1.1.1", "2.2.2.2"], "DoH:one.example"))


# ---------------------------------------------------------------- 2. sni_block_test
class TestSniBlockTest(unittest.TestCase):
    def _run(self, status: str, control: str = "1.2.3.4"):
        with mock.patch.object(netutil, "control_ip", return_value=control) as ctl, \
                mock.patch.object(netutil, "tls_probe",
                                  return_value={"status": status}) as probe:
            result = netutil.sni_block_test("steamcommunity.com", timeout=0.5)
        return result, ctl, probe

    def test_rst_on_target_sni_means_blocked(self) -> None:
        ok, _ctl, probe = self._run("sni_rst")
        self.assertTrue(ok)
        args = probe.call_args.args
        self.assertEqual(args[0], "1.2.3.4", "必须复用对照 IP，不能换 IP")
        self.assertEqual(args[1], "steamcommunity.com", "SNI 要换成目标域名")
        self.assertEqual(args[2], 443)

    def test_ok_means_not_blocked(self) -> None:
        ok, _ctl, _probe = self._run("ok")
        self.assertFalse(ok)

    def test_other_failures_are_not_sni_blocking(self) -> None:
        for status in ("tls", "tcp"):
            with self.subTest(status=status):
                ok, _ctl, _probe = self._run(status)
                self.assertFalse(ok, f"{status} 不能判成 SNI 阻断")

    def test_no_control_ip_short_circuits(self) -> None:
        ok, ctl, probe = self._run("sni_rst", control="")
        self.assertFalse(ok, "连对照 IP 都没有时不能下结论")
        ctl.assert_called_once()
        probe.assert_not_called()

    def test_control_host_defaults_to_store(self) -> None:
        with mock.patch.object(netutil, "control_ip", return_value="") as ctl, \
                mock.patch.object(netutil, "tls_probe") as probe:
            netutil.sni_block_test("community.example")
        self.assertEqual(ctl.call_args.args[0], "store.steampowered.com")
        probe.assert_not_called()


# ---------------------------------------------------------------- 3. control_ip
class TestControlIpCache(unittest.TestCase):
    def setUp(self) -> None:
        netutil._CONTROL_IP.clear()
        self.addCleanup(netutil._CONTROL_IP.clear)

    def _patched(self, race_result):
        return (
            mock.patch.object(netutil, "resolve_merged",
                              return_value=(["1.1.1.1", "2.2.2.2"], "DoH:x")),
            mock.patch.object(netutil, "race_tls", return_value=race_result),
        )

    def test_success_is_cached(self) -> None:
        p_res, p_race = self._patched([{"ip": "2.2.2.2", "ms": 12.0}])
        with p_res as res, p_race as race:
            first = netutil.control_ip("store.steampowered.com")
            second = netutil.control_ip("store.steampowered.com")
        self.assertEqual(first, "2.2.2.2")
        self.assertEqual(second, "2.2.2.2")
        self.assertEqual(res.call_count, 1, "命中缓存后不该再解析")
        self.assertEqual(race.call_count, 1, "命中缓存后不该再竞速握手")

    def test_failure_is_not_cached(self) -> None:
        p_res, p_race = self._patched([])
        with p_res as res, p_race as race:
            first = netutil.control_ip("store.steampowered.com")
            second = netutil.control_ip("store.steampowered.com")
        self.assertEqual((first, second), ("", ""))
        self.assertEqual(res.call_count, 2, "失败必须重试，不能缓存空值")
        self.assertEqual(race.call_count, 2)

    def test_cache_is_per_host(self) -> None:
        p_res, p_race = self._patched([{"ip": "2.2.2.2", "ms": 1.0}])
        with p_res as res, p_race:
            netutil.control_ip("a.example")
            netutil.control_ip("b.example")
        self.assertEqual(res.call_count, 2, "不同 host 的对照 IP 不能串用")

    def test_races_at_most_six_ips(self) -> None:
        eight = [f"10.0.0.{i}" for i in range(1, 9)]
        with mock.patch.object(netutil, "resolve_merged", return_value=(eight, "system")), \
                mock.patch.object(netutil, "race_tls",
                                  return_value=[{"ip": "10.0.0.1", "ms": 1.0}]) as race:
            netutil.control_ip("store.steampowered.com")
        passed = race.call_args.args[1]
        self.assertEqual(len(passed), 6, f"最多竞速 6 台，实际 {passed}")


# ---------------------------------------------------------------- 4. probe_many
class TestProbeMany(unittest.TestCase):
    def test_keeps_input_order_despite_completion_order(self) -> None:
        def slow_probe(ip, host, port, timeout):
            # 第一台最慢：如果实现按完成顺序返回，顺序就会翻转
            time.sleep(0.05 if ip == "1.1.1.1" else 0.0)
            return {"ip": ip, "status": "ok" if ip.endswith(".1") else "sni_rst",
                    "ms": 1.0}

        with mock.patch.object(netutil, "tls_probe", side_effect=slow_probe):
            got = netutil.probe_many("store.steampowered.com",
                                     ["1.1.1.1", "2.2.2.2", "3.3.3.3"], timeout=0.1)
        self.assertEqual([r["ip"] for r in got], ["1.1.1.1", "2.2.2.2", "3.3.3.3"])
        self.assertEqual([r["status"] for r in got], ["ok", "sni_rst", "sni_rst"])

    def test_failed_ips_are_kept(self) -> None:
        """probe_many 和 race_tls 的区别就在这：失败也要留下分层结果。"""
        with mock.patch.object(netutil, "tls_probe",
                               side_effect=[{"ip": "1.1.1.1", "status": "tcp"},
                                            {"ip": "2.2.2.2", "status": "sni_rst"}]):
            got = netutil.probe_many("x.example", ["1.1.1.1", "2.2.2.2"])
        self.assertEqual([r["status"] for r in got], ["tcp", "sni_rst"])

    def test_dedupes_and_drops_empty(self) -> None:
        with mock.patch.object(netutil, "tls_probe",
                               side_effect=lambda ip, *a: {"ip": ip, "status": "ok"}) as probe:
            got = netutil.probe_many("x.example", ["1.1.1.1", "1.1.1.1", "", "2.2.2.2"])
        self.assertEqual([r["ip"] for r in got], ["1.1.1.1", "2.2.2.2"])
        self.assertEqual(probe.call_count, 2)

    def test_empty_input_does_not_probe(self) -> None:
        with mock.patch.object(netutil, "tls_probe") as probe:
            self.assertEqual(netutil.probe_many("x.example", []), [])
        probe.assert_not_called()

    def test_passes_host_port_and_timeout(self) -> None:
        with mock.patch.object(netutil, "tls_probe",
                               return_value={"ip": "1.1.1.1", "status": "ok"}) as probe:
            netutil.probe_many("steamcommunity.com", ["1.1.1.1"], port=8443,
                               timeout=1.5)
        self.assertEqual(probe.call_args.args[1:], ("steamcommunity.com", 8443, 1.5))


# ---------------------------------------------------------------- 5. 宽度对齐
class TestDisplayWidth(unittest.TestCase):
    def test_cjk_counts_two_columns(self) -> None:
        self.assertEqual(netutil.display_width("域名"), 4)
        self.assertEqual(netutil.display_width("abc"), 3)
        self.assertEqual(netutil.display_width(""), 0)

    def test_mixed_text(self) -> None:
        self.assertEqual(netutil.display_width("域名abc"), 7)
        self.assertEqual(netutil.display_width("ＦＵＬＬ"), 8, "全角字母也是 2 列")

    def test_pad_left_align(self) -> None:
        out = netutil.pad("域名", 8)
        self.assertEqual(out, "域名    ")
        self.assertEqual(netutil.display_width(out), 8)

    def test_pad_right_align(self) -> None:
        out = netutil.pad("abc", 6, right=True)
        self.assertEqual(out, "   abc")
        self.assertEqual(netutil.display_width(out), 6)
        self.assertEqual(netutil.pad("域名", 6, right=True), "  域名")

    def test_pad_never_truncates(self) -> None:
        self.assertEqual(netutil.pad("abcdef", 3), "abcdef")
        self.assertEqual(netutil.pad("域名", 1, right=True), "域名")
        self.assertEqual(netutil.pad("abc", 3), "abc")


# ---------------------------------------------------------------- 6+7. 统计落盘
class TestStatsDump(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _server(self) -> ProxyServer:
        s = ProxyServer({})
        s.stats.conns = 3
        s.stats.errors = 1
        s.stats.via_upstream = 2
        s.stats.via_direct = 1
        s.stats.up_bytes = 2 * 1048576
        s.stats.down_bytes = 3 * 1048576
        s.stats.hit("store.steampowered.com")
        s.stats.hit("store.steampowered.com")
        s.stats.hit("cdn.akamai.steamstatic.com")
        return s

    def _read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_dump_writes_snapshot_json(self) -> None:
        s = self._server()
        path = free_path(self.dir)
        s._dump_stats(str(path))

        self.assertTrue(path.exists())
        data = self._read(path)
        expected = json.loads(json.dumps(s.stats.snapshot()))
        self.assertEqual(data, expected)
        self.assertEqual(data["connections"], 3)
        self.assertEqual(data["via_upstream"], 2)
        self.assertEqual(data["down_mb"], 3.0)
        self.assertEqual(data["top_hosts"][0], ["store.steampowered.com", 2])

    def test_dump_leaves_no_tmp_file(self) -> None:
        s = self._server()
        path = free_path(self.dir)
        s._dump_stats(str(path))
        leftovers = [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], f"tmp 残留：{leftovers}")

    def test_dump_overwrites_atomically(self) -> None:
        s = self._server()
        path = free_path(self.dir)
        s._dump_stats(str(path))
        s.stats.conns = 99
        s._dump_stats(str(path))
        self.assertEqual(self._read(path)["connections"], 99)

    def test_missing_parent_is_silent(self) -> None:
        s = self._server()
        path = self.dir / "no_such_dir" / "deeper" / "stats.json"
        try:
            s._dump_stats(str(path))
        except OSError as exc:                      # 不该冒泡
            self.fail(f"_dump_stats 不该抛异常，实际 {exc!r}")
        self.assertFalse(path.exists(), "父目录不存在时静默跳过，不建目录")

    def test_stats_loop_writes_periodically(self) -> None:
        s = self._server()
        path = free_path(self.dir, "loop.json")

        async def scenario() -> None:
            task = asyncio.create_task(s._stats_loop(str(path), interval=0.05))
            await asyncio.sleep(0.2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertTrue(path.exists(), "_stats_loop 应至少落盘一次")
        data = self._read(path)
        self.assertIn("connections", data)
        self.assertEqual(data["connections"], 3)

    def test_stats_loop_survives_bad_path(self) -> None:
        s = self._server()
        bad = self.dir / "missing" / "x.json"

        async def scenario() -> None:
            task = asyncio.create_task(s._stats_loop(str(bad), interval=0.02))
            await asyncio.sleep(0.1)
            self.assertFalse(task.done(), "路径写不进去也不该把循环搞崩")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(scenario())


# ---------------------------------------------------------------- 8. Upstream.enabled
class TestUpstreamEnabled(unittest.TestCase):
    def test_loopback_upstream_is_enabled(self) -> None:
        """P0 回归：本机 Clash/v2rayN/sing-box 就在 127.0.0.1 上。"""
        for kind, port in (("socks5", 7890), ("http", 7897), ("socks5", 10808),
                           ("http", 8889)):
            with self.subTest(kind=kind, port=port):
                self.assertTrue(Upstream(kind=kind, host="127.0.0.1",
                                         port=port).enabled)

    def test_placeholder_hosts_stay_disabled(self) -> None:
        for host in ("NODE_HOST", "node", "HOST", "example.com", "change_me"):
            with self.subTest(host=host):
                self.assertFalse(Upstream(kind="socks5", host=host, port=1080).enabled)

    def test_empty_host_disabled(self) -> None:
        self.assertFalse(Upstream(kind="socks5", host="", port=1080).enabled)

    def test_direct_kind_disabled(self) -> None:
        self.assertFalse(Upstream(kind="direct", host="127.0.0.1", port=7890).enabled)

    def test_real_node_enabled(self) -> None:
        self.assertTrue(Upstream(kind="socks5", host="203.0.113.9", port=1080).enabled)

    def test_proxyserver_wires_it_through(self) -> None:
        s = ProxyServer({"upstream": {"kind": "socks5", "host": "127.0.0.1",
                                      "port": 7890}})
        self.assertTrue(s.upstream.enabled, "从 cfg 构造出来也必须可用")
        self.assertEqual(s.router.decide("store.steampowered.com"), "upstream")


# ---------------------------------------------------------------- 9. IPv6 节点 URL
class TestNodeUrlIpv6(unittest.TestCase):
    def test_bracketed_ipv6_with_auth_and_port(self) -> None:
        n = netutil.parse_node_url("socks5://u:p@[::1]:1080")
        self.assertEqual((n.kind, n.host, n.port), ("socks5", "::1", 1080))
        self.assertEqual((n.username, n.password), ("u", "p"))

    def test_global_ipv6(self) -> None:
        n = netutil.parse_node_url("socks5://[2408::1]:1080")
        self.assertEqual((n.host, n.port, n.username), ("2408::1", 1080, ""))

    def test_bracketed_without_port_uses_default(self) -> None:
        self.assertEqual(netutil.parse_node_url("http://[::1]").port, 7890)
        self.assertEqual(netutil.parse_node_url("socks5://[::1]").port, 1080)

    def test_bare_ipv6_keeps_default_port(self) -> None:
        n = netutil.parse_node_url("2408::1")
        self.assertEqual((n.host, n.port), ("2408::1", 1080))

    def test_unclosed_bracket_raises(self) -> None:
        with self.assertRaises(ValueError):
            netutil.parse_node_url("socks5://[::1:1080")

    def test_ipv4_and_hostname_still_work(self) -> None:
        self.assertEqual(netutil.parse_node_url("1.2.3.4:1080").host, "1.2.3.4")
        self.assertEqual(netutil.parse_node_url("proxy.example:8080").host,
                         "proxy.example")


# ---------------------------------------------------------------- 10. 私有网段
class TestRouterPrivateRanges(unittest.TestCase):
    PRIVATE = ("10.1.2.3", "172.16.0.1", "172.17.0.2", "172.20.0.5",
               "172.31.255.254", "192.168.1.5", "169.254.1.1", "127.0.0.1")

    def test_all_mode_never_sends_private_upstream(self) -> None:
        r = Router(mode="all")
        for host in self.PRIVATE:
            with self.subTest(host=host):
                self.assertEqual(r.decide(host), "direct")

    def test_blacklist_mode_also_direct(self) -> None:
        r = Router(mode="blacklist")
        for host in ("172.17.0.2", "172.20.0.5"):
            with self.subTest(host=host):
                self.assertEqual(r.decide(host), "direct")

    def test_steam_cidr_still_upstream(self) -> None:
        """修私有段不能顺手把 Valve 网段也扣下来。"""
        r = Router(mode="all")
        self.assertEqual(r.decide("23.45.64.5"), "upstream")
        self.assertEqual(r.decide("155.133.240.10"), "upstream")

    def test_public_ip_still_follows_mode(self) -> None:
        self.assertEqual(Router().decide("1.2.3.4"), "direct")
        self.assertEqual(Router(mode="all").decide("1.2.3.4"), "upstream")

    def test_domain_still_matches_steam_suffix(self) -> None:
        self.assertEqual(Router(mode="all").decide("store.steampowered.com"),
                         "upstream")
        self.assertTrue(rules.in_cidrs("172.20.0.5", ("172.16.0.0/12",)))


# ---------------------------------------------------------------- 11. hosts 受管块
class TestHostsManagedBlock(unittest.TestCase):
    """真机写 hosts 要 UAC，测试里把 system.HOSTS 指到 tempfile 验证纯逻辑。

    apply/clear 都是「读全文 → 剥掉受管块 → 重新拼 → 整文件写回」，
    所以幂等性和「不动手写条目」是最容易写错的两点。
    """

    ORIGINAL = ("127.0.0.1 localhost\n"
                "# 自建条目，不许被删\n"
                "10.0.0.9   nas.local\n")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.hosts = self.root / "hosts"
        self.store = self.root / "runtime"
        self.store.mkdir()
        self.hosts.write_text(self.ORIGINAL, encoding="utf-8")
        patcher = mock.patch.object(system, "HOSTS", self.hosts)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ------------------------------------------------
    def text(self) -> str:
        return self.hosts.read_text(encoding="utf-8")

    def apply(self, entries: dict[str, str]) -> dict:
        return system.apply_hosts(entries, store=self.store)

    def block_lines(self) -> list[str]:
        lines, inside = [], False
        for line in self.text().splitlines():
            if line.strip() == system.MARK_BEGIN:
                inside = True
                continue
            if line.strip() == system.MARK_END:
                inside = False
                continue
            if inside:
                lines.append(line)
        return lines

    # -- 1. 写入 ------------------------------------------------
    def test_apply_writes_managed_block(self) -> None:
        res = self.apply({"1.2.3.4": "a.com b.com"})
        self.assertEqual(res["ok"], True)
        self.assertEqual(res["path"], str(self.hosts))
        self.assertEqual(res["entries"], 1)

        body = self.text()
        self.assertIn(system.MARK_BEGIN, body)
        self.assertIn(system.MARK_END, body)
        self.assertLess(body.index(system.MARK_BEGIN), body.index(system.MARK_END))

        block = self.block_lines()
        self.assertEqual(len(block), 1, f"受管块内容异常：{block}")
        self.assertEqual(block[0].split()[0], "1.2.3.4")
        self.assertEqual(block[0].split()[1:], ["a.com", "b.com"],
                         "一个 IP 后面的多个域名要留在同一行")

    def test_hosts_block_formatting(self) -> None:
        block = system.hosts_block({"1.2.3.4": "a.com"})
        lines = block.splitlines()
        self.assertEqual(lines[0], system.MARK_BEGIN)
        self.assertEqual(lines[-1], system.MARK_END)
        self.assertEqual(lines[1][:16].strip(), "1.2.3.4", "IP 左对齐补到 16 列")
        self.assertTrue(lines[1].endswith("a.com"))

    # -- 2. 幂等 ------------------------------------------------
    def test_apply_twice_keeps_single_block(self) -> None:
        self.apply({"1.2.3.4": "a.com b.com"})
        first = self.text()
        self.apply({"1.2.3.4": "a.com b.com"})
        second = self.text()

        self.assertEqual(second.count(system.MARK_BEGIN), 1, "重复 apply 不该叠块")
        self.assertEqual(second.count(system.MARK_END), 1)
        self.assertEqual(first, second, "同样输入两次 apply，文件应完全一致")
        self.assertEqual(len(self.block_lines()), 1)

    def test_apply_replaces_stale_entries(self) -> None:
        self.apply({"1.2.3.4": "old.com"})
        self.apply({"5.6.7.8": "new.com"})
        block = self.block_lines()
        self.assertEqual(len(block), 1)
        self.assertIn("new.com", block[0])
        self.assertNotIn("old.com", self.text(), "旧块里的条目必须被清掉")

    # -- 3. 手写条目保留 -----------------------------------------
    def test_manual_entries_survive_apply(self) -> None:
        self.apply({"1.2.3.4": "a.com b.com"})
        body = self.text()
        for line in ("127.0.0.1 localhost", "10.0.0.9   nas.local", "# 自建条目"):
            with self.subTest(line=line):
                self.assertIn(line, body)

    def test_manual_entries_survive_clear(self) -> None:
        self.apply({"1.2.3.4": "a.com"})
        self.assertEqual(system.clear_hosts(store=self.store)["ok"], True)
        body = self.text()
        for line in ("127.0.0.1 localhost", "10.0.0.9   nas.local", "# 自建条目"):
            with self.subTest(line=line):
                self.assertIn(line, body)

    # -- 4. 清除 ------------------------------------------------
    def test_clear_removes_block(self) -> None:
        self.apply({"1.2.3.4": "a.com b.com"})
        res = system.clear_hosts(store=self.store)

        self.assertEqual(res["ok"], True)
        body = self.text()
        self.assertNotIn(system.MARK_BEGIN, body)
        self.assertNotIn(system.MARK_END, body)
        self.assertNotIn("a.com", body)
        self.assertEqual(self.block_lines(), [])

    def test_clear_twice_is_stable(self) -> None:
        self.apply({"1.2.3.4": "a.com"})
        system.clear_hosts(store=self.store)
        once = self.text()
        system.clear_hosts(store=self.store)
        self.assertEqual(self.text(), once, "没有受管块时 clear 应是无操作")

    def test_clear_on_pristine_file_changes_nothing_meaningful(self) -> None:
        system.clear_hosts(store=self.store)
        body = self.text()
        self.assertIn("127.0.0.1 localhost", body)
        self.assertIn("10.0.0.9   nas.local", body)

    # -- 5. 多条 IP ---------------------------------------------
    def test_multiple_ips_one_line_each(self) -> None:
        entries = {"1.2.3.4": "a.com b.com",
                   "5.6.7.8": "c.com",
                   "9.9.9.9": "d.com e.com f.com"}
        res = self.apply(entries)

        self.assertEqual(res["entries"], 3)
        block = self.block_lines()
        self.assertEqual(len(block), 3, f"三个 IP 应各占一行：{block}")
        self.assertEqual([ln.split()[0] for ln in block],
                         ["1.2.3.4", "5.6.7.8", "9.9.9.9"], "顺序应与 entries 一致")
        self.assertEqual(block[0].split()[1:], ["a.com", "b.com"])
        self.assertEqual(block[2].split()[1:], ["d.com", "e.com", "f.com"])

    # -- 备份 / 异常路径 -----------------------------------------
    def test_backup_keeps_first_original(self) -> None:
        self.apply({"1.2.3.4": "a.com"})
        bak = self.store / "hosts.backup"
        self.assertTrue(bak.exists(), "第一次写 hosts 前应备份原件")
        self.assertEqual(bak.read_text(encoding="utf-8"), self.ORIGINAL)

        self.apply({"5.6.7.8": "b.com"})
        self.assertEqual(bak.read_text(encoding="utf-8"), self.ORIGINAL,
                         "备份只保留最开始那份，不能被第二次 apply 覆盖")

    def test_missing_hosts_file_is_handled(self) -> None:
        self.hosts.unlink()
        res = self.apply({"1.2.3.4": "a.com"})
        self.assertEqual(res["ok"], True)
        self.assertIn(system.MARK_BEGIN, self.text())
        self.assertFalse((self.store / "hosts.backup").exists(),
                         "原文件不存在时没有东西可备份")
        self.assertEqual(system.clear_hosts(store=self.store)["ok"], True)
        self.assertNotIn(system.MARK_BEGIN, self.text())

    def test_permission_denied_is_reported(self) -> None:
        """真机上的 UAC 拒绝路径：不能抛，要返回可读原因。

        实装改为按字节写回（保留原编码），所以这里 patch write_bytes。
        """
        with mock.patch.object(Path, "write_bytes",
                               side_effect=PermissionError("denied")):
            res = self.apply({"1.2.3.4": "a.com"})
        self.assertEqual(res["ok"], False)
        self.assertIn("管理员权限", res["reason"])
        self.assertEqual(self.text(), self.ORIGINAL, "写失败时不许改坏原文件")

    def test_lines_after_block_survive(self) -> None:
        """受管块后面若还有手写行（用户手动加过），重写时也得留着。"""
        self.hosts.write_text(self.ORIGINAL + system.hosts_block({"1.2.3.4": "a.com"})
                              + "\n192.168.0.1 printer.local\n", encoding="utf-8")
        self.apply({"5.6.7.8": "b.com"})
        body = self.text()
        self.assertIn("192.168.0.1 printer.local", body)
        self.assertIn("5.6.7.8", body)
        self.assertNotIn("1.2.3.4", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
