"""svpn 命令行入口。

设计原则：每条命令都要能单独跑、单独回滚；失败时说清楚下一步该敲什么。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from . import cdn as cdnmod
from . import discover
from . import netutil
from . import probe as probemod
from . import system as sysx
from .netutil import Node
from .pac import build_pac
from .proxy import ProxyServer
from .rules import STEAM_CIDRS, STEAM_SUFFIXES, Router

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
CONFIG = ROOT / "config.json"
PIDFILE = RUNTIME / "svpn.pid"
LOGFILE = RUNTIME / "svpn.log"
STATSFILE = RUNTIME / "stats.json"
PACFILE = RUNTIME / "svpn.pac"
CDNCACHE = RUNTIME / "cdn-cache.json"
PROBEFILE = RUNTIME / "probe.json"

PLACEHOLDERS = ("NODE_HOST", "NODE", "HOST", "EXAMPLE.COM", "CHANGE_ME", "")

# 契约（SPEC §4.1）：顶层键集就是下面这些，多一个少一个都算漂移。
# proxy_hosts / direct_hosts 由 Router 消费（显式覆盖优先于白/黑名单），
# 以前只写在 config.example.json 里、默认配置没有，导致「示例与默认值漂移」。
DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 7899,
    "http_port": 7900,
    "mode": "whitelist",
    "connect_timeout": 12,
    "auto_node": True,
    "upstream": {"kind": "socks5", "host": "NODE_HOST", "port": 1080,
                 "username": "", "password": ""},
    "auth": {"username": "", "password": ""},
    "browser": {"apply_system_proxy": True, "use_pac": False,
                "bypass": "<local>;localhost;127.*;10.*;"
                          + ";".join(f"172.{n}.*" for n in range(16, 32))
                          + ";192.168.*;*.cn"},
    "cdn": {"enabled": False, "refresh_hours": 6, "sni_port": 443,
            "hosts": list(cdnmod.CDN_DOMAINS)},
    "proxy_hosts": [],
    "direct_hosts": [],
}

MODES = ("whitelist", "all", "blacklist")
UPSTREAM_KINDS = ("socks5", "http", "direct")

# 不在模板里的可选高级键（Router / build_pac 会消费）。用户写了就放行并归一，
# 但不写进 config.example.json —— 默认值走 rules.py 内置名单。
EXTRA_KEYS = ("proxy_suffixes", "direct_suffixes")

OK = "[+]"
INFO = "[i]"
WARN = "[!]"
EQ = "[=]"


# ------------------------------------------------------------------ 基础设施
def setup_log(verbose: bool = False, to_file: bool = True) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        handlers.append(logging.FileHandler(LOGFILE, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", force=True)


# ------------------------------------------------------------------ 配置
def _as_number(value, default):
    """把配置里的「数值」归一成 int/float；None/""/乱码一律回落默认值。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if num != num or num in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return int(num) if num.is_integer() else num


def _as_port(value, default: int, allow_zero: bool = False) -> int:
    num = _as_number(value, default)
    try:
        port = int(num)
    except (TypeError, ValueError):
        return default
    if allow_zero and port == 0:
        return 0
    return port if 1 <= port <= 65535 else default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_text(value, default: str) -> str:
    if value is None or isinstance(value, (dict, list)):
        return default
    return str(value)


def _as_host_list(value, default: list[str]) -> list[str]:
    """list[str]；用户写成逗号/空格分隔的字符串也认（配置文件手写常见）。"""
    if isinstance(value, str):
        items: list = [p for p in value.replace(",", " ").split() if p]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return list(default)
    out: list[str] = []
    for item in items:
        if isinstance(item, (dict, list)) or item is None:
            logging.debug("config: 丢弃非法域名项 %r", item)
            continue
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalize(cfg: dict) -> dict:
    """把合并后的配置逐键规整到确定的类型/取值范围上。原地修改并返回。"""
    cfg["listen_host"] = _as_text(cfg.get("listen_host"),
                                  DEFAULT_CONFIG["listen_host"]) or "127.0.0.1"
    cfg["listen_port"] = _as_port(cfg.get("listen_port"),
                                  DEFAULT_CONFIG["listen_port"])
    # http_port=0 是合法值（表示不起 HTTP 入口），不能被打回默认 7900
    cfg["http_port"] = _as_port(cfg.get("http_port"),
                                DEFAULT_CONFIG["http_port"], allow_zero=True)
    mode = _as_text(cfg.get("mode"), "").strip().lower()
    cfg["mode"] = mode if mode in MODES else DEFAULT_CONFIG["mode"]
    cfg["connect_timeout"] = _as_number(cfg.get("connect_timeout"),
                                        DEFAULT_CONFIG["connect_timeout"])
    cfg["auto_node"] = _as_bool(cfg.get("auto_node"), DEFAULT_CONFIG["auto_node"])

    up = cfg.get("upstream")
    up = up if isinstance(up, dict) else {}
    kind = _as_text(up.get("kind"), "socks5").strip().lower()
    cfg["upstream"] = {
        "kind": kind if kind in UPSTREAM_KINDS else "socks5",
        "host": _as_text(up.get("host"), DEFAULT_CONFIG["upstream"]["host"]),
        "port": _as_port(up.get("port"), DEFAULT_CONFIG["upstream"]["port"]),
        "username": _as_text(up.get("username"), ""),
        "password": _as_text(up.get("password"), ""),
    }

    auth = cfg.get("auth")
    auth = auth if isinstance(auth, dict) else {}
    cfg["auth"] = {"username": _as_text(auth.get("username"), ""),
                   "password": _as_text(auth.get("password"), "")}

    browser = cfg.get("browser")
    browser = browser if isinstance(browser, dict) else {}
    cfg["browser"] = {
        "apply_system_proxy": _as_bool(browser.get("apply_system_proxy"), True),
        "use_pac": _as_bool(browser.get("use_pac"), False),
        "bypass": _as_text(browser.get("bypass"),
                           DEFAULT_CONFIG["browser"]["bypass"]),
    }

    cdn = cfg.get("cdn")
    cdn = cdn if isinstance(cdn, dict) else {}
    hours = _as_number(cdn.get("refresh_hours"),
                       DEFAULT_CONFIG["cdn"]["refresh_hours"])
    cfg["cdn"] = {
        "enabled": _as_bool(cdn.get("enabled"), False),
        "refresh_hours": hours if isinstance(hours, (int, float)) and hours > 0 else 6,
        "sni_port": _as_port(cdn.get("sni_port"), DEFAULT_CONFIG["cdn"]["sni_port"]),
        "hosts": _as_host_list(cdn.get("hosts"), DEFAULT_CONFIG["cdn"]["hosts"]),
    }

    cfg["proxy_hosts"] = _as_host_list(cfg.get("proxy_hosts"), [])
    cfg["direct_hosts"] = _as_host_list(cfg.get("direct_hosts"), [])

    # 可选高级键：不在 DEFAULT_CONFIG 里（模板不写），但用户手工加了要能生效。
    # 早期版本靠「未知键一律并入」把它们透传下来，现在改成显式白名单，
    # 既保住这两项能力，又不让拼错的键悄悄混进运行配置。
    for extra in EXTRA_KEYS:
        if extra in cfg:
            cfg[extra] = _as_host_list(cfg.get(extra), [])
    return cfg


