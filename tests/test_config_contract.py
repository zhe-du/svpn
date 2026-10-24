"""config.example.json / config.json 与 svpn.cli.DEFAULT_CONFIG 的契约测试。

目的：把「配置漂移」这件事钉死——以后谁改歪了 example 的键集、类型、CDN 域名列表，
或往 config.json 里塞了 `_` 开头的脏键 / 未知键，这个文件立刻红。

约定：
    * 标准库 only，不联网，整体 < 3 秒；
    * `config.example.json` 是唯一 canonical 模板，`config.json` 是用户当前的实配置；
    * `DEFAULT_CONFIG` 是代码侧真源，example 的键集（剔除 `_` 开头）+ 类型必须与它完全一致。

P2-cli 并行改 `svpn/cli.py`：`merge_config` 与 `proxy_hosts` / `direct_hosts` 两键由他补。
在他落地前，依赖这两者的用例会 `skipTest` 并打印原因——**不放宽断言**，只等对方到货后复跑。

运行：
    python tests/test_config_contract.py
    python tests/test_config_contract.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn import cdn as cdnmod  # noqa: E402
from svpn import cli  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.json"
USERC = ROOT / "config.json"

# 用模块级变量拿函数：塞进 TestCase 类属性会被描述符协议绑成 self 参数
_MERGE = getattr(cli, "merge_config", None)

# SPEC §4.1 的顶层键（顺序即 canonical 顺序）；proxy_hosts / direct_hosts 由 P2 补进默认值
SPEC_TOP_KEYS = [
    "listen_host", "listen_port", "http_port", "mode", "connect_timeout", "auto_node",
    "upstream", "auth", "browser", "cdn", "proxy_hosts", "direct_hosts",
]


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _real_keys(data: dict) -> set:
    """剔除 `_` 开头的说明键，剩下的才是运行配置。"""
    return {k for k in data if not k.startswith("_")}


def _type_of(value) -> str:
    """归一成可比较的类型名。

    bool 必须单独判：`isinstance(True, int)` 为 True，否则 True/12 会被当成同类型；
    int 与 float 允许互换（都记作 number），配置里 12 和 12.0 语义等价。
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _type_map(data: dict, prefix: str = "") -> dict:
    """递归展开成 {"cdn.hosts": "list", ...}，`_` 键整枝跳过。"""
    out: dict[str, str] = {}
    for key, value in data.items():
        if str(key).startswith("_"):
            continue
        path = f"{prefix}{key}"
        out[path] = _type_of(value)
        if isinstance(value, dict):
            out.update(_type_map(value, path + "."))
    return out


