# D1-docs 交付报告

> 只改了 4 个文档文件，未触碰任何 `.py` / `.json` / `.ps1` / `.cmd` / `tests/`。
> 所有结论都由「读源码 + 真跑命令」得出，没有照抄旧文档。

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `README.md` | 重写。新增「装/跑/卸」一节（`install.ps1` 一键安装 + `uninstall.ps1` 一键卸载，含退出码与全部开关）、`probe` 退出码专节 + CI 用法、`cdn.enabled` 两种接法的取舍表、12 键配置表 + 归一规则；删掉「暂勿使用安装器」与「附录 A：SNI 转发无 CLI 入口」；目录树标注 `forward.py` 已接线 / `split.py` 预留；新增「已知限制」7 条 | 352 → 535 |
| `README.en.md` | **新建**。可独立阅读的浓缩版：What / Why / Quickstart / Two CDN modes / Configuration / probe exit codes / Limits / Uninstall / More | 0 → 145（≤150 约束内） |
| `docs/ARCHITECTURE.md` | 重写。补 `_serve_async` 真实启动顺序、`_start_sni()` 接线、`_bridge` 新契约、`SniForwarder` 单连接流程与三个已修坑、hosts 两副面孔对照表、`summarize()` 六键语义；配置表从「10 键 + 预留键」改成真实 12 键；取舍从 3 条扩到 4 条（新增 `gather(return_exceptions=True)` 的取舍） | 225 → 387 |
| `docs/WINDOWS.md` | **新建**。BOM 硬要求与实测故障、`.cmd` 的 `call` 坑与中文行「载重规则」、UAC 与 hosts（含编码 round-trip 与 Latin-1 踩坑）、WinINET 注册表四值/备份/回滚、Steam 改代理必须重启、Win10 vs Win11 差异、WinError 10048/121/10054/10060/10013/10049 对照、安装/卸载脚本退出码与参数、排障清单 | 0 → 348 |

## 2. 逐条修复（旧文档 → 真实代码）

| 编号 | 旧文档的说法 | 真实代码 | 落点 |
|---|---|---|---|
| 1 | 「`cdn.enabled` 是预留键，当前未接线，设 true 无行为」；「附录 A：SNI 透明转发，目前无 CLI 入口」 | `cli._serve_async()` → `await _start_sni(cfg, srv)`：`cdn.enabled` 为真就构造 `SniForwarder(srv.upstream, sni_port, allow_suffixes=_sni_allow(...), stats=srv.stats, ip_map=best_ip_map(CDNCACHE, …))`，`OSError` 只 WARN 跳过；`_hosts_write()` 在 SNI 模式把 `cdn.hosts` 全量写成 `127.0.0.1` | README §2（取舍表）、§3.1、§9；ARCHITECTURE §2.2 / §3 / §4 |
| 2 | 「install.ps1 缺 BOM 且引用不存在的脚本 → 暂不使用安装器，走手动流程」 | `install.ps1` 已 UTF-8 BOM + 5 项文件清单 + `exit 2/3/4`；`start-svpn.cmd`、`uninstall.ps1` 已存在；`uninstall.ps1` 幂等、按 pid 杀、只删 hosts 托管块、`-Purge/-Yes/-KeepProxy/-HostsOnly` | README §1.1 / §1.3；WINDOWS §7 |
| 3 | 「退出码：0 = 没有死端点，1 = 有端点被判成不通·待排查」 | `probe.summarize()` 新增 `unusable`（直连不通且节点也不通）且 `ok = not unusable`；`cmd_probe` 返回 `1 if unusable else 0`；`--lenient` 才是旧语义（`1 if dead`）；末尾固定打印退出码说明 | README §4（含 CI 两段示例）；ARCHITECTURE §2.5 |
| 4 | 「`forward.py` 属死代码（无任何模块 import）」；`split.py` 未标注 | `cli.py:579` `from .forward import SniForwarder`；`split.py` 全仓无 import → 明确标为「预留，未接线」 | README §10 目录树 + 目录树下方提示；ARCHITECTURE §4 / §11 |
| 5 | 配置表 10 个键，`cdn.enabled` 标「预留」 | `DEFAULT_CONFIG` 恰好 12 键（`listen_host`…`direct_hosts`），`merge_config()` 丢弃 `_` 前缀/未知键、嵌套逐键合并、六类归一器；`config.example.json` 与之一致。另有 2 个可选高级键 `proxy_suffixes`/`direct_suffixes`（不在模板、写了生效、整表替换） | README §5 表 + §5.1；ARCHITECTURE §5.1 / §6 / §11 |
| 6 | （缺）已知限制 | 无节点 → 社区/头像不可用（SNI 阻断）；`_bridge`/`SniForwarder` 用 `gather(return_exceptions=True)` → 半开连接等到 TCP 超时 | README §12 第 1、2 条；ARCHITECTURE §8 第 4 条 / §11 |
| 7 | （缺）Windows 专项 | 全部写进 `docs/WINDOWS.md`，每条都能指到具体代码行（`_elevate`、`_bind` 的 121 重试、`uninstall.ps1` 的 Latin-1 读写、`$markBegin` 双份硬编码等） | `docs/WINDOWS.md` §1–§8 |
| 8 | `README.en.md` 缺失 | 新建，145 行，可独立阅读（不依赖中文版） | `README.en.md` |
| 9 | 旧文档没有 `.cmd` 中文「载重规则」 | WINDOWS §1.3 写全：**含中文的行（`rem` 也算）**会让 cmd.exe 在 `chcp 65001` 后吃掉下一行行首 → `'?' is not recognized`；解法两条（注释 ASCII / 中文行后留空行）+ CRLF 无 BOM；附 P5 的 3 轮对照实测表 + 一条可复制的自查命令（实跑 0 命中） | WINDOWS §1.3；README §10 末尾提醒「改 `.cmd` 前先读 §1.3」 |
| 10 | `call` 坑只被写成「探测要加」 | 明确**`%PY% -m svpn ...` 这行本身也必须 `call`**，并给出 `python.bat` 垫片实测对照：不加 `call` 输出停在第 1 步、**直接 EXIT=0**；全加则三步走完、`ERRORLEVEL` 正常（`node set` 缺参拿 2） | WINDOWS §1.2 |
| 11 | 子命令数量口径含糊（易被数成 12 或 13） | 统一写成**13 个解析器条目 = 12 个公开子命令 + `_serve`（内部，只有 `--system-proxy`，由 `up -b` 父进程拼参）** | README §10、ARCHITECTURE §2.1 |

