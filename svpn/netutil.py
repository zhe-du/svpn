"""网络底层工具：DoH 解析、TCP/TLS/SNI 探测、经节点拨号。

为什么不用系统 DNS？
    steamcommunity.com 在国内经常被污染成 Twitter/Cloudflare 的地址（例如
    104.244.46.x），拿到的 IP 根本不通。这里所有解析走 DoH（HTTPS 上的 DNS），
    拿真实记录；同时保留系统解析作为兜底。

为什么区分 TCP 通 / TLS 挂？
    TCP 通但 TLS 握手被 RST，说明是 SNI 阻断（GFW 读 ClientHello 里的域名），
    换 IP 没用，必须走节点；DNS 污染则表现为拿到的 IP 连 TCP 都不通。
    两种故障的处置完全不同，所以探测必须分层。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import struct
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable, Sequence

UA = "svpn/0.2 (+local split-tunnel)"

# DoH：前两个国内可达、返回真实记录；Cloudflare 仅作兜底
DOH_PROVIDERS: tuple[str, ...] = (
    "https://doh.pub/dns-query",
    "https://dns.alidns.com/resolve",
    "https://1.12.12.12/dns-query",
    "https://cloudflare-dns.com/dns-query",
)

_IPV4 = 1
_IPV6 = 28


# ---------------------------------------------------------------- 基础判定
def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def display_width(text: str) -> int:
    """终端显示宽度：CJK 算 2 列，用于表格对齐。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - display_width(text))
    return gap + text if right else text + gap


def norm_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


# ---------------------------------------------------------------- DoH 解析
_DNS_CACHE: dict[tuple[str, str], tuple[float, list[str], str]] = {}


