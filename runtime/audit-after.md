# svpn 静态审计（修复后 · 只读复跑）

**审计人**：D2-audit（本轮除本文件外未写任何仓库文件）
**审计对象**：`svpn/*.py` 全 14 个模块 + `install.ps1`/`uninstall.ps1`/`svpnctl.ps1` + `svpn.cmd`/`start-svpn.cmd` + `config*.json`
**审计方式**：自写 `ast` 脚本（未定义名 / 关键字参数 / argparse dest 对照）+ 字节级编码检测 + `-X dev -W error::ResourceWarning` 全命令扫描 + 纯内存 fake 对象验证异常边界（**没有**启动 `svpn up`、**没有**写 hosts/注册表、**没有**改系统代理、**没有**联网）
**对比基准**：`runtime/review.md`（修复前，指纹 03:2x 版）

环境：`cd /d/LE/AI/dshwork/steam-vpn && export PYTHONUTF8=1`，`PY=C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`

## ⚠ 审计期间的并发改动（读结论前必看）

本审计从 18:47 跑到 19:05，期间**有队友在并发改代码**，被审对象挪了三次。所有结论都已对**最终快照**重跑过：

| 文件 | 18:47 首轮 | 中途 | **最终快照（结论以此为准）** |
|---|---|---|---|
| svpn/cli.py | 51669 / 1270 行 | 51966 → 52928 | **52928 / 1287 行 / `4f90ea52dbe4` / 18:56:12** |
| svpn/discover.py | 9291 / 264 行 | — | **10834 / 305 行 / `f1b6b4cc8a8d` / 18:55:45** |
| svpn.cmd | 1666 / 58 行 | — | **1696 / 59 行 / `5cffd996bdf8`** |
| `_probe_proxy.py` | 存在（2611B） | — | **已删除**（不计入下文文件数，故编码审计从 39 个变 38 个） |

这轮并发改动**恰好闭合了 4 项**本来要被我记成"遗留"的问题：`discover` 自环（P1-4）、`cmd_down` 谎报还原（P1-6）、`DEFAULT_CONFIG.browser.bypass` 缺段（N-1）、`cmd_up` 端口回退绕过常量（N-2 的一半）。下表把它们标成「**审计期间闭合**」，并保留复核证据——这样你既知道它现在是好的，也知道它是谁在什么时候修好的。

> 剩余文件（proxy / netutil / system / rules / forward / pac / probe / cdn / steam / split）自 18:26–18:40 后再未变动，行号稳定。

---

## 结论

`review.md` 记的 **P0 × 2、P1 × 8、P2 × 21** 复跑结果：**P0 2/2 全闭合**、P1 **3 已修 / 2 部分 / 3 遗留**、P2 **5 已修 / 3 部分 / 13 遗留**；本轮另记 **6 项 review 未列的问题**（1 项已在审计期间被闭合、5 项仍在）。合计 **20 项遗留 + 6 项部分闭合**，其中**最高级别是 P1**（`proxy.py` 的 IPv6 上游 ATYP、SOCKS5 握手段超时、`up -b` 的 PIDFILE 竞态、`_DNS_CACHE` 无上限）——**没有 P0，不阻断发布**。

静态面全绿：未定义名 **0**、关键字参数不匹配 **0**、13 个子命令 `args.X` 与 `add_argument` **13/13 对齐**（`cmd_steam` 手工 Namespace 覆盖 `cmd_up` 需 6/6）、编码 **38/38 合规**、`-X dev` 下 **14 次 `--help` + 3 次真执行 stderr 全 0 字节**、`-W error` 逐模块 import **12/12 无告警**、裸 `except:` **0 处**、`compileall` exit 0、基线 50/57/62 + smoke ALL PASS、`test_forward` 16 用例在 `-X dev` 下全绿。

**发布建议**：可以发，但发行说明里应点名 3 条"用户能踩到但代码不报错"的行为——① `mode=all/blacklist` 下 IPv6 目标会被当域名发给上游（v6-only 站点必挂）；② 前台 `svpn up` 不写 PIDFILE，`status` 会显示"未运行"、`down` 停不掉；③ 笔记本睡醒/半开连接要等 TCP 超时才回收（`return_exceptions=True` 的既定取舍）。

---

## 0. 文件指纹（最终快照）

| 文件 | 字节 | sha256[:12] | 行数 | mtime |
|---|---|---|---|---|
| cli.py | 52928 | 4f90ea52dbe4 | 1287 | 18:56:12 |
| proxy.py | 27796 | 039855af689e | 710 | 18:40:01 |
| netutil.py | 27560 | 9294ad85777b | 737 | 18:31:14 |
| discover.py | 10834 | f1b6b4cc8a8d | 305 | 18:55:45 |
| forward.py | 10000 | e88e9ad07f51 | 235 | 18:26:49 |
| probe.py | 7829 | cc6d5184a88f | 213 | 18:39:30 |
| pac.py | 2533 | 499e7dddeae7 | 65 | 18:39:25 |
| system.py | 9670 | b0613eadca64 | 285 | 03:37:34 |
| rules.py | 6668 | c8aa274a2441 | 191 | 03:31:59 |
| cdn.py | 6050 | 42c2438680a9 | 171 | 03:12:35 |
| split.py | 2359 | 436f1b6837ef | 63 | 02:59:42 |
| steam.py | 3425 | ffc1c5f69fdf | 93 | 03:17:07 |

> 若你手上的 `cli.py` sha256 前 12 位不是 `4f90ea52dbe4`，行号会漂——下文每条引用都带了紧跟的代码片段，按片段 `grep` 即可定位。