## 3. 验证结果

环境：`PYTHONUTF8=1`，`PY=C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`，
工作目录 `/d/LE/AI/dshwork/steam-vpn`，运行前 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NODE_OPTIONS`。

### 3.1 真跑过的命令（逐条）

| 命令 | 结果 | 用来支撑文档里的 |
|---|---|---|
| `python -m svpn --help` | rc=0，13 个子命令全列出（`init/up/_serve/down/status/probe/node/cdn/hosts/system/pac/steam/doctor`） | README §10 命令表、ARCHITECTURE |
| `python -m svpn probe --help` | rc=0，`--node/--timeout/--json/--lenient` 全存在（`--lenient` help 文本「只在「不通 · 待排查」时返回 1（旧行为）」） | README §3.3 / §4 |
| `python -m svpn cdn --help` | rc=0，`--refresh/--no-bench/--domain/--timeout/--json`，**无 `--bench`** | README §3.4（刻意只写 `--no-bench`） |
| `python -m svpn hosts --help` | rc=0，位置参数 `{apply,clear}` + `--no-elevate/--timeout` | README §3.5 |
| `python -m svpn node --help` | rc=0，`{scan,set,clear}` + `url` + `--json` | README §7 |
| `python -m svpn up --help` | rc=0，`-b/-p/-m/--system-proxy/--node/--no-auto` | README §3.1 |
| `python -m svpn down --help` | rc=0，`--keep-system/--clear-hosts` | README §3.2 |
| `python -m svpn status --help` / `pac --help` / `system --help` / `steam --help` / `doctor --help` / `init --help` | 全 rc=0 | README §10 命令表 |
| `python -m svpn -v` | 输出 `1.0.0`，rc=0 | — |
| `python -m svpn status` | rc=0，`进程/端口/节点/模式/系统代理/CDN缓存/hosts/上次统计` 八行，与 README §9 描述一致 | README §3.2 / §10 |
| `python -m svpn system show` | rc=0，输出四个注册表值的 JSON | WINDOWS §3.2 |
| `cmd //c svpn.cmd up -b` | `[=] 已在运行 (pid 24796)；改配置请先 svpn down`，EXIT=0（当时另有 agent 的实例在跑，正好验证「已在运行」分支与退出码透传） | README §10 包装脚本 |
| `powershell -NoProfile -ExecutionPolicy Bypass -File .\svpnctl.ps1 status` | `→ python -m svpn status` + 完整状态，EXIT=0 | README §10 包装脚本 |
| `.\svpnctl.ps1 status`（不带 Bypass） | **失败**：`禁止运行脚本`（本机执行策略 Restricted） | 据此在 README §10 补了执行策略提示，WINDOWS §8 保留该条排障 |
| `python tests/smoke_test.py` | **ALL PASS**，实测 **15 项**检查 | README 附录（把旧文档/SPEC 的「17 项」改成实测的 15 项） |
| BOM 核对两行（WINDOWS §1.1 里那两条） | `install.ps1/uninstall.ps1/svpnctl.ps1 → True`；`svpn.cmd/start-svpn.cmd → False` | WINDOWS §1.1 的「期望输出」 |
| `Parser::ParseFile` 三个 `.ps1` | `errors=0` ×3 | WINDOWS §1.1 的语法自查一行 |
| `python -m svpn _serve --help` | rc=0，**只有 `--system-proxy` 一个开关** | 证明「13 个解析器条目 = 12 公开 + `_serve`」且 `_serve` 由父进程拼参（README §10、ARCHITECTURE §2.1） |
| 文档里新加的「`.cmd` 中文行后必须空行」自查一行 | 跑出 **0 命中**（两个脚本都合规） | WINDOWS §1.3 的自查命令；顺带证明这条规则当前没被破 |

