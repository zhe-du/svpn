"""节点发现：把机器上已经存在的代理/VPN 客户端找出来，并证明它真能到 Steam。

大多数人的电脑上其实已经躺着一个机场客户端（Clash / v2rayN / sing-box /
Watt Toolkit…），只是 Steam 不认它。这里做两件事：

1. 扫：配置、环境变量、Windows 系统代理、常见本地端口，全找一遍；
2. 验：逐个做真实 SOCKS5/HTTP 握手，再穿过去 TLS 握手 store.steampowered.com，
   只留下「握手成功 + 200」的节点，按耗时排序。

没有验证的节点等于没有节点——很多端口开着但早已失效或是别的服务。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import netutil
from .netutil import Node

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

# 常见客户端默认端口：(端口, 协议, 说明)
COMMON_PORTS: tuple[tuple[int, str, str], ...] = (
    (7890, "http", "Clash / Clash for Windows"),
    (7891, "socks5", "Clash socks"),
    (7897, "http", "Clash Verge Rev"),
    (10808, "socks5", "v2rayN"),
    (10809, "http", "v2rayN http"),
    (1080, "socks5", "Shadowsocks / 通用"),
    (1081, "http", "通用 http"),
    (1086, "http", "Surge / 通用"),
    (2080, "socks5", "sing-box"),
    (2081, "http", "sing-box http"),
    (20171, "socks5", "Netch"),
    (8889, "http", "Watt Toolkit / Steam++"),
    (6152, "http", "Surge"),
    (8118, "http", "Privoxy"),
    (7899, "socks5", "svpn 自身"),
    (7900, "http", "svpn 自身 http"),
)

SELF_PORTS = {7899, 7900}


@dataclass
class Found:
    node: Node
    source: str = ""
    ok: bool = False                 # 代理握手通过
    steam_ok: bool = False           # 穿过它能 TLS 到 Steam 并拿到 HTTP 响应
    ms: float = 0.0
    err: str = ""
    http_code: int = 0

    @property
    def score(self) -> tuple:
        # 能到 Steam 的排前面，其次看延迟
        return (0 if self.steam_ok else 1, round(self.ms, 1))

    def line(self) -> str:
        state = "可用" if self.steam_ok else ("代理通·Steam不通" if self.ok else "不可用")
        detail = f"{self.ms:6.0f}ms" if self.ok else "      "
        extra = f"HTTP {self.http_code}" if self.http_code else (self.err or "")
        return (netutil.pad(state, 18) + netutil.pad(detail, 9)
                + netutil.pad(self.node.kind, 8)
                + netutil.pad(f"{self.node.host}:{self.node.port}", 22)
                + netutil.pad(self.node.label or self.source, 28) + extra)


# ---------------------------------------------------------------- 收集候选
def _from_config(cfg: dict) -> list[Found]:
    up = (cfg or {}).get("upstream") or {}
    kind = (up.get("kind") or "").lower()
    host = up.get("host") or ""
    port = int(up.get("port") or 0)
    if kind not in ("socks5", "http") or not host or not port:
        return []
    if host.upper() in ("NODE_HOST", "NODE", "HOST", "EXAMPLE.COM", "CHANGE_ME"):
        return []
    return [Found(Node(kind=kind, host=host, port=port,
                       username=up.get("username") or "",
                       password=up.get("password") or "",
                       label="config.json"), source="config")]


def _from_env() -> list[Found]:
    out: list[Found] = []
    seen: set[tuple] = set()
    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
                 "HTTP_PROXY", "http_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            node = netutil.parse_node_url(raw)
        except ValueError:
            continue
        key = (node.kind, node.host, node.port)
        if key in seen:
            continue
        seen.add(key)
        node.label = f"env {name}"
        out.append(Found(node, source="env"))
    return out


def _from_wininet() -> list[Found]:
    if not IS_WIN:
        return []
    try:
        import winreg  # noqa: PLC0415
        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            enable = int(winreg.QueryValueEx(k, "ProxyEnable")[0])
            server = str(winreg.QueryValueEx(k, "ProxyServer")[0] or "")
    except (OSError, FileNotFoundError, ValueError):
        return []
    if not enable or not server:
        return []
    # 形如 "127.0.0.1:7890" 或 "http=127.0.0.1:7890;https=127.0.0.1:7890"
    picks: list[tuple[str, int]] = []
    for part in server.split(";"):
        part = part.strip()
        if "=" in part:
            _proto, _, part = part.partition("=")
        host, _, port_s = part.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        if host:
            picks.append((host, port))
    out = []
    for host, port in picks[:2]:
        out.append(Found(Node(kind="http", host=host, port=port, label="Windows 系统代理"),
                         source="wininet"))
    return out


def _from_ports() -> list[Found]:
    out: list[Found] = []
    for port, kind, label in COMMON_PORTS:
        if port in SELF_PORTS:
            continue
        if not _port_open("127.0.0.1", port):
            continue
        out.append(Found(Node(kind=kind, host="127.0.0.1", port=port, label=label),
                         source="scan"))
    return out


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- 验证
def verify(found: Found, target: str = "store.steampowered.com",
           timeout: float = 6.0, deep: bool = True) -> Found:
    node = found.node
    t0 = time.perf_counter()
    sock = None
    try:
        sock = netutil.open_via(node, target, 443, timeout)
        found.ms = (time.perf_counter() - t0) * 1000
        found.ok = True
        if deep:
            tls = netutil.tls_over(sock, target, timeout)
            found.ms = (time.perf_counter() - t0) * 1000
            code, _ = netutil.http_head(tls, target, "/", timeout)
            found.http_code = code
            found.steam_ok = code in (200, 301, 302, 403)  # 403 也算通（地区限制）
            try:
                tls.close()
            except OSError:
                pass
        else:
            found.steam_ok = found.ok
    except Exception as exc:  # noqa: BLE001
        found.err = f"{type(exc).__name__}: {str(exc)[:42]}"
        found.ok = False
        found.steam_ok = False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return found


def self_ports(cfg: dict | None = None) -> set[int]:
    """svpn 自己会占用的端口集合。

    硬编码的 7899/7900 只是历史默认值；用户改过 listen_port 或开了 SNI 转发后，
    真正要排除的是配置里的那几个。否则 `node scan` 会把 svpn 自己当节点写进
    config.json，所有流量绕回自身，几秒内就能把 fd 打满。
    """
    ports = set(SELF_PORTS)
    cfg = cfg if isinstance(cfg, dict) else {}
    for key in ("listen_port", "http_port"):
        try:
            value = int(cfg.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            ports.add(value)
    cdn = cfg.get("cdn") if isinstance(cfg.get("cdn"), dict) else {}
    if cdn.get("enabled"):
        try:
            value = int(cdn.get("sni_port") or 443)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            ports.add(value)
    return ports


def is_self(node, cfg: dict | None = None) -> bool:
    """这个候选节点是不是 svpn 自己。"""
    host = (getattr(node, "host", "") or "").strip().strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    try:
        port = int(getattr(node, "port", 0) or 0)
    except (TypeError, ValueError):
        return False
    return port in self_ports(cfg)


def scan(cfg: dict | None = None, include_ports: bool = True,
         deep: bool = True, timeout: float = 5.0) -> list[Found]:
    """收集 + 并发验证所有候选节点，按可用性排序返回。"""
    candidates: list[Found] = []
    candidates += _from_config(cfg)
    candidates += _from_env()
    candidates += _from_wininet()
    if include_ports:
        candidates += _from_ports()
    # 系统代理可能正指着 svpn 自己（up --system-proxy 的副作用），必须排掉
    candidates = [f for f in candidates if not is_self(f.node, cfg)]

    # 去重（按 kind/host/port）
    uniq: dict[tuple, Found] = {}
    for c in candidates:
        key = (c.node.kind, c.node.host, c.node.port)
        if key not in uniq:
            uniq[key] = c
    items = list(uniq.values())
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        done = list(pool.map(lambda f: verify(f, timeout=timeout, deep=deep), items))
    done.sort(key=lambda f: f.score)
    return done


def best(cfg: dict | None = None, include_ports: bool = True) -> Found | None:
    for f in scan(cfg, include_ports=include_ports):
        if f.steam_ok:
            return f
    return None


# ---------------------------------------------------------------- 持久化
def save_upstream(config_path, node: Node) -> None:
    """把选中的节点写进 config.json（保留其它字段）。"""
    import json
    from pathlib import Path
    path = Path(config_path)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.setdefault("listen_host", "127.0.0.1")
    data.setdefault("listen_port", 7899)
    data.setdefault("http_port", 7900)
    data.setdefault("mode", "whitelist")
    data["upstream"] = {"kind": node.kind, "host": node.host, "port": node.port,
                        "username": node.username, "password": node.password}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 辅助
def steam_client_running() -> bool:
    if not IS_WIN:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
                             capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW).stdout
        return "steam.exe" in (out or "").lower()
    except OSError:
        return False
