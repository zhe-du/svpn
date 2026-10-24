"""svpn/cli.py 契约测试（P2-cli）。

覆盖：配置归一（merge_config / load_config）、默认键集、pid/端口探测的编码健壮性、
子命令接线、probe 退出码语义、hosts 在 SNI 模式下指向 127.0.0.1、端口冲突预检。

全部离线：不联网、不 bind 固定端口、不弹 UAC、不写系统 hosts。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn import cdn as cdnmod          # noqa: E402
from svpn import cli                    # noqa: E402
from svpn import probe as probemod      # noqa: E402

# SPEC §4.1 明列的顶层键（12 个；SPEC 正文写「13 个」是笔误，逐项对过 dict 本体）
SPEC_KEYS = {
    "listen_host", "listen_port", "http_port", "mode", "connect_timeout",
    "auto_node", "upstream", "auth", "browser", "cdn",
    "proxy_hosts", "direct_hosts",
}

# build_parser() 里的 13 个子命令（SPEC 正文写「12 个」同样是笔误）
SUBCOMMANDS = {
    "init": ["init"],
    "up": ["up", "-b", "--port", "17899"],
    "_serve": ["_serve"],
    "down": ["down", "--clear-hosts"],
    "status": ["status"],
    "probe": ["probe", "--lenient", "--node", "none"],
    "node": ["node", "scan"],
    "cdn": ["cdn", "--refresh", "--no-bench"],
    "hosts": ["hosts", "apply", "--no-elevate"],
    "system": ["system", "on"],
    "pac": ["pac", "--write"],
    "steam": ["steam", "--no-autostart", "--exe", "steam.exe"],
    "doctor": ["doctor"],
}


class TmpRuntimeMixin(unittest.TestCase):
    """把 RUNTIME / PROBEFILE / CONFIG 换到临时目录，绝不碰真实 runtime。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.runtime = self.tmpdir / "runtime"
        self.patches = [
            mock.patch.object(cli, "RUNTIME", self.runtime),
            mock.patch.object(cli, "PROBEFILE", self.runtime / "probe.json"),
            mock.patch.object(cli, "CONFIG", self.tmpdir / "config.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()


# --------------------------------------------------------------- 配置归一
class TestMergeConfig(unittest.TestCase):
    def test_empty_equals_default_deep_copy(self):
        cfg = cli.merge_config({})
        self.assertEqual(cfg, cli.DEFAULT_CONFIG)
        # 必须是深拷贝：改返回值不能污染 DEFAULT_CONFIG
        cfg["cdn"]["hosts"].append("evil.example.com")
        cfg["proxy_hosts"].append("x")
        self.assertNotIn("evil.example.com", cli.DEFAULT_CONFIG["cdn"]["hosts"])
        self.assertEqual(cli.DEFAULT_CONFIG["proxy_hosts"], [])

    def test_none_and_non_dict_input(self):
        self.assertEqual(cli.merge_config(None), cli.DEFAULT_CONFIG)
        self.assertEqual(cli.merge_config(["x"]), cli.DEFAULT_CONFIG)  # type: ignore[arg-type]

    def test_drops_underscore_and_unknown_keys(self):
        cfg = cli.merge_config({
            "_note": "写给人看的",
            "bogus": 1,
            "listen_port": 17899,
            "cdn": {"_hint": "x", "refresh_hours": 3},
        })
        self.assertNotIn("_note", cfg)
        self.assertNotIn("bogus", cfg)
        self.assertNotIn("_hint", cfg["cdn"])
        self.assertEqual(set(cfg), SPEC_KEYS)
        self.assertEqual(cfg["listen_port"], 17899)
        self.assertEqual(cfg["cdn"]["refresh_hours"], 3)

    def test_nested_dict_merges_per_key(self):
        cfg = cli.merge_config({"cdn": {"refresh_hours": 2}})
        self.assertEqual(cfg["cdn"]["refresh_hours"], 2)
        # 其他子键保持默认
        self.assertEqual(cfg["cdn"]["sni_port"], 443)
        self.assertFalse(cfg["cdn"]["enabled"])
        self.assertEqual(cfg["cdn"]["hosts"], list(cdnmod.CDN_DOMAINS))

    def test_numeric_tolerance(self):
        cfg = cli.merge_config({"listen_port": None, "http_port": "",
                                "connect_timeout": "abc"})
        self.assertEqual(cfg["listen_port"], 7899)
        self.assertEqual(cfg["http_port"], 7900)
        self.assertEqual(cfg["connect_timeout"], 12)

    def test_numeric_tolerance_nested(self):
        cfg = cli.merge_config({"upstream": {"port": None},
                                "cdn": {"refresh_hours": "", "sni_port": "abc"}})
        self.assertEqual(cfg["upstream"]["port"], 1080)
        self.assertEqual(cfg["cdn"]["refresh_hours"], 6)
        self.assertEqual(cfg["cdn"]["sni_port"], 443)

    def test_numeric_range_and_string_numbers(self):
        cfg = cli.merge_config({"listen_port": "17899", "http_port": 70000,
                                "connect_timeout": "8.5"})
        self.assertEqual(cfg["listen_port"], 17899)
        self.assertEqual(cfg["http_port"], 7900)      # 越界回落
        self.assertEqual(cfg["connect_timeout"], 8.5)

    def test_http_port_zero_means_disabled(self):
        self.assertEqual(cli.merge_config({"http_port": 0})["http_port"], 0)
        self.assertEqual(cli.merge_config({"listen_port": 0})["listen_port"], 7899)

    def test_mode_and_bool_normalization(self):
        self.assertEqual(cli.merge_config({"mode": "ALL"})["mode"], "all")
        self.assertEqual(cli.merge_config({"mode": "nope"})["mode"], "whitelist")
        self.assertIs(cli.merge_config({"auto_node": "false"})["auto_node"], False)
        self.assertIs(cli.merge_config({"cdn": {"enabled": "1"}})["cdn"]["enabled"], True)

    def test_merge_config_importable_directly(self):
        # P6 的 tests/test_config_contract.py 就是这么导的（`from svpn.cli import merge_config`）
        from svpn.cli import merge_config
        self.assertEqual(merge_config({}), cli.DEFAULT_CONFIG)
        self.assertIs(merge_config, cli.merge_config)

    def test_merge_config_signature(self):
        import inspect
        params = list(inspect.signature(cli.merge_config).parameters.values())
        self.assertEqual(len(params), 1)
        self.assertIn(params[0].name, ("user", "cfg", "config"))

    def test_host_lists(self):
        cfg = cli.merge_config({"proxy_hosts": "a.com, b.com",
                                "direct_hosts": ["c.com", "c.com", 3]})
        self.assertEqual(cfg["proxy_hosts"], ["a.com", "b.com"])
        self.assertEqual(cfg["direct_hosts"], ["c.com", "3"])


class TestDefaultConfigContract(unittest.TestCase):
    def test_keyset_is_exact(self):
        self.assertEqual(set(cli.DEFAULT_CONFIG), SPEC_KEYS)
        self.assertEqual(len(SPEC_KEYS), 12)

    def test_cdn_hosts_equals_cdn_domains(self):
        self.assertEqual(cli.DEFAULT_CONFIG["cdn"]["hosts"], list(cdnmod.CDN_DOMAINS))
        # 默认值与 CDN 域名表必须真的同源，不能各写一份
        self.assertIsInstance(cli.DEFAULT_CONFIG["cdn"]["hosts"], list)

    def test_types(self):
        cfg = cli.DEFAULT_CONFIG
        self.assertEqual(cfg["listen_host"], "127.0.0.1")
        self.assertIsInstance(cfg["listen_port"], int)
        self.assertIsInstance(cfg["http_port"], int)
        self.assertIsInstance(cfg["connect_timeout"], (int, float))
        self.assertIsInstance(cfg["auto_node"], bool)
        self.assertEqual(cfg["proxy_hosts"], [])
        self.assertEqual(cfg["direct_hosts"], [])
        self.assertIn(cfg["cdn"]["refresh_hours"], (6,))
        self.assertEqual(cfg["cdn"]["sni_port"], 443)

    def test_router_consumed_keys_present(self):
        # Router 会读这两个键，默认配置缺了就会和 example 漂移
        self.assertIn("proxy_hosts", cli.DEFAULT_CONFIG)
        self.assertIn("direct_hosts", cli.DEFAULT_CONFIG)


class TestLoadConfig(TmpRuntimeMixin):
    def test_missing_file_uses_defaults(self):
        self.assertEqual(cli.load_config(), cli.DEFAULT_CONFIG)

    def test_drops_note_and_unknown(self):
        cli.CONFIG.write_text(json.dumps({
            "_note": "说明文字",
            "listen_port": 17899,
            "未知键": [1, 2],
            "cdn": {"enabled": True, "hosts": ["a.steamstatic.com"]},
        }), encoding="utf-8")
        cfg = cli.load_config()
        self.assertNotIn("_note", cfg)
        self.assertNotIn("未知键", cfg)
        self.assertEqual(set(cfg), SPEC_KEYS)
        self.assertEqual(cfg["listen_port"], 17899)
        self.assertTrue(cfg["cdn"]["enabled"])
        self.assertEqual(cfg["cdn"]["hosts"], ["a.steamstatic.com"])


# --------------------------------------------------------------- 系统探测
class TestSystemProbes(unittest.TestCase):
    def test_pid_alive_missing(self):
        self.assertFalse(cli.pid_alive(999999))
        self.assertFalse(cli.pid_alive(0))
        self.assertFalse(cli.pid_alive(-1))
        self.assertFalse(cli.pid_alive("abc"))  # type: ignore[arg-type]

    def test_pid_alive_self(self):
        self.assertTrue(cli.pid_alive(os.getpid()))

    def test_port_owner_never_raises(self):
        for port in (1, 65534):
            self.assertIsInstance(cli._port_owner(port), str)

    def test_decode_never_raises(self):
        self.assertEqual(cli._decode(None), "")
        self.assertEqual(cli._decode(b""), "")
        self.assertIsInstance(cli._decode(b"\xd6\xd0\xce\xc4 pid 123"), str)
        self.assertIsInstance(cli._decode(b"\xff\xfe\x00garbage"), str)


# --------------------------------------------------------------- 子命令
class TestParser(unittest.TestCase):
    def test_all_subcommands_parse(self):
        parser = cli.build_parser()
        self.assertEqual(len(SUBCOMMANDS), 13)
        for name, argv in SUBCOMMANDS.items():
            with self.subTest(cmd=name):
                args = parser.parse_args(argv)
                self.assertEqual(args.cmd, name)
                self.assertTrue(callable(args.func))

    def test_probe_lenient_flag(self):
        args = cli.build_parser().parse_args(["probe", "--lenient"])
        self.assertTrue(args.lenient)
        self.assertFalse(cli.build_parser().parse_args(["probe"]).lenient)

    def test_requires_subcommand(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args([])


# --------------------------------------------------------------- probe 语义
class TestProbeExitCode(TmpRuntimeMixin):
    def _run(self, rows, lenient: bool = False, json_out: bool = False):
        args = argparse.Namespace(node="none", timeout=0.01,
                                  json=json_out, lenient=lenient)
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config",
                               return_value=cli.merge_config({})), \
                mock.patch.object(cli.probemod, "run", return_value=rows), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_probe(args)
        return code, buf.getvalue()

    def test_probe_writes_file_creating_runtime_dir(self):
        rows = [probemod.Row(host="store.steampowered.com", note="商店",
                             direct={"ok": True, "ms": 12.0, "ip": "1.2.3.4"},
                             verdict="直连可用")]
        self.assertFalse(cli.RUNTIME.exists())
        code, _ = self._run(rows)
        self.assertEqual(code, 0)
        self.assertTrue(cli.PROBEFILE.exists())     # 目录是自动建的，不能 FileNotFoundError
        payload = json.loads(cli.PROBEFILE.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["unusable"], [])

    def test_all_direct_ok_exit_zero(self):
        rows = [probemod.Row(host="store.steampowered.com", note="商店",
                             direct={"ok": True, "ms": 9.0}, verdict="直连可用")]
        code, out = self._run(rows)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("退出码 0 = 全部端点可用"), 1)
        self.assertIn("见 summary.unusable", out)

    def test_need_node_without_node_exit_one(self):
        rows = [probemod.Row(host="steamcommunity.com", note="社区",
                             direct={"ok": False, "reason": "sni_rst"},
                             node=None, verdict="需节点 · SNI 阻断")]
        code, out = self._run(rows)
        self.assertEqual(code, 1)
        self.assertIn("不可用 1 个", out)

    def test_need_node_with_working_node_exit_zero(self):
        rows = [probemod.Row(host="steamcommunity.com", note="社区",
                             direct={"ok": False, "reason": "sni_rst"},
                             node={"ok": True, "code": 200},
                             verdict="需节点 · SNI 阻断")]
        code, _ = self._run(rows)
        self.assertEqual(code, 0)

    def test_dead_exit_one_both_modes(self):
        rows = [probemod.Row(host="api.steampowered.com", note="API",
                             direct={"ok": False, "reason": "tls"},
                             verdict="不通 · 待排查")]
        self.assertEqual(self._run(rows)[0], 1)
        self.assertEqual(self._run(rows, lenient=True)[0], 1)

    def test_lenient_keeps_old_behaviour(self):
        rows = [probemod.Row(host="steamcommunity.com", note="社区",
                             direct={"ok": False, "reason": "sni_rst"},
                             node=None, verdict="需节点 · SNI 阻断")]
        self.assertEqual(self._run(rows)[0], 1)                  # 严格
        self.assertEqual(self._run(rows, lenient=True)[0], 0)     # 宽松 = 旧行为

    def test_summary_keeps_five_original_keys(self):
        rows = [probemod.Row(host="store.steampowered.com", note="商店",
                             direct={"ok": True}, verdict="直连可用")]
        args = argparse.Namespace(node="none", timeout=0.01, json=True, lenient=False)
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config",
                               return_value=cli.merge_config({})), \
                mock.patch.object(cli.probemod, "run", return_value=rows), \
                contextlib.redirect_stdout(buf):
            cli.cmd_probe(args)
        payload = json.loads(cli.PROBEFILE.read_text(encoding="utf-8"))["summary"]
        self.assertEqual(set(payload) >= {"node", "direct_ok", "need_node",
                                          "dead", "ok", "unusable"}, True)
        self.assertEqual(payload["unusable"], [])

    def test_probe_unusable_classification(self):
        rows = [
            probemod.Row(host="a", note="", direct={"ok": True}, verdict="直连可用"),
            probemod.Row(host="b", note="", direct={"ok": False},
                         node={"ok": True}, verdict="需节点 · SNI 阻断"),
            probemod.Row(host="c", note="", direct={"ok": False},
                         node=None, verdict="需节点 · DNS 污染"),
            probemod.Row(host="d", note="", direct={"ok": False},
                         node={"ok": False}, verdict="节点也不通 · 换节点"),
            probemod.Row(host="e", note="", direct={"ok": False},
                         verdict="不通 · 待排查"),
        ]
        self.assertEqual(cli.probe_unusable(rows), ["c", "d", "e"])