def merge_config(user: dict | None) -> dict:
    """把用户配置合并进 DEFAULT_CONFIG 的深拷贝，返回规范化后的配置。

    规则（SPEC §4.1）：
    - 顶层键以 `_` 开头、或不在 DEFAULT_CONFIG 里的 → 丢弃 + logging.debug；
    - 嵌套 dict 按子键合并（`{"cdn": {"refresh_hours": 2}}` 只动这一项）；
    - 数值键 None/""/"abc" → 回落默认值，绝不抛 TypeError。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(user, dict):
        logging.debug("config: 顶层不是对象，忽略")
        return _normalize(cfg)
    for key, value in user.items():
        if not isinstance(key, str) or key.startswith("_"):
            logging.debug("config: 丢弃私有键 %r", key)
            continue
        if key not in DEFAULT_CONFIG and key not in EXTRA_KEYS:
            logging.debug("config: 丢弃未知键 %r", key)
            continue
        # EXTRA_KEYS 不在 DEFAULT_CONFIG 里，用 .get 取基线（None → 走下面的 else 分支）
        base = cfg.get(key)
        if isinstance(base, dict):
            if not isinstance(value, dict):
                logging.debug("config: %s 应为对象，忽略", key)
                continue
            for sub, subval in value.items():
                if not isinstance(sub, str) or sub.startswith("_") or sub not in base:
                    logging.debug("config: 丢弃 %s 下的未知键 %r", key, sub)
                    continue
                base[sub] = subval
        else:
            cfg[key] = value
    return _normalize(cfg)


def load_config() -> dict:
    if not CONFIG.exists():
        return merge_config({})
    raw = CONFIG.read_text(encoding="utf-8")
    try:
        user = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"{WARN} config.json 解析失败: {exc}")
        raise SystemExit(2) from exc
    return merge_config(user)


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _decode(raw: bytes | None) -> str:
    """解系统命令输出。

    tasklist / netstat 在中文 Windows 上吐的是 OEM 代码页（cp936/cp437 视控制台而定），
    用 text=True 让 Python 按 locale 猜，在 `-X utf8` / PYTHONUTF8=1 下会解码失败，
    异常被吞掉后就成了「进程没在跑」的误判。所以统一拿 bytes 自己按 mbcs 解。
    """
    if not raw:
        return ""
    try:
        return raw.decode("mbcs", "replace")        # Windows 专用；非 Windows 抛 LookupError
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _run_quiet(cmd: list[str], timeout: float = 20.0) -> str:
    """跑一条只读的系统命令，返回解码后的 stdout；失败返回空串（绝不抛）。"""
    kwargs: dict = {"capture_output": True}
    if IS_WIN:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        res = subprocess.run(cmd, timeout=timeout, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _decode(res.stdout)


def pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    out = _run_quiet(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
    want = str(pid)
    for line in out.splitlines():
        parts = line.split()
        # tasklist /NH 的列：映像名(可能带空格) … PID … 会话名 … 内存
        if len(parts) >= 2 and want in parts[1:3]:
            return True
    return False


def read_pid() -> int | None:
    if not PIDFILE.exists():
        return None
    try:
        pid = int(PIDFILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    return pid if pid_alive(pid) else None


def _port_busy(host: str, port: int) -> bool:
    sock = socket.socket()
    try:
        sock.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def _port_owner(port: int) -> str:
    """尽力找出占用端口的进程，纯排障用，失败返回空串。"""
    if not IS_WIN:
        return ""
    out = _run_quiet(["netstat", "-ano", "-p", "TCP"])
    pid = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                and parts[1].endswith(f":{port}"):
            pid = parts[-1]
            break
    if not pid:
        return ""
    info = _run_quiet(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
    tokens = info.split()
    name = tokens[0] if tokens else "?"
    return f"pid={pid} {name}"


def _cfg_port(value, default: int = 0) -> int:
    """配置里的端口取整用；`int(None)` / `int("abc")` 都不许炸。"""
    num = _as_number(value, default)
    try:
        port = int(num)
    except (TypeError, ValueError):
        return default
    return port if 0 <= port <= 65535 else default


def _node_from_cfg(cfg: dict) -> Node | None:
    up = cfg.get("upstream") or {}
    kind = (up.get("kind") or "").lower()
    host = up.get("host") or ""
    port = _cfg_port(up.get("port"))
    if kind not in ("socks5", "http") or host.upper() in PLACEHOLDERS or not port:
        return None
    return Node(kind=kind, host=host, port=port,
                username=up.get("username") or "",
                password=up.get("password") or "", label="config")


def _resolve_node(spec: str | None, cfg: dict) -> Node | None:
    """spec: None/auto -> 自动；none -> 不用；否则当成 URL。"""
    if spec and spec.lower() == "none":
        return None
    if spec and spec.lower() not in ("auto", ""):
        return netutil.parse_node_url(spec)
    node = _node_from_cfg(cfg)
    if node and discover.verify(discover.Found(node), timeout=5).steam_ok:
        return node
    found = discover.best(cfg)
    return found.node if found else node


def _banner() -> None:
    print(f"svpn {__version__}  ·  Steam 分流/加速")


def _dw(text: str) -> int:
    """终端显示宽度：CJK 算 2 列。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pl(text: str, width: int) -> str:
    return text + " " * max(0, width - _dw(text))


def _pr(text: str, width: int) -> str:
    return " " * max(0, width - _dw(text)) + text


def _tail_log(n: int = 8) -> str:
    try:
        return "\n".join(LOGFILE.read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-n:])
    except OSError:
        return ""


# ------------------------------------------------------------------ up / serve
def _serve_cfg(cfg: dict) -> dict:
    cfg = json.loads(json.dumps(cfg))
    hours = _cache_ttl_hours(cfg)
    cfg["ip_map"] = cdnmod.best_ip_map(CDNCACHE, max_age=hours * 3600)
    return cfg


