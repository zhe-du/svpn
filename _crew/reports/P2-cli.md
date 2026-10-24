# P2-cli 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `svpn/cli.py` | 配置归一（`merge_config`/`_normalize`/6 个 `_as_*` 归一器）、`DEFAULT_CONFIG` 补 `proxy_hosts`/`direct_hosts`、`pid_alive`/`_port_owner`/`taskkill`/`_elevate` 全改字节解码、`cmd_up` 端口预检（同端口 + SNI 端口）、`cmd_probe` 退出码 + `--lenient` + `RUNTIME.mkdir`、`_serve_async` 接线 `SniForwarder`、`_hosts_write` 双模式（一律经 `sysx.apply_hosts`）、`_print_route_summary` 加 CDN 模式行 | 925 → 1270（+345） |
| `tests/test_cli_contract.py` | 新建，50 条 unittest（离线、标准库 only） | 0 → 619（+619） |

只动了这两个文件。`config.json` / `config.example.json` / `runtime/*` / `README` / `WORKLOG` / `proxy.py` / `forward.py` / `probe.py` 一行未改（`svpn status`、`svpn pac`、`svpn up --port 7900` 这类实跑命令都不写配置）。

## 2. 逐条修复

| 编号 | 原缺陷 | 修法 | 验证方式 |
|---|---|---|---|
| 1 | `cmd_probe` 在 `PROBEFILE.write_text(...)` 前不建目录：删过 `runtime/` 或全新安装时，探测跑完（几十秒）才 `FileNotFoundError`，结果全丢 | 写文件前 `RUNTIME.mkdir(parents=True, exist_ok=True)`（在 `setup_log` 之外单独兜一层，因为 probe 不走 `setup_log`） | `test_probe_writes_file_creating_runtime_dir`：先断言 `RUNTIME` 不存在，跑完 `cmd_probe` 后 `PROBEFILE` 存在 |
| 2 | `svpn up --port 7900`（撞 `http_port`）→ 第二个 `bind` 抛裸 `OSError 10048`，一堆 asyncio traceback；`cdn.enabled` 时 `sni_port` 无人检查 | 预检加三条：① `listen_port == http_port` 且 http 非 0 → 直接 `return 1` +「SOCKS5 与 HTTP 不能同端口：用 --port 17899，或改 config.json 的 http_port」；② `--port` 越界（1-65535）→ `return 2`；③ `cdn.enabled` 时 `sni_port` 与本机代理端口相同 → `return 1`，被别的进程占用 → 只 `WARN` 放行（SNI 是可选增强） | `test_same_port_is_rejected_before_bind`、`test_bad_cli_port_rejected`、`test_sni_port_conflict_with_own_port_fails`、`test_sni_port_busy_only_warns`；实测 `python -m svpn up --port 7900 --no-auto` → `rc=1` 且只输出那一行，无 traceback |
| 3 | `pid_alive` / `_port_owner` / `taskkill` 用 `text=True`：`PYTHONUTF8=1` 下解码 `tasklist`/`netstat` 的 OEM 输出会 `UnicodeDecodeError`，`except OSError` 接不住 → 崩；早期版本被静默吞掉 → 误判「未运行」 | 新增 `_decode()`（Windows 先 `mbcs`+`replace`，`LookupError`/失败回落 `utf-8`+`replace`）与 `_run_quiet()`（`capture_output=True` 不带 `text`，吞 `OSError`/`SubprocessError`，带 `timeout`）。`pid_alive` 改按列比对 PID（`parts[1:3]`，兼容映像名带空格），非 Windows 走 `os.kill(pid, 0)`；`_port_owner` 的 `info.split()[0]` 改成 `tokens[0] if tokens else "?"`，`pid` 初值改 `""` 不再 `None`；`cmd_down` 的 `taskkill` 也改字节解码（结果只进 `logging.debug`） | `test_pid_alive_missing`（999999/0/-1/"abc"→False）、`test_pid_alive_self`（自身 PID→True）、`test_port_owner_never_raises`、`test_decode_never_raises`（含 `b"\xff\xfe\x00garbage"` 与 cp936 中文） |
| 4 | 配置漂移：`_note` 被并进 cfg；未知键静默接受；`DEFAULT_CONFIG` 缺 `proxy_hosts`/`direct_hosts`（`Router` 消费但默认值里没有，与 example 不一致）；`listen_port: null` 时 `int(cfg["listen_port"])` 抛 `TypeError` | ① `DEFAULT_CONFIG` 补两键（现 12 键）；② 新增 `merge_config(user) -> dict`：`DEFAULT_CONFIG` 深拷贝 + 顶层 `_` 前缀/未知键丢弃（`logging.debug`）+ 嵌套 dict 逐键合并（嵌套未知键也丢）+ 统一走 `_normalize()`；③ `load_config()` = 文件不存在 `merge_config({})` / 存在 `merge_config(json.loads(...))`；④ `_as_number`/`_as_port`/`_as_bool`/`_as_text`/`_as_host_list` 做数值、端口、布尔、字符串、名单归一（`None`/`""`/`"abc"`/`NaN`/`inf`/越界/`"17899"`/`"false"` 全覆盖），`http_port: 0` 保留「不起 HTTP 入口」语义；⑤ 所有 `int(cfg[...])` 换成 `_cfg_port()`（`cmd_status`/`cmd_doctor`/`cmd_up`/`_node_from_cfg`） | `TestMergeConfig`（11 条）、`TestDefaultConfigContract`（4 条）、`TestLoadConfig`（2 条）；另跑 P6 的 `tests/test_config_contract.py` 15 条全绿（它直接 import 我的 `merge_config`） |
| 5 | `probe` 退出码无意义：社区不通也 `exit 0`，CI 用不了；`summarize()` 只认「不通 · 待排查」 | `probe.py` 不归我，所以在 cli 侧新增 `probe_unusable(rows)`：直连可用→可用；`需节点 · X` 且无节点/节点也不通→不可用；`不通 · 待排查`/`节点也不通`→不可用。`summary` 保留原五键（`node/direct_ok/need_node/dead/ok`），`ok` 按新语义重算，只增 `unusable`；`cmd_probe` 返回 `1 if unusable else 0`；新增 `--lenient`（旧行为：只有 `dead` 才算失败）；末尾固定打印 `退出码 0 = 全部端点可用；1 = 有端点不可用（见 summary.unusable）` | `test_all_direct_ok_exit_zero`、`test_need_node_without_node_exit_one`、`test_need_node_with_working_node_exit_zero`、`test_dead_exit_one_both_modes`、`test_lenient_keeps_old_behaviour`、`test_summary_keeps_five_original_keys`、`test_probe_unusable_classification` |
| 6 | SNI 透明转发只有 `forward.py` 的实现，CLI 从不启动它；`hosts apply` 永远写最优 IP，与 SNI 模式互斥 | ① `_serve_async` 里 `await _start_sni(cfg, srv)`（`cmd_serve` 与 `up -b` 都走这条）：按 `cfg["cdn"]["enabled"]` 用 SPEC §4.2 的签名构造 `SniForwarder(srv.upstream, sni_port, allow_suffixes=…, stats=srv.stats, ip_map=cdnmod.best_ip_map(...))`，`OSError` → 只 `WARN` 返回 `None`，成功打印「SNI 透明转发 127.0.0.1:443 白名单 N 个域名」；`finally` 里先 `close()` 再关主服务；② `_hosts_write()`：`cdn.enabled` 为真时 entries 写成 `{"127.0.0.1": "<cdn.hosts 白名单>"}` 并打印「hosts 已指向 127.0.0.1，需 svpn up 常驻（SNI 模式）」，为假时保持原「按最优 IP 分组」行为；**两种模式都只调 `sysx.apply_hosts(entries, RUNTIME)`，绝不自己拼文本写 hosts 文件**，因此 `sanitize_entries`（合法 IP + 合法域名 + 拒绝主机名位置出现 IP）对 SNI 模式同样生效，被拒条目会逐条汇总成一行 `WARN`（成功条数以 `res["entries"]` 为准，不虚报）；SNI 模式不再要求 CDN 缓存（`cmd_hosts` 也不会白跑一轮联网测速）；③ `_print_route_summary` 增加 CDN 模式行（`SNI 透明转发:443` / `hosts 直指最优 IP`） | `TestSniWiring`（4 条：关闭→None / `OSError`→只警告且不 close / 成功→参数与白名单纯净 / 白名单空退回 `CDN_DOMAINS`）、`TestHostsWrite`（7 条：SNI 模式 entries 恰为 `{"127.0.0.1"}`、不经缓存也写、脏域名被 `sanitize_entries` 拦掉、`apply_hosts` 恰好被调一次且参数正确、被拒条目上报、IP 模式不受影响、写入失败上报）。另用一次性脚本起停真实 `SniForwarder`（动态端口）验证 `start()/close()` 无泄漏 |
| 附 | `_elevate()` 的 PowerShell 调用也是 `text=True`，中文 Windows + `PYTHONUTF8=1` 下 `UnicodeDecodeError` 会从提权路径抛出（`except` 只接 `OSError`/`TimeoutExpired`） | 同 `_decode` 家族：收字节、`logging.debug`、`except (OSError, subprocess.SubprocessError)` | 代码审阅 + `compileall`；无自动化用例（要真弹 UAC，不适合进测试） |

