# svpn — Steam 专用本地分流 / 加速器

一句话：在本机起一个只服务 Steam 的代理（SOCKS5 + HTTP），按域名把 Steam 流量交给你的节点；再带一个 CDN 选边器，把下载/更新钉到当前最快的 Akamai 边缘。**没有节点也能用。**

Windows 10/11 + Python 3.10+（本机实测 3.13），零第三方依赖，纯标准库。

命令都在项目根目录跑（`svpn.cmd` / `svpnctl.ps1` / `start-svpn.cmd` 会自动切到根目录）：

```powershell
cd D:\LE\AI\dshwork\steam-vpn    # 换成你的实际路径
python -m svpn status
```

---

## 0. 先对齐五条事实（本机实测，别再怀疑网络）

| 事实 | 结论 |
|---|---|
| `store` / `login` / `api` / CDN **直连正常** | 60~160ms，商店、登录、下载都不需要节点 |
| `steamcommunity.com` **SNI 阻断** | TCP 能连；ClientHello 里带这个域名立刻被 RST；**换 IP 无效** |
| `avatars.steamstatic.com` 同样阻断 | 头像/部分图片，选边时握手不过，不会写进 hosts |
| 社区、创意工坊 | **必须走节点**，没有别的办法 |
| 没有节点时的加速 | CDN 选边 + hosts 仍然能把下载指到最快边缘（需一次管理员授权） |

补一句：直连也会偶发单个 IP 超时（尤其 `store`），`probe` 会拿多个 IP 竞速，所以单次结果里某一行 `tcp TIMEOUT` 不代表商店整体不可用，重跑一次或看 `login`/`api` 那几行。

输出前缀：`[+]` 成功、`[i]` 信息、`[!]` 需要你处理、`[=]` 无需动作。

---

## 1. 装 / 跑 / 卸

### 1.1 一键安装（推荐）