class ConfigContractTest(unittest.TestCase):
    """canonical 模板 / 用户配置 / 代码默认值 三方一致性。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.example = _read_json(EXAMPLE)
        cls.user = _read_json(USERC)
        cls.default = cli.DEFAULT_CONFIG

    # ---------------------------------------------------------------- 工具
    def _need_merge(self, data: dict) -> dict:
        """P2 未落地 merge_config 时跳过，不伪造合并逻辑。"""
        if _MERGE is None:
            self.skipTest("merge_config 尚未由 P2-cli 提供（svpn/cli.py），等落地后复跑")
        return _MERGE(data)

    def _need_full_default(self) -> None:
        """P2 未把 proxy_hosts/direct_hosts 补进 DEFAULT_CONFIG 时跳过。"""
        missing = [k for k in SPEC_TOP_KEYS if k not in self.default]
        if missing:
            self.skipTest(f"DEFAULT_CONFIG 尚缺 {missing}（等 P2-cli 落地 SPEC §4.1 的键集）")

    # ------------------------------------------------- 1. example 键集 / 类型
    def test_example_key_set_equals_default(self) -> None:
        """example 的运行键集必须与 DEFAULT_CONFIG 完全相等（多一个/少一个都算漂移）。"""
        self._need_full_default()
        example_keys = _real_keys(self.example)
        default_keys = set(self.default)
        self.assertEqual(
            example_keys - default_keys, set(),
            f"example 多出 DEFAULT_CONFIG 没有的键: {sorted(example_keys - default_keys)}")
        self.assertEqual(
            default_keys - example_keys, set(),
            f"example 缺少 DEFAULT_CONFIG 有的键: {sorted(default_keys - example_keys)}")
        self.assertEqual(example_keys, default_keys)

    def test_example_top_level_order(self) -> None:
        """键序按 SPEC §4.1，读文件和 diff 时才稳定。"""
        order = [k for k in self.example if not k.startswith("_")]
        self.assertEqual(order, SPEC_TOP_KEYS)

    def test_example_has_note_last(self) -> None:
        """说明键保留一个 `_note`，且放在最后（不影响解析，只方便人看）。"""
        self.assertIn("_note", self.example)
        self.assertEqual(list(self.example)[-1], "_note")
        self.assertIsInstance(self.example["_note"], str)
        self.assertTrue(self.example["_note"].strip())

    def test_example_types_match_default(self) -> None:
        """递归比对类型：bool 与 int 必须区分，int/float 可互换。"""
        self._need_full_default()
        example_types = _type_map(self.example)
        default_types = _type_map(self.default)
        for key in sorted(set(example_types) | set(default_types)):
            with self.subTest(key=key):
                self.assertIn(key, default_types, f"example 独有键 {key}")
                self.assertIn(key, example_types, f"example 缺键 {key}")
                self.assertEqual(example_types[key], default_types[key],
                                 f"{key} 类型不一致: example={example_types[key]} "
                                 f"default={default_types[key]}")

    def test_merge_example_equals_default(self) -> None:
        """example 整份喂进 merge_config，结果必须等于 DEFAULT_CONFIG 且不含 _note。

        唯一的豁免是 `browser.bypass`：SPEC §4.1 给 DEFAULT_CONFIG 的是最小串
        （`172.16.*`），而 canonical 模板要求完整内网段（`172.16.*` … `172.31.*`）。
        所以这里先校验 bypass 是默认值的**超集**，再把这一项对齐后做整份深比较——
        其余键一个都不许差。
        """
        merged = self._need_merge(self.example)
        self.assertNotIn("_note", merged, "merge_config 没丢弃 _ 开头的说明键")
        self.assertEqual(set(merged), set(self.default))

        bypass = merged["browser"]["bypass"]
        default_bypass = self.default["browser"]["bypass"]
        example_tokens = [t for t in bypass.split(";") if t]
        default_tokens = [t for t in default_bypass.split(";") if t]
        lost = [t for t in default_tokens if t not in example_tokens]
        self.assertEqual(lost, [], f"example 的 bypass 丢了默认值里的绕行段: {lost}")

        expected = json.loads(json.dumps(self.default))
        expected["browser"]["bypass"] = bypass
        self.assertEqual(merged, expected, "example 归一后与 DEFAULT_CONFIG 深比较不相等")

    # --------------------------------------------------- 2. CDN 域名列表
    def test_default_config_key_count(self) -> None:
        """口径订正：顶层恰好 12 键（「13 键」是把 `_note` 也数进去的笔误）。"""
        self._need_full_default()
        self.assertEqual(len(self.default), len(SPEC_TOP_KEYS),
                         f"DEFAULT_CONFIG 顶层 {len(self.default)} 键，应为 12: "
                         f"{sorted(self.default)}")

    def test_default_cdn_hosts_match_cdn_domains(self) -> None:
        """DEFAULT_CONFIG.cdn.hosts 必须与 cdn.CDN_DOMAINS 内容与顺序都一致。"""
        self._need_full_default()
        self.assertEqual(self.default["cdn"]["hosts"], list(cdnmod.CDN_DOMAINS))

    def test_example_cdn_hosts_match_cdn_domains(self) -> None:
        """example 的 cdn.hosts 必须与 cdn.CDN_DOMAINS 完全一致，顺序也要一致。"""
        hosts = self.example["cdn"]["hosts"]
        self.assertEqual(hosts, list(cdnmod.CDN_DOMAINS))
        self.assertEqual(len(hosts), len(set(hosts)), "cdn.hosts 有重复域名")

    # ------------------------------------------------------ 3. config.json
    def test_user_config_has_no_private_keys(self) -> None:
        """用户的实配置里不该出现 `_note` 这类会被并入 cfg 的脏键。"""
        private = [k for k in self.user if k.startswith("_")]
        self.assertEqual(private, [], f"config.json 含 _ 开头键: {private}")

    def test_user_config_has_no_unknown_keys(self) -> None:
        """config.json 只准用 SPEC §4.1 的键，拼错的键不该被静默接受。"""
        unknown = _real_keys(self.user) - set(SPEC_TOP_KEYS)
        self.assertEqual(unknown, set(), f"config.json 有未知键: {sorted(unknown)}")

    def test_user_config_cdn_hosts_cover_all_domains(self) -> None:
        """config.json 的 cdn.hosts 必须覆盖全部 CDN 域名（不能在 refresh 时漏测）。"""
        hosts = self.user["cdn"]["hosts"]
        self.assertEqual(sorted(hosts), sorted(cdnmod.CDN_DOMAINS),
                         "config.json 的 cdn.hosts 与 CDN_DOMAINS 不一致")
        self.assertEqual(hosts, list(cdnmod.CDN_DOMAINS), "cdn.hosts 顺序应与 CDN_DOMAINS 一致")

    def test_user_config_shape_matches_default(self) -> None:
        """config.json 归一后：键集与类型都与默认值同形（数值可不同，形状不许漂）。"""
        merged = self._need_merge(self.user)
        self.assertEqual(set(merged), set(self.default),
                         "config.json 归一后的键集与 DEFAULT_CONFIG 不一致")
        merged_types = _type_map(merged)
        default_types = _type_map(self.default)
        for key in sorted(set(merged_types) | set(default_types)):
            with self.subTest(key=key):
                self.assertEqual(merged_types.get(key), default_types.get(key),
                                 f"{key} 类型漂了: config={merged_types.get(key)} "
                                 f"default={default_types.get(key)}")

    def test_user_config_keeps_placeholders(self) -> None:
        """本机没有节点，upstream 必须留占位，不能瞎填 127.0.0.1 假装能用。"""
        up = self.user["upstream"]
        self.assertEqual(up["host"], "NODE_HOST")
        self.assertEqual(up["port"], 1080)
        self.assertEqual(up["kind"], "socks5")
        self.assertIn(up["host"], cli.PLACEHOLDERS)

    def test_user_config_numeric_ports_sane(self) -> None:
        """端口必须是 1-65535 的整数，且 socks 与 http 不能撞车。"""
        for key in ("listen_port", "http_port"):
            with self.subTest(key=key):
                value = self.user[key]
                self.assertIsInstance(value, int)
                self.assertTrue(1 <= value <= 65535, f"{key}={value} 越界")
        self.assertNotEqual(self.user["listen_port"], self.user["http_port"])

    # ------------------------------------------------------ 4. bypass 覆盖
    def test_bypass_covers_private_ranges(self) -> None:
        """两份配置的 browser.bypass 都要吃掉 172.16-172.31 全段、192.168.*、*.cn。"""
        for name, data in (("config.example.json", self.example), ("config.json", self.user)):
            tokens = [t.strip().lower() for t in data["browser"]["bypass"].split(";")]
            with self.subTest(file=name):
                for octet in range(16, 32):
                    self.assertIn(f"172.{octet}.*", tokens,
                                  f"{name} 的 bypass 缺 172.{octet}.*")
                self.assertIn("192.168.*", tokens, f"{name} 的 bypass 缺 192.168.*")
                self.assertIn("*.cn", tokens, f"{name} 的 bypass 缺 *.cn")
                self.assertTrue(any(t.startswith("<local>") for t in tokens),
                                f"{name} 的 bypass 缺 <local>")
                self.assertIn("127.*", tokens, f"{name} 的 bypass 缺 127.*")
                self.assertIn("localhost", tokens, f"{name} 的 bypass 缺 localhost")


if __name__ == "__main__":
    unittest.main(verbosity=2)