复跑：`for f in svpn/*.py; do printf "%-18s %7s %s\n" $f $(stat -c%s $f) $(sha256sum $f|cut -c1-12); done`

---

## 1. AST 扫描

工具：自写 `ast` 脚本（标准库 only）。**先做自检**：把同一脚本指向一个故意写坏的样本目录，确认它能报出模块级未定义名、类方法里的未定义名、以及错关键字参数——排除"扫了个寂寞"的假阴性。

自检输出：
```
== 未定义名 ==
bad.py:11  undefined_thing  scope_depth=1
bad.py:18  OTHER  scope_depth=2
== 关键字参数与形参不匹配 ==
bad.py:14  ok(...['zzz'])  实际形参=['a']
```

对真实代码的结果（最终快照重跑）：

| 问题 | 位置 | 级别 | 状态 |
|---|---|---|---|
| 未定义名（`NameError` 风险） | 14 个模块，**0 处** | — | 已修 |
| `ast.Call` 关键字参数与目标形参不匹配 | **0 处** | — | 已修 |
| `argparse` 属性未注册 | **0 处**（13/13 子命令对齐） | — | 已修 |
| `cmd_steam` 手工 `Namespace` 缺字段 | **0 处**（覆盖 `cmd_up` 需要的 6 个） | — | 已修 |
| `cmd_down` → `cmd_hosts` 手工 `Namespace` 缺字段 | **0 处**（3/3） | — | 已修 |
| 生产路径无调用、仅测试引用的包装函数 | `cli.py:703 probe_unusable` | P3 | 新增（见 §4） |

逐子命令对照（`cmd_*` 读的 `args.X` vs `add_parser` 注册的 dest）：

| 子命令 | 注册的 dest | `cmd_*` 读的 | 判定 |
|---|---|---|---|
| init | force | force | OK |
| up | background, mode, no_auto, node, port, system_proxy | 同左 6 个 | OK |
| _serve | system_proxy | system_proxy | OK |
| down | clear_hosts, keep_system | 同左 | OK |
| status | （无） | （无） | OK |
| probe | json, lenient, node, timeout | 同左 | OK |
| node | action, json, url | 同左 | OK |
| cdn | domain, json, no_bench, refresh, timeout | 同左 | OK |
| hosts | action, no_elevate, timeout | 同左 | OK |
| system | action | action | OK |
| pac | apply, write | 同左 | OK |
| steam | args, exe, no_autostart, restart, system | 同左 | OK |
| doctor | （无） | （无） | OK |

`cmd_steam` 的手工 `Namespace`（`cli.py:1067-1068`）实测：
```
Namespace 提供: ['action','background','mode','no_auto','no_elevate','node','port','system_proxy','timeout']
cmd_up 需要   : ['background','mode','no_auto','node','port','system_proxy']
缺失: 无
```

**语义提醒（不是缺陷）**：`steam` 用 `nargs=argparse.REMAINDER`（`cli.py:1254`），所以 `svpn steam --args x --restart` 里的 `--restart` 会被 `--args` 吃掉，必须写在 `--args` **前面**。`--help` 看不出这一点，文档举例要避开这个顺序。

---

## 2. 编码审计

口径：`.ps1` = UTF-8 **带** BOM + 纯 CRLF；`.cmd` = **无** BOM + 纯 CRLF；`.py`/`.json`/`.toml` = UTF-8 **无** BOM。

| 文件 | 期望 | 实际（BOM / CRLF / 裸LF / utf8） | 判定 |
|---|---|---|---|
| install.ps1 | UTF-8+BOM, CRLF | Y / 151 / 0 / Y | 通过 |
| uninstall.ps1 | UTF-8+BOM, CRLF | Y / 374 / 0 / Y | 通过 |
| svpnctl.ps1 | UTF-8+BOM, CRLF | Y / 182 / 0 / Y | 通过 |
| svpn.cmd | 无 BOM, CRLF | N / 58 / 0 / Y | 通过 |
| start-svpn.cmd | 无 BOM, CRLF | N / 106 / 0 / Y | 通过 |
| svpn/__init__.py | UTF-8 无 BOM | N / 0 / 2 / Y | 通过 |
| svpn/__main__.py | 同上 | N / 0 / 4 / Y | 通过 |
| svpn/cdn.py | 同上 | N / 0 / 171 / Y | 通过 |
| svpn/cli.py | 同上 | N / 0 / 1287 / Y | 通过 |
| svpn/discover.py | 同上 | N / 0 / 305 / Y | 通过 |
| svpn/forward.py | 同上 | N / 0 / 235 / Y | 通过 |
| svpn/netutil.py | 同上 | N / 0 / 737 / Y | 通过 |
| svpn/pac.py | 同上 | N / 0 / 65 / Y | 通过 |
| svpn/probe.py | 同上 | N / 0 / 213 / Y | 通过 |
| svpn/proxy.py | 同上 | N / 0 / 710 / Y | 通过 |
| svpn/rules.py | 同上 | N / 0 / 191 / Y | 通过 |
| svpn/split.py | 同上 | N / 0 / 63 / Y | 通过 |
| svpn/steam.py | 同上 | N / 0 / 93 / Y | 通过 |
| svpn/system.py | 同上 | N / 0 / 285 / Y | 通过 |
| tests/domain_audit.py | 同上 | N / 0 / 486 / Y | 通过 |
| tests/e2e_proxy.py | 同上 | N / 0 / 425 / Y | 通过 |
| tests/live_e2e.py | 同上 | N / 0 / 287 / Y | 通过 |
| tests/live_test.py | 同上 | N / 0 / 101 / Y | 通过 |
| tests/smoke_test.py | 同上 | N / 0 / 215 / Y | 通过 |
| tests/test_cli_contract.py | 同上 | N / 0 / 619 / Y | 通过 |
| tests/test_config_contract.py | 同上 | N / 0 / 255 / Y | 通过 |
| tests/test_forward.py | 同上 | N / 0 / 294 / Y | 通过 |
| tests/test_fuzz.py | 同上 | N / 0 / 507 / Y | 通过 |
| tests/test_integration_local.py | 同上 | N / 0 / 715 / Y | 通过 |
| tests/test_logic.py | 同上 | N / 0 / 485 / Y | 通过 |
| tests/test_netutil.py | 同上 | N / 0 / 751 / Y | 通过 |
| tests/test_netutil_extra.py | 同上 | N / 0 / 470 / Y | 通过 |
| tests/test_pac.py | 同上 | N / 0 / 321 / Y | 通过 |
| tests/test_proxy_hardening.py | 同上 | N / 0 / 347 / Y | 通过 |
| tests/test_regression.py | 同上 | N / 0 / 651 / Y | 通过 |
| config.example.json | UTF-8 无 BOM | N / 0 / 42 / Y | 通过 |
| config.json | UTF-8 无 BOM | N / 41 / 0 / Y | 通过 |
| pyproject.toml | UTF-8 无 BOM | N / 0 / 27 / Y | 通过 |

