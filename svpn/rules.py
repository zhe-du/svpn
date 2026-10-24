"""域名规则库：Steam / Valve 全域 + 国内直连清单 + Valve 网段。

分流判断只看域名后缀，不做 DNS 预解析（尽量把解析交给远端节点，
避免 DNS 污染导致 502/连接超时）。
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

# ---------------------------------------------------------------- Steam 域名
# 覆盖：商店、社区、登录/CM、Web API、内容分发(CDN)、创意工坊、聊天、直播、支付
STEAM_SUFFIXES: tuple[str, ...] = (
    # 主域
    "steampowered.com",
    "steamcommunity.com",
    "steamstatic.com",
    "steamcontent.com",
    "steamusercontent.com",
    "steam-chat.com",
    "steamgames.com",
    "steamdeck.com",
    "steam.tv",
    "s.team",
    "steamserver.net",
    "steamconnecttest.com",      # 客户端连通性探针，被污染会误判「离线」
    "steamcdn-a.akamaihd.net",
    "steamcommunity-a.akamaihd.net",
    "steamusercontent-a.akamaihd.net",
    # Valve 基础设施
    "valvesoftware.com",
    "valve.net",
    "valve.steamstatic.com",
    "underlords.com",
    "dota2.com",
    "counter-strike.net",
    "csgo.com",
    "tf2.com",
    "artifact.com",
    # 登录 / 支付相关第三方
    "steampowered.com.edgesuite.net",
    "akamai.steamstatic.com",
    "cdn.akamai.steamstatic.com",
    "community.akamai.steamstatic.com",
    "shared.akamai.steamstatic.com",
    "store.akamai.steamstatic.com",
    # Steam 客户端遥测 / 更新
    "client-update.akamai.steamstatic.com",
    "media.steampowered.com",
    "api.steampowered.com",
    "login.steampowered.com",
    "help.steampowered.com",
    "checkout.steampowered.com",
    "store.steampowered.com",
    "partner.steampowered.com",
    "partner.steamgames.com",
    "support.steampowered.com",
)

# 只在对国内站点做“代理优先”时才用得到；默认模式下这些走直连
CN_DIRECT_SUFFIXES: tuple[str, ...] = (
    "cn", "qq.com", "gtimg.com", "myqcloud.com", "tencent.com",
    "baidu.com", "bdstatic.com", "bilibili.com", "hdslb.com", "bilivideo.com",
    "taobao.com", "tmall.com", "alicdn.com", "aliyun.com", "alipay.com",
    "jd.com", "360buyimg.com", "163.com", "126.net", "sina.com.cn",
    "weibo.com", "zhihu.com", "douyin.com", "byteimg.com", "kuaishou.com",
    "xiaomi.com", "mi.com", "huawei.com", "hinova.com", "meituan.com",
    "pinduoduo.com", "douban.com", "csdn.net", "gitee.com", "jianshu.com",
    "microsoft.com", "windowsupdate.com", "msftconnecttest.com",
    "apple.com", "icloud.com", "mzstatic.com",
)

# Valve 自有网段（AS32590 / 关联段）。仅 split-tunnel 模式用；
# CDN 段会变，正式使用前建议 `whois -h whois.radb.net -- '-i origin AS32590'` 复核。
STEAM_CIDRS: tuple[str, ...] = (
    "23.45.64.0/18",
    "45.121.184.0/24",
    "103.10.124.0/23",
    "103.28.54.0/23",
    "146.66.152.0/21",
    "155.133.224.0/19",
    "162.254.192.0/18",
    "185.25.182.0/23",
    "192.69.96.0/22",
    "205.196.6.0/24",
    "208.64.200.0/22",
    "209.197.0.0/19",
)

# Steam 客户端进程名（用于识别/收尾）
STEAM_PROCESSES: tuple[str, ...] = (
    "steam", "steamwebhelper", "steamservice", "gameoverlayui",
    "steamerrorreporter",
)


def _norm(host: str) -> str:
    h = host.strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(_norm(host))
        return True
    except ValueError:
        return False


def match_suffix(host: str, suffixes: Iterable[str]) -> bool:
    """host 等于 suffix 或为其子域。`cn` 这种顶级后缀也能匹配 .cn。"""
    h = _norm(host)
    if not h:
        return False
    for s in suffixes:
        s = s.lower().lstrip(".")
        if h == s or h.endswith("." + s):
            return True
    return False


def in_cidrs(host: str, cidrs: Iterable[str]) -> bool:
    if not is_ip(host):
        return False
    ip = ipaddress.ip_address(_norm(host))
    for c in cidrs:
        try:
            if ip in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


class Router:
    """三档策略：

    whitelist  Steam/名单内 -> 上游，其余直连        （日常推荐，国内网站不绕路）
    all       全部 -> 上游                            （整机走节点）
    blacklist 国内名单 -> 直连，其余 -> 上游          （自建节点全代理）
    """

    def __init__(
        self,
        mode: str = "whitelist",
        proxy_suffixes: Iterable[str] | None = None,
        direct_suffixes: Iterable[str] | None = None,
        proxy_cidrs: Iterable[str] | None = None,
        direct_hosts: Iterable[str] | None = None,
        proxy_hosts: Iterable[str] | None = None,
    ) -> None:
        self.mode = mode
        self.proxy_suffixes = tuple(proxy_suffixes or STEAM_SUFFIXES)
        self.direct_suffixes = tuple(direct_suffixes or CN_DIRECT_SUFFIXES)
        self.proxy_cidrs = tuple(proxy_cidrs or STEAM_CIDRS)
        self.direct_hosts = tuple(_norm(h) for h in (direct_hosts or ()))
        self.proxy_hosts = tuple(_norm(h) for h in (proxy_hosts or ()))

    def decide(self, host: str) -> str:
        h = _norm(host)
        # 显式覆盖优先级最高
        if h in self.direct_hosts:
            return "direct"
        if h in self.proxy_hosts:
            return "upstream"
        # 回环/内网永远直连，避免把本地服务送出去；
        # steamloopback.host 是 Steam CEF 的本地页面源，必须直连
        if h in ("localhost", "127.0.0.1", "::1", "steamloopback.host") \
                or h.endswith(".localhost"):
            return "direct"
        if is_ip(h):
            if in_cidrs(h, STEAM_CIDRS):
                return "upstream"
            # 私有/回环/链路本地一律直连；用 ipaddress 判定，避免 172.16/12
            # 只写了 172.16.* 而把 Docker(172.17)/WSL(172.20) 漏进上游
            try:
                ip = ipaddress.ip_address(h)
                if ip.is_private or ip.is_loopback or ip.is_link_local \
                        or ip.is_multicast or ip.is_reserved:
                    return "direct"
            except ValueError:
                pass
        if match_suffix(h, self.proxy_suffixes) or in_cidrs(h, self.proxy_cidrs):
            return "upstream"
        if self.mode == "all":
            return "upstream"
        if self.mode == "blacklist":
            return "direct" if match_suffix(h, self.direct_suffixes) else "upstream"
        return "direct"  # whitelist