```powershell
cd D:\LE\AI\dshwork\steam-vpn
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

它做五件事（不写注册表、不设开机自启、不装服务、**不需要管理员**）：

1. 检查项目文件是否齐全（`svpn\cli.py` / `svpn.cmd` / `svpnctl.ps1` / `start-svpn.cmd` / `uninstall.ps1`），缺了直接列出来并 `exit 2`
2. 找一个真能跑的 Python 3.10+（会跳过 Microsoft Store 的 `python.exe` 占位），找不到 `exit 3`
3. `python -m svpn init`（生成 `config.json` + 备份系统代理），失败 `exit 4`
4. `python -m svpn doctor` 体检；**返回 1 是正常的**（没节点 → 社区不可用），不算安装失败
5. 桌面建快捷方式 `svpn 启动.lnk` → `start-svpn.cmd`

装完双击桌面「svpn 启动」，等于：起代理（后台常驻）→ 接管系统代理 → 拉起 Steam → 打一张 `status` 回执。**没装 Steam 也照常跑，只是不拉客户端。**

`start-svpn.cmd` 也可以直接双击/命令行跑；`up` 失败会打印排查建议并 `pause`，窗口不会一闪而过。

### 1.2 手动三步（不想用脚本）

```powershell
python -m svpn init                     # 生成 config.json + 备份系统代理
python -m svpn up -b --system-proxy     # 后台起代理 + 接管系统代理
python -m svpn status                   # 确认监听
```

### 1.3 一键卸载

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

五步，**可安全重复执行**（做过的会跳过）：

1. 只按 `runtime\svpn.pid` 停进程（**不按镜像名乱杀 python**）
2. 读 `runtime\wininet-backup.json` 还原系统代理（没有备份就明确跳过，绝不猜默认值）
3. 删 hosts 里的 svpn 托管块（只删两个标记之间的行，其余字节原样保留；要管理员，会弹**一次** UAC）
4. 删桌面 `svpn 启动.lnk`
5. 只有加 `-Purge` 才删 `runtime\` 与 `svpn.egg-info\`（删前打印路径 + 确认；自动化里要 `-Purge -Yes`）

其它开关：`-KeepProxy`（不动系统代理）、`-Purge`、`-Yes`。

> 卸载脚本**不会删** `config.json`、`svpn\` 源码、pip 装的包。要彻底干净：删掉整个项目目录即可。

---

## 2. CDN 加速的两种接法（`cdn.enabled`）

这是全项目最容易搞混的一处，先讲清：**两种接法互斥，靠 `config.json` 的 `cdn.enabled` 切换。**

| | `cdn.enabled: false`（默认，老路子） | `cdn.enabled: true`（SNI 透明转发） |
|---|---|---|
| `hosts apply` 写什么 | 每个 CDN 域名 → 该域名的**实测最优边缘 IP** | `cdn.hosts` 里的域名**全部** → `127.0.0.1` |
| 谁在干活 | 操作系统解析直接打到边缘，svpn 不参与 | 常驻的 `svpn up` 里那个 `SniForwarder`：读客户端 TLS ClientHello 的 SNI，还原域名，命中选边缓存就直连最优 IP，否则走上游节点 |
| 要不要常驻进程 | **不要**。写完 hosts 就能关工具 | **要**。`svpn up` 一停，这些域名全部连不上 |
| 占不占本机 443 | 不占 | 占 `cdn.sni_port`（默认 443），可能和 Watt Toolkit 等抢 |
| IP 漂了怎么办 | hosts 里是死 IP，要手动 `cdn --refresh` + `hosts apply` | hosts 指向 127.0.0.1，`cdn --refresh` 只换缓存，**不用重写 hosts**；转发层每次拨号都用最新缓存 |
| 失败兜底 | 无（hosts 写错就是不通） | 端口被占只警告，退回 `hosts 直指最优 IP`；白名单为空则**全部拒绝** |

一句话选：**图省事、不想常驻 → 保持 `false`；想长期挂着、怕 IP 漂 → 改 `true` 并让 `svpn up` 一直跑。**

> `cdn.sni_port` 虽然可配，但这条接法是靠 hosts 把域名指到 `127.0.0.1`、客户端自己连 443 实现的——hosts 条目里没有端口位。所以**实际必须用 443**，改成别的值是"转发生效但没人来连"。

打开 SNI 模式：

```powershell
# config.json: "cdn": { "enabled": true, "sni_port": 443, ... }
python -m svpn cdn --refresh     # 先有选边缓存（转发的 ip_map 就来自这里）
python -m svpn hosts apply       # 这次写的是 127.0.0.1，会提示「需 svpn up 常驻」
python -m svpn up -b             # 必须常驻；启动日志会打「SNI 透明转发 127.0.0.1:443 白名单 N 个域名」
```

SNI 模式的安全边界（都在代码里，不是口号）：

- 只绑 `127.0.0.1`，只放行 `cdn.hosts` 白名单（精确相等或子域后缀）；
- **白名单为空 = 全部拒绝**，会打一条 warning，绝不会「没配就全放行」变成谁都能用的 443 出口；
- 非 TLS 首包直接断；读头最多 16KB / 10 秒；
- 不做中间人：只读出域名，ClientHello 原样转发，证书校验仍由客户端自己做。

---

## 3. 四条主线命令

### 3.1 `up` — 起服务

```powershell
python -m svpn up -b --system-proxy                     # 后台 + 接管系统代理
python -m svpn up -b --node socks5://127.0.0.1:7890      # 指定节点（写进 config.json）
python -m svpn up -b --port 17899 --mode all             # 换 SOCKS5 端口 + 全量走节点
python -m svpn up                                         # 前台跑，Ctrl+C 退出
```

做什么：

- 监听 **SOCKS5 `127.0.0.1:7899`** 和 **HTTP `127.0.0.1:7900`**（只听回环）
- 按 `--mode` 分流，默认 `whitelist`：`whitelist` 只有 Steam/Valve 名单走节点；`all` 全走节点；`blacklist` 国内常用站点直连、其余走节点
- 没有真节点且 `auto_node: true` → 自动扫（配置 / 环境变量 / 系统代理 / 14 个常见客户端端口），挑一个**真能到 Steam** 的写回 `config.json`；`--no-auto` 关掉。**找不到节点也照常启动**，纯直连分流
- 启动前预检：`--port` 越界 → 返回 2；`--port` 撞 `http_port` → 返回 1；端口被占 → 打印占用者 `pid=… 进程名` 并返回 1；`cdn.enabled` 时还会检查 `sni_port`（与自身端口冲突 → 1，被别的程序占 → 只警告放行）
- 起服务时写一份 `runtime/svpn.pac`；后台模式 pid → `runtime/svpn.pid`，日志 → `runtime/svpn.log`
- 已经在跑时 `up` 只提示不重启（返回 0），改配置要 `down` 再 `up`

### 3.2 `down` — 收工与回滚

```powershell
python -m svpn down                  # 停进程 + 还原系统代理
python -m svpn down --keep-system    # 只停进程，系统代理保持（马上要重启服务时用，中间网页会不通）
python -m svpn down --clear-hosts    # 停进程 + 还原系统代理 + 清 hosts（会弹 UAC）
```

- 用 `taskkill /PID <pid> /T /F`（连子进程），然后删 `runtime/svpn.pid`
- 默认还原系统代理（从 `runtime/wininet-backup.json`），并打印还原前的 `ProxyEnable`
- **不动 hosts**，除非加 `--clear-hosts`
- 系统代理还指着 7900 而服务已经停了，网页会全挂；所以顺序是**先 `down` 再动系统代理**（`down` 已顺手还原）

### 3.3 `probe` — 全链路体检（先跑这个再猜）

```powershell
python -m svpn probe                              # 直连 + 自动挑的节点，两条路都测
python -m svpn probe --node none                  # 只看直连
python -m svpn probe --node socks5://127.0.0.1:7890
python -m svpn probe --timeout 6                  # 单点超时（秒），默认 4
python -m svpn probe --json                       # 表格后再打一段 JSON 摘要
python -m svpn probe --lenient                    # 旧语义退出码（见 §4）
```

9 个端点 × 两条路（全是 443）：`store` / `login` / `api` / `steamcommunity.com` / community 静态资源 / `avatars` / `steamcdn-a` / `cdn.akamai` / `help.steampowered.com`。

直连那一列失败时分三层，处置方式完全不同：

```
DNS  → 解析出来的是不是假地址（污染）
TCP  → 这个 IP 通不通（IP 封锁 / 路由不通）
TLS  → ClientHello 发出去会不会被 RST（SNI 阻断）
```

解析不赌单一来源：DoH 的真实记录 + 系统 DNS 的记录合并去重，再交给 TLS 竞速淘汰——前者可能"真实但很远"，后者可能"近但被污染"。

结论列怎么读：

| 结论 | 含义 | 怎么办 |
|---|---|---|
| `直连可用` | 这条路本来通 | 不用管 |
| `需节点 · SNI 阻断` | TCP 通，带域名握手被 RST | 只能走节点，换 IP 没用 |
| `需节点 · DNS 污染` | 解析到假地址 | 只能走节点 |
| `需节点 · IP 不可达` | 拿到的 IP 全部连不上 | 走节点 |
| `节点也不通 · 换节点` | 上游本身到不了 Steam | `node scan` 换一个 |
| `不通 · 待排查` | 两边都不通 | 先 `doctor`，再看防火墙/上游 |

结果同时落盘 `runtime/probe.json`（`{ts, node, rows[], summary}`）。

### 3.4 `cdn` — 选边测速

```powershell
python -m svpn cdn                                # 缓存存在就只读缓存并打印
python -m svpn cdn --refresh                      # 重新解析 + 竞速 + 1MB 实测
python -m svpn cdn --refresh --no-bench           # 只测握手，不下载
python -m svpn cdn --domain steamcdn-a.akamaihd.net --json
python -m svpn cdn --timeout 5                    # 单点超时（秒），默认 3
```

四步：

1. DoH 拿这个域名的**全量 A 记录**（绕开被污染的本地 DNS）
2. 每台 IP 用**正确 SNI** 做 TLS 握手，淘汰不可达/证书不对的
3. 对前几名发 `Range: bytes=0-1MB` 真实下载，按实测吞吐排序（`--no-bench` 跳过）
4. 结果写 `runtime/cdn-cache.json`，`hosts apply`（和 SNI 转发器的 `ip_map`）用的就是这里

只有 `steamcdn-a.akamaihd.net`、`cdn.akamai.steamstatic.com` 配了真实下载路径，其余域名只测握手/HEAD，所以它们的「实测」列是 `-`。

缓存按 `cdn.refresh_hours`（默认 6h）过期：**代理和 `hosts apply` 都只用未过期的 `best_ip`**，`cdn` 列表里过期的会带 `*`。`hosts apply` 一旦发现缓存过期（或根本没有），会先自动重选一轮再写，**不会把过期 IP 钉进 hosts**；实在刷不出来（断网）就直接拒绝写并提示先 `cdn --refresh`。Akamai 边缘会漂，下载变慢时先 `cdn --refresh`，隔几天重跑一次不亏。

历史样例（2026-10-24 那次选边，**IP 会漂，别当固定答案**）：

| 域名 | 最优 IP | 握手 | 实测 |
|---|---|---|---|
| `cdn.akamai.steamstatic.com` | `23.195.119.69` | 153ms | 1.64MB/s |
| `steamcdn-a.akamaihd.net` | `173.222.248.89` | 157ms | 1.58MB/s |
| `store.akamai.steamstatic.com` | `23.195.119.90` | 158ms | - |
| `client-update.akamai.steamstatic.com` | `23.195.119.13` | 164ms | - |
| `media.steampowered.com` | `23.33.184.246` | 121ms | - |
| `community.akamai.steamstatic.com` | `23.33.184.228` | 188ms | - |
| `shared.akamai.steamstatic.com` | `23.32.91.11` | 200ms | - |
| `avatars.steamstatic.com` | -（握手不过） | - | - |

### 3.5 `hosts` — 让选边生效

```powershell
python -m svpn hosts apply                # 非管理员会自动弹一次 UAC
python -m svpn hosts apply --no-elevate   # 只在「当前已是管理员」时写
python -m svpn hosts clear                # 只删自己的托管块
python -m svpn hosts apply --timeout 5
```

- `apply` 前如果还没有选边缓存，会先自动跑一轮选边（`cdn.enabled: true` 时跳过这步，SNI 模式不用 IP）
- 只写 `cdn.hosts` 白名单里的域名，缓存里其它域名一律跳过并说明
- 所有条目都过 `sanitize_entries`（合法 IP + 合法域名，拒绝主机名位置出现 IP / 换行 / `#` / 伪造标记），被拒的会汇总成一行 `WARN`
- 写完 `ipconfig /flushdns`