**38 个文件，0 异常**（比首轮的 39 个少 1 个，是 `_probe_proxy.py` 在审计期间被删除，不是漏检）。修复前 `install.ps1` 缺 BOM、`start-svpn.cmd`/`uninstall.ps1` 缺失的问题已闭合。

编码引出的**运行时**风险另外查过：
- 两个 `.cmd` 都含中文，但都在第 2 行 `chcp 65001 >nul`，注释行纯 ASCII（`svpn.cmd:20-22`、`start-svpn.cmd:13-20` 都写了原因），中文只出现在 `echo` 行并后跟空行吸收偏移。
- `call` 已覆盖所有 `%PY%` 调用点（`svpn.cmd:36/38/49/54`、`start-svpn.cmd:36/39/57/63/83`），`.bat` shim 吞控制权的坑已处理。
- `config.json` 是 CRLF、`config.example.json` 是 LF——都不违规（JSON 无换行要求），只是 diff 会难看。

复跑：
```bash
python -c "import pathlib;b=b'\xef\xbb\xbf';[print(p.name,p.read_bytes()[:3]==b,p.read_bytes().count(b'\r\n'),p.read_bytes().count(b'\n')) for p in list(pathlib.Path('.').glob('*.ps1'))+list(pathlib.Path('.').glob('*.cmd'))]"
```

---

## 3. `-X dev` 告警扫描

命令：`"$PY" -X dev -W error::ResourceWarning -m svpn <cmd> --help`，统计 stderr 字节数。

| # | 命令 | exit | stderr |
|---|---|---|---|
| 1 | `-m svpn --help`（顶层） | 0 | **0 字节** |
| 2 | `init --help` | 0 | **0 字节** |
| 3 | `up --help` | 0 | **0 字节** |
| 4 | `_serve --help` | 0 | **0 字节** |
| 5 | `down --help` | 0 | **0 字节** |
| 6 | `status --help` | 0 | **0 字节** |
| 7 | `probe --help` | 0 | **0 字节** |
| 8 | `node --help` | 0 | **0 字节** |
| 9 | `cdn --help` | 0 | **0 字节** |
| 10 | `hosts --help` | 0 | **0 字节** |
| 11 | `system --help` | 0 | **0 字节** |
| 12 | `pac --help` | 0 | **0 字节** |
| 13 | `steam --help` | 0 | **0 字节** |
| 14 | `doctor --help` | 0 | **0 字节** |

> 口径订正：`SPEC-WAVE2.md` 写「12 个子命令 + status/doctor」，`build_parser()`（`cli.py:1181`）实际注册 **13 个**（含内部 `_serve`）。上表 14 行 = 顶层 1 + 13 子命令，全覆蓋。

另外 3 个**真执行**（只读、不写盘、不联网、不动注册表）：

| 命令 | exit | stderr | 走到的真实路径 |
|---|---|---|---|
| `pac` | 0 | **0 字节** | `build_pac` 全路径 + 中文排版 |
| `system show` | 0 | **0 字节** | 读 WinINET 四个值 |
| `status` | 0 | **0 字节** | pid / 端口占用探测 / CDN 缓存 / hosts 解析 |

再收紧到 `-W error` 逐个 `import` 全部 12 个可导入模块（`cli proxy netutil forward probe cdn rules system discover pac steam split`）→ **12/12 无任何 Warning**；`python -m compileall -q svpn` → exit 0。

**原始 stderr：全部 0 字节**（无 `ResourceWarning`、无 `DeprecationWarning`、无 `Task was destroyed but it is pending`）。

---

## 4. 死代码

口径：`svpn/*.py` 顶层函数/类，在包内既无 `import`、无 `getattr(x,"name")`、无属性/裸名引用（`tests/` 的引用单独标注）。

