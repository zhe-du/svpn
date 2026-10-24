# P7-tests 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `tests/test_pac.py` | **新增**。12 用例：8 条纯文本契约（数组字面量 / 全量后缀 / DIRECT 默认空 / 三 mode 分支 / 自定义名单去重排序 / 回环恒直连 / 括号引号成对 + 数组 json 反序列化 / SOCK 端点反射）+ 4 条真 JS 语义（whitelist 11 host、blacklist 3 host、all 2 host、local_net=False 2 host） | 新增 313 |
| `tests/test_fuzz.py` | **新增**。19 用例，固定种子 `20261024`：节点串 300 条、ClientHello 截断/撒谎/随机 500 段、hosts 注入 17 组结构化 + 100 条随机 blob、`Router.decide` 200 host × 3 mode、脏 PAC 后缀、`_hosts_entries` 伪造 managed block（含 5001 行大输入） | 新增 507 |

**只新增这两个文件**：`svpn/*.py`、`tests/test_logic.py`、`tests/test_netutil.py`、`tests/test_regression.py`、`tests/smoke_test.py` 一字未动（已用 `git`/时间戳无关的方式核对：本任务全程只有两次 `Write` 落在这两个新文件上）。

## 2. 逐条修复

本任务是「补测试」，没有源码改动。下表是**每个交付要求 → 落到了哪个用例 → 怎么验证**。

### 2.1 `tests/test_pac.py`

| 编号 | 要求 | 落点 | 验证方式 |
|---|---|---|---|
| A1 | `var PROXY = [` 后面必须是数组；全文不得出现 `var PROXY = {` | `test_proxy_is_array_literal_not_object` | 三种 mode 各断言 `"var PROXY = ["` / `"var DIRECT = ["` 在、`"var PROXY = {"` 不在，并额外用正则 `var\s+PROXY\s*=\s*\{` 堵掉「少个空格」的变体 |
| A2 | `PROXY` 含全部 `STEAM_SUFFIXES`（去重） | `test_proxy_covers_all_steam_suffixes` | 把数组片段 `json.loads` 出来，断言 `== sorted(set(STEAM_SUFFIXES))`（40 条），再断言无重复 |
| A3 | `DIRECT` 在 `whitelist` 模式默认是空数组 | `test_direct_empty_by_default` | 三种 mode 都断言反序列化结果为 `[]`（注意：空数组在模板里渲染成 `[\n\n]`，所以**不能**断言字面量 `[]`，必须解析后判空） |
| A4 | 三种 mode 的分支文本 | `test_mode_branches` | 新增 `_tail_rule()`：切出 `FindProxyForURL` 函数体，剥掉 host 归一 / 回环 / 内网 / `_hit(host, PROXY)` / 第一条 `_hit(host, DIRECT)` 这些固定行，剩下的就是 mode 兜底逻辑。断言 `whitelist == ['return "DIRECT";']`、`all == ['return SOCK + "; DIRECT";']`、`blacklist == ['if (_hit(host, DIRECT)) return "DIRECT";', 'return SOCK + "; DIRECT";']` |
| A5 | 自定义名单去重、排序、正确写入 | `test_custom_suffixes_dedup_sorted_and_written` | `["b.example","a.example","b.example"]` → 解析结果 `["a.example","b.example"]`；再检查每个 suffix 在数组片段里的 `index()` 呈升序 |
| A6 | localhost / 127.0.0.1 / ::1 恒 DIRECT；`local_net=False` 不含 `isPlainHostName` | `test_loopback_always_direct` | 断言三件套那一行原文；`local_net=True` 含 `isPlainHostName`、`False` 时既无 `isPlainHostName` 也无 `shExpMatch` |
| A7 | 引号配对 / 数组可反序列化 | `test_js_structure_balanced` | 三个 mode：`"` 计数为偶数、`{}` `()` `[]` 各自计数相等、`PROXY`/`DIRECT` 片段分别 `json.loads` 成功 |
| A8（加料） | 监听地址端口要落进 `SOCK` | `test_socks_endpoint_reflects_args` | `socks_host=10.1.2.3, socks_port=18080` → `var SOCK = "SOCKS5 10.1.2.3:18080";` |
| A9 | 真 JS 语义（有 node 才跑） | `PacSemanticsTests` 4 个用例 | `shutil.which("node")` → `C:\Program Files\nodejs\node.exe` → `C:\Program Files (x86)\nodejs\node.exe`，都没有 `setUpClass` 里 `skipTest`。node 里用 `new Function(src + "return FindProxyForURL;")` **先做语法解析**（PAC 有语法错这里直接抛 → 断言失败），并补齐 PAC 引擎内置的 `isPlainHostName` / `shExpMatch` / `dnsDomainIs` |
| A10 | 13 条语义用例 | 同上 | whitelist：`store.steampowered.com`/`steamcommunity.com`/`STORE.STEAMPOWERED.COM.`/`steamcommunity.com.` → 含 SOCKS5；`example.com`/`notsteampowered.com`/`evil-steamcommunity.com`/`localhost`/`127.0.0.1`/`192.168.1.1` → `DIRECT`；`xn--fiqs8s.cn` → 不崩（返回值必须非空字符串）。blacklist（`direct_suffixes=CN_DIRECT_SUFFIXES`）：`bilibili.com` → DIRECT、`example.com`/`store.steampowered.com` → SOCKS5。all：`example.com`/`qq.com` → SOCKS5。另加 `local_net=False` 两份 PAC 能跑通 |