---

## 4. `probe` 的退出码（CI 用这个）

| 退出码 | 含义 | 判据 |
|---|---|---|
| `0` | **全部端点可用** | 每个端点「直连可用」或「有节点且节点通」 |
| `1` | **有端点不可用** | 存在 `summary.unusable` 里的域名（见下） |
| `2` | 参数/配置错误（argparse、`config.json` 解析失败、`--node URL` 非法） | — |

`summary.unusable` = 该端点在**当前条件下怎么都用不了**：直连不通且没配节点 / 节点也失败 / `不通 · 待排查`。

`--lenient` 保留旧语义：**只有**「不通 · 待排查」才返回 1（社区没节点、头像被阻断都不算失败）。老脚本/老习惯用它。

每次运行末尾都会打印这一行，`echo $?` 有据可依：

```
退出码 0 = 全部端点可用；1 = 有端点不可用（见 summary.unusable）
```

CI 里两种典型用法：

```bash
# 门禁：Steam 有端点不可用就让流水线红（无头环境建议先 --node none 只测直连，省掉节点等待）
python -m svpn probe --node none --timeout 6
rc=$?
[ "$rc" -eq 0 ] || { echo "svpn probe: $rc 个非 0 退出，Steam 链路不可用"; exit 1; }

# 只要没「待排查」的死端点就放行（允许社区因无节点而不通）
python -m svpn probe --lenient
```

> `--json` 是「**先打表格，再打一段 JSON**」，不是纯 JSON 流。脚本要取机器可读结果，直接读落盘的 `runtime/probe.json`（`{ts, node, rows[], summary}`），别去 `sed` 终端输出。

---

## 5. 配置（`config.json`）

`config.json` 由 `svpn init` 生成；`config.example.json` 是**唯一 canonical 模板**（键集/类型必须与 `svpn/cli.py` 的 `DEFAULT_CONFIG` 一致，`tests/test_config_contract.py` 守着）。顶层**恰好 12 个键**：