def cmd_up(args) -> int:
    cfg = load_config()
    if args.port:
        if not 1 <= int(args.port) <= 65535:
            print(f"{WARN} --port 必须在 1-65535 之间：{args.port}")
            return 2
        cfg["listen_port"] = int(args.port)
    if args.mode:
        cfg["mode"] = args.mode
    if args.node:
        node = netutil.parse_node_url(args.node)
        discover.save_upstream(CONFIG, node)
        cfg = load_config()
        print(f"{OK} 节点已写入 config.json: {node.url()}")

    running = read_pid()
    if running:
        print(f"{EQ} 已在运行 (pid {running})；改配置请先 `svpn down`")
        return 0

    node = _node_from_cfg(cfg)
    if node is None and not args.no_auto and cfg.get("auto_node", True):
        print(f"{INFO} 没配置节点，扫描本机已装好的代理客户端…")
        found = discover.scan(cfg)[:6]
        for f in found:
            print("    " + f.line())
        best = next((f for f in found if f.steam_ok), None)
        if best:
            discover.save_upstream(CONFIG, best.node)
            cfg = load_config()
            print(f"{OK} 自动选用 {best.node.url()}（{best.ms:.0f}ms），已写入 config.json")
        else:
            print(f"{WARN} 没找到能到 Steam 的节点：商店/下载照常加速，社区需节点")
            print(f"    有节点后执行: python -m svpn node set socks5://host:port")

    problems = []
    listen_port = _cfg_port(cfg.get("listen_port"), DEFAULT_CONFIG["listen_port"])
    http_port = _cfg_port(cfg.get("http_port"))
    # SOCKS5 与 HTTP 共用同一个监听地址，撞库时第二个 bind 只会抛裸 OSError 10048，
    # 在这里先拦下来，给一条能照抄的命令。
    if http_port and listen_port == http_port:
        print(f"{WARN} SOCKS5 与 HTTP 不能同端口（都是 {listen_port}）："
              f"用 --port 17899，或改 config.json 的 http_port")
        return 1
    for port in (listen_port, http_port):
        if port and _port_busy(cfg["listen_host"], port):
            problems.append(f"端口 {port} 被占用 {_port_owner(port)}")
    if problems:
        for p in problems:
            print(f"{WARN} {p}")
        print("    换端口: python -m svpn up -b --port 17899"
              "（http 端口在 config.json 的 http_port 改）")
        return 1

    cdn_cfg = cfg.get("cdn") or {}
    if cdn_cfg.get("enabled"):
        sni_port = _cfg_port(cdn_cfg.get("sni_port"), 443)
        if sni_port in (listen_port, http_port):
            print(f"{WARN} cdn.sni_port={sni_port} 与本机代理端口冲突："
                  f"改 config.json 的 listen_port/http_port，或关掉 cdn.enabled"
                  f"（注意 sni_port 一般必须是 443 —— hosts 只认 IP，客户端连的就是 "
                  f"127.0.0.1:443，改别的端口转发会起来但没人来连）")
            return 1
        # SNI 透明转发是可选增强：占不到 443 也照常起代理，退回 hosts 直指最优 IP
        if sni_port and _port_busy("127.0.0.1", sni_port):
            print(f"{WARN} SNI 转发端口 {sni_port} 已被占用 {_port_owner(sni_port)}："
                  f"仍会启动，但 SNI 透明转发会跳过（下载加速退回 hosts 直指 IP）")

    if args.background:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        logf = open(LOGFILE, "a", encoding="utf-8")     # noqa: SIM115
        detach = 0x00000008 | 0x00000200
        cmd = [sys.executable, "-m", "svpn", "_serve"]
        if args.system_proxy:
            cmd.append("--system-proxy")
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=logf, stderr=logf,
                                stdin=subprocess.DEVNULL, creationflags=detach,
                                close_fds=True)
        # pid 文件要等「真的起来了」再写：先写后删的写法在并发启动时会踩到
        # ——第二个 up 把自己的 pid 覆盖上去、bind 失败后又 unlink，
        # 结果是把第一个实例的 pid 记录删掉，`down` 从此找不到它，端口永久被占。
        ok = False
        for _ in range(20):
            time.sleep(0.15)
            if not pid_alive(proc.pid):
                break
            # 端口已被监听 = 我们的子进程 bind 成功（预检已确认起跑前是空闲的）
            if _port_busy(cfg["listen_host"], _cfg_port(cfg.get("listen_port"), 7899)):
                ok = True
                break
        if not ok:
            print(f"{WARN} 后台启动失败，日志尾部：")
            print(_tail_log())
            # 只清掉确实是自己那条记录，别误删别的实例
            try:
                if PIDFILE.exists() and PIDFILE.read_text(encoding="utf-8").strip() == str(proc.pid):
                    PIDFILE.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            return 1
        PIDFILE.write_text(str(proc.pid), encoding="utf-8")
        if args.system_proxy:
            print(f"{OK} 系统代理: {_apply_system(cfg)}")
        print(f"{OK} 后台已启动 pid={proc.pid}  "
              f"socks5://{cfg['listen_host']}:{cfg['listen_port']}  "
              f"http://{cfg['listen_host']}:{cfg.get('http_port')}")
        _print_route_summary(cfg)
        return 0

    try:
        return asyncio.run(_serve_async(cfg, system_proxy=args.system_proxy))
    except KeyboardInterrupt:
        return 130


async def _serve_async(cfg: dict, system_proxy: bool = False) -> int:
    setup_log(verbose=bool(os.environ.get("SVPN_DEBUG")))
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PACFILE.write_text(build_pac(cfg["listen_host"], cfg["listen_port"],
                                 cfg.get("proxy_suffixes"),
                                 cfg.get("direct_suffixes"),
                                 mode=cfg.get("mode", "whitelist")),
                       encoding="utf-8")
    srv = ProxyServer(_serve_cfg(cfg))
    await srv.start()
    sni_srv = await _start_sni(cfg, srv)
    if system_proxy:
        print(f"{OK} 系统代理: {_apply_system(cfg)}")
    print(f"{OK} SOCKS5 {cfg['listen_host']}:{cfg['listen_port']}   "
          f"HTTP {cfg['listen_host']}:{cfg.get('http_port')}")
    _print_route_summary(cfg, srv)
    print("    Ctrl+C 退出（退出会自动还原系统代理）")
    try:
        await srv.serve_forever(str(STATSFILE))
    finally:
        if sni_srv is not None:
            try:
                await sni_srv.close()
            except OSError:
                pass
        await srv.close()
        snap = srv.stats.snapshot()
        print(f"\n{INFO} 本次 {snap['connections']} 连接 / 下行 {snap['down_mb']}MB"
              f" / 错误 {snap['errors']}")
        if system_proxy:
            sysx.restore_wininet(RUNTIME)
            print(f"{INFO} 系统代理已还原")
    return 0


