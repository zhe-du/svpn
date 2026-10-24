# P6-config 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `config.example.json` | 重写为 canonical 模板：12 个运行键按 SPEC §4.1 顺序 + 末尾一个 `_note`；`cdn.hosts` 补齐 8 个域名并与 `CDN_DOMAINS` 同序；`browser.bypass` 用完整内网段；`_note` 内容改写为「`cdn.enabled` 两种真实行为 + `proxy_hosts`/`direct_hosts` 是显式覆盖名单」 | 42 → 42（内容重写，仍 42 行） |
| `config.json` | 与默认值对齐：补 `auto_node: true`、`cdn.refresh_hours: 6`、`cdn.hosts` 补到 8 个（加回 `avatars.steamstatic.com` 并按 `CDN_DOMAINS` 重排）、补 `proxy_hosts: []` / `direct_hosts: []`、`browser.bypass` 换成完整版；保留用户既有选择（7899/7900/whitelist/12/`cdn.enabled=false`），`upstream` 仍为占位 `NODE_HOST:1080`，不写 `_note` | 35 → 41 |
| `tests/test_config_contract.py` | 新建契约测试，15 用例，标准库 only，全量 0.004s | 0 → 255 |

**没有碰 `svpn/cli.py`**（P2-cli 并行改），也没有碰任何其它文件。

## 2. 逐条修复

| 编号 | 原缺陷（WORKLOG §7 配置审计） | 修法 | 验证方式 |
|---|---|---|---|
| C1 | `config.example.json` 是 `DEFAULT_CONFIG` 的兼容超集，`proxy_hosts`/`direct_hosts` 只在 example 里，默认值缺 → `Router` 消费的键在默认配置里不存在 | 引入「example = canonical」的口径：example 保持 12 键，测试强制要求 `DEFAULT_CONFIG` 补齐同键集（P2 已落地 12 键）；example 一旦多键/少键立刻红 | `test_example_key_set_equals_default`、`test_example_types_match_default` |
| C2 | `config.json` 漂移：`cdn.hosts` 只 7 个（缺 `avatars.steamstatic.com`）、缺 `auto_node` / `cdn.refresh_hours`、缺 `proxy_hosts` / `direct_hosts` | 直接补齐并对齐顺序；测试断言 `cdn.hosts == list(CDN_DOMAINS)`（含顺序），排序后也不许有差集 | `test_user_config_cdn_hosts_cover_all_domains`、`test_example_cdn_hosts_match_cdn_domains` |
| C3 | `_note` 会被 `load_config` 原样并入 cfg（脏数据） | example 保留 `_note` 但要求 `merge_config` 丢弃（P2 已实现 `_` 前缀丢弃）；`config.json` 干脆不写 `_note` | `test_merge_example_equals_default`（断言合并结果无 `_note`）、`test_user_config_has_no_private_keys`；实测 `load_config()` 返回 12 键、`'_note' in cfg == False` |
| C4 | `browser.bypass` 两份不一致：`config.json` 少了 `172.17–172.31` 一大串 | 两份统一为完整版；测试循环校验 `172.16.*`…`172.31.*` 全段 + `192.168.*` + `*.cn` + `<local>`/`localhost`/`127.*`/`10.*` | `test_bypass_covers_private_ranges`（对两个文件都跑） |
| C5 | `cdn.enabled` 的语义在两份文件里没有说明，用户不知道 `true` 要常驻进程 | `_note` 明确写：`true` = 启 SNI 透明转发（hosts 指向 `127.0.0.1`，由 `svpn up` 常驻按 SNI 拨号，进程停了下不了）；`false` = hosts 直指选边 IP（不用常驻）；`proxy_hosts`/`direct_hosts` 优先于 `rules.py` 内置规则 | 人工核对 `_note` 文本；example 与 `cli.py` 的实际接线（SPEC §4.2）一致 |
| C6 | 无任何机制阻止下一次漂移 | 新建 15 条契约测试，覆盖键集 / 键序 / 类型 / CDN 域名顺序（含 `DEFAULT_CONFIG` 侧）/ merge 语义 / 脏键 / 端口合法性 / 占位符 / bypass | 本文件全部用例 |

**测试设计上的两个坑，已在代码注释里写明：**

1. `getattr(cli, "merge_config")` 存进 `TestCase` 类属性会被描述符协议绑成方法（首跑报 `takes 1 positional argument but 2 were given`）→ 改为模块级 `_MERGE` 常量。
2. `merge_config(example) == DEFAULT_CONFIG` 的严格深比较与「example 的 bypass 用完整版」天然冲突（默认值是 `172.16.*` 最小串）→ 先断言 example 的 bypass 是默认值的**超集**，再把这一项对齐后做整份深比较，其余键一个都不许差。