| # | 符号 | 位置 | 被谁引用 | 级别 | 状态 |
|---|---|---|---|---|---|
| 1 | `Forwarder` | `forward.py:19` | 无（只有 `SniForwarder` 被实例化：`cli.py:584/587`） | P3 | 遗留（删或注明"预留"） |
| 2 | `discover.steam_client_running` | `discover.py:296` | 无 | P3 | 遗留（内部还是 `text=True`） |
| 3 | `system.set_user_env` / `clear_user_env` | `system.py:120` / `:128` | 无 | P3 | 遗留（后者仍 `KEY_ALL_ACCESS`） |
| 4 | `split.add_routes` / `del_routes` / `list_vpn_adapters` | `split.py:24/37/51` | 无（整文件无人 import） | — | **如实记录：`split.py` 仍是未接线**，与 `docs/ARCHITECTURE.md:364` 一致 |
| 5 | `cli.probe_unusable` | `cli.py:703` | 仅 `tests/test_cli_contract.py:330/342` | P3 | 新增（生产路径未调用） |

确认项：

| 检查 | 结论 | 证据 |
|---|---|---|
| `forward._need` 已删 | **是** | `grep -n "_need" svpn/forward.py` → 无输出 |
| `SniForwarder` 已被 cli 引用 | **是** | `cli.py:584`（局部 import）、`cli.py:587`（构造） |
| 方法级死代码 | **无** | 全包类方法扫描，全部有调用点 |
| `discover` 新增的 `self_ports`/`is_self` 有消费点 | **是** | `discover.py:250`（scan 过滤）、`cli.py:785`（node set 拒绝） |

---

## 5. 异常处理

**裸 `except:`（无类型）= 0 处** ✅

`except Exception:`（含 `except (..., Exception)`）共 **23 处**，全部可见如下（均为"兜底不许穿到 asyncio / 不许把 CLI 打死"的关口，且都带 `# noqa: BLE001`）：

| # | 位置 | 所在函数 | 为什么可以接受 |
|---|---|---|---|
| 1 | `proxy.py:461` | `handle_socks` | `client_connected_cb` 入口兜底，防 `Unhandled exception` |
| 2 | `proxy.py:543` | `handle_http` | 同上（修复前缺失 → 原 P0-2） |
| 3 | `proxy.py:519` | `_socks_session` | 单次拨号失败 → 回 `0x04/0x05` 并计数 |
| 4 | `proxy.py:533` | `_socks_session` | 会话级兜底 |
| 5 | `proxy.py:596` | `_http_session` | CONNECT 拨号失败 → 502 |
| 6 | `proxy.py:630` | `_http_session` | 绝对 URI 拨号失败 → 502 |
| 7 | `forward.py:38` | `Forwarder._handle` | 拨号失败即关 client（该类是死代码） |
| 8 | `forward.py:208` | `SniForwarder._handle` | SNI 解析/拨号任何失败都不许穿出 |
| 9 | `netutil.py:157` | `resolve` | 单个 DoH provider 失败只跳过 |
| 10 | `netutil.py:222` | `resolve_merged` | 承诺"绝不抛" |
| 11 | `netutil.py:226` | `resolve_merged` | 同上（系统解析那一侧） |
| 12 | `netutil.py:248` | `_close_quietly` | 清理路径不许再抛 |
| 13 | `netutil.py:324` | `tls_probe` | 证书解析只是附加信息 |
| 14 | `netutil.py:730` | `download_bench` | 测速失败返回 `(0, 0.0, ErrName)` |
| 15 | `probe.py:72` | `_direct_probe` | HEAD 状态码拿不到就记 0 |
| 16 | `probe.py:105` | `_node_probe` | 节点拨号失败转 `{"ok":False}` |
| 17 | `probe.py:117` | `_node_probe` | TLS/HEAD 失败同上 |
| 18 | `cdn.py:90` | `_bench` | 单 IP 测速失败记 `err` |
| 19 | `discover.py:188` | `verify` | 单候选验证失败记 `err` |
| 20 | `steam.py:42` | `find_steam` | 注册表读取失败降级到路径列表 |
| 21 | `system.py:66` | `_notify_change` | `InternetSetOptionW` 失败不影响已写入的值 |
| 22 | `cli.py:875` | `_is_admin` | `ctypes` 不可用时当非管理员 |
| 23 | `cli.py:1269` | `main` | `SetConsoleOutputCP` 失败照常跑 |

**没有 `except BaseException`**；`asyncio.CancelledError` 不被 `PIPE_SILENT`（`proxy.py:308-313`）捕获（它是 `BaseException`），`_bridge` 在 finally 里只吞收尾那一次 `gather` 的 `CancelledError`，随后原异常继续上抛——正确。

---

## 6. 配置一致性（实测输出）

### 6.1 `DEFAULT_CONFIG` vs `config.example.json` / `config.json`

```
merge(example) == merge({}) ? True
merge(config.json) == merge({}) ? True
DEFAULT bypass : <local>;localhost;127.*;10.*;172.16.*;…;172.31.*;192.168.*;*.cn
connect_timeout -1 -> -1
```

- 键集（12）、类型、**值**现在三者完全一致 ✅（首轮 `bypass` 少了 `172.17.*`–`172.31.*`，是审计期间被并发改动闭合的：现在 `cli.py:57-60` 用 `";".join(f"172.{n}.*" for n in range(16, 32))` 生成）。
- 唯一残留：`connect_timeout: -1` 不会被归一（`cli.py:168-169` 的 `_as_number` 只挡 `None`/非数字/NaN/inf）。运行期 `proxy._as_timeout` 会再救一次（回落 12.0），所以不炸，但配置里留了个非法值。P3。

### 6.2 端口 7899 / 7900 的硬编码点