| 键 | 默认 | 说明 |
|---|---|---|
| `listen_host` | `127.0.0.1` | 只监听回环；改 `0.0.0.0` 前先配 `auth` |
| `listen_port` | `7899` | SOCKS5 端口 |
| `http_port` | `7900` | HTTP 端口；写 `0` = 不起 HTTP 入口 |
| `mode` | `whitelist` | `whitelist` / `all` / `blacklist` |
| `connect_timeout` | `12` | 建连与握手超时（秒） |
| `auto_node` | `true` | `up` 时是否自动发现节点 |
| `upstream` | `{kind:"socks5", host:"NODE_HOST", port:1080, username:"", password:""}` | `kind` = `socks5` / `http`（代码里还认 `direct`，表示不走上游）；`host` 为 `NODE_HOST` 视为未配置 |
| `auth` | 空 | 本地鉴权（SOCKS5 用户名密码 / HTTP Basic），空 = 不鉴权 |
| `browser` | `{apply_system_proxy:true, use_pac:false, bypass:"<local>;…;*.cn"}` | WinINET 接管开关、PAC 开关、绕过表 |
| `cdn` | `{enabled:false, refresh_hours:6, sni_port:443, hosts:[8 个域名]}` | §2 的两种接法 + 选边缓存 TTL + 白名单 |
| `proxy_hosts` | `[]` | **强制走节点**的域名（显式覆盖，优先于内置规则） |
| `direct_hosts` | `[]` | **强制直连**的域名（显式覆盖，优先级最高） |

规范化规则（`merge_config()` 的契约，`svpn/cli.py`）：

- 顶层键以 `_` 开头（如 `_note`）或不在上面 12 键里的 → **丢弃**，只 `logging.debug` 记一条，不报错；
- 嵌套 dict 按子键合并（`{"cdn": {"refresh_hours": 2}}` 只动这一项），嵌套里的未知键同样丢弃；
- 数值键写 `null` / `""` / `"abc"` / `NaN` / 越界 → **回落默认值**，绝不抛 `TypeError`；
- 布尔键认 `true/false/1/0/yes/no/on/off` 字符串；
- 名单（`proxy_hosts` / `direct_hosts` / `cdn.hosts`）写成逗号/空格分隔的字符串也认。

`proxy_hosts` / `direct_hosts` 就是给"内置规则没覆盖到的域名"用的显式覆盖，改完要 `down` 再 `up`。

### 5.1 两个可选高级键（不在模板里，但写了就生效）

| 键 | 默认 | 说明 |
|---|---|---|
| `proxy_suffixes` | 内置 `rules.STEAM_SUFFIXES` | **替换**「走节点」的后缀名单（Router 判定 + 生成的 PAC 都跟着变） |
| `direct_suffixes` | 内置 `rules.CN_DIRECT_SUFFIXES`（仅 `blacklist` 模式参与默认策略） | **替换**「直连」的后缀名单 |

行为与注意：

- 它们是 `cli.EXTRA_KEYS`，**不在** `config.example.json` 里，所以模板的 12 键契约不受影响；**写了才生效**，不写（或留空）就用内置名单；
- 写法同其它名单：JSON 数组，或 `"a.com, b.com"` 这种逗号/空格分隔字符串——**都会归一成字符串数组**，脏值（嵌套 list/dict、`null`）逐项丢弃，不是列表也不是字符串的值回落空表；
- **是"替换"不是"追加"**：写 `["corp.example.com"]` 就等于「只有它走节点」，Steam 全被挤出去；想保留内置名单就在自己的名单后面手动补齐；
- 写成空数组 `[]` 等于没写（`Router` 与 `build_pac` 都是 `xxx or 内置表` 的口径）；
- **`mode: blacklist` 的例外**：`direct_suffixes` 为空会自动回落到内置国内清单——否则「国内直连」就是一句空话。这是 `build_pac` 里显式 `fallback = CN_DIRECT_SUFFIXES if mode == "blacklist"` 加的，别当成 bug；
- `node set` / `node clear` / `up --node` 会原样保留它们；**`init --force` 会用模板重写 `config.json`，把它们丢掉**；
- 改完要 `down` 再 `up`（Router 在启动时构造，PAC 也是启动时重写）。

**优先级**（从高到低，命中即短路）：

```
proxy_hosts / direct_hosts（精确主机）  >  *_suffixes（后缀表）  >  内置 STEAM_SUFFIXES / CN_DIRECT_SUFFIXES
```

`proxy_hosts` 与 `direct_hosts` 谁更优先：**`direct_hosts` 先判**（同一个域名两边都写时走直连）。日常优先用这两个精确覆盖，`*_suffixes` 是整表替换、影响面大。

---

## 6. 没有节点怎么办

1. **商店 / 登录 / API**：本来直连就通，什么都不用做。
2. **下载慢**：`python -m svpn cdn --refresh` 然后 `python -m svpn hosts apply`。Akamai 一个域名会返回十几台边缘，快慢能差 10 倍；选边就是把 DNS 答案换成实测最快那台。只写握手成功的域名，像 `avatars.steamstatic.com` 这种被阻断的不会写。
3. **社区 / 创意工坊**：没有节点过不去。别浪费时间换 DNS、换 IP、挂手机热点——阻断在链路上。三条路：
   - `python -m svpn node scan` 看本机是不是已经有能用的客户端，有就 `node set`
   - 找同事/机场要一个 SOCKS5 或 HTTP 节点（见下一节）
   - 接受现状：商店能买、游戏能下，社区打不开
4. hosts 方案只影响下载/更新域名，不动登录链路，也不改系统代理——关掉它只要 `python -m svpn hosts clear`。

---