> 关于 blacklist 的 `bilibili.com → DIRECT`：`build_pac()` **不会**自动给 `direct_suffixes` 填国内清单（默认 `()`），所以这条必须显式传 `CN_DIRECT_SUFFIXES`。这一点在 §5 第 2 条里作为「真实使用路径上的缺口」记了一笔。

### 2.2 `tests/test_fuzz.py`

全部固定种子 `random.Random(20261024)`；**只断言不变量，不断言具体值**；每条用 `subTest` 打样本。

| 编号 | 目标 | 样本量 | 断言 |
|---|---|---|---|
| B1 | `netutil.parse_node_url` | 300 条（36 条命名样本 + 264 条随机：片段拼接 / 结构化模板 / 长度极值 0,1,255,256,4096,60000 / 纯随机字符） | 成功 → 必须是 `Node`，`kind ∈ {socks5,http,direct}`，`1 ≤ port ≤ 65535`（SPEC §4.5）；失败 → **只准 `ValueError`**，其它异常一律 `self.fail` 并带上样本。另有 8 条「语义上不可能成功」的样本单独断言必抛 `ValueError`；4 条合法输入做对照（防止「全都抛」也算通过）；`test_samples_are_reproducible` 断言同种子出同批样本 |
| B2 | `forward.extract_sni` | 500 段随机字节（一半强加 `16 03 01 + 伪记录长` 头部）+ 结构化畸形 | 返回 `str | None`，绝不抛。结构化覆盖：每个偏移截断（含去头）、`rec_len` 0/0xFFFF、`hs_len` 0/0xFFFFFF、扩展块长 0xFFFF、扩展项长 0xFFFF、`sn_list_len` 0/0xFFFF、`name_len` 0xFFFF、`sid_len` 255、`cs_len` 0xFFFF、`comp_len` 255、空/`\x00\x00\x00`/300 字节/UTF-8/IDN SNI。另有一条正向用例先把「健康 ClientHello → `store.steampowered.com`」证出来，避免「全返回 None」也能过 |
| B3 | `system.sanitize_entries` | 17 组结构化（换行、`\r\n`、`#`、伪造 `MARK_BEGIN/END`、空格、超长 300、IP 当域名、非 ASCII、`_x`/`-bad`/`dn--`、制表符、非法 IP 键、`[::1]` 键）+ 100 条随机 blob（字母表含标记串/`\x00`/中文/`*`/`@`） | 返回 `(dict, list)`；**每个 IP 键都必须 `ipaddress.ip_address()` 通过**；每个 host 必须 `_SAFE_HOST.match()`、`len ≤ 253`、不含 `#`/`MARK_BEGIN`/`MARK_END`、不含 `\n`/`\r`、且在主机名位置 `ipaddress.ip_address()` 必须抛 `ValueError`（即不许是 IP）。另断言合法条目 `1.2.3.4` 被保留、`not-an-ip`/`999.1.1.1` 被剔除 |
| B4 | `proxy.Upstream` + `rules.Router.decide` | 200 个畸形 host（29 条固定：空串/`...`/`[]`/`\x00`/`*.example`/`a..b`/`-lead`/超长 300/`例え.テスト`/`[::1]`/尾点/大写…，其余随机） | 三种 mode 各跑一遍：返回值必须 `in ("direct","upstream")`，不抛。另测 `Upstream.enabled` 对同一批畸形 host 只返回 `bool`；`kind="direct"`、`host="NODE_HOST"` → `False`，`host="127.0.0.1"` → `True`（占位符判定没把本机客户端误杀） |
| B5 | `pac.build_pac` 脏后缀 | `["a\nb", 'c"d', "e\\f", "g\rh", "  x  ", "", "ok.example"]` | ① 不抛、仍含 `var PROXY = [` / `var DIRECT = [`；② 断言 PROXY 数组里**每一非空行正好一对引号且无控制字符**（即「一行一个 suffix」结构不被破坏）。②在现状下不成立 → 见 §5 第 1 条，用 `skipTest` 记录，修好后自动转为 PASS |
| B6 | `cli._hosts_entries` | 5 组伪造 block：嵌套 `BEGIN,BEGIN,...,END,...,END`、只有 BEGIN 无 END、END 在 BEGIN 之前、块内空行/纯 IP 行/制表符/500 字符长 host/行内出现 `MARK_BEGIN`、5001 行大输入 | `read_hosts()` 用 `mock.patch.object(sysx, "read_hosts", ...)` 顶掉（**绝不碰真实 hosts 文件**，只读不写）；断言返回 dict、键/值均为非空 `str`、不抛；大输入用 `time.monotonic()` 断言 < 3s（防 `while` 写错死循环） |