def _doh_query(url: str, name: str, rtype: str, timeout: float) -> list[str]:
    """向单个 DoH provider 要 A/AAAA 记录。

    这个函数是「可能出任何问题」的地方：链路被劫持会回一段 HTML、provider 限流会回
    非 200、TCP 被切断会回半截 JSON。所有异常都往上抛给 resolve() 统一跳过该 provider，
    这里只负责把「响应长得不对」也变成异常，不要静默返回半份数据。
    """
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}name={name}&type={rtype}"
    req = urllib.request.Request(
        full, headers={"accept": "application/dns-json", "user-agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None)
        if status is None:
            status = getattr(resp, "code", 200)     # 老接口/假响应只有 code
        if int(status) != 200:
            raise ValueError(f"DoH HTTP {status}")
        raw = resp.read()
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError) as exc:       # 截断 / 非 JSON（多半被劫持）
        raise ValueError("DoH 响应不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("DoH 响应不是 JSON 对象")
    answers = payload.get("Answer")
    if answers is None:                             # NXDOMAIN / NODATA：正常空结果
        return []
    if not isinstance(answers, list):
        raise ValueError("Answer 不是列表")
    want = _IPV4 if rtype == "A" else _IPV6
    out: list[str] = []
    for ans in answers:
        if not isinstance(ans, dict) or ans.get("type") != want:
            continue
        val = ans.get("data")
        if not isinstance(val, str):
            continue
        try:
            ip = ipaddress.ip_address(val.strip())
        except ValueError:                          # CNAME 串 / 空串 / 脏数据
            continue
        if (ip.version == 4) != (rtype == "A"):
            continue
        text = str(ip)
        if text not in out:
            out.append(text)
    return out


def resolve(name: str, rtype: str = "A", timeout: float = 5.0,
            ttl: float = 300.0, providers: Sequence[str] | None = None) -> tuple[list[str], str]:
    """返回 (ip 列表, 来源)。先查缓存，再逐个 DoH，最后系统解析。"""
    name = norm_host(name)
    if not name:
        return [], "empty"
    if len(name) > 253:                 # DNS 名上限，别拿去问 DoH 浪费一个超时
        return [], "invalid"
    if is_ip(name):
        return [name], "literal"
    key = (name, rtype)
    hit = _DNS_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return list(hit[1]), hit[2]

    ips: list[str] = []
    source = ""
    asked = 0
    for url in (providers or DOH_PROVIDERS):
        try:
            got = _doh_query(url, name, rtype, timeout)
            if not got or not isinstance(got, (list, tuple)):
                continue
            if not source:      # source 记首个成功的 provider，合并细节不污染来源字段
                host = str(url).split("://", 1)[-1].split("/", 1)[0]
                source = f"DoH:{host}" if host else "DoH"
            for ip in got:
                if isinstance(ip, str) and ip and ip not in ips:
                    ips.append(ip)
        except Exception:  # noqa: BLE001 —— 任何 provider 出问题都只跳过它
            continue
        if not ips:
            continue
        asked += 1
        # 单个 DoH 常常只回一条 A（failover 不够），拿到 2 条或问过 2 家就停
        if len(ips) >= 2 or asked >= 2:
            break
    if ips:
        _DNS_CACHE[key] = (time.time(), ips, source)
        return ips, source

    # 兜底：系统解析（可能被污染，但至少不空）
    fam = socket.AF_INET if rtype == "A" else socket.AF_INET6
    try:
        infos = socket.getaddrinfo(name, None, fam, socket.SOCK_STREAM)
        ips = []
        for info in infos:
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
        if ips:
            _DNS_CACHE[key] = (time.time(), ips, "system")
            return ips, "system"
    except OSError:
        pass
    return [], "fail"


def clear_dns_cache() -> None:
    _DNS_CACHE.clear()


def system_resolve(name: str, family: int = socket.AF_INET) -> list[str]:
    """系统解析（可能被污染，但 CDN 场景下往往给到就近节点，值得并进候选）。"""
    name = norm_host(name)
    if not name or is_ip(name):
        return []
    try:
        infos = socket.getaddrinfo(name, None, family, socket.SOCK_STREAM)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def resolve_merged(name: str, timeout: float = 5.0,
                   ttl: float = 300.0) -> tuple[list[str], str]:
    """DoH 真实记录 + 系统记录 合并去重。

    DoH 给的是「真实但可能很远」的 IP；系统 DNS 给的是「近但可能被污染」的 IP。
    两边都收进来，交给 TLS 竞速去淘汰，比信任何单一一侧都稳。

    对外承诺：**绝不抛异常**。两家都挂（或域名本身非法）时返回 `([], "none")`，
    调用方只需要判空。来源字段仍是 `DoH:xxx` / `DoH:xxx+system` / `system` /
    `literal` / `empty` / `invalid` / `none`。
    """
    doh: list[str] = []
    source = "none"
    try:
        doh, source = resolve(name, timeout=timeout, ttl=ttl)
    except Exception:  # noqa: BLE001 —— 解析层再稳，也不能让调用方炸
        doh, source = [], "none"
    try:
        sys_ips = system_resolve(name)
    except Exception:  # noqa: BLE001
        sys_ips = []
    merged = list(dict.fromkeys(list(doh or []) + list(sys_ips or [])))
    if not merged:
        # 两家全挂：明确给一个可判定的来源，别把 "fail"/"" 这类半成品漏给调用方
        if source in ("", "fail"):
            source = "none"
        return [], source or "none"
    if sys_ips and source.startswith("DoH"):
        source = f"{source}+system"
    elif not doh and sys_ips:
        source = "system"
    return merged, source


# ---------------------------------------------------------------- 连接探测
def _close_quietly(sock) -> None:
    """关闭连接，任何异常都吞掉 —— 清理路径不允许再抛。"""
    if sock is None:
        return
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass


def tcp_probe(ip: str, port: int = 443, timeout: float = 4.0) -> tuple[bool, float, str]:
    """(是否连上, 毫秒, 错误名)"""
    t0 = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout)
        return True, (time.perf_counter() - t0) * 1000, ""
    except ConnectionResetError:
        return False, (time.perf_counter() - t0) * 1000, "RST"
    except socket.timeout:
        return False, (time.perf_counter() - t0) * 1000, "TIMEOUT"
    except OSError as exc:
        return False, (time.perf_counter() - t0) * 1000, type(exc).__name__
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def tls_probe(ip: str, sni: str, port: int = 443, timeout: float = 5.0,
              ctx: ssl.SSLContext | None = None) -> dict:
    """对指定 IP 用指定 SNI 做完整 TLS 握手，返回分层结果。

    status:
      ok        握手成功
      tcp       TCP 都没连上（多半 IP 被封 / DNS 污染）
      sni_rst   TCP 通、ClientHello 一发就被 RST → SNI 阻断
      tls       其它 TLS 错误（证书/协议/超时）
    """
    t0 = time.perf_counter()
    try:
        raw = socket.create_connection((ip, port), timeout)
    except ConnectionResetError:
        return {"status": "tcp", "ms": _ms(t0), "err": "RST", "ip": ip, "sni": sni}
    except socket.timeout:
        return {"status": "tcp", "ms": _ms(t0), "err": "TIMEOUT", "ip": ip, "sni": sni}
    except OSError as exc:
        return {"status": "tcp", "ms": _ms(t0), "err": type(exc).__name__,
                "ip": ip, "sni": sni}

    context = ctx or _client_ctx()
    tls_start = time.perf_counter()
    handshake = {"tcp_ms": _ms2(t0, tls_start), "ip": ip, "sni": sni}
    try:
        wrapped = context.wrap_socket(raw, server_hostname=sni)
    except BaseException as exc:      # 握手失败：raw 必须关，否则每探测一次漏一个 fd
        _close_quietly(raw)
        if isinstance(exc, ConnectionResetError):
            return {**handshake, "status": "sni_rst", "ms": _ms(tls_start), "err": "RST"}
        if isinstance(exc, socket.timeout):
            return {**handshake, "status": "tls", "ms": _ms(tls_start), "err": "TIMEOUT"}
        if isinstance(exc, ssl.SSLError):
            return {**handshake, "status": "tls", "ms": _ms(tls_start),
                    "err": f"{type(exc).__name__}:{getattr(exc, 'reason', '')}"}
        if isinstance(exc, OSError):
            return {**handshake, "status": "tls", "ms": _ms(tls_start),
                    "err": type(exc).__name__}
        raise
    out = {**handshake, "status": "ok", "ms": _ms(tls_start),
           "cert_cn": "", "version": "", "tls_ms": _ms(tls_start)}
    try:
        # 证书解析只是附加信息，出什么错都不改「握手成功」这个结论
        try:
            cert = wrapped.getpeercert() or {}
            if isinstance(cert, dict):
                for rdn in cert.get("subject", ()):
                    for k, v in rdn:
                        if k == "commonName":
                            out["cert_cn"] = v
            out["version"] = wrapped.version() or ""
        except Exception:  # noqa: BLE001
            pass
    finally:
        _close_quietly(wrapped)
    return out


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _ms2(t0: float, t1: float) -> float:
    return round((t1 - t0) * 1000, 1)


def _client_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # 探测用：证书问题单独看 cert_cn
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def race_tls(host: str, ips: Iterable[str], port: int = 443,
             timeout: float = 3.0, workers: int = 12) -> list[dict]:
    """并发对多个 IP 做 TLS 握手，只返回成功的，按握手耗时排序。"""
    ips = [ip for ip in dict.fromkeys(ips) if ip]
    if not ips:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(ips))) as pool:
        results = list(pool.map(lambda ip: tls_probe(ip, host, port, timeout), ips))
    good = [r for r in results if r["status"] == "ok"]
    good.sort(key=lambda r: r["ms"])
    return good