| 位置 | 内容 | 是否"第二事实来源" |
|---|---|---|
| `cli.py:49-50` | `DEFAULT_CONFIG` 定义 | 准（权威） |
| `cli.py:467` | `_cfg_port(..., DEFAULT_CONFIG["listen_port"])` | ~~是~~ → **已改引用，闭合** |
| `cli.py:516` | `_cfg_port(cfg.get("listen_port"), 7899)` | **是**（`up -b` 就绪判定） |
| `cli.py:668` | `cmd_status` 同款回退 | **是** |
| `cli.py:1112` | `cmd_doctor` 同款回退 | **是** |
| `proxy.py:30-31` | `DEFAULT_LISTEN_PORT/DEFAULT_HTTP_PORT` | **是**（另一套常量） |
| `pac.py:23` | `build_pac(socks_port: int = 7899)` 默认参数 | **是** |
| `discover.py:44-45` | `COMMON_PORTS` 的"svpn 自身"两条 | 是（语义不同，保留） |
| `discover.py:48` | `SELF_PORTS = {7899, 7900}` | 是（被 `self_ports()` 扩展） |
| `discover.py:287-288` | `save_upstream` 的 `setdefault` | **是** |
| `config.example.json:3-4`、`config.json:3-4` | 配置值 | 数据，正常 |
| `start-svpn.cmd:97` | 报错文案 | 文案 |

结论：仍有 **5 处可漂移的代码级重复**（`cli.py:516/668/1112`、`proxy.py:30-31`、`pac.py:23`、`discover.py:287-288`；其中前三条建议统一改引用 `DEFAULT_CONFIG["listen_port"]`）。

### 6.3 CDN 域名列表来源

```
cdn.py::CDN_DOMAINS       8 个（唯一代码来源）
config.example.json  cdn.hosts == CDN_DOMAINS ? True（顺序一致）
config.json          cdn.hosts == CDN_DOMAINS ? True
DEFAULT_CONFIG       cdn.hosts == CDN_DOMAINS ? True
rules.STEAM_SUFFIXES 与 CDN_DOMAINS 重叠 7 个（语义不同：路由后缀 vs 选边名单，允许）
```

代码里**只有一个来源** ✅：`cli.py:60 / 573-576 / 806 / 924` 全部引 `cdnmod.CDN_DOMAINS`，没有第二份字面量。但镜像有 3 份（`DEFAULT_CONFIG` 内联 + 2 个 JSON），且 `tests/test_config_contract.py:179/186/205` 三条断言把它们焊死——改 `cdn.py` 而不改 JSON 会立刻红。这是"可接受的冗余"，代价是改一个域名要动 **5 个地方**。

### 6.4 占位符名单：3 处重复，且 1 处不同

```
cli.py:42      ("NODE_HOST","NODE","HOST","EXAMPLE.COM","CHANGE_ME","")   ← 多一个 ""
discover.py:84 ("NODE_HOST","NODE","HOST","EXAMPLE.COM","CHANGE_ME")
proxy.py:93    ("NODE_HOST","NODE","HOST","EXAMPLE.COM","CHANGE_ME")
```

`cli` 版多出来的 `""` 是判"没配节点"用的（语义不同），但三处字面量没有共享常量，将来加一个占位符要改三处。P3。

### 6.5 脏配置归一（`merge_config` 实测，均不抛 `TypeError`）

| 输入 | 结果 | 判定 |
|---|---|---|
| `listen_port: None / "" / "abc" / 70000` | 全部 → 7899 | ✅ |
| `http_port: 0` → **0**（合法，表示不起 HTTP）；`None` → 7900 | ✅ 语义保留 |
| `connect_timeout: None` → 12；`-1` → **-1** | ⚠️ P3（见 6.1） |
| `upstream.port: None / "x"` → 1080 | ✅ |
| `cdn.refresh_hours: None` → 6；`cdn.sni_port: 0` → 443 | ✅ |
| `cdn.enabled: "yes"` → `True` | ✅ |
| `upstream.kind: "socks4"` → `"socks5"`；`mode: "WHITELIST"` → `"whitelist"` | ✅ |
| `{_note, bogus_key}` → 全丢弃，仍 12 键 | ✅ |

### 6.6 PAC 生成物（P0 回归的实测证据）

`build_pac()` 四种样本经 node 22 解析：

```
pac_whitelist    SyntaxOK
pac_all          SyntaxOK
pac_blacklist    SyntaxOK
pac_inject       SyntaxOK     ← proxy_suffixes 塞 ev"il.com / a\b.com 仍是合法 JS
```

语义抽查（`FindProxyForURL`）：

| host | whitelist | blacklist |
|---|---|---|
| store.steampowered.com | `SOCKS5 127.0.0.1:7899; DIRECT` | 同左 |
| steamcommunity.com | SOCKS…; DIRECT | 同左 |
| avatars.steamstatic.com | SOCKS…; DIRECT | 同左 |
| www.baidu.com / www.qq.com | DIRECT | DIRECT |
| example.org | DIRECT | **SOCKS…; DIRECT** |
| localhost / 127.0.0.1 | DIRECT | DIRECT |

---

## 7. 与 review.md 的差异

图例：**已修** / **部分** / **遗留** / **审计期间闭合** / **新增**

### 7.1 P0（2 项）→ 2/2 已修

