# svpn 只读验证报告：PAC / UTF-8 模式 / 命令面

- 时间：2026-10-24 03:24–03:33（Asia/Shanghai）
- 环境：Windows，Python 3.12.10，node v24.19.0
- 约束：全程只读，未起 `up` / `_serve`，未碰 7899/7900，未改 `svpn/` 任何代码
- 脚本与原始输出：`%TEMP%\svpn-verify\`（`pac.js`、`pac_test.js`、`help-*.txt` 等）

---

## 1. PAC 验证

### 1.1 生成

```
python -m svpn pac        →  EXIT=0, 1725 bytes (UTF-8), 39 条 Steam 后缀
```

生成结果片段（节选）：

```js
var SOCK = "SOCKS5 127.0.0.1:7899";
var PROXY = {
  "akamai.steamstatic.com",
  "api.steampowered.com",
  "artifact.com",
  ...
};
```

### 1.2 JS 引擎解析：**FAIL**

`node` 加载（`new Function(...)`，等价于 PAC 引擎编译）：

```
SyntaxError: Unexpected string
  at new Function (<anonymous>)
```

根因：`build_pac()` 生成的是**对象字面量** `var PROXY = { "a", "b" };`，
裸字符串既不是 key 也不是合法元素；而 `_hit()` 用的是数组语义
（`tbl.length` / `tbl[i]`）。`var DIRECT` 同样问题。

```js
function _hit(host, tbl) {
  for (var i = 0; i < tbl.length; i++) { ... }   // 期望数组
```

结论：**当前任何 PAC 引擎（Chrome/Edge/Steam 内置 Chromium/WinHTTP）都无法加载这份 PAC**。
`runtime/svpn.pac`（`up` 时落盘的那份）实测同样失败：

```
node -e "...new Function(...runtime/svpn.pac...)"  →  PARSE FAIL SyntaxError: Unexpected string
```

即 PAC 通道（`pac --apply` / 浏览器 PAC）目前是全局不可用，不是边界情况。

### 1.3 匹配逻辑（把 `{ }` 最小改成 `[ ]` 后验证）

修复方式仅替换两个容器括号，函数体与名单保持原样：

```
修复后解析: OK
```

| # | host | 期望 | 实际 | 结果 |
|---|---|---|---|---|
| 1 | store.steampowered.com | PROXY | `SOCKS5 127.0.0.1:7899; DIRECT` | PASS |
| 2 | steamcommunity.com | PROXY | `SOCKS5 127.0.0.1:7899; DIRECT` | PASS |
| 3 | steamcdn-a.akamaihd.net | PROXY | `SOCKS5 127.0.0.1:7899; DIRECT` | PASS |
| 4 | www.baidu.com | DIRECT | `DIRECT` | PASS |
| 5 | localhost | DIRECT | `DIRECT` | PASS |
| 6 | 127.0.0.1 | DIRECT | `DIRECT` | PASS |
| 7 | STORE.STEAMPOWERED.COM（大写） | PROXY | `SOCKS5 …; DIRECT` | PASS |
| 8 | SteamCommunity.COM（混合大小写） | PROXY | `SOCKS5 …; DIRECT` | PASS |
| 9 | store.steampowered.com**. **（末尾点） | PROXY | `DIRECT` | **FAIL** |
| 10 | api.steampowered.com | PROXY | `SOCKS5 …; DIRECT` | PASS |
| 11 | notstore.steampowered.com（合法子域） | PROXY | `SOCKS5 …; DIRECT` | PASS |
| 12 | evilsteampowered.com（lookalike） | DIRECT | `DIRECT` | PASS |
| 13 | steampowered.com.evil.net（后缀伪装） | DIRECT | `DIRECT` | PASS |
| 14 | steamcommunity.com.evil.io | DIRECT | `DIRECT` | PASS |
| 15 | foo.local | DIRECT | `DIRECT` | PASS |
| 16 | intranet（无点主机名） | DIRECT | `DIRECT` | PASS |

汇总：**PASS 15 / FAIL 1**（第 9 条）。

第 9 条根因：`FindProxyForURL` 只做了 `host.toLowerCase()`，没有去掉末尾点（FQDN 等价写法的 host 是 `store.steampowered.com.`）。`_hit()` 的 `"." + s` 后缀比较对末尾点天然失配 → 落到兜底 `DIRECT`。多数浏览器传入的是无点形式，所以这条是健壮性缺口，不是主路径故障。

其余两条已确认正常：
- 大小写不敏感（`toLowerCase()`）✓
- lookalike 不误判（`evilsteampowered.com`、`*.evil.net` 均 DIRECT）✓ —— 因为比较的是 `.` + suffix 的边界，不是 `indexOf`

### 1.4 修复建议（未改代码，仅建议）

```python
pjs = ",\n".join(f'  "{s}"' for s in prox)
...
var PROXY = [            # ← 由 { 改成 [
{pjs}
];                       # ← 由 }; 改成 ];
```

```js
host = host.toLowerCase().replace(/\.$/, "");   // ← 末尾点归一
```

---

## 2. `PYTHONUTF8=1` 下 doctor / status 复验：**PASS（已修复）**

```
$env:PYTHONUTF8="1"; python -m svpn doctor    →  EXIT=1 (无节点，预期), stderr=0B
$env:PYTHONUTF8="1"; python -m svpn status    →  EXIT=0, stderr=0B
```

上轮我在这里抓到的是崩溃：

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd0 in position 0
AttributeError: 'NoneType' object has no attribute 'lower'   # steam.py:56
```

现在 `steam.is_running()` 已改成 `return "steam.exe" in _tasklist().lower()`，
不再出现 `out=None`；doctor 两次（干净 env / PYTHONUTF8=1）输出一致、无 traceback：

```
Steam    d:\app\bapp\st\steam.exe
端口     7899 可用            # 该时刻 e2e-runner 实例未占端口
端口     7900 可用
系统代理 ProxyEnable=0
CDN 缓存 8 域名 / 17 分钟前
hosts    未写入

探测     store.steampowered.com    直连 OK 60ms
探测     steamcommunity.com        直连失败 sni_rst
         └ 社区是 SNI 阻断（换 IP 无效），必须走节点

[!] 待处理 1 项:
    - 没有节点，社区/创意工坊不可用
```

status 同时在跑着，输出正常（pid / 端口监听 / 模式 / CDN 缓存 / hosts / 上次统计）。

附带确认：上轮我报的「`stats.json` 只在 `finally` 落盘、`down` 用 taskkill 必然缺失」**已修复**，
`runtime/stats.json` 现在有真实数据：

```json
{ "uptime_s": 45.0, "connections": 37, "errors": 0, "via_upstream": 0,
  "via_direct": 37, "up_mb": 0.02, "down_mb": 0.24,
  "top_hosts": [["www.baidu.com", 22], ["store.steampowered.com", 14], ["steamcdn-a.akamaihd.net", 1]] }
```

（注：`status` 打印「上次统计 0 连接」是读文件快照，实例刚换代时会是上一轮的 0；以文件里的 `uptime_s` 判断新鲜度。）

---

## 3. 命令面审计：12 条 `--help` 全部正常

`python -m svpn <cmd> --help`：**12/12 EXIT=0，stderr 全 0B，无 argparse 崩**。

| 命令 | 实际开关 / 位置参数（来自 --help） |
|---|---|
| `init` | `--force` |
| `up` | `-b/--background` `-p/--port` `-m/--mode {whitelist,all,blacklist}` `--system-proxy` `--node URL` `--no-auto` |
| `down` | `--keep-system` `--clear-hosts` |
| `status` | 无 |
| `probe` | `--node auto\|none\|URL` `--timeout` `--json` |
| `node` | 位置参数 `{scan,set,clear} [url]`，选项 `--json` |
| `cdn` | `--refresh` `--no-bench` `--domain` `--timeout` `--json` |
| `hosts` | 位置参数 `{apply,clear}`，选项 `--no-elevate` `--timeout` |
| `system` | 位置参数 `{on,off,show}` |
| `pac` | `--write` `--apply` |
| `steam` | `--exe` `--restart` `--system` `--no-autostart` `--args ...` |
| `doctor` | 无 |

### 3.1 与 README 第 2 节（四条主线命令）对不上的点

| 项 | 说明 |
|---|---|
| `probe --timeout` | README §2.3 未提到该开关（实际存在） |
| `cdn --timeout` | README §2.4 未提到该开关（实际存在） |
| README §2.4 自相矛盾 | 第 144 行「缓存按 `cdn.refresh_hours`（默认 6h）过期…过期的会标 `*`」 vs 第 148 行「选边结果不会自动过期（`--refresh` 才会重测）」 |
| `up` 其余开关 | README §2.1 与 help 完全一致（含 `--no-auto`、`--node`、`--system-proxy`、`-p`、`-m`） |
| `down` / `status` / `doctor` | 一致 |

### 3.2 与 README 第 7 节（目录结构与命令表）对不上的点

| 项 | 说明 |
|---|---|
| `hosts --no-elevate` / `--timeout` | `--no-elevate` 只在 §5 故障表里出现一次（不在 §7 命令表）；`--timeout` 全文 0 次 |
| `node --json` | README 只写了 `probe --json` / `cdn --json`，`node --json` 未提 |
| §7 命令表缺 `hosts` | 表里只有 status/doctor/init/pac/system/steam；`hosts` 只在 §3/§5 零散出现 |
| 测试文件清单 | 文档列 7 个，实际 8 个：多出 `tests/test_regression.py`（未记录）；`live_e2e.py` 已在文档中 ✓ |
| 结构树缺文件 | 根目录 `_probe_proxy.py` 未出现在 §7 结构树 |

---

## 4. 一句话结论

1. **PAC 目前完全不可用**：`var PROXY = { "…" }` 是非法 JS，`pac --apply` / 浏览器 PAC 全线解析失败；把 `{}` 换成 `[]` 后，16 条断言 15 通过，唯一缺口是末尾点未归一。
2. **`PYTHONUTF8=1` 崩溃已修复**，doctor/status 在 UTF-8 模式下干净退出（doctor 无节点仍 EXIT=1，符合预期）；`stats.json` 也已能正常落盘。
3. **命令面健康**：12 条 `--help` 全部 EXIT=0；文档差异集中在 `--timeout`（probe/cdn/hosts 三处未记录）、`node --json` 未记录、`hosts` 不在 §7 命令表，以及 §2.4 关于缓存过期的两处自相矛盾。