另外做了 **42 条文档命令组合的 argparse 解析验证**（只 `parse_args`，不执行）：把 README/WINDOWS 里出现的
每种命令行原样喂给 `build_parser()`，断言 `SystemExit` 为 0 且 `args.func` 可调用 → **42/42 通过**，其中包含
`up -b --port 17899 --mode all`、`probe --node none --timeout 6 --json`、`cdn --refresh --no-bench`、
`hosts apply --timeout 5`、`steam --restart --system --args -console` 这类组合。

### 3.2 语义验证（不联网）

| 断言 | 方法 | 结果 |
|---|---|---|
| `DEFAULT_CONFIG` 是 12 键 | `len(DEFAULT_CONFIG)` + `sorted()` | 12 键，与 `config.example.json` 一致 |
| `merge_config` 归一 | 喂 `listen_port=None / http_port=0 / cdn.enabled=true / cdn.hosts=['x.com']` | `7899 / 0 / true / ['x.com']`，无异常 |
| `_` 前缀与未知键被丢弃 | 喂 `_note` / `typo_key` | 都不在返回结果里 |
| 可选高级键生效 | 喂 `proxy_suffixes=['a.com','b.com']`、`direct_suffixes='c.com, d.com'` | `['a.com','b.com']` / `['c.com','d.com']`（字符串写法也归一成列表），结果含这两键、仍不含 `_note`/`typo` |
| 后缀表是「替换」不是「追加」 | `Router(proxy_suffixes=['corp.example.com'])` | `store.steampowered.com → direct`、`corp.example.com → upstream` |
| `direct_hosts` 先于 `proxy_hosts` | `Router(direct_hosts=['a.com'], proxy_hosts=['a.com'])` | `a.com → direct` |
| `blacklist` 空 `direct_suffixes` 回落内置 | `Router(mode='blacklist', direct_suffixes=[])` + `build_pac(mode='blacklist', direct_suffixes=[])` | `www.baidu.com → direct`、`example.org → upstream`；PAC 里仍有 `baidu.com`；同参数 `whitelist` 时 PAC 不含 `baidu.com` |
| `probe.summarize` 六键与判据 | 构造三种 Row（全直连可用 / 无节点+SNI 阻断 / 有可用节点+SNI 阻断 / 两边都不通） | 无节点时 `unusable=['steamcommunity.com','avatars.steamstatic.com']`、`ok=False`；有可用节点时 `unusable=[]`、`ok=True`；不通时 `dead` 与 `unusable` 同时命中。键集 = `['dead','direct_ok','need_node','node','ok','unusable']` |
| `forward` 已被 cli 引用、`split` 无人 import | 全仓 grep `from .forward import SniForwarder` / `import split` | cli.py:579 命中；`split` 零命中 |
| 文档里的 `smoke` 项数 | 实跑后统计 PASS 行 | 15 项（旧文档与 SPEC 的「17」都是笔误） |
| 文档里的单测总数 | `unittest.TestLoader().discover('tests','test_*.py').countTestCases()` | 356 个用例（旧文档写「100+」） |
| 脚本/配置编码 | 读前 3 字节 + 统计裸 LF | 三个 `.ps1` BOM=True/CRLF；两个 `.cmd` BOM=False/CRLF/裸 LF=0；两份 json 无 BOM |