## 7. 有节点怎么填

三种写法，任选一种，效果一样：

```powershell
python -m svpn node set socks5://127.0.0.1:7890
python -m svpn node set http://127.0.0.1:10809
python -m svpn node set socks5://user:pass@10.0.0.2:1080
```

规则：

- 省略协议默认按 `socks5`；`1.2.3.4:1080` 也认；`socks`/`socks5h` 归一成 socks5，`https://` 归一成 http
- 端口必须在 1–65535；裸 IPv6（不带方括号）会明确报错——请写 `socks5://[::1]:1080`
- `node set` **会先真握手验一遍**：通了就写进 `config.json`；代理通但到不了 Steam 会提示（仍写入）；连代理都握不上直接报错返回 1，不写
- `up --node URL`：同样写进 `config.json`，然后启动
- 清掉：`python -m svpn node clear` → `config.json` 回到占位 `NODE_HOST`，纯直连分流
- 手改 `config.json` 也行：`upstream.kind` = `socks5` / `http`，配 `host` / `port` / `username` / `password`

`node scan`（`--json` 给脚本用）会看这些地方，并逐个做**真实握手**（SOCKS5 全流程 → TLS 到 `store.steampowered.com` → 取 HTTP 状态码）：

| 来源 | 内容 |
|---|---|
| 配置文件 | `config.json` 的 `upstream` |
| 环境变量 | `ALL_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY`（大小写都看） |
| Windows 系统代理 | 注册表 `Internet Settings` 里的 `ProxyServer` |
| 常见客户端端口 | Clash 7890/7891、Clash Verge 7897、v2rayN 10808/10809、sing-box 2080/2081、Watt Toolkit 8889、Netch 20171、Surge 6152/1086、Privoxy 8118、通用 1080/1081（共 14 个，svpn 自己的 7899/7900 会排除） |

状态列：`可用`（能到 Steam）/ `代理通·Steam不通`（端口是对的，节点本身出不去）/ `不可用`（握手失败）。端口开着不等于能用，这是这个工具和其他"端口探测"的最大区别。

---

## 8. 常见故障对照表

| 现象 | 判定 | 处置 |
|---|---|---|
| 社区打不开，商店正常 | SNI 阻断：`probe` 里 `steamcommunity.com` 直连 `sni_rst` | 只能走节点：`node scan` → `node set <URL>` → `up -b` |
| 域名解析到奇怪 IP / 证书报错 | DNS 污染 | 工具解析走 DoH 并和系统 DNS 结果一起竞速；浏览器层面自己开 DoH，或 `pac --write --apply` 只让 Steam 走代理 |
| `up` 报端口被占用（返回 1） | 7899/7900 被占（上次没退干净，或另一份 svpn） | `python -m svpn status` 看 pid → `down`；还不行按提示 `taskkill /PID <pid> /F`；或 `up --port 17899` |
| `up` 说「SOCKS5 与 HTTP 不能同端口」 | `--port` 撞上了 `http_port` | 换 `--port 17899`，或改 `config.json` 的 `http_port` |
| `cdn.enabled: true` 时 443 被占 | 本机 443 通常被别的加速器/服务占（Watt Toolkit、IIS…） | 启动只警告、SNI 转发会跳过 → 下载退回 hosts 直指 IP。**注意 hosts 条目无法表达端口**，客户端连的就是 `127.0.0.1:443`，所以这条接法里 `cdn.sni_port` 实际上必须是 443；改成别的值等于转发生效但没人来连。要么退掉占用 443 的程序，要么把 `cdn.enabled` 改回 `false` 走「hosts 直指最优 IP」 |
| `hosts apply` 弹 UAC 被拒 | 写 hosts 需要管理员 | 用管理员 PowerShell 跑 `python -m svpn hosts apply --no-elevate`；`--no-elevate` 只在已是管理员时才真写 |
| 下载速度没变化 | 选边结果过时，或 hosts 没生效 | `cdn --refresh` → `hosts apply`；`ipconfig /flushdns`；`hosts clear` 后重来 |
| 网页全都变慢 | 系统代理指向本机但服务没跑，或 `--mode all` 把国内站点也送出去了 | `status` 看进程；`down` 还原系统代理；要精细控制就 `pac --write --apply` |
| `probe` 里 `节点也不通` | 节点失效 | `node scan` 换一个；先 `up -b --node <URL>` 试 |
| Steam 客户端里社区还是打不开 | 客户端没重启 / 系统代理没设 | `python -m svpn steam --restart --system`（**改代理必须重启客户端**） |
| PowerShell 提示"禁止运行脚本" | 执行策略 | `powershell -ExecutionPolicy Bypass -File .\svpnctl.ps1 status`，或 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| 输出乱码 / 显示成方块 | 老终端（conhost）字体或代码页 | 用 Windows Terminal；命令本身已强制 UTF-8 输出 |

Windows 专属的坑（BOM、`call`、UAC、注册表、WinError 10048/121…）单独写在 **[docs/WINDOWS.md](docs/WINDOWS.md)**。

---

## 9. 回滚

```powershell
python -m svpn down                    # 停服务 + 系统代理还原
python -m svpn down --clear-hosts      # 顺带清 hosts（UAC）
python -m svpn hosts clear             # 只清 hosts
python -m svpn node clear              # 清节点 → 纯直连
python -m svpn system off              # 只还原系统代理
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1   # 连快捷方式/runtime 一起收
```

原件都在 `runtime/`：