## 3. 验证结果

环境：`export PYTHONUTF8=1`，`PY=C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`，工作目录 `/d/LE/AI/dshwork/steam-vpn`。

- `compileall`：`python -m compileall -q svpn` → **exit 0**
- 新增测试：`tests/test_cli_contract.py` → **50 用例，OK，1.8s**（`-W error::RuntimeWarning` 下也干净，无未 await 协程泄漏）
- 基线回归（全部在改完后重跑）：
  - `tests/test_logic.py` → **50 OK**
  - `tests/test_netutil.py` → **57 OK**
  - `tests/test_regression.py` → **62 OK**
  - `tests/smoke_test.py` → **ALL PASS（17 项）**
- 交叉验证：`tests/test_config_contract.py`（P6）→ **15 OK**（直接消费我的 `merge_config` 与 12 键 `DEFAULT_CONFIG`）
- CLI 面：`python -m svpn --help` + 13 个子命令 `--help` → 全部 `rc=0`、**stderr 0 字节**
- 实跑抽查：`svpn status`（stderr 0 字节、`_note` 未进 cfg、`hosts`/`CDN缓存`/`统计` 正常）、`svpn pac`（PAC 正常生成）、`svpn up --port 7900 --no-auto` → `rc=1` + 单行提示

## 4. 契约变更 / 需要别人配合