### 3.3 没有真跑、只做静态核对的（说明理由）

| 命令 | 为什么没跑 | 怎么核对的 |
|---|---|---|
| `python -m svpn up -b` / `_serve` | 当时已有另一个 agent 的实例占着 7899/7900，再起会互相干扰；且属「常驻代理」，本轮不启动 | 读 `cmd_up`/`_serve_async`/`_start_sni`/`_hosts_write` 全路径 + `cmd //c svpn.cmd up -b` 走了一遍「已在运行」分支 |
| `python -m svpn probe` / `doctor` / `node scan` / `cdn --refresh` | 会联网，且会与并行 e2e 抢网络、污染其 10 连测数据 | 读 `probe.run/summarize`、`doctor` 的 issues 逻辑、`discover.scan` 的来源表；`probe` 语义用构造 Row 验证 |
| `install.ps1` | 会写 `config.json`、跑联网 doctor、往桌面写 `.lnk` | `Parser::ParseFile` 0 错误 + 逐行读 5 个 stage 与三个退出码；P5 报告有「空目录→exit 2 / 占位树→exit 4」的实跑证据 |
| `uninstall.ps1` | 会杀掉**别的 agent 正在跑的** `runtime/svpn.pid` 进程 | `Parser::ParseFile` 0 错误 + 逐行核对 5 步与 4 个参数；P5 报告有 `-KeepProxy` 空跑与 `-HostsOnly` 字节级样例的实跑证据 |
| `hosts apply/clear`、`system on/off` | 会改真实 hosts / HKCU | 读 `_hosts_write`/`apply_hosts`/`sanitize_entries`/`apply_wininet`/`restore_wininet` |
| `python -m unittest discover` | 356 用例、含会绑端口的集成测试，与在跑的实例冲突风险高 | 只做用例计数（静态 discover），不执行 |

## 4. 契约变更 / 需要别人配合

### 4.1 `proxy_suffixes` / `direct_suffixes`：我先报了缺陷，随后被人修好，文档已按修后行为重写

**发现（18:44 前后）**：`svpn/cli.py` 的 `merge_config()` 放行 `EXTRA_KEYS` 后紧接着 `base = cfg[key]`，
而 `cfg` 是 `DEFAULT_CONFIG` 的深拷贝、这两键不在里面 → `KeyError`：

```
python -c "from svpn.cli import merge_config; merge_config({'proxy_suffixes':['a.com']})"
→ KeyError: 'proxy_suffixes'
```

后果是「用户写了这两个键，任何子命令（含 `svpn status`）启动即崩」，`_normalize()` 里对它们的归一是不可达代码。
我按「文档必须与代码一致」的规矩先给 team-lead 发了缺陷报告（含一行修法），没有自己改代码。

**结果（18:51）**：`cli.py` 被改成 `base = cfg.get(key)`（并在上面加了注释）。我随即复验：

```
merge_config({'proxy_suffixes':['a.com','b.com'], 'direct_suffixes':'c.com, d.com', '_note':'x', 'typo':1})
→ proxy_suffixes=['a.com','b.com']  direct_suffixes=['c.com','d.com']  '_note'/'typo' 均被丢弃
```

**文档已按 team-lead 给的口径改成「可用」**（README §5.1 / ARCHITECTURE §5.1 段末 + §6 表尾 + §11 / `README.en.md`
Configuration 段），并补了四条原先没写清的语义：

1. 不在 `config.example.json` 模板里，属**可选高级键**，写了才生效；
2. 值可以是 JSON 数组**或**逗号/空格分隔字符串，都会归一成 `list[str]`（嵌套 list/dict/`None` 逐项丢弃，
   非 list 非 str 回落空表）；