def probe_many(host: str, ips: Iterable[str], port: int = 443,
               timeout: float = 3.0, workers: int = 6) -> list[dict]:
    """并发探测，保留每台 IP 的分层结果（用于失败定性）。"""
    ips = [ip for ip in dict.fromkeys(ips) if ip]
    if not ips:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(ips))) as pool:
        return list(pool.map(lambda ip: tls_probe(ip, host, port, timeout), ips))


_CONTROL_IP: dict[str, tuple[float, str]] = {}
CONTROL_TTL = 300.0


def control_ip(host: str = "store.steampowered.com", timeout: float = 3.0,
               refresh: bool = False) -> str:
    """找一个「当前确实连得通」的 IP，用来做 SNI 对照实验。

    带 TTL：对照 IP 会失效，缓存太久会让 sni_block_test 永远漏报。
    """
    hit = _CONTROL_IP.get(host)
    if hit and not refresh and time.time() - hit[0] < CONTROL_TTL:
        return hit[1]
    ips, _ = resolve_merged(host, timeout=timeout)
    good = race_tls(host, ips[:6], 443, timeout)
    ip = good[0]["ip"] if good else ""
    if ip:
        _CONTROL_IP[host] = (time.time(), ip)
    else:
        _CONTROL_IP.pop(host, None)
    return ip