def _sni_allow(cdn_cfg: dict) -> list[str]:
    """SNI 白名单：优先 cdn.hosts，空则退回 CDN_DOMAINS（绝不放行全部）。"""
    allow = [str(h) for h in (cdn_cfg.get("hosts") or cdnmod.CDN_DOMAINS) if h]
    return allow or list(cdnmod.CDN_DOMAINS)


async def _start_sni(cfg: dict, srv: ProxyServer):
    """按 cdn.enabled 挂上 SNI 透明转发；端口占用只警告，不拖垮主服务。"""
    cdn_cfg = cfg.get("cdn") or {}
    if not cdn_cfg.get("enabled"):
        return None
    from .forward import SniForwarder  # noqa: PLC0415  可选依赖路径，别拖慢启动

    allow = _sni_allow(cdn_cfg)
    sni_srv = SniForwarder(srv.upstream, _cfg_port(cdn_cfg.get("sni_port"), 443),
                           allow_suffixes=allow, stats=srv.stats,
                           ip_map=cdnmod.best_ip_map(
                               CDNCACHE, max_age=_cache_ttl_hours(cfg) * 3600))
    try:
        await sni_srv.start()
    except OSError as exc:
        print(f"{WARN} SNI 转发端口被占用，跳过（下载加速退回 hosts 直指 IP）: {exc}")
        return None
    print(f"{OK} SNI 透明转发 127.0.0.1:{sni_srv.listen_port} 白名单 {len(allow)} 个域名")
    return sni_srv


def _print_route_summary(cfg: dict, srv: ProxyServer | None = None) -> None:
    node = _node_from_cfg(cfg)
    if node:
        print(f"    节点 {node.url()}   模式 {cfg.get('mode')}")
    else:
        print(f"    节点 未配置（纯直连分流）   模式 {cfg.get('mode')}")
    cdn_cfg = cfg.get("cdn") or {}
    if cdn_cfg.get("enabled"):
        print(f"    CDN 模式 SNI 透明转发:{_cfg_port(cdn_cfg.get('sni_port'), 443)}"
              f"（hosts 指向 127.0.0.1，需 svpn up 常驻）")
    else:
        print("    CDN 模式 hosts 直指最优 IP")
    ipmap = cdnmod.best_ip_map(CDNCACHE,
                               max_age=_cache_ttl_hours(cfg) * 3600)
    if ipmap:
        print(f"    CDN 已选边 {len(ipmap)} 个域名（缓存 {CDNCACHE.name}）")
    else:
        print("    CDN 未选边：跑 `python -m svpn cdn --refresh` 再 "
              "`python -m svpn hosts apply`")


def cmd_serve(args) -> int:
    return asyncio.run(_serve_async(load_config(), system_proxy=args.system_proxy))


