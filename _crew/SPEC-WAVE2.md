# svpn 收尾工程 · 第二轮规范（文档 / 审计 / 验收）

> 先读 `_crew/SPEC.md`（第一轮规范，硬约束仍然有效），再读本文件。
> 第一轮已把 8 类缺陷修完。**第二轮只做三件事：文档对齐、静态审计、端到端验收。**
> 你是**只读**方：除本文件指定的输出文件外，不许改任何代码。

---

## D1-docs（文档）

**只准写**：`README.md`、`README.en.md`（新建）、`docs/ARCHITECTURE.md`、`docs/WINDOWS.md`（新建）

必须反映第一轮的**真实**改动，逐条核对源码后再写，不许照抄旧文档：

| 变化 | 文档要怎么写 |
|---|---|
| `cdn.enabled=true` 现在是**真的**生效：启 `SniForwarder`，`hosts apply` 改写 `127.0.0.1` | 讲清两种 CDN 接法的取舍：`false` = hosts 直指选边 IP（简单、IP 会漂）；`true` = hosts 指 127.0.0.1 + 常驻转发（IP 漂了能重选，但要求 `svpn up` 一直开着） |
| `install.ps1` 现在可用（BOM 已修，`start-svpn.cmd`/`uninstall.ps1` 已存在） | 把「暂勿使用安装器」改掉，给一键安装 + 一键卸载 |
| `probe` 退出码语义变了 | 写清 `0 = 全部端点可用`、`1 = 有端点不可用`、`--lenient` 保留旧语义；CI 怎么用 |
| `forward.py` 已接线（不再是死代码） | 目录树注释更新；`split.py` 仍是「预留，未接线」要标注清楚 |
| 配置键 12 个，`_` 开头的键会被丢弃 | 配置表更新；说明 `proxy_hosts`/`direct_hosts` 显式覆盖 |
| 已知限制 | 无节点时社区/头像不可用（SNI 阻断）；`_bridge`/`pipe` 半开连接会等 TCP 超时（`return_exceptions=True` 的取舍） |

`docs/WINDOWS.md` 要写实的：UTF-8 BOM 强制要求（否则 PowerShell 5.1 中文乱码/解析失败）、
`.cmd` 的 `call` 坑（python 是 `.bat` shim 时会吞掉控制权）、UAC 与 hosts、
WinINET 注册表位置与回滚、Steam 客户端改代理后必须重启、Win10 vs Win11 差异、常见 WinError 10048/121。
`README.en.md` 不用全文翻译，做成可独立阅读的浓缩版：What/Why/Quickstart/Limits/Uninstall，≤ 150 行。

**验证**：文档里出现的每条命令都要真跑一遍（`--help` 级别即可，别真启代理），确认参数名、子命令名、
文件路径都对得上。跑不对的写进报告，别硬写。

## D2-audit（静态审计，只读）

**只准写**：`runtime/audit-after.md`

复用 `runtime/review.md` 的方法，复跑一遍并对比「修复前 vs 修复后」：
1. **AST 扫描**：未定义名（`NameError` 风险）、函数签名不匹配、`argparse` 属性审计（`cmd_*` 读的每个
   `args.X` 是否都在对应 `add_argument` 里注册；注意 `cmd_steam` 手工构造的 Namespace）
2. **编码审计**：所有 `.ps1` 是否 UTF-8 BOM（头三字节 `ef bb bf`）、`.cmd` 是否 CRLF 且无 BOM、
   `.py` 是否 UTF-8、`config*.json` 是否 UTF-8 无 BOM
3. **`-X dev` 告警扫描**：12 个子命令 `--help` + `status`/`doctor`，stderr 必须 0 字节，
   无 `ResourceWarning`/`DeprecationWarning`
4. **死代码**：未被任何模块 import 的顶层函数/类；`grep` 确认 `_need` 已删、`SniForwarder` 已被 cli 引用
5. **重复/矛盾**：同一常量在多处硬编码（如端口 7899/7900、CDN 域名列表）、`DEFAULT_CONFIG` 与
   `config.example.json` 是否仍一致
6. **不可达分支 / 裸 except**：`except:`（无类型）必须 0 处

输出格式：`## 结论`（一段）+ 分节表格（问题 / 位置 / 级别 / 状态：已修/遗留/新增）。
遗留项必须给出**可复现命令**。

## D3-e2e（端到端验收，会联网）

**只准写**：`runtime/e2e-after.md`

工作日志 §7 第 2 条留了个未完成项：**安静环境下的 10 连测**（之前 13 个 agent 抢网，测得 6/10，
判「稳定性未达标」，但那不是干净数据）。本轮补上：

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NODE_OPTIONS NODE_EXTRA_CA_CERTS
```
1. `python -m svpn up -b` 起后台 → `status` 确认监听
2. **安静环境 10 连测**：经 SOCKS5 + 经 HTTP 各 10 次取 `https://store.steampowered.com/`
   （用 `svpn` 自带的 `tests/e2e_proxy.py` 或自己写小脚本），记录每次耗时/状态码/成功率
3. 经代理取 CDN 头（`steamcdn-a.akamaihd.net`）一次，确认 200
4. `python -m svpn probe --node none --json` 一次，记录 summary（社区 SNI 阻断是**预期**结果）
5. `python -m svpn down` → 确认进程消失、端口释放、系统代理未被改坏（`ProxyEnable` 回原值）
6. 收尾必须**确保不留后台进程**（`svpn down` + 复查 pid/端口）

报告要给出：成功率、P50/P95 耗时、失败样本的 `<rep 码/错误>`、以及「是否达到可用标准」的判断
（标准：10/10 成功且 P95 < 2s 判「达标」；≥ 9/10 且 P95 < 3s 判「基本可用」；否则「未达标」并给证据）。
失败不要掩盖，也不要重试到好看为止——**只跑一轮，如实记录**，重试另起一节标明。