| # | 原问题 | 位置（修复后） | 状态 | 证据 |
|---|---|---|---|---|
| P0-1 | PAC 是非法 JS（`var PROXY = { … }` 对象字面量），且 `ProxyEnable` 被置 0 → 整机不走代理 | `pac.py:42-47`（数组字面量）、`pac.py:13-20`（`json.dumps` 转义） | **已修** | node 解析 4/4 `SyntaxOK`；语义表见 §6.6；`runtime/svpn.pac` 也是数组 |
| P0-2 | `pipe` 漏捕裸 `OSError`（WinError 121）；`handle_http` 无外层 try/finally | `proxy.py:308-313`（`PIPE_SILENT` 含 `OSError`）、`proxy.py:538-547`、`proxy.py:456-465` | **已修** | 内存 fake 实测 5 类异常全静默、`CancelledError` 正确上抛、`_bridge` 两侧都炸时不外抛且两侧 writer 都关、取消后残留 task = 0；`handle_http` 8 组畸形输入全部 400 或静默 |

`bridge_check.py` 实测摘录（`-X dev` 下跑，stderr 0）：
```
pipe(OSError / ConnectionResetError / TimeoutError / IncompleteReadError / RuntimeError): 全部未外抛
pipe: CancelledError 照常上抛
_bridge 两侧都炸: 未外抛   stats up=10 down=20 cw.closed=True sw.closed=True
_bridge gather 期间被取消: 上抛 CancelledError，finally 已跑完，残留 task 数 0
```

### 7.2 P1（8 项）→ 3 已修（含 2 项审计期间闭合）/ 2 部分 / 3 遗留

| # | 原问题 | 状态 | 现状（行号）+ 复现 |
|---|---|---|---|
| P1-1 | `int(p or 443)` 端口 `ValueError` 穿出 | **已修** | `proxy.py:584-591`、`:615-622`。实测 `CONNECT host:abc` / `:70000` / 无端口 / `http://a:b:c/` 全部 `HTTP/1.1 400 Bad Request` |
| P1-2 | `all(c.isdigit() or c==".")` 把 IPv6 当域名发上游 | **遗留** | `proxy.py:240` 未改。`python -c "h='2606:4700::1111';print(bool(h) and all(c.isdigit() or c=='.' for c in h))"` → `False`（走 ATYP=3）；`netutil.is_ip('2606:4700::1111')` → `True`。**触发面收窄**：whitelist 下 v6 目标一律 `direct`（`rules.py:173-192`），只有 `mode=all/blacklist` 或 `proxy_hosts` 命中时才走上游 |
| P1-3 | `_DNS_CACHE` 无上限、无负缓存、无总 deadline | **遗留** | `netutil.py:76` 裸 dict，`:166/:179` 只写不淘汰。`grep -n "_DNS_CACHE" svpn/netutil.py` → 4 行（定义+1 读+2 写），无淘汰代码 |
| P1-4 | `discover` 不排除自身端口 → 自环到 fd 耗尽 | **审计期间闭合** | `discover.py:201-237` 新增 `self_ports()`/`is_self()`（把 `listen_port`/`http_port`/`cdn.sni_port` 一并算进自身端口），`scan():250` 统一过滤，`cli.py:785` 在 `node set` 里显式拒绝。实测：`127.0.0.1:7900`/`127.0.0.1:7899`/`localhost:443`/`127.0.0.1:17899`（改过端口）都判 `self=True`，`127.0.0.1:7890`、`192.168.1.2:7900` 判 `False` |
| P1-5 | 提权写 hosts 前不校验（可注入任意行） | **部分** | 校验已补：`system.py:185-220 sanitize_entries` + `apply_hosts:246-252` 整块拒写。实测 review 的注入样本 `{"5.6.7.8":"a.com\n127.0.0.1 login.steampowered.com"}` → `clean={'5.6.7.8':'a.com login.steampowered.com'}`、`rejected=['127.0.0.1']`（多出的那一行没了）。**原子写仍缺**：`system.py:180-182` 还是 `HOSTS.write_bytes(...)` 直接覆盖 |
| P1-6 | `cmd_down` 无备份时仍打印"系统代理已还原" | **审计期间闭合** | `cli.py:637-646` 现在判 `res.get("ok") is False` → 打 `[!] 系统代理没动过：no backup（可能从没接管过）` + 回显当前 `ProxyEnable/ProxyServer`，不再谎报 |
| P1-7 | `up -b` 的 PIDFILE 先写后删 / 就绪判据只看端口 | **遗留** | `cli.py:509`（先写 PIDFILE）、`:511-518`（只判 `pid_alive` + `_port_busy`）、`:522`（失败 unlink）。仍无"子进程自写 pid / magic 握手" |
| P1-8 | SOCKS5 握手无整段超时、无并发上限 | **部分** | 首包 10s（`proxy.py:471`）、4 字节头 10s（`:498`）；`methods`（`:474`）、认证（`:486-490`）、地址/端口（`:503-513`）仍是裸 `readexactly`；全包无 `Semaphore`。`sed -n '469,515p' svpn/proxy.py \| grep -c wait_for` → **2**（15 处读操作里 2 处有超时） |

### 7.3 P2（21 项）→ 5 已修 / 3 部分 / 13 遗留

