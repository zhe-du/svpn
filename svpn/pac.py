"""PAC 生成：让浏览器 / Steam 内置 Chromium 按域名自动选路。

注意：PAC 里必须是 **数组字面量**（`[ ... ]`），对象字面量在这里是语法错误，
浏览器会整个 PAC 加载失败 —— 这是本文件历史上翻过的车，别再改回去。
"""
from __future__ import annotations

import json

from .rules import CN_DIRECT_SUFFIXES, STEAM_SUFFIXES


def _js_list(items) -> str:
    """生成合法的 JS 数组元素。

    必须走 json.dumps：早先直接 f'"{s}"' 拼接，suffix 里只要带一个双引号或
    换行就会把整份 PAC 变成非法 JS（浏览器静默回退直连，表现是「设了代理没用」）。
    json.dumps 同时也处理了反斜杠、控制字符和中文（ensure_ascii=False）。
    """
    return ",\n".join(f"  {json.dumps(str(s), ensure_ascii=False)}" for s in items)


def build_pac(socks_host: str = "127.0.0.1", socks_port: int = 7899,
              proxy_suffixes=None, direct_suffixes=None,
              local_net: bool = True, mode: str = "whitelist") -> str:
    prox = sorted(set(proxy_suffixes or STEAM_SUFFIXES))
    # blacklist 模式必须真的带上直连名单：空表会让「国内直连」变成一句空话
    # （Router 侧是 `direct_suffixes or CN_DIRECT_SUFFIXES`，这里必须同口径）
    fallback = CN_DIRECT_SUFFIXES if mode == "blacklist" else ()
    direct = sorted(set(direct_suffixes or fallback))
    pjs = _js_list(prox)
    djs = _js_list(direct)
    if mode == "all":
        default_rule = 'return SOCK + "; DIRECT";'
    elif mode == "blacklist":
        default_rule = ('if (_hit(host, DIRECT)) return "DIRECT";\n'
                        '  return SOCK + "; DIRECT";')
    else:
        default_rule = 'return "DIRECT";'
    return f"""// svpn 自动生成，重跑 `svpn pac --write` 覆盖
var SOCK = "SOCKS5 {socks_host}:{socks_port}";
var PROXY = [
{pjs}
];
var DIRECT = [
{djs}
];

function _hit(host, tbl) {{
  for (var i = 0; i < tbl.length; i++) {{
    var s = tbl[i];
    if (host === s || host.substring(host.length - s.length - 1) === "." + s) return true;
  }}
  return false;
}}

function FindProxyForURL(url, host) {{
  host = host.toLowerCase().replace(/\\.$/, "");
  if (host === "localhost" || host === "127.0.0.1" || host === "::1") return "DIRECT";
  {"if (isPlainHostName(host) || shExpMatch(host, '*.local')) return 'DIRECT';" if local_net else ""}
  if (_hit(host, PROXY)) return SOCK + "; DIRECT";
  if (_hit(host, DIRECT)) return "DIRECT";
  {default_rule}
}}
"""