3. **优先级**：`direct_hosts` > `proxy_hosts`（精确主机）> `*_suffixes`（后缀表）> 内置
   `STEAM_SUFFIXES` / `CN_DIRECT_SUFFIXES`。实测钉住：`direct_hosts=['a.com']` + `proxy_hosts=['a.com']` → `direct`；
   `proxy_suffixes=['corp.example.com']` → `store.steampowered.com` 变 `direct`、`corp.example.com` 变 `upstream`（证明确实是替换不是追加）；
4. **`mode=blacklist` 的回落**：`direct_suffixes` 为空时自动回落到内置国内清单
   （`build_pac` 里 `fallback = CN_DIRECT_SUFFIXES if mode == "blacklist" else ()`；`Router` 是 `direct_suffixes or CN_DIRECT_SUFFIXES`）。
   实测：`build_pac(mode="blacklist", direct_suffixes=[])` 生成的 PAC 里仍有 `baidu.com`；`Router(mode="blacklist", direct_suffixes=[])`
   对 `www.baidu.com` 判 `direct`、对 `example.org` 判 `upstream`。

另外补了一条使用代价：**是整表替换**，写 `["corp.example.com"]` 会把 Steam 整个挤出「走节点」名单，想保留要自己补齐。

> 留一条给审计方复核：`EXTRA_KEYS` 仍是「不在 `DEFAULT_CONFIG` 里」的特例，`init --force` 用 `DEFAULT_CONFIG`
> 重写文件时会静默丢掉用户手写的这两键（`node set/clear` 走的是「读原文件再写回」，不会丢）。这一行为差异已写进
> README §5.1 与 `docs/ARCHITECTURE.md`，属于 P6 报告里提过的 `cmd_init` 同类问题（它同时也把 `browser.bypass`
> 打回最小版），不是本轮我能改的范围。

### 4.2 其它给维护者的三条

1. **`cdn.sni_port` 实际上必须是 443**。`svpn up` 的预检提示写着「改 `cdn.sni_port`（比如 4431）」，但 hosts 条目
   无法表达端口，客户端连的就是 `127.0.0.1:443`——改成 4431 等于「转发生效但没人来连」。文档（README §2 提示块 +
   §8 故障表）按「必须 443」写，并给了正确出路（退掉占用 443 的程序，或把 `cdn.enabled` 改回 `false`）。
   建议下一轮把 `cli.py:488` 那句提示文案改掉。
2. **`svpn.cmd` 的 `--bench` 已修**：我先实测确认 `--bench` 从未注册（`error: unrecognized arguments: --bench`，rc=2），
   team-lead 随后把 `svpn.cmd` 第 13 行改成 `svpn cdn --refresh --no-bench`。文档里只出现 `--no-bench`，已复核无残留。
3. **`smoke_test.py` 是 15 项检查，不是 SPEC/旧文档写的 17 项**（实跑计数）。文档已按 15 写。

## 5. 没做完 / 发现的其它问题

1. **`README.md` 与 `docs/WINDOWS.md` 落盘是 CRLF，`docs/ARCHITECTURE.md` 是 LF**（`README.en.md` 是 CRLF）。
   Markdown 无功能影响，但仓库内不统一；如需统一请说明要 LF 还是 CRLF，我再刷一遍（属纯格式改动）。
2. **`README.en.md` 只有 145 行，是「浓缩版」不是翻译**：术语/命令与中文版一致，但省略了故障对照表、
   排障清单和回滚表，只保留 What/Why/Quickstart/两种 CDN 模式/配置/probe 退出码/Limits/Uninstall。
   如果希望英文版也能独立完成排障，需要再加一节（会超过 150 行约束，故本轮按约束收住）。
3. **`probe --json` 的「表格 + JSON 尾巴」不是纯 JSON**：文档已提示脚本去读 `runtime/probe.json`，
   但根因是 CLI 输出契约本身不干净（P2 范围内的历史设计）。若未来要当 API 用，建议加 `--json-only`。
4. **一处文档无法验证的悬空说法**：`docs/WINDOWS.md` §5 的 Win10/Win11 差异里，除「默认终端」「自带 curl」
   以外，其余条目我都写成「与系统版本无关」，避免编造版本差异；真正影响使用的差异只有终端字体与执行策略默认值。
5. **我没做的事**：`WORKLOG.md`、`runtime/*`、任何代码文件都未改；`_crew/SPEC.md` §4.1 的「13 个键」字样仍在（不属我文件），
   但三份文档与四份报告已统一为 12 键口径。
