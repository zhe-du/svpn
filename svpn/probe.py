"""Steam 全链路体检：每个关键域名分「直连 / 经节点」两路探测，给出可执行结论。

探测分三层，故障点不同结论也不同：
    DNS  → 拿到的 IP 是不是假的（污染）
    TCP  → 这个 IP 通不通（IP 封锁 / 路由不通）
    TLS  → ClientHello 发出去会不会被 RST（SNI 阻断）

只有把这三层分开，才知道该换 DNS、换 IP，还是只能走节点。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import netutil
from .netutil import Node

# host, 端口, 需要 TLS?, 用途
ENDPOINTS: tuple[tuple[str, int, bool, str], ...] = (
    ("store.steampowered.com", 443, True, "商店 / 购买"),
    ("login.steampowered.com", 443, True, "登录"),
    ("api.steampowered.com", 443, True, "API / 令牌"),
    ("steamcommunity.com", 443, True, "社区 / 创意工坊"),
    ("community.akamai.steamstatic.com", 443, True, "社区静态资源"),
    ("avatars.steamstatic.com", 443, True, "头像 / 图片"),
    ("steamcdn-a.akamaihd.net", 443, True, "游戏下载 CDN"),
    ("cdn.akamai.steamstatic.com", 443, True, "客户端更新"),
    ("help.steampowered.com", 443, True, "客服 / 找回"),
)


def _verdict(direct: dict, node: dict | None) -> str:
    if direct.get("ok"):
        return "直连可用"
    reason = direct.get("reason", "")
    if node and not node.get("ok"):
        return "节点也不通 · 换节点"
    if reason == "sni_rst":
        return "需节点 · SNI 阻断"
    if reason == "dns":
        return "需节点 · DNS 污染"
    if reason == "tcp":
        return "需节点 · IP 不可达"
    return "不通 · 待排查"


def _direct_probe(host: str, port: int, use_tls: bool, timeout: float = 4.0) -> dict:
    ips, source = netutil.resolve_merged(host, timeout=timeout)
    base = {"host": host, "port": port, "dns_source": source, "ips": ips[:6]}
    if not ips:
        return {**base, "ok": False, "reason": "dns", "err": "解析失败"}

    if not use_tls:                    # CM 之类只看 TCP
        for ip in ips[:3]:
            ok, ms, err = netutil.tcp_probe(ip, port, timeout)
            if ok:
                return {**base, "ok": True, "ip": ip, "ms": round(ms, 1)}
        return {**base, "ok": False, "reason": "tcp", "err": err}

    good = netutil.race_tls(host, ips[:8], port, timeout=timeout, workers=8)
    if good:
        best = good[0]
        code = 0
        try:
            sock = netutil.open_via(Node("direct"), best["ip"], port, timeout)
            tls = netutil.tls_over(sock, host, timeout)
            code, _ = netutil.http_head(tls, host, "/", timeout)
            try:
                tls.close()
            except OSError:
                pass
        except Exception:  # noqa: BLE001
            code = 0
        return {**base, "ok": True, "ip": best["ip"], "ms": best["ms"], "code": code,
                "alive": len(good)}

    # 全部失败：并发看每台 IP 的失败类型，SNI 阻断优先于普通不可达
    details = netutil.probe_many(host, ips[:3], port, timeout=min(timeout, 3.0))
    reasons = {d["status"] for d in details}
    if "sni_rst" in reasons:
        reason = "sni_rst"
    elif reasons == {"tcp"} or not reasons:
        reason = "tcp"
    else:
        reason = "tls"
    first = next((d for d in details if d["status"] == reason), details[0] if details else {})
    note = ""
    if reason == "tcp":
        blocked = netutil.sni_block_test(host, timeout=min(timeout, 3.0))
        if blocked:
            reason = "sni_rst"
            note = "控制IP复测RST"
        elif blocked is None:
            note = "对照不可用"
    err = f"{note} {first.get('err', '')} {first.get('ip', '')}".strip()
    return {**base, "ok": False, "reason": reason, "err": err}


def _node_probe(node: Node | None, host: str, port: int, use_tls: bool,
                timeout: float = 6.0) -> dict | None:
    if node is None or not node.usable:
        return None
    try:
        sock = netutil.open_via(node, host, port, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": f"{type(exc).__name__}: {str(exc)[:50]}"}
    try:
        if not use_tls:
            return {"ok": True}
        tls = netutil.tls_over(sock, host, timeout)
        code, _ = netutil.http_head(tls, host, "/", timeout)
        try:
            tls.close()
        except OSError:
            pass
        return {"ok": code in (200, 301, 302, 400, 403, 404), "code": code}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": f"{type(exc).__name__}: {str(exc)[:50]}"}
    finally:
        try:
            sock.close()
        except OSError:
            pass


@dataclass
class Row:
    host: str
    note: str
    direct: dict = field(default_factory=dict)
    node: dict | None = None
    verdict: str = ""

    @property
    def needs_node(self) -> bool:
        return self.verdict.startswith("需节点")


def run(node: Node | None = None, timeout: float = 4.0,
        workers: int = 6) -> list[Row]:
    """并发跑完所有端点，返回带结论的行。"""
    def one(ep: tuple[str, int, bool, str]) -> Row:
        host, port, use_tls, note = ep
        direct = _direct_probe(host, port, use_tls, timeout)
        via = _node_probe(node, host, port, use_tls, timeout + 2)
        return Row(host=host, note=note, direct=direct, node=via,
                   verdict=_verdict(direct, via))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, ENDPOINTS))


def _width(text: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def format_table(rows: list[Row]) -> str:
    cols = (34, 16, 24, 10)
    out = [f"{_pad('域名', cols[0])}{_pad('用途', cols[1])}"
           f"{_pad('直连', cols[2])}{_pad('节点', cols[3])}结论",
           "-" * (sum(cols) + 4)]
    for r in rows:
        d = r.direct
        if d.get("ok"):
            dtext = f"OK {d.get('ms', 0):.0f}ms {d.get('ip', '')}"
        else:
            dtext = f"{d.get('reason', '?')} {str(d.get('err', ''))[:14]}"
        n = r.node
        if n is None:
            ntext = "未配置"
        elif n.get("ok"):
            ntext = f"OK {n.get('code', '')}"
        else:
            ntext = "失败"
        out.append(f"{_pad(r.host, cols[0])}{_pad(r.note, cols[1])}"
                   f"{_pad(dtext, cols[2])}{_pad(ntext, cols[3])}{r.verdict}")
    return "\n".join(out)


def _usable(row: Row) -> bool:
    """这个端点在当前条件下到底能不能用。

    直连通 → 能用；直连不通但配置的节点能通 → 能用；两边都不行 → 不能用。
    「需节点 · SNI 阻断」在手边没节点时属于不能用，不是「一切正常」。
    """
    if row.direct.get("ok"):
        return True
    return bool(row.node and row.node.get("ok"))


def summarize(rows: list[Row], node: Node | None) -> dict:
    need = [r.host for r in rows if r.needs_node]
    dead = [r.host for r in rows if r.verdict.startswith("不通")]
    direct_ok = [r.host for r in rows if r.verdict == "直连可用"]
    unusable = [r.host for r in rows if not _usable(r)]
    return {
        "node": node.url() if node else None,
        "direct_ok": direct_ok,
        "need_node": need,
        "dead": dead,
        # 新增键（只增不改）：真正用不了的端点。ok 以它为准，
        # 这样 `svpn probe` 的退出码才能当「Steam 可用」的 CI 判据。
        "unusable": unusable,
        "ok": not unusable,
    }


direct_probe = _direct_probe
