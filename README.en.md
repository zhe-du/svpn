# svpn — a Steam-only local split proxy (Windows)

A condensed, standalone English overview. The Chinese `README.md` is the full manual.

## What

`svpn` runs a loopback-only proxy on your PC and routes **only Steam traffic** through your
upstream node, leaving everything else direct:

- SOCKS5 `127.0.0.1:7899` and HTTP `127.0.0.1:7900` (loopback only, by design)
- `whitelist` mode by default: only Steam/Valve domains (plus Valve CIDRs) use the node
- an optional CDN "edge picker": DoH → TLS race → 1 MB benchmark, then pin Steam download
  domains to the fastest Akamai edge
- **works without any node** (store/login/download still fine; community needs a node)

Windows 10/11 + Python 3.10+, **standard library only**, nothing to `pip install`.

## Why

Two facts drive the whole design:

1. `store` / `login` / `api` / CDN are reachable directly from most networks — they don't need a proxy.
2. `steamcommunity.com` (and `avatars.steamstatic.com`) are **SNI-blocked**: TCP connects, then the
   ClientHello carrying that hostname is reset. Changing DNS or IP does not help; only an upstream
   node does.

So the tool does not proxy everything. It splits: Steam domains → node, the rest → direct, and it
speeds up downloads by choosing a better CDN edge rather than by proxying them (game downloads are
plain TCP/HTTP and a proxy can't help).

## Quickstart

```powershell
cd D:\path\to\steam-vpn

# one-shot install: checks files, finds Python 3.10+, runs init + doctor, creates a desktop shortcut
powershell -ExecutionPolicy Bypass -File .\install.ps1

# or manually
python -m svpn init                     # writes config.json, backs up the system proxy
python -m svpn up -b --system-proxy     # start in background + take over the Windows proxy
python -m svpn status                   # verify listeners / node / mode
```

Double-click the desktop shortcut (`start-svpn.cmd`) for the usual flow: start the proxy →
set the system proxy → launch Steam → print one status receipt.

Download speed only (no node needed):

```powershell
python -m svpn cdn --refresh     # resolve via DoH, race TLS, benchmark 1 MB
python -m svpn hosts apply       # write the winning edge IPs into the hosts managed block (one UAC prompt)
```

With an existing client (Clash / v2rayN / sing-box / Watt Toolkit…):

```powershell
python -m svpn node scan                 # probe common local ports, real handshake to Steam
python -m svpn node set socks5://127.0.0.1:7890
python -m svpn up -b --system-proxy
python -m svpn steam --restart --system  # Steam must be restarted to pick up a new proxy
```

`svpn.cmd` (`.\svpn.cmd up -b`) is the plain cmd wrapper and works for everyone; `svpnctl.ps1`
(`.\svpnctl.ps1 up -Background -System`) is an **optional** PowerShell-flavoured layer for people who
prefer switches — it only forwards to `python -m svpn`. Both default to `status` with no arguments.

> Editing a `.cmd` to add your own steps? Read `docs/WINDOWS.md` §1.3 first: comments must be ASCII,
> every line containing Chinese must be followed by an empty line, and every `python` call needs `call`.
> Miss any of these and the window either flashes `'?' is not recognized` or silently exits 0.

## Two CDN modes (`cdn.enabled`)

| | `false` (default) | `true` |
|---|---|---|
| `hosts apply` writes | best edge IP per domain | every `cdn.hosts` domain → `127.0.0.1` |
| Needs a resident process | no | **yes** — `svpn up` must keep running |
| Uses local port 443 | no | yes (`cdn.sni_port`, effectively 443) |
| When the edge IP drifts | re-run `cdn --refresh` + `hosts apply` | refresh cache only, hosts untouched |

`true` starts a SNI transparent forwarder inside `svpn up`: it reads the SNI from the client
ClientHello, maps the hostname back, dials the cached best IP (or the node). Whitelist only;
an empty whitelist rejects everything. If port 443 is taken it logs a warning and the proxy still runs.

## Configuration

`config.json` has exactly **12 top-level keys**: `listen_host`, `listen_port`, `http_port`, `mode`,
`connect_timeout`, `auto_node`, `upstream`, `auth`, `browser`, `cdn`, `proxy_hosts`, `direct_hosts`.
`config.example.json` is the canonical template. Unknown keys and `_`-prefixed keys are dropped at
load time; invalid numbers fall back to defaults instead of raising.

Two optional advanced keys are accepted if you write them by hand, even though they are not in the
template: `proxy_suffixes` / `direct_suffixes` **replace** (not extend) the built-in "go through the
node" / "connect directly" suffix lists used by the router and the generated PAC. Values may be a JSON
array or a comma/space separated string (both normalised to a list); empty or missing means "use the
built-in list". In `mode=blacklist`, an empty `direct_suffixes` still falls back to the built-in CN
list (otherwise "CN goes direct" would be meaningless). They survive `node set` / `node clear`, but
`init --force` rewrites the file from the template and drops them.
Priority: `direct_hosts` > `proxy_hosts` (exact host) > `*_suffixes` > built-ins.

## `probe` exit codes (CI)

```bash
python -m svpn probe --timeout 6
# 0 = every endpoint usable
# 1 = at least one endpoint unusable (summary.unusable)
# 2 = bad arguments / unparsable config.json

python -m svpn probe --lenient   # legacy: exit 1 only for "不通 · 待排查" (hard-dead endpoints)
```

Machine-readable results: read `runtime/probe.json` (`{ts, node, rows[], summary}`); `--json` prints
the table first and the JSON block afterwards, so it is not a pure JSON stream.

## Limits

- No node → community / workshop / avatars stay unreachable. That is the SNI block, not a bug.
- Download acceleration only covers Steam CDN domains listed in `cdn.hosts`.
- Half-open connections wait for the OS TCP timeout: `_bridge` / `SniForwarder` use
  `asyncio.gather(..., return_exceptions=True)`, which waits for both directions to end rather than
  cancelling the other side. Deliberate trade-off (no byte truncation) — see `docs/ARCHITECTURE.md`.
- `hosts` writes need one administrator (UAC) approval.
- Only `socks5://` and `http://` upstreams are understood; `ss://`, `vmess://`, `trojan://` need an
  external core exposing a local SOCKS5 port.
- `svpn/split.py` (route-injection split tunnel) is **not wired** to any command.

## Uninstall

```powershell
python -m svpn down --clear-hosts                                        # stop + restore proxy + clear hosts
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1                  # also removes the shortcut
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge -Yes      # ... and runtime/ backups
```

`uninstall.ps1` is idempotent: it kills only the PID in `runtime/svpn.pid` (never by image name),
restores the WinINET proxy from `runtime/wininet-backup.json`, removes only svpn's own hosts block
(byte-preserving for the rest of the file), deletes the desktop shortcut, and — only with `-Purge` —
removes `runtime\` and `svpn.egg-info\`. It never touches `config.json`, the source tree, or pip packages.

## More

- `docs/ARCHITECTURE.md` — data flow, routing table, failure taxonomy, design trade-offs
- `docs/WINDOWS.md` — UTF-8 BOM rules, the `call` trap in `.cmd`, UAC and hosts, WinINET registry layout,
  WinError 10048/121, Win10 vs Win11 differences
- Tests: `python tests/smoke_test.py` (offline, 15 checks), `python -m unittest discover -s tests -p "test_*.py"` (offline, 364 cases)
