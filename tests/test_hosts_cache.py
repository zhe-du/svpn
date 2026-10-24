"""hosts 写入路径必须校验 CDN 缓存是否过期。

历史 bug：`_hosts_write()` 用 `load_cache(max_age=10**9)`，等于「永不过期」，
于是一份 16 小时前的 Akamai 边缘 IP 会被原样写进 hosts；`cmd_hosts` 又只在
「缓存为空」时才自动刷新 —— 过期缓存被当成有效数据，下载直连一个已经下线的节点。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from svpn import cli  # noqa: E402

DOMAIN = "steamcdn-a.akamaihd.net"


class HostsCacheTtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.applied: list[dict] = []

    def _apply(self, entries, runtime):
        self.applied.append(dict(entries))
        return {"ok": True, "entries": len(entries), "rejected": []}

    def _cache(self, age_hours: float, ip: str = "1.2.3.4") -> Path:
        path = self.tmp / f"cache-{age_hours}.json"
        path.write_text(json.dumps({
            "updated": time.time() - age_hours * 3600,
            "domains": {DOMAIN: {"best_ip": ip, "ts": time.time()}},
        }), encoding="utf-8")
        return path

    def _run(self, cache_path: Path, cfg: dict | None = None) -> bool:
        cfg = cfg or {"cdn": {"enabled": False, "hosts": [DOMAIN], "refresh_hours": 6}}
        with mock.patch.object(cli, "CDNCACHE", cache_path), \
             mock.patch.object(cli, "load_config", lambda: cfg), \
             mock.patch.object(cli.sysx, "apply_hosts", self._apply), \
             mock.patch.object(cli.sysx, "flush_dns", lambda: None):
            return cli._hosts_write()

    # ------------------------------------------------------------ 过期拦截
    def test_stale_cache_is_refused(self):
        """超过 TTL 的缓存不许写 hosts，且一个字都不许下发给 apply_hosts。"""
        self.assertFalse(self._run(self._cache(16.3)))
        self.assertEqual(self.applied, [])

    def test_boundary_just_past_ttl(self):
        """刚好越过 TTL 1 秒也算过期（边界不许漏）。"""
        self.assertFalse(self._run(self._cache(6 + 1 / 3600)))
        self.assertEqual(self.applied, [])

    def test_fresh_cache_is_written(self):
        """TTL 内的缓存正常写，IP 原样落到 entries。"""
        self.assertTrue(self._run(self._cache(0.2)))
        self.assertEqual(len(self.applied), 1)
        self.assertIn("1.2.3.4", self.applied[0])

    def test_missing_cache_is_refused(self):
        """没有缓存文件时给出「先 cdn --refresh」的明确指引。"""
        self.assertFalse(self._run(self.tmp / "nope.json"))
        self.assertEqual(self.applied, [])

    # ------------------------------------------------------- TTL 来自配置
    def test_ttl_follows_config(self):
        """`cdn.refresh_hours` 是 TTL 的唯一来源：调成 1 小时后，2 小时前的缓存即过期。"""
        cfg = {"cdn": {"enabled": False, "hosts": [DOMAIN], "refresh_hours": 1}}
        self.assertFalse(self._run(self._cache(2), cfg))
        self.assertEqual(self.applied, [])

    def test_ttl_defaults_when_config_is_junk(self):
        """refresh_hours 写坏（None/负数/字符串）时回落 6 小时，不抛异常。"""
        for junk in (None, -3, "abc", 0):
            with self.subTest(refresh_hours=junk):
                cfg = {"cdn": {"enabled": False, "hosts": [DOMAIN],
                               "refresh_hours": junk}}
                self.assertEqual(cli._cache_ttl_hours(cfg), 6.0)
                # 5 小时前 → 仍在 6 小时 TTL 内，应当写入
                self.assertTrue(self._run(self._cache(5), cfg))

    # ------------------------------------------------------------- SNI 模式
    def test_sni_mode_ignores_cache(self):
        """SNI 模式只写 127.0.0.1，与最优 IP 缓存无关（缓存过期也照样写）。"""
        cfg = {"cdn": {"enabled": True, "hosts": [DOMAIN], "refresh_hours": 6}}
        self.assertTrue(self._run(self._cache(100, ip="9.9.9.9"), cfg))
        self.assertEqual(self.applied, [{"127.0.0.1": DOMAIN}])

    # -------------------------------------------------------- 缓存年龄读取
    def test_cache_age_reads_updated(self):
        """`_cache_age_hours()` 直接读文件里的 updated，与 load_cache 的过滤无关。"""
        with mock.patch.object(cli, "CDNCACHE", self._cache(3)):
            self.assertAlmostEqual(cli._cache_age_hours(), 3.0, delta=0.05)
        with mock.patch.object(cli, "CDNCACHE", self.tmp / "nope.json"):
            self.assertIsNone(cli._cache_age_hours())


if __name__ == "__main__":
    unittest.main(verbosity=2)