def sni_block_test(host: str, control: str = "store.steampowered.com",
                   timeout: float = 3.0) -> bool | None:
    """SNI 阻断判定：拿一台通的 IP，换上目标域名的 SNI 再握手。

    同一个 IP：自己的 SNI 能握手，换 host 的 SNI 立刻 RST → 目标域名被按 SNI 阻断
    （换 IP 没用，只能走节点）。目标 IP 全不可达时，这是唯一能定性的办法。

    返回 True=阻断 / False=没阻断 / None=没法做对照（对照组也不通）。
    """
    ip = control_ip(control, timeout)
    if not ip:
        return None
    status = tls_probe(ip, host, 443, timeout).get("status")
    if status == "sni_rst":
        return True
    if status == "ok":
        return False
    return None


# ---------------------------------------------------------------- 经节点拨号
@dataclass
class Node:
    kind: str = "direct"               # direct | socks5 | http
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    label: str = ""

    @property
    def usable(self) -> bool:
        return self.kind in ("socks5", "http") and bool(self.host) and self.port > 0

    def url(self, hide_secret: bool = True) -> str:
        auth = ""
        if self.username:
            auth = f"{self.username}:***@" if hide_secret else \
                f"{self.username}:{self.password}@"
        return f"{self.kind}://{auth}{self.host}:{self.port}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "host": self.host, "port": self.port,
                "username": self.username, "password": self.password}