# ------------------------------------------------------------------ down
def cmd_down(args) -> int:
    pid = read_pid()
    if pid:
        # 同样走 bytes 解码：taskkill 的中文提示在 UTF-8 模式下会解码失败
        logging.debug("taskkill: %s",
                      _run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"]))
        print(f"{OK} 已停止 pid={pid}")
    else:
        print(f"{EQ} 没有在运行的实例")
        # pid 文件丢了但端口还被占着 = 上次异常退出的孤儿进程，直接给出手动出路，
        # 否则用户会看到「没在运行」却怎么也起不来（10048）
        cfg = load_config()
        for port in (_cfg_port(cfg.get("listen_port"), 7899),
                     _cfg_port(cfg.get("http_port"), 0)):
            if port and _port_busy(cfg.get("listen_host", "127.0.0.1"), port):
                owner = _port_owner(port)
                print(f"{WARN} 但端口 {port} 仍被占用 {owner}")
                if owner.startswith("pid="):
                    print(f"    手动结束: taskkill /PID {owner[4:].split()[0]} /T /F")
    PIDFILE.unlink(missing_ok=True)
    if not args.keep_system:
        before = sysx.read_wininet()
        res = sysx.restore_wininet(RUNTIME)
        # 没备份就什么都没做：这时候说「已还原」会让用户以为回滚了，
        # 实际 WinINET 还指着已经死掉的端口，浏览器全部打不开。
        if res.get("ok") is False:
            print(f"{WARN} 系统代理没动过：{res.get('reason')}（可能从没接管过）")
            print(f"    当前 ProxyEnable={before.get('ProxyEnable')} "
                  f"{before.get('ProxyServer') or before.get('AutoConfigURL') or ''}")
        else:
            print(f"{INFO} 系统代理已还原（原 ProxyEnable={before.get('ProxyEnable')}）")
    if args.clear_hosts:
        if _hosts_entries():
            cmd_hosts(argparse.Namespace(action="clear", no_elevate=False, timeout=3.0))
        else:
            print(f"{EQ} hosts 里没有 svpn 记录，跳过")
    if STATSFILE.exists():
        try:
            snap = json.loads(STATSFILE.read_text(encoding="utf-8"))
            print(f"{INFO} 上次 {snap.get('connections')} 连接 / "
                  f"下行 {snap.get('down_mb')}MB")
        except json.JSONDecodeError:
            pass
    return 0


# ------------------------------------------------------------------ status
def cmd_status(args) -> int:
    cfg = load_config()
    pid = read_pid()
    _banner()
    print(f"进程     {'运行中 pid=' + str(pid) if pid else '未运行'}")
    for port in (_cfg_port(cfg.get("listen_port"), 7899),
                 _cfg_port(cfg.get("http_port"))):
        if not port:
            continue
        listening = _port_busy(cfg["listen_host"], port)
        print(f"端口     {cfg['listen_host']}:{port}  "
              f"{'监听中' if listening else '未监听'}")
    node = _node_from_cfg(cfg)
    print(f"节点     {node.url() if node else '未配置（纯直连）'}")
    print(f"模式     {cfg.get('mode')}")
    if IS_WIN:
        w = sysx.read_wininet()
        print(f"系统代理 ProxyEnable={w.get('ProxyEnable')} "
              f"{w.get('ProxyServer') or w.get('AutoConfigURL') or ''}")
    cache = cdnmod.load_cache(CDNCACHE, max_age=10 ** 9)
    if cache:
        age = (time.time() - max(v.get("ts", 0) for v in cache.values())) / 60
        print(f"CDN缓存  {len(cache)} 个域名，{age:.0f} 分钟前更新")
    else:
        print("CDN缓存  无")
    entries = _hosts_entries()
    print(f"hosts    {len(entries)} 条 svpn 记录" if entries else "hosts    无 svpn 记录")
    if STATSFILE.exists():
        try:
            snap = json.loads(STATSFILE.read_text(encoding="utf-8"))
            print(f"上次统计 {snap.get('connections')} 连接 / "
                  f"下行 {snap.get('down_mb')}MB / 错误 {snap.get('errors')}")
            for host, n in (snap.get("top_hosts") or [])[:5]:
                print(f"         {n:>5}x {host}")
        except json.JSONDecodeError:
            pass
    return 0


# ------------------------------------------------------------------ probe
def probe_unusable(rows) -> list[str]:
    """按 SPEC §4.3 判定「怎么都用不了」的域名。

    规则本体收在 `probe.summarize()` 里（单一事实来源），这里只做兼容包装，
    免得 cli 和 probe 各判一套、日后改一处漏一处。
    """
    return list(probemod.summarize(rows, None)["unusable"])


def cmd_probe(args) -> int:
    cfg = load_config()
    node = _resolve_node(args.node, cfg)
    print(f"{INFO} 节点: {node.url() if node else '无（只看直连）'}")
    rows = probemod.run(node, timeout=args.timeout)
    print(probemod.format_table(rows))
    # 原有五键（node/direct_ok/need_node/dead/ok）必须保留，unusable 只增不改
    summary = probemod.summarize(rows, node)
    unusable = summary["unusable"]
    # 全新安装 / 删过 runtime 时这里以前直接 FileNotFoundError，探测白跑
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PROBEFILE.write_text(json.dumps(
        {"ts": time.time(), "node": node.url() if node else None,
         "rows": [asdict(r) for r in rows], "summary": summary},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    if summary["need_node"]:
        print(f"{WARN} 必须走节点: {', '.join(summary['need_node'])}")
        if not node:
            print("    配一个节点: python -m svpn node set socks5://host:port")
        elif not any(r.node and r.node.get("ok") for r in rows
                     if r.host in summary["need_node"]):
            print("    当前节点到这些域名也不通，换节点")
    if summary["direct_ok"]:
        print(f"{OK} 可直连: {', '.join(summary['direct_ok'])}")
    if unusable:
        print(f"{WARN} 不可用 {len(unusable)} 个: {', '.join(unusable)}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("退出码 0 = 全部端点可用；1 = 有端点不可用（见 summary.unusable）")
    if args.lenient:
        # 旧行为：只有「不通 · 待排查」算失败，社区没节点也放行
        return 1 if summary["dead"] else 0
    return 1 if unusable else 0


# ------------------------------------------------------------------ node
def cmd_node(args) -> int:
    cfg = load_config()
    if args.action == "scan":
        print(f"{INFO} 扫描配置 / 环境变量 / 系统代理 / 常见客户端端口…")
        found = discover.scan(cfg)
        if not found:
            print(f"{WARN} 一个候选都没有。装个客户端，或直接 "
                  "`node set socks5://host:port`")
            return 1
        print(f"{_pl('状态', 14)}{_pr('延迟', 8)}  {_pl('协议', 8)}"
              f"{_pl('地址', 22)}{_pl('来源', 28)}备注")
        print("-" * 100)
        for f in found:
            print(f.line())
        if args.json:
            print(json.dumps([{"node": f.node.url(), "steam_ok": f.steam_ok,
                               "ok": f.ok, "ms": round(f.ms, 1),
                               "source": f.source, "err": f.err} for f in found],
                             ensure_ascii=False, indent=2))
        good = [f for f in found if f.steam_ok]
        print()
        if good:
            print(f"{OK} 可用节点 {len(good)} 个，最快 {good[0].node.url()} "
                  f"{good[0].ms:.0f}ms")
            print(f"    采用: python -m svpn node set "
                  f"{good[0].node.url(hide_secret=False)}")
        else:
            print(f"{WARN} 找到端口但都到不了 Steam，检查客户端是否开启/选好节点")
        return 0

    if args.action == "set":
        if not args.url:
            print(f"{WARN} 用法: python -m svpn node set socks5://user:pass@host:port")
            return 2
        node = netutil.parse_node_url(args.url)
        # 自环保护：把 svpn 自己的端口设成上游 = 所有流量绕回自身，fd 几秒打满
        if discover.is_self(node, cfg):
            print(f"{WARN} {node.url()} 就是 svpn 自己（listen_port/http_port/sni_port）")
            print(f"    换个端口，或 `python -m svpn node scan` 找本机代理客户端")
            return 1
        found = discover.verify(discover.Found(node))
        if found.steam_ok:
            print(f"{OK} 节点验证通过 {found.ms:.0f}ms  HTTP {found.http_code}")
        elif found.ok:
            print(f"{WARN} 代理通了但到不了 Steam: {found.err or found.http_code}")
        else:
            print(f"{WARN} 节点不可用: {found.err}")
            return 1
        discover.save_upstream(CONFIG, node)
        print(f"{OK} 已写入 config.json: {node.url()}")
        return 0

    cfg["upstream"] = {"kind": "socks5", "host": "NODE_HOST", "port": 1080,
                       "username": "", "password": ""}
    save_config(cfg)
    print(f"{OK} 已清空节点（回到纯直连分流）")
    return 0


# ------------------------------------------------------------------ cdn
def cmd_cdn(args) -> int:
    cfg = load_config()
    domains = [args.domain] if args.domain else None
    cache = cdnmod.load_cache(CDNCACHE, max_age=10 ** 9)
    need = args.refresh or not cache or bool(domains)
    if need:
        targets = domains or [d for d in cdnmod.CDN_DOMAINS
                              if d not in cache or args.refresh]
        print(f"{INFO} 解析 + 竞速 {len(targets)} 个域名"
              f"（DoH 绕污染，TLS 握手淘汰，1MB 实测限速）…")
        fresh = cdnmod.refresh(targets, bench=not args.no_bench, timeout=args.timeout)
        cache.update(fresh)
        cdnmod.save_cache(cache, CDNCACHE)
    print(f"{_pl('域名', 38)}{_pl('最优 IP', 17)}{_pr('握手', 7)}{_pr('实测', 10)}"
          f"{_pr('更新', 7)}  候选")
    print("-" * 92)
    ttl_h = _cache_ttl_hours(cfg)
    now = time.time()
    for domain, info in sorted(cache.items()):
        ip = info.get("best_ip") or "-"
        ms = f"{info.get('best_ms', 0):.0f}ms"
        mbps = f"{info.get('best_mbps', 0):.2f}MB/s" if info.get("best_mbps") else "  -"
        age_h = (now - float(info.get("ts") or 0)) / 3600
        age = f"{age_h * 60:.0f}m" if age_h < 1 else f"{age_h:.1f}h"
        if age_h > ttl_h:
            age += "*"
        print(f"{_pl(domain, 38)}{_pl(ip, 17)}{_pr(ms, 7)}{_pr(mbps, 10)}"
              f"{_pr(age, 7)}  {info.get('candidates', 0)}")
    print()
    print(f"    * 超过 cdn.refresh_hours={ttl_h:g}h 视为过期（代理不再使用），"
          f"`cdn --refresh` 重测")
    print()
    if any(i.get("best_mbps") for i in cache.values()):
        best = max(cache.values(), key=lambda i: i.get("best_mbps") or 0)
        print(f"{OK} 最快边缘 {best.get('best_ip')} @ {best.get('best_mbps'):.2f}MB/s")
    print("    让 Steam 用上: python -m svpn hosts apply（需一次管理员授权）")
    if args.json:
        print(json.dumps(cache, ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------------------------------ hosts
def _hosts_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    raw = sysx.read_hosts()
    inside = False
    for line in raw.splitlines():
        if line.strip() == sysx.MARK_BEGIN:
            inside = True
            continue
        if line.strip() == sysx.MARK_END:
            inside = False
            continue
        if inside and line.strip() and not line.strip().startswith("#"):
            parts = line.split()
            if len(parts) >= 2:
                entries[parts[0]] = " ".join(parts[1:])
    return entries


def _is_admin() -> bool:
    if not IS_WIN:
        return False
    try:
        import ctypes  # noqa: PLC0415
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _elevate(action: str) -> bool:
    """用 UAC 重新拉起自己执行 hosts 写入。返回是否「跑完了」，成败靠 hosts 内容判定。"""
    if not IS_WIN:
        return False
    args = ["-m", "svpn", "hosts", action, "--no-elevate"]
    quoted = ",".join(f"'{a}'" for a in args)
    ps = (f"Start-Process -FilePath '{sys.executable}' "
          f"-ArgumentList @({quoted}) "
          f"-WorkingDirectory '{ROOT}' -Verb RunAs -Wait -WindowStyle Hidden")
    try:
        # 同样按字节收：PowerShell 的报错在 UTF-8 模式下会让 text=True 解码失败，
        # UnicodeDecodeError 不在下面的 except 里，一提权就整个崩掉。
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=180,
                             creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    logging.debug("elevate(%s): %s", action, _decode(res.stdout).strip()[:200])
    return True


def _cache_ttl_hours(cfg: dict) -> float:
    raw = (cfg.get("cdn") or {}).get("refresh_hours")
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return 6.0
    return hours if hours > 0 else 6.0


def _cache_age_hours() -> float | None:
    """CDN 缓存距今多少小时；文件不存在或读不出来返回 None。

    `load_cache()` 过期时返回 `{}`，和"根本没缓存"没法区分，所以年龄要单独读。
    """
    try:
        data = json.loads(CDNCACHE.read_text(encoding="utf-8"))
        return max(0.0, (time.time() - float(data.get("updated", 0))) / 3600)
    except (OSError, ValueError, TypeError):
        return None


def _cdn_sni_mode(cfg: dict) -> bool:
    return bool((cfg.get("cdn") or {}).get("enabled"))


def _hosts_write(clear: bool = False) -> bool:
    cfg = load_config()
    if clear:
        res = sysx.clear_hosts(RUNTIME)
        sysx.flush_dns()
        if res.get("ok"):
            print(f"{INFO} hosts 已清理")
            return True
        print(f"{WARN} hosts 清理失败: {res.get('reason')}")
        return False

    cdn_cfg = cfg.get("cdn") or {}
    allowed = {str(h).lower() for h in (cdn_cfg.get("hosts") or cdnmod.CDN_DOMAINS) if h}
    if _cdn_sni_mode(cfg):
        # SNI 模式：hosts 只负责把域名导到本机，真实目标由 SniForwarder 读 SNI 决定。
        # 这里绝不能写最优 IP，否则 ClientHello 直接打到边缘，转发的意义就没了。
        # 一律走 sysx.apply_hosts（内部 sanitize_entries），绝不自己拼文件写 hosts。
        domains = sorted(allowed)
        if not domains:
            print(f"{WARN} cdn.hosts 白名单为空，没有可写的域名")
            return False
        entries = {"127.0.0.1": " ".join(domains)}
        res = sysx.apply_hosts(entries, RUNTIME)
        sysx.flush_dns()
        if not res.get("ok"):
            print(f"{WARN} hosts 写入失败: {res.get('reason')}")
            return False
        kept = res.get("entries")
        kept = kept if isinstance(kept, int) else len(domains)
        print(f"{OK} hosts 已写入 {kept} 个域名 -> 127.0.0.1（SNI 透明转发）")
        print(f"{INFO} hosts 已指向 127.0.0.1，需 svpn up 常驻（SNI 模式）")
        _print_hosts_rejected(res)
        return True

    cache = cdnmod.load_cache(CDNCACHE, max_age=_cache_ttl_hours(cfg) * 3600)
    if not cache:
        # 写死 IP 的代价太高：过期边缘节点会被 DNS 缓存钉住，下载直接连不上。
        # 走到这里说明缓存要么不存在、要么已过期，宁可拒绝也不写脏数据。
        age = _cache_age_hours()
        if age is None:
            print(f"{WARN} 没有 CDN 选边结果，先跑 `python -m svpn cdn --refresh`")
        else:
            print(f"{WARN} CDN 选边结果已过期（{age:.1f} 小时前更新，TTL "
                  f"{_cache_ttl_hours(cfg):.0f} 小时），不写 hosts")
            print(f"    先刷新: python -m svpn cdn --refresh")
        return False
    ipmap = {d: i["best_ip"] for d, i in cache.items()
             if (i or {}).get("best_ip") and d.lower() in allowed}
    dropped = [d for d in cache if d.lower() not in allowed]
    if dropped:
        print(f"{INFO} 跳过 {len(dropped)} 个不在 cdn.hosts 白名单里的域名: "
              f"{', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''}")
    if not ipmap:
        print(f"{WARN} 没有 CDN 选边结果，先跑 `python -m svpn cdn --refresh`")
        return False
    by_ip: dict[str, list[str]] = {}
    for domain, ip in ipmap.items():
        by_ip.setdefault(ip, []).append(domain)
    entries = {ip: " ".join(sorted(hosts)) for ip, hosts in by_ip.items()}
    res = sysx.apply_hosts(entries, RUNTIME)
    sysx.flush_dns()
    if res.get("ok"):
        kept = res.get("entries")
        kept = kept if isinstance(kept, int) else len(entries)
        print(f"{OK} hosts 已写入 {len(ipmap)} 个域名 -> {kept} 个最优边缘 IP")
        for ip, hosts in entries.items():
            print(f"    {ip:<16} {hosts}")
        _print_hosts_rejected(res)
        return True
    print(f"{WARN} hosts 写入失败: {res.get('reason')}")
    return False


def _print_hosts_rejected(res: dict) -> None:
    """apply_hosts 内部 sanitize 掉的脏条目要说出来，别让用户以为「写了但没生效」。"""
    rejected = res.get("rejected") or []
    if rejected:
        head = ", ".join(str(x) for x in rejected[:3])
        more = "…" if len(rejected) > 3 else ""
        print(f"{WARN} {len(rejected)} 个条目未通过 hosts 校验（非法 IP/域名）: {head}{more}")


def cmd_hosts(args) -> int:
    if args.action == "clear":
        if not _is_admin() and not args.no_elevate:
            print(f"{INFO} 需要管理员权限，正在请求提权…")
            _elevate("clear")
            ok = not _hosts_entries()
            print(f"{OK} hosts 已清理" if ok else
                  f"{WARN} 未清理，手动执行（管理员）: "
                  "python -m svpn hosts clear --no-elevate")
            return 0 if ok else 1
        return 0 if _hosts_write(clear=True) else 1

    # SNI 模式用不到最优 IP 缓存，别在这里白跑一轮联网测速
    cfg = load_config()
    if not _cdn_sni_mode(cfg):
        ttl = _cache_ttl_hours(cfg) * 3600
        if not cdnmod.load_cache(CDNCACHE, max_age=ttl):
            # 过期缓存和空缓存都必须重选：写死 16 小时前的边缘 IP，
            # 等于把下载指向可能已经下线的机器（而且 hosts 还会被 DNS 缓存钉住）
            age = _cache_age_hours()
            if age is None:
                print(f"{INFO} 还没有 CDN 数据，先选边…")
            else:
                print(f"{WARN} CDN 选边结果已过期（{age:.1f} 小时前，"
                      f"TTL {ttl / 3600:.0f} 小时），重新选边…")
            cdnmod.save_cache(cdnmod.refresh(bench=True, timeout=args.timeout), CDNCACHE)
    if not _is_admin() and not args.no_elevate:
        print(f"{INFO} 写 hosts 需要管理员，正在弹 UAC…（拒绝就手动用管理员运行）")
        _elevate("apply")
        entries = _hosts_entries()
        if entries:
            print(f"{OK} hosts 已生效 {len(entries)} 条（提权进程写入）")
            return 0
        print(f"{WARN} 提权未完成。手动执行（管理员 PowerShell）：")
        print(f"    cd {ROOT}")
        print(f"    python -m svpn hosts apply --no-elevate")
        return 1
    return 0 if _hosts_write() else 1


# ------------------------------------------------------------------ system / pac
def _apply_system(cfg: dict) -> dict:
    bypass = cfg.get("browser", {}).get("bypass", "<local>")
    if cfg.get("browser", {}).get("use_pac"):
        PACFILE.write_text(build_pac(cfg["listen_host"], cfg["listen_port"],
                                     cfg.get("proxy_suffixes"),
                                     cfg.get("direct_suffixes"),
                                     mode=cfg.get("mode", "whitelist")),
                           encoding="utf-8")
        return sysx.apply_wininet("", bypass, PACFILE.as_uri(), RUNTIME)
    http_port = cfg.get("http_port") or cfg["listen_port"]
    return sysx.apply_wininet(f"{cfg['listen_host']}:{http_port}",
                              bypass, None, RUNTIME)


def cmd_system(args) -> int:
    cfg = load_config()
    if args.action == "on":
        print(f"{OK} 系统代理: {_apply_system(cfg)}")
    elif args.action == "off":
        print(f"{INFO} 已还原: {sysx.restore_wininet(RUNTIME)}")
    else:
        print(json.dumps(sysx.read_wininet(), ensure_ascii=False, indent=2))
    return 0


def cmd_pac(args) -> int:
    cfg = load_config()
    text = build_pac(cfg["listen_host"], cfg["listen_port"],
                     cfg.get("proxy_suffixes"), cfg.get("direct_suffixes"),
                     mode=cfg.get("mode", "whitelist"))
    if args.write:
        PACFILE.write_text(text, encoding="utf-8")
        print(f"{OK} 已写入 {PACFILE}")
    else:
        print(text)
    if args.apply:
        cfg.setdefault("browser", {})["use_pac"] = True
        print(f"{INFO} 已应用: {_apply_system(cfg)}")
    return 0


# ------------------------------------------------------------------ steam
def cmd_steam(args) -> int:
    from . import steam as st  # noqa: PLC0415
    cfg = load_config()
    exe = Path(args.exe) if args.exe else st.find_steam()
    if not exe or not Path(exe).exists():
        print(f"{WARN} 没找到 steam.exe，用 --exe 指定")
        return 2
    if not read_pid() and not args.no_autostart:
        print(f"{INFO} 代理没跑，先拉起来…")
        ns = argparse.Namespace(background=True, port=None, mode=None, node=None,
                                system_proxy=args.system, no_auto=False)
        if cmd_up(ns) != 0:
            return 1
    if args.system:
        print(f"{INFO} 系统代理: {_apply_system(cfg)}")
    if st.is_running():
        if not args.restart:
            print(f"{EQ} Steam 已在运行；改代理要重启客户端（加 --restart）")
            return 0
        print(f"{INFO} 结束旧进程…")
        st.stop(force=True)
        time.sleep(2)
    proxy = f"socks5://{cfg['listen_host']}:{cfg['listen_port']}"
    proc = st.launch(exe, proxy=proxy, extra_args=args.args)
    print(f"{OK} 已拉起 Steam pid={proc.pid}  代理 {proxy}")
    print("    收尾: python -m svpn down --clear-hosts")
    return 0


# ------------------------------------------------------------------ doctor
def cmd_doctor(args) -> int:
    from . import steam as st  # noqa: PLC0415
    _banner()
    cfg = load_config()
    issues: list[str] = []
    print(f"python   {sys.version.split()[0]}  {sys.executable}")
    print(f"平台     {sys.platform}  管理员={_is_admin()}")
    print(f"配置     {CONFIG if CONFIG.exists() else '缺失（python -m svpn init）'}")
    node = _node_from_cfg(cfg)
    if node:
        f = discover.verify(discover.Found(node), timeout=6)
        if f.steam_ok:
            print(f"节点     {node.url()}  可用 {f.ms:.0f}ms HTTP {f.http_code}")
        else:
            print(f"节点     {node.url()}  不通: {f.err}")
            issues.append("节点到不了 Steam")
    else:
        print("节点     未配置（纯直连分流；社区不可用）")
        issues.append("没有节点，社区/创意工坊不可用")
    exe = st.find_steam()
    print(f"Steam    {exe if exe else '未找到'}"
          f"{'  运行中' if st.is_running() else ''}")
    if not exe:
        issues.append("没找到 Steam 安装")
    for port in (_cfg_port(cfg.get("listen_port"), 7899),
                 _cfg_port(cfg.get("http_port"))):
        if not port:
            continue
        busy = _port_busy(cfg["listen_host"], port)
        print(f"端口     {port} "
              f"{'被占用 ' + _port_owner(port) if busy else '可用'}")
        if busy and not read_pid():
            issues.append(f"端口 {port} 被别的程序占用")
    if IS_WIN:
        w = sysx.read_wininet()
        print(f"系统代理 ProxyEnable={w.get('ProxyEnable')} "
              f"{w.get('ProxyServer') or w.get('AutoConfigURL') or ''}")
    cache = cdnmod.load_cache(CDNCACHE, max_age=10 ** 9)
    if cache:
        age = (time.time() - max(v.get("ts", 0) for v in cache.values())) / 60
        print(f"CDN 缓存 {len(cache)} 域名 / {age:.0f} 分钟前")
    else:
        print("CDN 缓存 无（python -m svpn cdn --refresh）")
    entries = _hosts_entries()
    print(f"hosts    {len(entries)} 条 svpn 记录" if entries else "hosts    未写入")
    print()
    rows = [probemod.Row(host=h, note=n, direct=probemod.direct_probe(h, 443, True, 3.0))
            for h, n in (("store.steampowered.com", "商店"),
                         ("steamcommunity.com", "社区"))]
    for r in rows:
        d = r.direct
        state = f"直连 OK {d.get('ms', 0):.0f}ms" if d.get("ok") else \
            f"直连失败 {d.get('reason')}"
        print(f"探测     {r.host:<26}{state}")
    if rows[1].direct.get("reason") == "sni_rst":
        print("         └ 社区是 SNI 阻断（换 IP 无效），必须走节点")
    print()
    if issues:
        print(f"{WARN} 待处理 {len(issues)} 项:")
        for i in issues:
            print(f"    - {i}")
        print("    下一步: python -m svpn node scan")
        return 1
    print(f"{OK} 体检通过")
    return 0


# ------------------------------------------------------------------ init
def cmd_init(args) -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists() and not args.force:
        print(f"{EQ} {CONFIG} 已存在（--force 覆盖）")
    else:
        save_config(DEFAULT_CONFIG)
        print(f"{OK} 已生成 {CONFIG}")
    from . import steam as st  # noqa: PLC0415
    print(f"{INFO} Steam: {st.find_steam() or '未找到'}")
    if IS_WIN:
        sysx.backup_wininet(RUNTIME)
        print(f"{INFO} 系统代理已备份到 runtime/wininet-backup.json")
    print("""
下一步：
  1. python -m svpn node scan                看本机有没有现成节点
  2. python -m svpn up -b --system-proxy     起代理并接管系统代理
  3. python -m svpn cdn --refresh            给下载挑最快的 CDN 边缘
     python -m svpn hosts apply              写入 hosts（会弹一次 UAC）
  4. python -m svpn steam                    拉起 Steam
  5. python -m svpn down --clear-hosts       一键回滚
""")
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("svpn", description="Steam 专用本地分流/加速器")
    parser.add_argument("-v", "--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="生成 config.json + 备份系统代理")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("up", help="启动代理")
    p.add_argument("-b", "--background", action="store_true")
    p.add_argument("-p", "--port", type=int)
    p.add_argument("-m", "--mode", choices=["whitelist", "all", "blacklist"])
    p.add_argument("--system-proxy", action="store_true",
                   help="同时接管 Windows 系统代理")
    p.add_argument("--node", help="临时指定节点 URL（会写入 config.json）")
    p.add_argument("--no-auto", action="store_true", help="不自动扫描节点")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("_serve", help="前台服务（内部用）")
    p.add_argument("--system-proxy", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("down", help="停止代理")
    p.add_argument("--keep-system", action="store_true", help="保留系统代理设置")
    p.add_argument("--clear-hosts", action="store_true", help="同时清 hosts")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("status", help="状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("probe", help="Steam 全链路体检（直连 vs 节点）")
    p.add_argument("--node", help="auto | none | socks5://host:port")
    p.add_argument("--timeout", type=float, default=4.0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--lenient", action="store_true",
                   help="只在「不通 · 待排查」时返回 1（旧行为）")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("node", help="节点发现 / 设置")
    p.add_argument("action", choices=["scan", "set", "clear"])
    p.add_argument("url", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_node)

    p = sub.add_parser("cdn", help="CDN 选边测速")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--no-bench", action="store_true", help="只测握手不下载")
    p.add_argument("--domain")
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cdn)

    p = sub.add_parser("hosts", help="写入/清理 hosts（CDN 选边生效）")
    p.add_argument("action", choices=["apply", "clear"])
    p.add_argument("--no-elevate", action="store_true")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(func=cmd_hosts)

    p = sub.add_parser("system", help="系统代理开关")
    p.add_argument("action", choices=["on", "off", "show"])
    p.set_defaults(func=cmd_system)

    p = sub.add_parser("pac", help="生成/应用 PAC")
    p.add_argument("--write", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_pac)

    p = sub.add_parser("steam", help="拉起 Steam 并接管")
    p.add_argument("--exe")
    p.add_argument("--restart", action="store_true")
    p.add_argument("--system", action="store_true")
    p.add_argument("--no-autostart", action="store_true")
    p.add_argument("--args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_steam)

    p = sub.add_parser("doctor", help="体检")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 cp936，中文会乱码或直接 UnicodeEncodeError
    if IS_WIN:
        try:
            import ctypes  # noqa: PLC0415
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, ConnectionError) as exc:
        print(f"{WARN} {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