| # | 原问题 | 状态 | 现状 |
|---|---|---|---|
| P2-1 | `_race_connect` 多成功只留一个、其余 transport 泄漏 | **已修** | `proxy.py:158-179` + `_close_result():185-198`，非赢家全部 `close()` |
| P2-2 | 只校验 `head[1]`，不校验 `head[0]==5` | **遗留** | `proxy.py:251-253`：只有 `if head[1] != 0x00` |
| P2-3 | `_bridge` 无 idle timeout（半开连接永久占 fd） | **遗留（取舍）** | `proxy.py:337-368` 无超时；`P1-proxy.md:56` 与 SPEC-WAVE2「已知限制」均已如实记录 |
| P2-4 | `close()` 不清空 `_servers` | **已修** | `proxy.py:707`：`servers, self._servers = self._servers, []` |
| P2-5 | 结果只竞速前 4 个，系统 DNS 可能挤掉 DoH | **遗留** | `proxy.py:150` `ips[:4]`；`netutil.resolve_merged:228` 仍是 `dict.fromkeys(doh + sys)` 拼接，无分组交替 |
| P2-6 | DoH 查询串未 URL 编码 | **遗留** | `netutil.py:87`：`f"{url}{sep}name={name}&type={rtype}"`，无 `quote()` |
| P2-7 | `parse_node_url` 无端口范围 / 裸 IPv6 误解析 | **已修** | `netutil.py:481-482`、`_split_bare():486-499`。实测 `:70000`→`端口超出范围`、`::1:1080`→`裸 IPv6 有歧义…`、`[::1]:1080`→OK |
| P2-8 | `http_connect` 丢掉 `\r\n\r\n` 之后的残留字节 | **遗留** | `netutil.py:567-579` 仍 `return sock` |
| P2-9 | `rules` 漏 CGNAT 等非 global 地址 | **遗留（范围缩小）** | `rules.py:180`。实测 `Router(mode='blacklist').decide('100.64.0.1')` → **`upstream`**；`198.18.0.1`/`192.0.0.1` 在 3.13 里 `is_private=True` 已能挡住。**只剩 `100.64.0.0/10`** |
| P2-10 | `proxy_cidrs` 支持存在但 `ProxyServer` 不传 | **遗留** | `proxy.py:388-394` 无 `proxy_cidrs`；`grep -c proxy_cidrs config.example.json` → `0` |
| P2-11 | `pid_alive` 用子串匹配、不校验镜像名 | **部分** | `cli.py:296` 起已按列精确匹配（`want in parts[1:3]`）；实测 `pid_alive(1)/(0)/('abc')/(None)/(999999)` 全 `False`，自身 pid `True`。**仍无镜像名校验** → PID 复用 + 残留 PIDFILE 时 `taskkill /T /F` 仍可能误杀 |
| P2-12 | `subprocess(text=True)` 严格解码 | **部分** | 活路径已改 `_decode(mbcs)`（`cli.py:269-292`）；残留 3 处 `text=True` **全在死代码**：`discover.py:301`、`system.py:124`、`split.py:19` |
| P2-13 | `_elevate` 单引号拼参（路径含 `'` 就断） | **遗留** | `cli.py:879-887`：`quoted = ",".join(f"'{a}'" ...)`、`-WorkingDirectory '{ROOT}'` |
| P2-14 | 日志 `FileHandler` 无轮转 | **遗留** | `cli.py:85` |
| P2-15 | 前台 `up` 不写 PIDFILE | **遗留** | `grep -n "PIDFILE.write_text" svpn/cli.py` → 只有 `:509`（后台分支）；`_serve_async`（`cli.py:538`）不写不删 |
| P2-16 | `cdn.enabled`/`sni_port`/`hosts` 三键没代码读（假开关） | **已修** | `cli.py:573-596`（`_sni_allow`/`_start_sni` 真启转发）、`cli.py:927-945`（SNI 模式 hosts 改指 `127.0.0.1`）、`cli.py:485-497`（端口预检） |
| P2-17 | `forward` 未接线 + `sw` 永不关 + 空名单全放行 + `_need` 死函数 | **已修** | `cli.py:584/587`；`forward.py:171`、`:211-219`（`sw` 在 try 外、finally 关）；`_allowed` 空表 `False`（`:164-166`）+ `start()` warning（`:222-223`）；`_need` 已删。`tests/test_forward.py` 16 用例全绿、stderr 0 |
| P2-18 | `save_upstream` 解析失败即清空用户配置 + 非原子写 | **遗留** | `discover.py:280-292`：`except json.JSONDecodeError: data = {}` 后整份重写 |
| P2-19 | `backup_wininet` 文件存在就永不更新 | **遗留** | `system.py:50-51`：`if f.exists(): return f` |
| P2-20 | hosts 非原子写 + 只捕 `PermissionError` | **部分** | 捕获已扩到 `OSError`（`system.py:264-267`、`:277-280`）；**非原子写未改**（`:180-182`） |
| P2-21 | `set_user_env`/`clear_user_env` 死代码 + `KEY_ALL_ACCESS` | **遗留** | `system.py:120-138` |

### 7.4 本轮新增记录（review.md 未列的 6 项）

| # | 问题 | 位置 | 级别 | 状态 |
|---|---|---|---|---|
| N-1 | `DEFAULT_CONFIG.browser.bypass` 比模板少 `172.17.*`–`172.31.*`（`svpn init` 写出最小串） | `cli.py:57-60` vs `config.example.json:22` | P3 | **审计期间闭合**（现改为 `range(16,32)` 生成；`merge(example)==merge({})` → `True`） |
| N-2 | 端口/占位符常量多处无共享 | `cli.py:516/668/1112`、`proxy.py:30-31`、`pac.py:23`、`discover.py:287-288`；占位符 `cli.py:42`/`discover.py:84`/`proxy.py:93` | P3 | **部分**（`cli.py:467` 已改引用 `DEFAULT_CONFIG`；其余仍在） |
| N-3 | `cmd_down --clear-hosts` 会弹 UAC | `cli.py:649` 传 `no_elevate=False` → `cmd_hosts` 提权分支 | P3 | 遗留（`down` 是收工回滚，不该再要一次 UAC） |
| N-4 | `probe_unusable` 生产路径无调用 | `cli.py:703` | P3 | 遗留（仅 `tests/test_cli_contract.py` 引用） |
| N-5 | `connect_timeout` 为负不归一 | `cli.py:168-169` | P3 | 遗留（运行期 `_as_timeout` 兜住，不炸） |
| N-6 | `discover.is_self` 只判回环地址，不覆盖 `listen_host` 改成 LAN IP 的情况 | `discover.py:228-237` | P3 | 新增记录（硬约束要求只监听 127.0.0.1，故实际风险低） |