# --------------------------------------------------------------- cmd_up 预检
class TestUpPreflight(TmpRuntimeMixin):
    def _up(self, cfg, port=None):
        args = argparse.Namespace(background=False, port=port, mode=None,
                                  node=None, system_proxy=False, no_auto=True)
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli, "read_pid", return_value=None), \
                mock.patch.object(cli, "_port_busy", return_value=False), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_up(args)
        return code, buf.getvalue()

    def test_same_port_is_rejected_before_bind(self):
        cfg = cli.merge_config({"listen_port": 7900, "http_port": 7900})
        code, out = self._up(cfg)
        self.assertEqual(code, 1)
        self.assertIn("SOCKS5 与 HTTP 不能同端口", out)
        self.assertIn("--port 17899", out)
        self.assertIn("http_port", out)

    def test_bad_cli_port_rejected(self):
        cfg = cli.merge_config({})
        self.assertEqual(self._up(cfg, port=70000)[0], 2)

    def test_sni_port_conflict_with_own_port_fails(self):
        cfg = cli.merge_config({"cdn": {"enabled": True, "sni_port": 7899}})
        code, out = self._up(cfg)
        self.assertEqual(code, 1)
        self.assertIn("cdn.sni_port", out)

    def test_sni_port_busy_only_warns(self):
        cfg = cli.merge_config({"cdn": {"enabled": True, "sni_port": 443}})
        calls = []

        def busy(host, port):
            calls.append(port)
            return port == 443          # 只有 SNI 端口被占，代理端口空闲

        def fake_run(coro, *a, **kw):
            # 真 asyncio.run 会驱动协程；这里只要它别把未 await 的协程漏给 GC
            if hasattr(coro, "close"):
                coro.close()
            return 0

        args = argparse.Namespace(background=False, port=None, mode=None,
                                  node=None, system_proxy=False, no_auto=True)
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli, "read_pid", return_value=None), \
                mock.patch.object(cli, "_port_busy", side_effect=busy), \
                mock.patch.object(cli, "_port_owner", return_value="pid=1 x.exe"), \
                mock.patch.object(cli.asyncio, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_up(args)
        self.assertEqual(code, 0)                       # 不中止
        self.assertIn("SNI 转发端口 443 已被占用", buf.getvalue())
        self.assertIn(443, calls)


# --------------------------------------------------------------- hosts 模式
class TestHostsWrite(TmpRuntimeMixin):
    def _write(self, cfg):
        captured = {}

        def fake_apply(entries, runtime):
            captured.update(entries)
            captured["__runtime__"] = runtime
            return {"ok": True}

        buf = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli.sysx, "apply_hosts", side_effect=fake_apply), \
                mock.patch.object(cli.sysx, "flush_dns", return_value=None), \
                mock.patch.object(cli.cdnmod, "load_cache", return_value={}), \
                contextlib.redirect_stdout(buf):
            ok = cli._hosts_write()
        captured.pop("__runtime__", None)
        return ok, captured, buf.getvalue()

    def test_sni_mode_points_hosts_to_loopback(self):
        cfg = cli.merge_config({"cdn": {"enabled": True,
                                       "hosts": ["a.steamstatic.com",
                                                 "b.steamstatic.com"]}})
        ok, entries, out = self._write(cfg)
        self.assertTrue(ok)
        self.assertEqual(list(entries), ["127.0.0.1"])
        hosts = entries["127.0.0.1"].split()
        self.assertEqual(sorted(hosts), ["a.steamstatic.com", "b.steamstatic.com"])
        self.assertIn("hosts 已指向 127.0.0.1，需 svpn up 常驻（SNI 模式）", out)

    def test_sni_mode_does_not_need_cdn_cache(self):
        cfg = cli.merge_config({"cdn": {"enabled": True}})
        ok, entries, _ = self._write(cfg)
        self.assertTrue(ok)
        self.assertEqual(list(entries), ["127.0.0.1"])
        # 白名单为空时退回 CDN_DOMAINS，不能一条都不写
        self.assertEqual(len(entries["127.0.0.1"].split()), len(cdnmod.CDN_DOMAINS))

    def test_ip_mode_still_writes_best_ip(self):
        cfg = cli.merge_config({"cdn": {"enabled": False,
                                       "hosts": ["a.steamstatic.com"]}})
        cache = {"a.steamstatic.com": {"best_ip": "1.2.3.4", "ts": 0},
                 "other.example.com": {"best_ip": "5.6.7.8", "ts": 0}}
        captured = {}

        def fake_apply(entries, runtime):
            captured.update(entries)
            return {"ok": True}

        buf = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli.sysx, "apply_hosts", side_effect=fake_apply), \
                mock.patch.object(cli.sysx, "flush_dns", return_value=None), \
                mock.patch.object(cli.cdnmod, "load_cache", return_value=cache), \
                contextlib.redirect_stdout(buf):
            ok = cli._hosts_write()
        self.assertTrue(ok)
        self.assertEqual(list(captured), ["1.2.3.4"])
        self.assertIn("a.steamstatic.com", captured["1.2.3.4"])
        self.assertIn("跳过 1 个不在 cdn.hosts 白名单", buf.getvalue())

    def test_sni_entries_pass_sanitizer(self):
        """SNI 模式的条目必须过 sysx.sanitize_entries 的同等约束（走 apply_hosts，不自己写文件）。"""
        cfg = cli.merge_config({"cdn": {"enabled": True,
                                       "hosts": ["a.steamstatic.com",
                                                 "#evil.example.com",
                                                 "b.steamstatic.com"]}})
        ok, entries, _ = self._write(cfg)
        self.assertTrue(ok)
        clean, rejected = cli.sysx.sanitize_entries(entries)
        self.assertEqual(list(clean), ["127.0.0.1"])
        self.assertEqual(sorted(clean["127.0.0.1"].split()),
                         ["a.steamstatic.com", "b.steamstatic.com"])
        self.assertIn("#evil.example.com", rejected)

    def test_sni_mode_uses_apply_hosts_not_raw_write(self):
        """只准通过 sysx.apply_hosts 落盘：一旦有人改成直接写 hosts 文件，这条会红。"""
        cfg = cli.merge_config({"cdn": {"enabled": True}})
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli.sysx, "apply_hosts",
                                  return_value={"ok": True, "entries": 8}) as apply, \
                mock.patch.object(cli.sysx, "flush_dns", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli._hosts_write())
        apply.assert_called_once()
        entries, store = apply.call_args.args
        self.assertEqual(store, cli.RUNTIME)
        self.assertEqual(list(entries), ["127.0.0.1"])

    def test_rejected_entries_are_reported(self):
        cfg = cli.merge_config({"cdn": {"enabled": True}})
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli.sysx, "apply_hosts",
                                  return_value={"ok": True, "entries": 7,
                                                "rejected": ["#junk"]}), \
                mock.patch.object(cli.sysx, "flush_dns", return_value=None), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(cli._hosts_write())
        self.assertIn("未通过 hosts 校验", buf.getvalue())

    def test_sni_mode_apply_failure_reports(self):
        cfg = cli.merge_config({"cdn": {"enabled": True}})
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli.sysx, "apply_hosts",
                                  return_value={"ok": False, "reason": "拒绝"}), \
                mock.patch.object(cli.sysx, "flush_dns", return_value=None), \
                contextlib.redirect_stdout(buf):
            self.assertFalse(cli._hosts_write())
        self.assertIn("hosts 写入失败", buf.getvalue())