| 文件 | 内容 | 什么时候写 |
|---|---|---|
| `wininet-backup.json` | 系统代理原始值 | 首次 `init` / 首次改系统代理前；**只保留第一次的值**，后面再 apply 不覆盖 |
| `hosts.backup` | hosts 原始副本 | 首次写 hosts 前 |
| `hosts.backup-uninstall` | 卸载前 hosts 原始副本 | `uninstall.ps1` 清 hosts 前 |
| `svpn.pid` | 后台进程号 | `up -b` |
| `svpn.log` | 运行日志 | 每次起服务 |
| `stats.json` | 会话统计 | 服务运行中每 5s 落盘一次，退出时再写一次（`down` 是强杀，所以必须周期落盘） |
| `probe.json` | 最近一次 probe 明细 | 每次 `probe` |
| `cdn-cache.json` | 选边结果 | 每次 `cdn` 测速 |
| `svpn.pac` | 自动生成的 PAC | 每次起服务 / `pac --write` |

hosts 只动自己的受管块（`false` 模式下长这样；`true` 模式下 IP 全是 `127.0.0.1`）：

```
# >>> steam-vpn managed block (do not edit) >>>
23.195.119.69    cdn.akamai.steamstatic.com
# <<< steam-vpn managed block <<<
```

手写的条目不受影响，`hosts clear` 只删这一块；读写会按原编码（UTF-8 / BOM / GBK）round-trip，不毁你的中文注释。

---

## 10. 目录结构与模块说明

```
steam-vpn/
├─ install.ps1           一键安装（检查文件 → 找 Python → init → doctor → 桌面快捷方式）
├─ uninstall.ps1         一键卸载（停进程 → 还原代理 → 清 hosts → 删快捷方式 → 可选 -Purge）
├─ start-svpn.cmd        双击即用：up -b --system-proxy → steam --system → status
├─ svpn.cmd              cmd 包装：透传 python -m svpn %*，cwd 固定到脚本目录
├─ svpnctl.ps1           可选：PowerShell 参数风格的包装（给习惯 -Background/-System 的人）
├─ config.json           运行配置（init 生成；含节点账号，别外传）
├─ config.example.json   配置 canonical 模板（12 键 + `_note`）
├─ runtime/              运行期文件（见上一节）
├─ tests/
│  ├─ smoke_test.py      离线冒烟：分流规则 / SNI 解析 / SOCKS5 / HTTP / 鉴权（15 项检查）
│  ├─ test_logic.py      离线单测：分流判定、故障定性、选边排序、缓存过期
│  ├─ test_netutil.py    离线单测：DoH 缓存、分层探测、SOCKS5 协议字节、限速下载
│  ├─ test_regression.py 离线回归：DoH 合并、SNI 对照、stats 落盘、hosts 受管块
│  ├─ test_forward.py    SNI 转发：白名单语义、ClientHello 解析、转发与回收
│  ├─ test_cli_contract.py / test_config_contract.py / test_proxy_hardening.py
│  ├─ test_netutil_extra.py / test_pac.py / test_fuzz.py / test_integration_local.py
│  ├─ live_test.py       真网验证：需先 up -b
│  ├─ live_e2e.py        真网验证：DoH → TLS → probe 全表 → 选边 → 缓存往返
│  ├─ domain_audit.py    真网审计：43 个 Steam 域名直连可达性（--confirm N 复测）
│  └─ e2e_proxy.py       端到端：起服务后用 SOCKS5/HTTP 真打 Steam
└─ svpn/
   ├─ __main__.py        入口 → cli.main()
   ├─ cli.py             子命令、配置归一、后台进程、UAC、体检、SNI 转发接线
   ├─ proxy.py           SOCKS5 + HTTP CONNECT + 绝对 URI 转发，本地鉴权，统计
   ├─ rules.py           域名/网段名单 + Router 三档分流（whitelist/all/blacklist）
   ├─ netutil.py         DoH 解析、TCP/TLS/SNI 分层探测、经节点拨号
   ├─ discover.py        节点发现（配置/环境变量/系统代理/端口）+ 真握手验证
   ├─ probe.py           全链路体检：9 端点 × 直连/节点，给结论 + summary
   ├─ cdn.py             选边：DoH → TLS 淘汰 → 1MB 实测 → 缓存
   ├─ forward.py         TCP 固定转发 + 按 SNI 白名单的透明转发（由 cli 按 cdn.enabled 启停）
   ├─ system.py          WinINET 代理开关、hosts 受管块、备份与还原
   ├─ pac.py             PAC 生成（只有 Steam 后缀走 SOCKS5）
   ├─ split.py           【预留，未接线】只把 Valve 网段塞进 VPN 网卡（route add，需管理员）
   └─ steam.py           找 steam.exe / 启动（带 -tcp 等参数）/ 结束
```

一句话分工：**cli 编排，proxy 干活，rules 决定走不走节点，netutil 负责"到底哪一层挂了"，discover/probe/cdn 是三个探测工具，forward 只在 SNI 模式下当透明隧道，system/steam 碰系统。**

> `svpn/split.py` 是"给 WireGuard/TAP 这类 VPN 网卡只加 Steam 路由"的可选方案，**当前没有任何模块 import 它，也没有 CLI 入口**，属于预留代码，别指望 `svpn` 里有个命令能用它。

其他命令（用到再看）。解析器里共 **13 个条目**：**12 个公开子命令**（§3 的五个：`up`/`down`/`probe`/`cdn`/`hosts`，加下表的七个）+ `_serve`（内部命令，`up -b` 的后台进程自己用，别手动调）：

