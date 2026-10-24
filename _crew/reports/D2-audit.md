# D2-audit 交付报告（静态审计 · 只读复跑）

> 完整报告：**`runtime/audit-after.md`**（逐条行号 + 修复前后对比 + 每条遗留的可复现命令）。
> 本文件只是结论摘要。

## 0. 改了哪些文件

| 文件 | 改动 |
|---|---|
| `runtime/audit-after.md` | 新建（唯一产出） |
| 其它 | **零改动**（无代码、无配置、无 hosts、无注册表、无系统代理） |

未启动 `svpn up`/`_serve`，未执行 `hosts apply/clear`、`system on/off`、`down`，未联网。

## ⚠ 审计期间代码被并发修改（结论已对最终快照重跑）

审计区间 18:47–19:05，期间有队友在改代码：

| 文件 | 首轮 | **最终快照（结论基准）** |
|---|---|---|
| `svpn/cli.py` | 51669 / 1270 行 | **52928 / 1287 行 / `4f90ea52dbe4` / 18:56:12** |
| `svpn/discover.py` | 9291 / 264 行 | **10834 / 305 行 / `f1b6b4cc8a8d` / 18:55:45** |
| `svpn.cmd` | 1666 / 58 行 | **1696 / 59 行** |
| `_probe_proxy.py` | 存在 | **已删除**（故编码审计 39→38 个文件） |

这轮改动**恰好闭合了 4 项**我本来要记成遗留的问题（`discover` 自环、`cmd_down` 谎报还原、`bypass` 缺段、`cmd_up` 端口回退绕过常量）；表中标「审计期间闭合」并留了复核证据。

## 1. 结论

- **P0 2/2 全闭合**：PAC 现在是合法 JS（node 解析 4/4 `SyntaxOK` + 分流语义正确）；`pipe` / `handle_http` 异常边界用内存 fake 实测（5 类异常全静默、`CancelledError` 正确上抛、`_bridge` 两侧都炸不外抛且两侧 writer 都关、取消后残留 task = 0）。
- **P1 8 项 → 3 已修（含 2 项审计期间闭合）/ 2 部分 / 3 遗留**；**P2 21 项 → 5 已修 / 3 部分 / 13 遗留**；**另记 6 项 review 未列问题 → 1 闭合 / 1 部分 / 4 遗留**。
- 静态面全绿：未定义名 **0**、关键字参数不匹配 **0**、13 子命令 `args.X` 与 `add_argument` **13/13**（`cmd_steam` 手工 Namespace 覆盖 `cmd_up` 6/6）、编码 **38/38**、`-X dev` 下 **14 次 `--help` + 3 次真执行 stderr 全 0 字节**、`-W error` 逐模块 import **12/12**、裸 `except:` **0**、`compileall` exit 0、基线 50/57/62 + smoke ALL PASS、`test_forward` 16 用例全绿。
- **判断：20 项遗留 + 6 项部分闭合，最高级别 P1，无 P0 —— 不阻断发布。**

发行说明建议点名 3 条「用户能踩到但代码不报错」的行为：
1. `mode=all/blacklist` 下 IPv6 目标会被当域名发给上游（`proxy.py:240`），v6-only 站点必挂；
2. 前台 `svpn up` 不写 PIDFILE → `status` 显示"未运行"、`down` 停不掉（`cli.py:509` 只有后台分支）；
3. 半开连接/睡醒要等 TCP 超时才回收（`_bridge` 无 idle timeout，是 `return_exceptions=True` 的既定取舍）。

## 2. 关键数据（对比 `runtime/review.md`）