### 7.5 附带勘误：`P7-tests.md` §5.3 的说法已不成立

`P7-tests.md:105` 记「`http://a:b:c` 会被当成主机名 `a:b:c` 接受」。按现码复测：`hostport.partition(":")` → `("a","b:c")` → `int("b:c")` 抛 `ValueError` → `_bad_request`，**实测回 `HTTP/1.1 400 Bad Request`**。该条可从遗留清单划掉（同批 8 组畸形输入全部被拦）。

---

## 附：可复现命令（逐条对应"遗留"）

```bash
cd /d/LE/AI/dshwork/steam-vpn && export PYTHONUTF8=1
PY="C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe"

# P1-2 IPv6 被当域名发上游
"$PY" -c "h='2606:4700::1111';print(bool(h) and all(c.isdigit() or c=='.' for c in h))"   # False
sed -n '238,248p' svpn/proxy.py

# P1-3 DNS 缓存无淘汰 + 无闸门
grep -n "_DNS_CACHE" svpn/netutil.py ; grep -rn "Semaphore" svpn/ || echo "(无并发闸门)"

# P1-5 / P2-20 hosts 非原子写
sed -n '180,182p' svpn/system.py

# P1-7 PIDFILE 先写后删
grep -n "PIDFILE" svpn/cli.py          # 只有 :509 写、:522/:635 删

# P1-8 握手只有 2 处超时
sed -n '469,515p' svpn/proxy.py | grep -c wait_for     # 2

# P2-2 head[0] 未校验
sed -n '251,253p' svpn/proxy.py

# P2-5 只竞速前 4 个
grep -n "ips\[:4\]" svpn/proxy.py

# P2-6 DoH 未编码
sed -n '86,89p' svpn/netutil.py

# P2-8 http_connect 残留字节
sed -n '567,579p' svpn/netutil.py

# P2-9 CGNAT 被送上游
"$PY" -c "import sys;sys.path.insert(0,'.');from svpn.rules import Router;print(Router(mode='blacklist').decide('100.64.0.1'))"   # upstream

# P2-10 proxy_cidrs 未接线
sed -n '388,394p' svpn/proxy.py ; grep -c proxy_cidrs config.example.json   # 0

# P2-13 _elevate 引号拼接
sed -n '879,887p' svpn/cli.py

# P2-14 日志无轮转 / P2-15 前台无 PIDFILE
grep -n "FileHandler" svpn/cli.py ; grep -n "PIDFILE.write_text" svpn/cli.py

# P2-18 save_upstream 清空 + 非原子 / P2-19 备份只取第一份 / P2-21 死代码
sed -n '275,292p' svpn/discover.py
sed -n '48,54p' svpn/system.py
sed -n '120,138p' svpn/system.py

# N-3 down --clear-hosts 弹 UAC / N-4 probe_unusable / N-5 负数不归一
sed -n '647,651p' svpn/cli.py ; sed -n '703,709p' svpn/cli.py
"$PY" -c "import sys;sys.path.insert(0,'.');from svpn.cli import merge_config;print(merge_config({'connect_timeout':-1})['connect_timeout'])"   # -1

# 已闭合项的复核（证明不是"改回去"）
"$PY" -c "import sys;sys.path.insert(0,'.');from svpn import discover;from svpn.netutil import Node;print([discover.is_self(n) for n in (Node('http','127.0.0.1',7900),Node('socks5','127.0.0.1',7899),Node('http','127.0.0.1',7890))])"   # [True, True, False]
sed -n '636,646p' svpn/cli.py            # cmd_down 现在会判 res.get("ok") is False

# 全绿项复跑
"$PY" -m compileall -q svpn && echo "compileall OK"
for c in "" init up _serve down status probe node cdn hosts system pac steam doctor; do
  "$PY" -X dev -W error::ResourceWarning -m svpn $c --help >/dev/null 2>/tmp/e; echo "[$c] stderr=$(stat -c%s /tmp/e)"; done
for t in test_logic test_netutil test_regression smoke_test; do "$PY" tests/$t.py 2>&1 | tail -1; done
"$PY" -X dev tests/test_forward.py 2>&1 | tail -3
```

**本次审计脚本落点**（一次性，放在 `%TEMP%/svpn-audit/`，未进仓库）：
`ast_scan.py`（未定义名 + 签名，支持 `SVPN_AUDIT_ROOT` 指向自检样本）、`argparse_audit.py`（dest 对照）、`encoding_audit.py`（BOM/CRLF）、`deadcode_audit.py`（引用图 + except 统计）、`bridge_check.py`（内存 fake 验证异常边界）。

**未做（越界隔离）**：没有启动 `svpn up`/`_serve`、没有执行 `hosts apply/clear`、没有 `system on/off`、没有 `svpn down`（它会写注册表）、没有联网。