1. **SPEC §4.1 的「13 个键」是笔误，实为 12 个**（`listen_host`…`direct_hosts`）。我按 dict 本体实现 12 键，`tests/test_cli_contract.py` 用「集合相等」断言（另断言 `len(SPEC_KEYS) == 12`），失败时能直接看出多/少了哪个键。**team-lead 已确认此为最终口径：`ip_map` 是 `_serve_cfg()` 运行时注入，不是配置键。** P6 已同口径（`config.example.json` = 12 键 + `_note`），其 15 条契约测试对我是全绿的。
2. **SPEC 里「12 个子命令」也是笔误，实为 13 个**（`init/up/_serve/down/status/probe/node/cdn/hosts/system/pac/steam/doctor`）。team-lead 已确认。测试按 13 条全解析 + `args.func` 可调用。
3. `merge_config(user: dict | None) -> dict` 是新增公开函数，`from svpn.cli import merge_config` 可直接导入（P6 `tests/test_config_contract.py` 就用这条路，15 条全绿）。语义：未知键/`_` 前缀键丢弃并 `logging.debug`，嵌套 dict 逐键合并，返回值一定是 `_normalize()` 过的完整配置。`test_merge_config_importable_directly` / `test_merge_config_signature` 钉住这条。
4. **P3-sni**：我按 `SniForwarder(up, listen_port=, allow_suffixes=, target_port=, stats=, max_head=, ip_map=)` + `async start()/close()` 调用，且只把 `allow_suffixes/stats/ip_map` 当关键字传。`tests/test_cli_contract.py::TestSniWiring` 用桩类把这套签名钉住了 —— **改签名会直接红**，请保持。
5. **P1-proxy**：`cmd_up` 的同端口现在在 cli 侧就 `return 1`，不会再走到 `ProxyServer.start()` 的 `ValueError`（那条路 `main()` 兜成 exit 2）。两处判定口径一致（`http_port` 为 0 时不算撞车）。
6. **给后续维护者的一条注意**：`proxy.py` / `build_pac` 仍会读 `cfg["proxy_suffixes"]` / `cfg["direct_suffixes"]`，但这两个键**不在** SPEC §4.1 的 12 键里，按「未知顶层键丢弃」的规则，**用户已经无法通过 config.json 自定义这两个名单**了（`Router` 会退回内置 `STEAM_SUFFIXES` / `CN_DIRECT_SUFFIXES`）。我按 SPEC 执行（P6 的 example 也没这两个键），但这属于功能面收缩，是否要在下一轮把两键正式纳入契约请 team-lead 定夺。
7. **hosts 落盘只有一条路**：`_hosts_write()` 的 SNI 模式与最优 IP 模式都只调 `sysx.apply_hosts(entries, RUNTIME)`，条目必须过 `sanitize_entries`；`test_sni_mode_uses_apply_hosts_not_raw_write` 断言 `apply_hosts` 恰好被调一次，`test_sni_entries_pass_sanitizer` 用真 `sanitize_entries` 验脏域名被剔除，谁绕过它直接写 hosts 文件这两条就红。