## 3. 验证结果

环境：`export PYTHONUTF8=1`；PY = `C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`（实际报 3.13.14）；node = `v22.22.2`（`shutil.which("node")` 命中，语义部分**真跑了**，非跳过）。

- `python -m compileall -q svpn tests/test_pac.py tests/test_fuzz.py` → **exit 0**
- `tests/test_pac.py` → **12 用例全绿，0.38s**（进程总耗时约 2.7s，其中 node 冷启动约 1.1s；4 份 PAC 合并进**一个** node 进程，否则光启动就要 4.4s）
- `tests/test_fuzz.py` → **19 用例全绿 + 1 skipped，0.13s**（skip 就是 §5 第 1 条的 BUG 记录）
- 基线回归（硬指标，全绿）：

| 测试 | 预期 | 实际 |
|---|---|---|
| `tests/test_logic.py` | 50 | **50 OK（0.11s）** |
| `tests/test_netutil.py` | 57 | **57 OK（0.73s）** |
| `tests/test_regression.py` | 62 | **62 OK（0.73s）** |
| `tests/smoke_test.py` | ALL PASS 17 | **ALL PASS** |

- **反向自检（证明断言有牙齿）**：把 `build_pac` 临时替换成「历史事故版本」（输出 `var PROXY = { ... }`），`PacTextTests` 的 8 条里有 6 条当场红（含 subTest 共 15 处失败）。说明 `test_pac.py` 不是摆设 —— 谁把数组改回对象，提交前就会被打回。

## 4. 契约变更 / 需要别人配合

无源码改动，无签名变更。以下是**测试对源码的隐性依赖**，改这些地方会直接打到测试（属于应有约束，不是意外）：

1. `pac.build_pac()` 的三处文本被钉死：`var PROXY = [`（数组）、`host === "localhost" || host === "127.0.0.1" || host === "::1"`、三种 mode 的兜底行原文。要改 PAC 模板请同步 `tests/test_pac.py::_tail_rule`。
2. `parse_node_url()` 的「只准 `ValueError`」是硬约束（`test_fuzz.py::test_fuzz_300_samples`）。任何内部 `int()`/`.split()`/`ipaddress` 漏出来的 `TypeError`/`IndexError`/`AttributeError` 都会被抓到。
3. `cli._hosts_entries()` 私有名被直接引用（`getattr` 取，取不到 `skipTest` 并提示同步测试）。P2 若重命名/抽走它，请通知我改一行。
4. `system._SAFE_HOST`、`system.MARK_BEGIN/MARK_END` 同样被直接 import 做 oracle（只读）。

## 5. 没做完 / 发现的其它问题

### 5.1 【真 BUG，建议修】`build_pac()` 不对 suffix 做 JS 字符串转义 → 生成的 PAC 会整份加载失败

- **位置**：`svpn/pac.py:16-17`
  ```python
  pjs = ",\n".join(f'  "{s}"' for s in prox)
  djs = ",\n".join(f'  "{s}"' for s in direct)
  ```