| 命令 | 作用 |
|---|---|
| `python -m svpn status` | 进程、端口、节点、模式、系统代理、CDN 缓存、hosts 条数、上次统计 |
| `python -m svpn doctor` | 体检：python / 配置 / 节点可达 / Steam 路径 / 端口 / 系统代理 / 缓存 / 两条关键域名探测；有未处理项返回 1（无节点时社区不可用属已知缺项） |
| `python -m svpn init [--force]` | 生成 `config.json`、备份系统代理、打印下一步 |
| `python -m svpn pac [--write] [--apply]` | 生成 PAC；`--write` 落盘，`--apply` 设为系统 PAC（只让 Steam 域名走 SOCKS5） |
| `python -m svpn system on\|off\|show` | 系统代理开 / 还原 / 查看原始注册表值 |
| `python -m svpn node scan [--json]` | 只做节点发现与验证，不改 `config.json` |
| `python -m svpn steam [--exe PATH] [--restart] [--system] [--no-autostart] [--args …]` | 拉起 Steam：代理没跑会自动拉起；带 `-tcp -noreactlogin -console -nointro`；`--system` 同时设系统代理 |

两个包装层都是**可选**的，等价于 `python -m svpn ...`：

- `svpn.cmd`（cmd，任何人可用）：`.\svpn.cmd up -b --system-proxy`，不带参数 = `status`
- `svpnctl.ps1`（PowerShell 参数风格，**给习惯 `-Background` / `-System` 这种写法的人**用）：`.\svpnctl.ps1 up -Background -System`。它不是必需品，参数最终也是透传给 `python -m svpn`

> 默认执行策略是 `Restricted` 的机器上，`.ps1` 直接调用会报「禁止运行脚本」：
> 用 `powershell -ExecutionPolicy Bypass -File .\svpnctl.ps1 status`，或一次性
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。`svpn.cmd` 不受此限制。

> **要改 `.cmd` 给自己加启动步骤？** 先读 [docs/WINDOWS.md](docs/WINDOWS.md) §1.3：注释必须 ASCII、
> 每个含中文的行后面必须紧跟一个空行、每处 `python` 调用都要写 `call`。违反任一条，用户双击时会看到
> `'?' is not recognized` 或"静默退出（exit 0）"，而且极难排查。

---

## 11. 安全边界

- **只听 `127.0.0.1`**：`listen_host` 默认回环，`null`/空串也会回落回环。不要为了"顺便给手机用"改成 `0.0.0.0`；真要共享，先把 `auth.username/password` 配好（SOCKS5 用户名密码 + HTTP Basic）。
- **白名单分流**：只有 `rules.py` 里的 Steam/Valve 后缀和网段走节点，国内站点直连；`hosts` 只写 CDN 域名。
- **SNI 转发不是开放代理**：只绑回环、只放行 `cdn.hosts` 白名单、白名单为空即全拒、非 TLS 首包直接断。
- **不做 TLS 中间人**：`hosts → 边缘 IP` 与 SNI 转发都是纯转发，TLS 端到端。**不装根证书，不碰证书库。**
- **hosts 写入只认合法条目**：`sanitize_entries` 挡换行/空格/`#`/标记伪造/主机名位置的 IP——`runtime/cdn-cache.json` 是可被替换的文件，不能信里面的字符串。
- **探测阶段不校验证书**：为了区分"握手被 RST"和"证书错误"，探测用 `CERT_NONE`，只用于诊断，不参与转发。
- **凭据**：上游账号明文存 `config.json`。`status` 打印时密码打码，但 `node scan` 末尾那行"采用: node set …"**是完整 URL**（方便复制），贴到哪之前先看清楚。这个文件别提交、别同步网盘。
- **系统改动可回滚**：只改 WinINET 代理项和 hosts 受管块，改前备份，`down`／`system off`／`hosts clear`／`uninstall.ps1` 还原。
- **不注入 Steam**：不读写游戏进程、不改 Steam 配置文件；没有服务、没有开机自启，进程退了就是退了。

---

## 12. 已知限制（不是 bug，别再当 bug 报）

1. **没有节点时社区/头像一定不可用。** `steamcommunity.com`、`avatars.steamstatic.com` 是 SNI 阻断，`probe` 会如实报 `需节点 · SNI 阻断`，退出码 1。要么配节点，要么用 `--lenient` 只卡真正的死端点。
2. **半开连接会一直等到 TCP 超时。** `_bridge` / `SniForwarder` 都用 `asyncio.gather(..., return_exceptions=True)`：它的语义是"等两个方向都自然结束"，**不会**在一侧结束后取消另一侧。所以客户端只发不收、对端也不关连接时，那条连接会挂到系统 TCP 超时（几分钟）才释放。取舍是稳（不会因为一侧异常把另一侧的字节截断），代价是极少数半开连接占着 fd。真要改得用 `wait(FIRST_COMPLETED)` + `cancel()`，属于已知设计取舍，本轮不动。
3. **`probe` 的 `cert_cn` 恒为空**：探测用 `CERT_NONE`，`getpeercert()` 返回 `{}`。看证书得另写解析（未实现）。
4. **`probe` 单轮可能出现一条 `不通 · 待排查`**：`store` 偶发解析到异常边缘，复跑即恢复（代理侧有多 IP failover）。
5. **`hosts` 写入需要一次管理员授权**；UAC 被拒只能手动用管理员 PowerShell 加 `--no-elevate`。
6. **`parse_node_url` 只认 socks5/http**：`ss://`、`vmess://`、`trojan://` 需要外部内核（sing-box/xray）转成本地 socks5 再 `node set`。
7. **`svpn/split.py` 未接线**（见 §10），`cdn.enabled` 的 SNI 模式才需要常驻进程。

---

## 附录：自检

