"""回归：DoH 解析绝不能走系统代理（否则 `up --system-proxy` 会自我环路）。

真实故障：svpn 接管 WinINET 后，`urllib.request.urlopen` 读到
`http://127.0.0.1:7900` 这个代理（就是 svpn 自己），DoH 请求绕回自身，
每个域名解析卡到 12s 超时，表现为「开了系统代理反而全站打不开」。
修法是 `_NO_PROXY_OPENER = build_opener(ProxyHandler({}))`，这里把它焊死。
"""
import json
import os
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from svpn import netutil  # noqa: E402


class _FakeResp:
    status = 200

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


PAYLOAD = {"Status": 0, "Answer": [{"name": "a.example.com", "type": 1,
                                    "TTL": 60, "data": "1.2.3.4"}]}


class DohProxyBypassTests(unittest.TestCase):
    def test_opener_has_no_proxy_handler(self) -> None:
        # build_opener(ProxyHandler({})) 的语义：这个 opener 根本不挂代理处理器，
        # 因此既不会读环境变量，也不会读 WinINET 注册表。
        handlers = [h for h in netutil._NO_PROXY_OPENER.handlers
                    if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(handlers, [], "opener 里不允许出现 ProxyHandler")

    def test_env_proxy_is_ignored(self) -> None:
        env = {"HTTP_PROXY": "http://127.0.0.1:7900",
               "HTTPS_PROXY": "http://127.0.0.1:7900",
               "ALL_PROXY": "socks5://127.0.0.1:7900"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(netutil._NO_PROXY_OPENER, "open",
                                  return_value=_FakeResp(PAYLOAD)) as opener:
            ips = netutil._doh_query("https://doh.pub/dns-query", "a.example.com", "A", 3)
        self.assertEqual(ips, ["1.2.3.4"])
        opener.assert_called_once()

    def test_system_proxy_registry_is_ignored(self) -> None:
        # 模拟 WinINET 里配了指向 svpn 自己的代理：解析结果不受影响
        with mock.patch.object(urllib.request, "getproxies",
                               return_value={"https": "http://127.0.0.1:7900"}), \
                mock.patch.object(netutil._NO_PROXY_OPENER, "open",
                                  return_value=_FakeResp(PAYLOAD)) as opener:
            ips = netutil._doh_query("https://dns.alidns.com/resolve",
                                     "a.example.com", "A", 3)
        self.assertEqual(ips, ["1.2.3.4"])
        self.assertEqual(opener.call_count, 1)

    def test_non_200_still_raises(self) -> None:
        resp = _FakeResp(PAYLOAD)
        resp.status = 502
        with mock.patch.object(netutil._NO_PROXY_OPENER, "open", return_value=resp):
            with self.assertRaises(ValueError):
                netutil._doh_query("https://doh.pub/dns-query", "a.example.com", "A", 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