- **触发**：`proxy_suffixes` / `direct_suffixes` 来自 `config.json`（用户可编辑，`cli.py:522/920/944` 直接 `cfg.get("proxy_suffixes")`）。suffix 里只要含 `"`、`\n`、`\r`、`\` 就会破坏 JS 字符串。
- **最小复现**（无 node 也能看）：
  ```python
  from svpn.pac import build_pac
  print(build_pac(proxy_suffixes=["a\nb", 'c"d', "g\rh"]))
  # 得到：
  #   var PROXY = [
  #     "a
  #   b",
  #     "c"d",
  #     "g
  #   h"
  #   ];
  ```
  这三行都是非法 JS（字符串里裸换行 / 提前闭合）。浏览器会**整份 PAC 加载失败** —— 和 `pac.py` 头部注释里警告的「对象字面量事故」是同一类故障，只是入口从「代码写错」换成了「配置文件写脏」。危害等级相同：PAC 一挂，浏览器既不走上游也不走直连。
- **建议修法**（`pac.py` 不在本任务范围内，我没动）：改用一个 `_js_str(s)`，做 `s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")`，或直接 `json.dumps(s, ensure_ascii=False)` 当 JS 字符串字面量（JSON 字符串是 JS 字符串的子集）。顺手建议对 suffix 做 `strip().lower().lstrip(".")` 归一，`"  x  "` / 空串这类现在会原样进数组。
- **测试侧处置**：`test_dirty_suffixes_stay_on_one_line` 断言了这个结构，现状下走 `skipTest`（消息里带出坏行），源码修好后**自动变 PASS**，不需要再改测试。

### 5.2 【缺口，非崩溃】blacklist 模式的 PAC 默认没有国内直连清单

`build_pac(mode="blacklist")` 在不传 `direct_suffixes` 时 `DIRECT = []`，于是 `_hit(host, DIRECT)` 恒假 → 黑名单模式下**所有域名都走上游**，与 `rules.Router` 的黑名单语义（`CN_DIRECT_SUFFIXES` 直连）不一致。`cli.py:522/920/944` 传的是 `cfg.get("direct_suffixes")`，而 13 个配置键里没有 `direct_suffixes` → 实际跑起来这个模式等于 `all`。
按 SPEC 我没改 `pac.py`/`cli.py`；`test_pac.py` 里 blacklist 语义用例显式传 `CN_DIRECT_SUFFIXES` 才能测出 `bilibili.com → DIRECT`。**建议**：`pac.build_pac` 在 `mode=="blacklist"` 且 `direct_suffixes is None` 时回落 `rules.CN_DIRECT_SUFFIXES`（或由 P2 在 `DEFAULT_CONFIG` 里补 `direct_suffixes` 键并接线）。注意后者会动 §4.1 的「恰好 13 键」契约，需 P2/P6 一起过。

### 5.3 【缺口，低危】`http://a:b:c` 会被当成主机名 `a:b:c` 接受

`parse_node_url("http://a:b:c")` → `Node(kind="http", host="a:b:c", port=7890)`。P4 已按 SPEC §4.5 修掉「端口越界」和「裸 IPv6 + 冒号端口」两种，但「多个冒号且尾部非数字」仍原样落到 `host`（`netutil.py:493-499` 的 `_split_bare` 只拦 `tail.isdigit() and is_ip(head)`）。后果只是连接时 `getaddrinfo("a:b:c")` 失败 → 报错，不会静默连错地方，所以定低危。`test_fuzz.py` 对这条是容错的（Node 或 ValueError 都算过），若 P4 后续加严不需要改测试。

### 5.4 未覆盖（明确不做）

- `system.read_wininet` / `apply_hosts` / `clear_hosts`（读注册表、写真实 hosts）——按任务要求**不调用**。
- `SniForwarder.start()` / `ProxyServer.start()`（要 bind 端口）——归 P8 的集成测试。
- 需要联网的 DoH/TLS 探测——SPEC §1.4 禁止。

### 5.5 并行环境备注

跑的过程中 `svpn/{cli,netutil,proxy,forward}.py` 一直在被其他 agent 改写（时间戳 18:26–18:29 仍在变）。本报告的所有数字是**最后一次全量复跑**的结果（基线 50/57/62 + smoke 全绿）。我的两个文件只依赖 `pac.py`/`rules.py`/`netutil.parse_node_url`/`forward.extract_sni`/`proxy.Upstream`/`system.sanitize_entries`/`cli._hosts_entries`，其中 `pac.py`/`rules.py`/`system.py` 属「不要碰」文件，最稳；其余四个若后续再改，重跑这两条命令即可复核。