| 维度 | 修复前 | 修复后 |
|---|---|---|
| P0 | 2 | **0** |
| P1 已修 / 部分 / 遗留 | 0 / — / 8 | **3 / 2 / 3** |
| P2 已修 / 部分 / 遗留 | 0 / — / 21 | **5 / 3 / 13** |
| 新增记录 | — | 6（1 闭合 / 1 部分 / 4 遗留） |
| 裸 `except:` | 未计 | **0** |
| `except Exception:` | — | 23（逐条列出，均有理由） |
| 未定义名 / 签名不匹配 | — | **0 / 0** |
| argparse dest 对齐 | — | 13/13 + Namespace 6/6 |
| 编码合规 | `install.ps1` 缺 BOM | **38/38** |
| `-X dev` stderr | — | **17/17 = 0 字节** |
| 死代码 | `_need`、`split.py` 未接线 | `_need` 已删、`SniForwarder` 已接线、`split.py` 仍未接线（与文档一致）；仍有 4 个顶层符号无人引用 |

## 3. 遗留清单（要点，完整证据见 audit-after.md §7）

**P1 遗留 3 项**
1. `proxy.py:240` IPv6 字面量仍按"域名"发 ATYP=3（`mode=all/blacklist` 或 `proxy_hosts` 命中时）
2. `cli.py:509-522` `up -b` 的 PIDFILE 先写后删、就绪判据只看端口（并发 `up -b` 可覆盖真实例 PID 并删掉）
3. `netutil.py:76` `_DNS_CACHE` 无上限、无负缓存、无总 deadline

**P1 部分 2 项**：`system.py:180-182` hosts 校验已补但**非原子写**未改；`proxy.py` SOCKS5 握手 15 处读操作只有 2 处带超时、且无并发闸门。

**P2 遗留 13 项**：`head[0]` 未校验、`_bridge` 无 idle timeout（取舍）、`ips[:4]` 取样、DoH 查询串未编码、`http_connect` 残留字节、`100.64.0.0/10` 仍送上游、`proxy_cidrs` 未接线、`pid_alive` 无镜像名校验（部分）、`_elevate` 单引号拼接、日志无轮转、前台 `up` 无 PIDFILE、`save_upstream` 解析失败清空配置、`backup_wininet` 只取第一份、`set_user_env`/`clear_user_env` 死代码。

**新增 4 项（P3）**：`down --clear-hosts` 会弹 UAC；`probe_unusable` 仅测试引用；`connect_timeout` 负数不归一；`is_self` 不覆盖 `listen_host` 改 LAN IP 的情况。

**已闭合（本轮期间）**：`discover` 自环（新增 `self_ports`/`is_self` + `scan` 过滤 + `node set` 拒绝）、`cmd_down` 谎报还原（判 `res.get("ok") is False`）、`bypass` 缺 16 段（改 `range(16,32)` 生成）、`cmd_up` 端口回退改用 `DEFAULT_CONFIG`。

**勘误**：`P7-tests.md` §5.3 说 `http://a:b:c` 会被当主机名接受 —— 现码实测回 `400 Bad Request`，该条不成立。

## 4. 契约变更 / 需要别人配合

- 无（本轮只读，未改任何契约）。
- 下一轮若统一端口常量（`cli.py:516/668/1112`、`proxy.py:30-31`、`pac.py:23`、`discover.py:287-288`），注意 `tests/test_config_contract.py` 与 `test_cli_contract.py` 里有口径断言。
- 若改 `cdn.py::CDN_DOMAINS`，须同步 `config.example.json` + `config.json`（`test_config_contract.py:179/186/205` 三条断言焊死）。

## 5. 没做完 / 发现的其它问题

- `probe` 退出码语义（`unusable` / `--lenient`）只做静态核对 + 读 `probe.summarize`，**未真跑**（会联网）；留给 D3 端到端。
- 审计脚本是一次性工具，在 `%TEMP%/svpn-audit/`，未入库；报告里每条遗留都附了等价的 `grep`/`sed`/`python -c`，不依赖脚本。
- 因为代码在审计期间变动过，若最终提交的 `cli.py` 指纹不是 `4f90ea52dbe4`，请以报告里的「代码片段锚点」重新 `grep` 定位，而不是照抄行号。