def parse_node_url(text: str) -> Node:
    """socks5://user:pass@1.2.3.4:1080  /  http://1.2.3.4:7890  /  1.2.3.4:1080"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("空节点")
    kind = "socks5"
    if "://" in raw:
        kind, raw = raw.split("://", 1)
        kind = kind.lower()
        if kind in ("socks", "socks5h"):
            kind = "socks5"
        if kind not in ("socks5", "http", "https"):
            raise ValueError(f"不支持的协议 {kind}（只认 socks5 / http）")
        if kind == "https":
            kind = "http"
    user = pwd = ""
    if "@" in raw:
        cred, raw = raw.rsplit("@", 1)
        if ":" in cred:
            user, pwd = cred.split(":", 1)
        else:
            user = cred
    # 支持 [2408::1]:1080 / [::1] 这种方括号 IPv6
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            raise ValueError("IPv6 方括号没闭合")
        host = raw[1:end]
        rest = raw[end + 1:]
        if rest and not rest.startswith(":"):
            raise ValueError(f"方括号后缺少冒号: {rest}")
        port_s = rest[1:] if rest.startswith(":") else rest
    elif raw.count(":") == 1:
        host, port_s = raw.split(":", 1)
    else:                       # 裸 IPv6 或纯主机名
        host, port_s = _split_bare(raw)
    host = host.strip()
    try:
        port = int(port_s or (1080 if kind == "socks5" else 7890))
    except ValueError as exc:
        raise ValueError(f"端口不是数字: {port_s}") from exc
    if not host:
        raise ValueError("缺主机名")
    if not 1 <= port <= 65535:
        raise ValueError(f"端口超出范围 1-65535: {port}")
    return Node(kind=kind, host=host, port=port, username=user, password=pwd)


def _split_bare(raw: str) -> tuple[str, str]:
    """切「裸 IPv6」与「主机:端口」，返回 (host, port 字符串)。

    冒号 >= 2 个时只可能是 IPv6。`2408::1` 是完整地址（不带端口），
    但 `::1:1080` / `2408::1:1080` 既可能是地址也可能是「地址:端口」——
    这种写法本身有歧义，硬猜只会让用户莫名其妙地连到错误的端口，直接报错。
    """
    if raw.count(":") < 2:
        return raw, ""
    head, _, tail = raw.rpartition(":")
    if tail.isdigit() and head and is_ip(head):
        raise ValueError(
            f"裸 IPv6 有歧义，请用方括号把地址括起来：socks5://[{head}]:{tail}")
    return raw, ""


def socks5_connect(node: Node, dst_host: str, dst_port: int,
                   timeout: float = 8.0) -> socket.socket:
    sock = socket.create_connection((node.host, node.port), timeout)
    try:
        sock.settimeout(timeout)
        if node.username:
            sock.sendall(b"\x05\x02\x00\x02")
        else:
            sock.sendall(b"\x05\x01\x00")
        head = _recv_exact(sock, 2)
        if head[0] != 5:
            raise ConnectionError("上游不是 SOCKS5")
        if head[1] == 0x02:
            u = node.username.encode()[:255]
            p = node.password.encode()[:255]
            sock.sendall(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
            _ver, status = _recv_exact(sock, 2)
            if status != 0:
                raise ConnectionError("SOCKS5 认证失败")
        elif head[1] != 0x00:
            raise ConnectionError(f"SOCKS5 拒绝认证方式 {head[1]}")

        if is_ip(dst_host):
            ip = ipaddress.ip_address(dst_host.strip("[]"))
            atyp = b"\x01" if ip.version == 4 else b"\x04"
            addr = ip.packed
        else:
            hb = dst_host.encode("idna") if not dst_host.isascii() else dst_host.encode()
            atyp, addr = b"\x03", bytes([len(hb)]) + hb
        sock.sendall(b"\x05\x01\x00" + atyp + addr + struct.pack(">H", dst_port))
        ver, code, _rsv, at = _recv_exact(sock, 4)
        if code != 0:
            raise ConnectionError(f"SOCKS5 CONNECT 失败 code={code}")
        if at == 1:
            _recv_exact(sock, 6)
        elif at == 3:
            n = _recv_exact(sock, 1)[0]
            _recv_exact(sock, n + 2)
        elif at == 4:
            _recv_exact(sock, 18)
        return sock
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise


def http_connect(node: Node, dst_host: str, dst_port: int,
                 timeout: float = 8.0) -> socket.socket:
    sock = socket.create_connection((node.host, node.port), timeout)
    try:
        sock.settimeout(timeout)
        auth = ""
        if node.username:
            import base64
            token = base64.b64encode(
                f"{node.username}:{node.password}".encode()).decode()
            auth = f"Proxy-Authorization: Basic {token}\r\n"
        target = f"[{dst_host}]" if ":" in dst_host else dst_host
        req = (f"CONNECT {target}:{dst_port} HTTP/1.1\r\n"
               f"Host: {target}:{dst_port}\r\n{auth}"
               "Proxy-Connection: keep-alive\r\n\r\n")
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("上游 HTTP 代理提前关闭")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("上游 HTTP 代理响应过长")
        status_line = buf.split(b"\r\n", 1)[0].decode("latin-1")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].startswith("2"):
            raise ConnectionError(f"HTTP CONNECT 被拒: {status_line}")
        return sock
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise


def open_via(node: Node, dst_host: str, dst_port: int,
             timeout: float = 8.0) -> socket.socket:
    """按节点类型拨号，返回一条已连通的 socket。

    三条分支的所有失败路径都已经在内部关掉自己开的 socket（socks5_connect /
    http_connect 的 `except BaseException` 里关，direct 分支是 create_connection
    自己的失败，压根没分配），所以这里不需要再兜一层。
    """
    if node.kind == "socks5":
        return socks5_connect(node, dst_host, dst_port, timeout)
    if node.kind in ("http", "https"):
        return http_connect(node, dst_host, dst_port, timeout)
    return socket.create_connection((dst_host, dst_port), timeout)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被关闭")
        buf += chunk
    return buf


def tls_over(sock: socket.socket, sni: str, timeout: float = 8.0) -> ssl.SSLSocket:
    """在已有 TCP 连接上做 TLS 握手。

    失败时**先把 sock 关掉再抛**：调用方拿不到 SSLSocket，也就没机会关它，
    留着就是一个泄漏的 fd（probe/cdn 的调用点都是 `tls = tls_over(sock, ...)`
    这种一步到位的写法，抓不到异常后的 raw socket）。
    """
    try:
        ctx = _client_ctx()
        sock.settimeout(timeout)
        return ctx.wrap_socket(sock, server_hostname=sni)
    except BaseException:
        _close_quietly(sock)
        raise


def _status_code(line: str) -> int:
    """从状态行取三位状态码；任何畸形输入都返回 0（不抛）。

    畸形来源：空行、只有 `HTTP/1.1`、`HTTP/1.1 200OK`、`HTTP/1.1 \xff\xfe`、
    反代回的垃圾字节（已按 latin-1 解码，不会 UnicodeDecodeError）。
    """
    parts = line.split()
    if len(parts) < 2:
        return 0
    if not parts[0].upper().startswith("HTTP/"):
        return 0
    text = parts[1]
    if len(text) != 3 or not text.isdigit():
        return 0
    code = int(text)
    return code if 100 <= code <= 599 else 0


def _parse_head(buf: bytes) -> tuple[int, dict[str, str]]:
    """把原始响应头字节解析成 (状态码, 头字典)。头解析绝不抛。"""
    if not buf:
        return 0, {}
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
    lines = head.split("\r\n")
    code = _status_code(lines[0] if lines else "")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, _, v = line.partition(":")       # `: x` 这种空名行会被下面跳过
        k = k.strip().lower()
        if not k:
            continue
        headers[k] = v.strip()
    return code, headers


def http_head(sock: ssl.SSLSocket | socket.socket, host: str, path: str = "/",
              timeout: float = 8.0) -> tuple[int, dict[str, str]]:
    """在已建立的（TLS）连接上发一个 HEAD，返回状态码 + 响应头。

    容错口径：网络错误 / 畸形响应一律返回 `(0, {})`，不抛异常。
    第二个元素保持 dict（调用方按响应头取值），空 dict 与空 bytes 同为假值。
    """
    req = (f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\n"
           f"User-Agent: {UA}\r\nAccept: */*\r\nConnection: close\r\n\r\n")
    try:
        sock.settimeout(timeout)
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 65536:
                break
    except (OSError, ValueError):
        # 连接已死 / 超时 / 假 socket：调用方只看状态码，0 就代表「没拿到」
        return 0, {}
    return _parse_head(buf)


def download_bench(sock_factory, host: str, path: str, byte_count: int = 1_048_576,
                   timeout: float = 8.0) -> tuple[int, float, str]:
    """用 Range 下一个 1MB 片段，返回 (字节数, MB/s, 错误)。

    sock_factory 负责返回一条已连好的 TLS 连接（不同边缘 IP/不同节点复用同一套逻辑）。
    """
    sock = None
    try:
        sock = sock_factory()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"User-Agent: Valve/Steam HTTP Client 1.0\r\n"
               f"Range: bytes=0-{byte_count - 1}\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n")
        t0 = time.perf_counter()
        sock.sendall(req.encode())
        sock.settimeout(timeout)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(16384)
            if not chunk:
                return 0, 0.0, "无响应"
            buf += chunk
        header, _, body = buf.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        code = 0
        parts = status.split()
        if len(parts) >= 2 and parts[1].isdigit():
            code = int(parts[1])
        if code not in (200, 206):
            return len(body), 0.0, f"HTTP {code}"
        got = len(body)
        while got < byte_count:
            chunk = sock.recv(min(65536, byte_count - got))
            if not chunk:
                break
            got += len(chunk)
        elapsed = max(time.perf_counter() - t0, 1e-6)
        return got, (got / 1048576) / elapsed, ""
    except Exception as exc:  # noqa: BLE001
        return 0, 0.0, type(exc).__name__
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