## 3. 验证结果

- `compileall -q svpn`: exit **0**
- 新增 `tests/test_config_contract.py`: **15 用例 / OK / 0.004s**
- 基线回归（全部仍绿）：
  - `tests/test_logic.py` → Ran 50, **OK**
  - `tests/test_netutil.py` → Ran 57, **OK**
  - `tests/test_regression.py` → Ran 62, **OK**
  - `tests/smoke_test.py` → **ALL PASS**
- JSON 合法性 / 编码（`PYTHONUTF8=1`，Python 3.13.12）：
  - `config.example.json` ok，顶层键序 `listen_host…direct_hosts, _note`，无 BOM，LF
  - `config.json` ok，顶层键序 `listen_host…direct_hosts`，无 BOM，CRLF（沿用原文件换行，Python 读无所谓）
  - `tests/test_config_contract.py` 无 BOM，LF
- 真实加载核对：`cli.load_config()` → 12 键、`_note` 未泄漏、`cdn.hosts` 8 个、`refresh_hours=6`、`auto_node=True`、`proxy_hosts=[]`、`direct_hosts=[]`、`7899/7900`、`upstream.host=NODE_HOST`

## 4. 契约变更 / 需要别人配合

1. **键数口径已由 team-lead 订正为 12**：SPEC §4.1 正文写的「13 个键」是把 `_note` 也数进去的笔误，顶层运行键实为 12 个（`listen_host`…`direct_hosts`）。测试按「集合相等」判定、不硬编码数量，另加 `test_default_config_key_count` 显式断言 `len(DEFAULT_CONFIG) == len(SPEC_TOP_KEYS) == 12`，失败时能一眼看出多/少了哪个键。**SPEC.md 本身仍是 13 的字样（不属我文件所有权，未改）。**
2. **`DEFAULT_CONFIG.browser.bypass` 与 example 不同**（SPEC §4.1 给的是最小串 `172.16.*`，example 要求 `172.16–172.31` 全段）。这是有意为之：默认值只保证核心段，模板给完整段。若 P2 愿意把默认值也改成完整版，测试的 bypass 豁免分支会自动退化成严格深比较，不需要改测试。
3. P2 的 `merge_config` 已落地（签名 `merge_config(user: dict | None) -> dict`），`DEFAULT_CONFIG` 已是 12 键 → 本文件所有用例都在**真实执行**，没有走 `skipTest` 分支。后续若 P2 再改键集/类型，会直接红。
4. 无人消费 `_note`，但 `save_config(DEFAULT_CONFIG)`（`cmd_node` 相关路径）会写掉 example 的完整 bypass —— 这是既有行为，不在 P6 范围，未动。

## 5. 没做完 / 发现的其它问题

- `config.json` 的 `listen_port 7899` / `http_port 7900` 是用户既有选择，与默认值恰好相同；若用户改端口，`test_user_config_numeric_ports_sane` 只校验范围与两者不等，不会红。
- 本机确实没有上游节点，`upstream.host` 保持占位 `NODE_HOST`（`cli.PLACEHOLDERS` 里含它）；测试 `test_user_config_keeps_placeholders` 会盯住这一点，防止有人为「跑通」把占位改成 `127.0.0.1` 制造假可用。
- 发现但**未修**（不属 P6 文件所有权，留报告）：`cli.cmd_init()` 在 `--force` 或文件不存在时走 `save_config(DEFAULT_CONFIG)`（`svpn/cli.py:1125`），会把 SPEC 的最小 bypass（只有 `172.16.*`）整份写回 `config.json` —— 这正是 C4 那类漂移的**再次发生路径**，而我刚把 `config.json` 的 bypass 补全了，跑一次 `svpn init --force` 就会退回去。建议把 `cmd_init` 改成写 `config.example.json`（canonical 模板）或至少在 `save_config` 里统一 bypass 口径。（对照：`cmd_node` 的 `save_config(cfg)` 走的是 `load_config()` 的合并结果，会保留用户完整 bypass，无此问题。）
- `_note` 的文本较长（单行），如果后续希望 example 更易读，可考虑改用字符串数组；但那样 `_note` 的类型会从 `str` 变成 `list`，测试不校验 `_note` 类型以外的内容，改起来无痛。
