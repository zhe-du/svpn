"""PAC 生成回归测试（对应 `svpn/pac.py`）。

分两部分：

1. **纯文本契约**（不需要 node、不联网）：`PROXY` / `DIRECT` 必须是**数组字面量**
   `[ ... ]`。历史事故：某次改动把 `PROXY` 写成了对象字面量 `var PROXY = { ... }`，
   那在 JS 里是非法语法，浏览器会整份 PAC 加载失败 —— 表现是「开了 PAC 之后
   浏览器完全没网」。所以这里用断言把 `[` 钉死，任何人改回 `{` 都会立刻红。
2. **真 JS 语义**（需要 node）：把生成的 PAC 丢进 node 里跑 `FindProxyForURL`，
   验证「哪些域名走上游、哪些走直连」这件事本身对不对。

node 的定位顺序：`shutil.which("node")` → `C:\\Program Files\\nodejs\\node.exe`
→ `C:\\Program Files (x86)\\nodejs\\node.exe`。三者都没有就 `skipTest`，
**不计失败**（第 2 部分并非任何环境都能跑；第 1 部分必须永远全绿）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svpn.pac import build_pac
from svpn.rules import CN_DIRECT_SUFFIXES, STEAM_SUFFIXES

# ------------------------------------------------------------------ node 探测
_NODE_HINTS = (
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
)


def find_node() -> str | None:
    """找 node 可执行文件；Windows 上 PATH 里常常没有它，所以要兜默认安装目录。"""
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    for hint in _NODE_HINTS:
        if Path(hint).is_file():
            return hint
    return None


NODE = find_node()


# ------------------------------------------------------------------ 文本抽取
def _array_body(pac: str, name: str) -> str:
    """取 `var <name> = [ ... ];` 方括号**内部**的原文（用于反序列化验证）。"""
    m = re.search(r"var %s = \[(.*?)\];" % name, pac, re.S)
    if m is None:
        raise AssertionError(f"PAC 里找不到 `var {name} = [ ... ];` 数组（是不是又被改成对象了？）")
    return m.group(1)


def _array(pac: str, name: str) -> list:
    """把数组片段当 JSON 反序列化：顺带验证引号/逗号是成对的合法序列。"""
    return json.loads("[" + _array_body(pac, name) + "]")


def _tail_rule(pac: str) -> list[str]:
    """取出 `FindProxyForURL` 里「模式默认分支」那几行。

    函数体里的固定判断（host 归一、localhost/回环、内网、PROXY 命中、
    DIRECT 命中的第一条）都剥掉，剩下的就是 mode 决定的兜底逻辑。
    """
    body = pac.split("function FindProxyForURL", 1)[1]
    out: list[str] = []
    seen_direct_guard = False
    for line in body.splitlines():
        s = line.strip()
        if not s or s == "}" or s.startswith("("):     # 函数头尾巴 / 函数结束
            continue
        if s.startswith("host =") or 'host === "localhost"' in s:
            continue
        if "isPlainHostName" in s:                     # local_net 分支
            continue
        if "_hit(host, PROXY)" in s:
            continue
        if "_hit(host, DIRECT)" in s and not seen_direct_guard:
            seen_direct_guard = True                   # 三种模式共有的固定判断
            continue
        out.append(s)
    return out


# ------------------------------------------------------------------ 文本契约
class PacTextTests(unittest.TestCase):
    """纯文本契约：不依赖 node，任何环境都必须全绿。"""

    def test_proxy_is_array_literal_not_object(self):
        """`var PROXY` 后面必须是 `[`；全文不得出现 `var PROXY = {`（历史事故）。"""
        for mode in ("whitelist", "all", "blacklist"):
            pac = build_pac(mode=mode)
            with self.subTest(mode=mode):
                self.assertIn("var PROXY = [", pac)
                self.assertIn("var DIRECT = [", pac)
                self.assertNotIn("var PROXY = {", pac)
                self.assertNotIn("var DIRECT = {", pac)
                # 顺手把「少个空格」的变体也堵掉
                self.assertIsNone(re.search(r"var\s+PROXY\s*=\s*\{", pac))
                self.assertIsNone(re.search(r"var\s+DIRECT\s*=\s*\{", pac))

    def test_proxy_covers_all_steam_suffixes(self):
        """PROXY 去重后必须正好等于 STEAM_SUFFIXES 全集（一个都不能漏）。"""
        prox = _array(build_pac(), "PROXY")
        self.assertEqual(prox, sorted(set(STEAM_SUFFIXES)))
        self.assertEqual(len(prox), len(set(STEAM_SUFFIXES)))
        self.assertEqual(len(prox), len(set(prox)), "PROXY 里有重复项")

    def test_direct_empty_by_default(self):
        """whitelist/all 下没给 direct_suffixes → DIRECT 空数组。

        blacklist 例外：它的语义就是「国内直连、其余走代理」，DIRECT 空了
        等于把所有流量送上游，和 Router 的 `direct_suffixes or CN_DIRECT_SUFFIXES`
        不一致 —— 必须回落到内置国内清单。
        """
        for mode in ("whitelist", "all"):
            with self.subTest(mode=mode):
                self.assertEqual(_array(build_pac(mode=mode), "DIRECT"), [])
        with self.subTest(mode="blacklist"):
            self.assertEqual(_array(build_pac(mode="blacklist"), "DIRECT"),
                             sorted(set(CN_DIRECT_SUFFIXES)))

    def test_mode_branches(self):
        """三种 mode 的兜底分支文本必须各自正确。"""
        expect = {
            "whitelist": ['return "DIRECT";'],
            "all": ['return SOCK + "; DIRECT";'],
            "blacklist": ['if (_hit(host, DIRECT)) return "DIRECT";',
                          'return SOCK + "; DIRECT";'],
        }
        for mode, want in expect.items():
            pac = build_pac(mode=mode, direct_suffixes=CN_DIRECT_SUFFIXES)
            with self.subTest(mode=mode):
                self.assertEqual(_tail_rule(pac), want)
        # blacklist 的两条特征单独再点一次，免得 _tail_rule 改坏后失去区分度
        bl = build_pac(mode="blacklist", direct_suffixes=CN_DIRECT_SUFFIXES)
        self.assertIn("_hit(host, DIRECT)", bl)
        self.assertIn("SOCK", bl)

    def test_custom_suffixes_dedup_sorted_and_written(self):
        """自定义名单要去重、排序，并且按顺序写进数组。"""
        pac = build_pac(proxy_suffixes=["b.example", "a.example", "b.example"],
                        direct_suffixes=["z.example", "m.example", "z.example"])
        self.assertEqual(_array(pac, "PROXY"), ["a.example", "b.example"])
        self.assertEqual(_array(pac, "DIRECT"), ["m.example", "z.example"])
        # 文件里的实际出现顺序也要是升序
        for name, want in (("PROXY", ["a.example", "b.example"]),
                           ("DIRECT", ["m.example", "z.example"])):
            body = _array_body(pac, name)
            positions = [body.index('"%s"' % s) for s in want]
            with self.subTest(name=name):
                self.assertEqual(positions, sorted(positions))

    def test_loopback_always_direct(self):
        """localhost / 127.0.0.1 / ::1 三件套恒直连；local_net 开关只影响 isPlainHostName。"""
        pac = build_pac()
        self.assertIn('host === "localhost" || host === "127.0.0.1" || host === "::1"', pac)
        self.assertIn("isPlainHostName", pac)                    # local_net=True 是默认
        self.assertNotIn("isPlainHostName", build_pac(local_net=False))
        self.assertNotIn("shExpMatch", build_pac(local_net=False))

    def test_js_structure_balanced(self):
        """括号/引号成对，数组片段能被 json 反序列化（即 JS 里也是合法字面量）。"""
        for mode in ("whitelist", "all", "blacklist"):
            pac = build_pac(mode=mode, direct_suffixes=("b.cn", "a.com"))
            with self.subTest(mode=mode):
                self.assertEqual(pac.count('"') % 2, 0, "引号不成对")
                self.assertEqual(pac.count("{"), pac.count("}"), "花括号不成对")
                self.assertEqual(pac.count("("), pac.count(")"), "圆括号不成对")
                self.assertEqual(pac.count("["), pac.count("]"), "方括号不成对")
                json.loads("[" + _array_body(pac, "PROXY") + "]")
                json.loads("[" + _array_body(pac, "DIRECT") + "]")

    def test_socks_endpoint_reflects_args(self):
        """监听地址/端口要写进 SOCK 变量。"""
        pac = build_pac(socks_host="10.1.2.3", socks_port=18080)
        self.assertIn('var SOCK = "SOCKS5 10.1.2.3:18080";', pac)


# ------------------------------------------------------------------ JS 语义
_JS_DRIVER = r"""
const fs = require('fs');
// node 里没有 PAC 引擎，按微软文档语义补两个内置函数出来
globalThis.isPlainHostName = (h) => String(h).indexOf('.') === -1;
globalThis.dnsDomainIs = (h, d) => String(h).slice(-String(d).length) === String(d);
globalThis.shExpMatch = (h, p) => {
  const rx = String(p).replace(/[.+^${}()|[\]\\]/g, '\\$&')
                      .replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp('^' + rx + '$').test(String(h));
};
const jobs = JSON.parse(fs.readFileSync(process.env.SVPN_JOBS, 'utf8'));
const out = [];
for (const job of jobs) {
  const src = fs.readFileSync(job.pac, 'utf8');
  // new Function 会先做一次语法解析：PAC 有语法错误时这里直接抛，正好当断言用
  const find = new Function(src + '\nreturn FindProxyForURL;')();
  out.push(job.hosts.map(h => find('https://' + h + '/', h)));
}
process.stdout.write(JSON.stringify(out));
"""

# 白名单用例：11 个 host，覆盖 13 条语义用例里的 10 条
_WHITELIST_HOSTS = [
    "store.steampowered.com",        # 商店
    "steamcommunity.com",            # 社区
    "STORE.STEAMPOWERED.COM.",       # 大写 + 尾点
    "steamcommunity.com.",           # 尾点
    "example.com",
    "notsteampowered.com",           # 前缀伪装
    "evil-steamcommunity.com",       # 连字符伪装
    "localhost",
    "127.0.0.1",
    "192.168.1.1",                   # 内网
    "xn--fiqs8s.cn",                 # IDN（中国.cn），只要求不崩
]
_WHITELIST_UPSTREAM = {"store.steampowered.com", "steamcommunity.com",
                       "STORE.STEAMPOWERED.COM.", "steamcommunity.com."}
_BLACKLIST_HOSTS = ["bilibili.com", "example.com", "store.steampowered.com"]
_ALL_HOSTS = ["example.com", "qq.com"]
_LOCALNET_HOSTS = ["intranet", "example.com"]


class PacSemanticsTests(unittest.TestCase):
    """真 JS 语义：把 PAC 交给 node 跑。没有 node 就整体跳过。

    四份 PAC 一次性交给同一个 node 进程（node 冷启动约 1 秒，跑四次太亏）。
    """

    results: dict[str, list[str]] = {}

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest(
                "未找到 node（PATH 与默认安装目录都没有），跳过 PAC 语义测试")
        specs = [
            ("whitelist", build_pac(), _WHITELIST_HOSTS),
            ("local_net_off", build_pac(local_net=False), _LOCALNET_HOSTS),
            ("blacklist", build_pac(mode="blacklist",
                                    direct_suffixes=CN_DIRECT_SUFFIXES),
             _BLACKLIST_HOSTS),
            ("all", build_pac(mode="all"), _ALL_HOSTS),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            jobs = []
            for name, pac_text, hosts in specs:
                pac_path = Path(tmp) / f"{name}.pac"
                pac_path.write_text(pac_text, encoding="utf-8")
                jobs.append({"name": name, "pac": str(pac_path), "hosts": hosts})
            jobs_path = Path(tmp) / "jobs.json"
            jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
            env["SVPN_JOBS"] = str(jobs_path)
            proc = subprocess.run([NODE, "-e", _JS_DRIVER], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=60, env=env)
        if proc.returncode != 0:
            raise AssertionError(
                f"node 拒绝执行生成的 PAC（语法/运行时错误）：\n{proc.stderr}")
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise AssertionError(
                f"node 输出不是 JSON：{proc.stdout!r} / stderr={proc.stderr!r}")
        if len(out) != len(specs):
            raise AssertionError(f"node 只返回了 {len(out)} 组结果，期望 {len(specs)} 组")
        cls.results = {spec[0]: got for spec, got in zip(specs, out)}

    def _assert_pairs(self, hosts: list[str], got: list[str]) -> None:
        self.assertEqual(len(got), len(hosts), "返回条数与输入条数不一致")
        for host, res in zip(hosts, got):
            self.assertIsInstance(res, str, f"{host} 的返回值不是字符串")
            self.assertTrue(res, f"{host} 返回了空串")

    def test_whitelist_semantics(self):
        """白名单模式：Steam 域走上游，其余直连；大小写/尾点/伪装域都要正确。"""
        got = self.results["whitelist"]
        self._assert_pairs(_WHITELIST_HOSTS, got)
        for host, res in zip(_WHITELIST_HOSTS, got):
            with self.subTest(host=host):
                if host in _WHITELIST_UPSTREAM:
                    self.assertIn("SOCKS5", res.upper(), f"{host} 应走上游，实际 {res!r}")
                else:
                    self.assertEqual(res, "DIRECT", f"{host} 应直连，实际 {res!r}")

    def test_blacklist_semantics(self):
        """黑名单模式：国内清单直连，其余走上游。"""
        got = self.results["blacklist"]
        self._assert_pairs(_BLACKLIST_HOSTS, got)
        self.assertEqual(got[0], "DIRECT", "bilibili.com 在黑名单里，应直连")
        self.assertIn("SOCKS5", got[1].upper(), "example.com 不在黑名单里，应走上游")
        self.assertIn("SOCKS5", got[2].upper(), "Steam 域始终走上游")

    def test_all_mode_semantics(self):
        """全局模式：一切走上游。"""
        got = self.results["all"]
        self._assert_pairs(_ALL_HOSTS, got)
        for host, res in zip(_ALL_HOSTS, got):
            with self.subTest(host=host):
                self.assertIn("SOCKS5", res.upper(), f"all 模式下 {host} 应走上游")

    def test_local_net_switch_runs_in_node(self):
        """local_net=False 生成的 PAC 也要能跑（这条分支以前没被执行过）。"""
        got = self.results["local_net_off"]
        self._assert_pairs(_LOCALNET_HOSTS, got)
        for host, res in zip(_LOCALNET_HOSTS, got):
            with self.subTest(host=host):
                self.assertEqual(res, "DIRECT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