# --------------------------------------------------------------- SNI 接线
class TestSniWiring(unittest.TestCase):
    """把 SPEC §4.2 的调用骨架钉死：P3 改 forward.py 时签名不许漂。"""

    class FakeSrv:
        upstream = object()
        stats = object()

    def _cfg(self, **cdn):
        base = {"enabled": True}
        base.update(cdn)
        return cli.merge_config({"cdn": base})

    def test_disabled_returns_none(self):
        got = asyncio.run(cli._start_sni(cli.merge_config({}), self.FakeSrv()))
        self.assertIsNone(got)

    def test_oserror_only_warns(self):
        seen = {}

        class Boom:
            listen_port = 443

            def __init__(self, up, listen_port=443, **kw):
                seen.update(kw, listen_port=listen_port, up=up)

            async def start(self):
                raise OSError("[WinError 10048] 端口被占用")

            async def close(self):
                raise AssertionError("start 失败时不该 close")

        buf = io.StringIO()
        with mock.patch("svpn.forward.SniForwarder", Boom), \
                mock.patch.object(cli.cdnmod, "best_ip_map", return_value={}), \
                contextlib.redirect_stdout(buf):
            got = asyncio.run(cli._start_sni(self._cfg(sni_port=443),
                                            self.FakeSrv()))
        self.assertIsNone(got)                      # 不拖垮主服务
        self.assertIn("SNI 转发端口被占用", buf.getvalue())
        self.assertEqual(seen["listen_port"], 443)
        self.assertIn("allow_suffixes", seen)
        self.assertIn("ip_map", seen)

    def test_start_ok_passes_whitelist(self):
        seen = {}

        class Ok:
            listen_port = 4431

            def __init__(self, up, listen_port=443, allow_suffixes=None,
                         target_port=443, stats=None, max_head=16384,
                         ip_map=None):
                seen.update(locals())

            async def start(self):
                return None

            async def close(self):
                return None

        buf = io.StringIO()
        with mock.patch("svpn.forward.SniForwarder", Ok), \
                mock.patch.object(cli.cdnmod, "best_ip_map", return_value={}), \
                contextlib.redirect_stdout(buf):
            got = asyncio.run(cli._start_sni(
                self._cfg(sni_port=4431, hosts=["a.steamstatic.com"]),
                self.FakeSrv()))
        self.assertIsNotNone(got)
        self.assertEqual(seen["listen_port"], 4431)
        self.assertEqual(seen["allow_suffixes"], ["a.steamstatic.com"])
        self.assertEqual(seen["target_port"], 443)
        self.assertIn("SNI 透明转发 127.0.0.1:4431", buf.getvalue())

    def test_allow_falls_back_to_cdn_domains(self):
        cfg = self._cfg(hosts=[])
        self.assertEqual(cli._sni_allow(cfg["cdn"]), list(cdnmod.CDN_DOMAINS))


# --------------------------------------------------------------- 路由摘要
class TestRouteSummary(unittest.TestCase):
    def _summary(self, cfg):
        buf = io.StringIO()
        with mock.patch.object(cli.cdnmod, "best_ip_map", return_value={}), \
                contextlib.redirect_stdout(buf):
            cli._print_route_summary(cfg)
        return buf.getvalue()

    def test_sni_mode_line(self):
        out = self._summary(cli.merge_config({"cdn": {"enabled": True,
                                                      "sni_port": 443}}))
        self.assertIn("SNI 透明转发:443", out)

    def test_ip_mode_line(self):
        out = self._summary(cli.merge_config({}))
        self.assertIn("hosts 直指最优 IP", out)
        self.assertNotIn("SNI 透明转发", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