```powershell
python tests/smoke_test.py                            # 离线冒烟：规则分流、SNI 解析、回环、CONNECT、鉴权（15 项，ALL PASS）
python -m unittest discover -s tests -p "test_*.py"    # 离线全量单测（当前 364 个用例）
python tests/live_e2e.py                               # 真网：DoH / TLS / probe / 选边 / 缓存
python tests/live_test.py                              # 真网：要先 python -m svpn up -b
```

离线用例不需要网络、不需要节点；`live_*.py` 要联网，且 `live_test.py` 必须先 `up -b`。

架构与数据流见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**，Windows 踩坑见 **[docs/WINDOWS.md](docs/WINDOWS.md)**。

## 另一个实现（C++）

同一套 CLI 与配置格式的 Windows 原生实现：**[zhe-du/svpn-cpp](https://github.com/zhe-du/svpn-cpp)**

- 单个 `svpn.exe`（约 1.6 MB），**零 DLL 依赖、不需要 Python 运行时**
- TLS 走 SChannel，转发是线程 + `select`，不是 asyncio
- **行为以本仓库为基准**，两者可互换 `config.json` 与 `runtime/cdn-cache.json`
- 发布包在 [Releases](https://github.com/zhe-du/svpn-cpp/releases)

取舍：C++ 版启动更快、分发更简单；Python 版改动更容易、调试更直观。按场景选。

---

## 声明

### 用途

做一件事：**给 Steam 的流量做本地分流**。商店、登录、下载走你指定的上游节点或 CDN 最优边缘，国内站点直连。
附带三个小功能：CDN 选边（写 hosts）、端点探测（`probe`）、PAC 生成。

**它不做什么**：

- **不是 VPN，没有虚拟网卡（TUN）**。只有 SOCKS5 / HTTP 代理入口（默认 `127.0.0.1:7899` / `:7900`），不认代理的程序用不上它。
- **不内置任何服务器或节点**。`upstream` 出厂是占位符 `NODE_HOST:1080`，你不填就永远是纯直连。它解决"Steam 该走哪条路"，不解决"怎么出境"。
- **不代理 UDP**。SOCKS5 只实现了 CONNECT，QUIC/HTTP3 与走 UDP 的游戏联机用不了。
- **不做 TLS 中间人**。不装根证书、不碰证书库，`hosts` 与 SNI 转发都是纯转发，TLS 端到端。

### 数据与隐私

- **会联网**，只在这几件事上：DoH 查 CDN 域名的 A 记录（`doh.pub` / `dns.alidns.com` / `1.12.12.12` / `223.5.5.5`）、对候选边缘 IP 做 TLS 握手与 1MB 测速、`probe` 连 Steam 各域名、`node scan` 连本机常见代理端口。
- **不上传任何数据**。没有账号、没有遥测、没有中心服务器；所有结果都落在本地 `runtime/`。
- **会写本地文件**：`runtime/svpn.log`（运行日志）、`stats.json`（连接数与流量计数）、`probe.json`、`cdn-cache.json`。
- **两个备份文件里有你的系统信息**，默认不随仓库发布：
  - `runtime/hosts.backup` —— 写 hosts 前的原始内容（含你本机 hosts 全文）
  - `runtime/wininet-backup.json` —— 接管前的系统代理设置（可能含公司内网代理地址）
- **`config.json` 里上游账号是明文**（SOCKS5/HTTP 认证决定的，本机代理协议本身没有更好的存法）。这个文件已列入 `.gitignore`，别提交、别同步网盘、贴 `node scan` 输出前先看一眼——那行"采用: node set …"是完整 URL。

### 安全

- **写 hosts 需要一次管理员授权（UAC）**。只增删带标记的托管块（`# >>> steam-vpn managed block >>>` … `# <<< … <<<`），块外字节原样保留（含 BOM 与编码），写前备份、写时先落 `.tmp` 再原子替换。
- **改系统代理不需要管理员**（写的是 `HKCU`）。只在有备份时才还原；没有备份就明说"没动过"，绝不猜默认值去清空你的设置。
- **不装服务、不加开机自启、不注入进程、不加载驱动**。`up` 起的只是一个普通后台进程，`down` 就结束。
- **默认只监听回环**。把 `listen_host` 改成 `0.0.0.0` 等于把它变成开放代理，任何人都能用——真要共享，先配 `auth.username/password`。
- **SNI 转发不是开放代理**：只绑回环、只放行 `cdn.hosts` 白名单、白名单为空则全部拒绝。
- **只接受合法 hosts 条目**：IP 必须合法、主机名不许出现空格/`#`/标记伪造，`cdn-cache.json` 是可被替换的文件，里面的字符串一律不信任。

### 兼容性

- **Windows 10 / 11 + Python 3.10+**。依赖 WinINET 注册表与 `hosts`，macOS / Linux 不支持。
- **零第三方依赖**，全部标准库；`tests/` 离线可跑，不需要网络与节点。
- **只代理 TCP**；半开连接会等系统 TCP 超时（见 §12 第 2 条）。
- 上游只认 `socks5://` 与 `http://`；`ss://` / `vmess://` / `trojan://` 需要 sing-box / xray 之类先转成本地 socks5。

### 免责

软件按「现状」提供，不附带任何明示或暗示的担保。因使用或无法使用本工具造成的直接或间接损失（包括但不限于误写 hosts 导致某些网站打不开、改了系统代理忘了还原、下载加速无效），作者不承担责任。

工具本身不提供任何网络出口，也不做任何绕过；你用什么样的上游、访问什么内容，由你自己决定并自行承担相应责任。跨境网络访问请遵守所在地法律法规。

### 许可

MIT，见 [LICENSE](LICENSE)。