## 5. 没做完 / 发现的其它问题

1. **`probe.py:summarize()` 的 `ok` 仍是 `not dead`**，与 SPEC §4.3「`ok = not unusable`」不一致。该文件不归我，我在 `cli.py` 里重算了 `ok` 并只增 `unusable`（原五键保留），所以 `--json` 与退出码语义已经一致；但**直接 import `probe.summarize()` 的调用方（未来若有）拿到的还是旧语义**，建议下一轮统一到 `probe.py`。
2. `cmd_up` 的 `asyncio.run(_serve_async(...))` 是在**预检全部通过之后**才求值的（`return 1` / `return 2` 的早退路径都在它之前），所以不存在「协程创建了却没 await」的 `RuntimeWarning`；新测试用 `-W error::RuntimeWarning` 跑过一遍确认了这点。
3. `_port_busy()` 用裸 `bind` 判占用，在 Windows 上若对方用 `SO_REUSEADDR` 偷绑，可能判不出口径（经典 Windows 历史包袱）。本轮只做「同端口 + SNI 端口」的预检，没有改 `_port_busy` 语义（属 proxy 侧，且改了会影响基线）。实测 `sni_port=1`（特权端口）在本机非管理员也能绑成功，说明 Windows 侧预检查不出特权限定，只能靠 `SniForwarder.start()` 的 `OSError` 兜底 —— 这两条路径都已覆盖（前者警告放行，后者只警告跳过）。
4. `cmd_hosts` 的提权分支（非管理员 → UAC）无自动化覆盖（会弹窗），本轮只做了代码审阅。
5. `SVPN_DEBUG=1` 时，`load_config()` 里那几条 `logging.debug` 在 `setup_log()` 之前执行，实际不会打出来（`logging` 用 lastResort，只放行 WARNING+）。这符合 SPEC「用 `logging.debug` 记一条」，但**DEBUG 模式下也看不到脏键丢弃记录**；若要可诊断，需要在 `load_config` 里检测 `SVPN_DEBUG` 并临时 `basicConfig` —— 那会污染全局 logging（测试里不好收拾），故本轮没做。
