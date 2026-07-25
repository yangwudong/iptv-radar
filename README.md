# iptv-radar

[English](README.md) | [中文](README.zh-CN.md)

**Scan · Manage · Publish** system for China Telecom IPTV (Zhejiang) channels.

Automatically probes multicast/unicast stream quality, uses **SQLite as the single source
of truth**, and generates m3u playlists (with logos & EPG) plus a channel-monitoring dashboard.

> ⚠️ A personal reverse-engineering / automation project, for study and reference only.
> The multicast/unicast addresses involved are carrier-distributed information; no personal
> account data is included (credentials live in `.env`, which is never committed).

## Architecture (three decoupled layers)

```
scan → SQLite (source of truth) → ETL (link/select) → generate (m3u + dashboard) → publish
```

- **Scan** — probe multicast/unicast sources, record facts (technical attributes) only; no naming or ranking
- **ETL** — link sources to channels (`link_sources`) + pick the best source per channel (`etl_process`)
- **Generate** — m3u playlists + monitoring dashboard + official channel list page + EPG

## Core data model

SQLite is the single source of truth; three tables with strictly separated responsibilities:

| Table | Responsibility |
|-------|----------------|
| `channels` | Channel metadata ledger (name / logo / EPG id / groups). Append-only |
| `sources` | Discovered playable sources (multicast / unicast). Being listed ≠ identified |
| `channel_preferred_sources` | Preference relation produced by ETL (with `rank`, supports multiple sources per channel ordered by quality) |

Design highlights:

- **Stable surrogate key `channel_id`** (autoincrement integer) — renaming a channel never breaks relations
- `channel_key` (canonical channel name) is a UNIQUE column: human-readable, used for logo matching
- Dead sources are never deleted — flagged via `available=0` / `fail_count`; a channel with no usable source is kept (marked `offline`)
- Any source address → `channel_id` → all channel info (name / logo / EPG). Logo matching follows this path

## Layout

```
src/
  db_schema.py         # authoritative schema
  db_util.py           # shared SQLite connection (timeout + foreign keys on)
  seed.py              # rebuild channels/groups from a seed file
  scan_multicast.py    # multicast scanning
  scan_rtsp.py         # RTSP unicast scanning + redirect chain tracing
  probe.py             # ffprobe stream probing
  link_sources.py      # ETL: link sources to channels
  etl_process.py       # source selection + change detection
  orphan_export.py     # export unidentified sources for manual review
  orphan_import.py     # consume review decisions back into the database
  gen_m3u.py           # m3u generation (3 variants)
  gen_dashboard.py     # monitoring dashboard (preferred sources)
  gen_channels_page.py # official channel list page
  template_util.py     # Jinja2 rendering helper
  templates/           # dashboard / channel page templates
  fetch_channels.py    # EPG authentication, refreshes unicast/catchup tokens
  probe_timeshift.py   # catchup (time-shift) day detection
  fetch_epg.py         # EPG program guide
  run_pipeline.sh      # one-shot pipeline
tests/                 # regression tests (data integrity + generation)
docs/                  # design docs & how the carrier IPTV works
reference/             # official channel sample, logo index
data/                  # SQLite database (not distributed)
output/                # artifacts (m3u / dashboard)
```

## Usage

```bash
# Create the database (first run)
python3 src/db_schema.py

# One-shot pipeline: scan → ETL → select → generate
./src/run_pipeline.sh

# Pipeline modes (append --publish to publish the m3u files):
./src/run_pipeline.sh                  # default: incremental scan of known sources (~11 min, weekly)
./src/run_pipeline.sh --full           # full scan of all ranges + catchup probing (~20 min, monthly)
./src/run_pipeline.sh --timeshift-only # only refresh catchup-day data (~5 min)
./src/run_pipeline.sh --gen-only       # only regenerate m3u + dashboard from the existing DB (seconds)

# Or step by step
python3 src/scan_multicast.py     # multicast scan
python3 src/scan_rtsp.py --trace  # unicast scan
python3 src/link_sources.py       # link
python3 src/etl_process.py        # select
python3 src/gen_m3u.py            # generate m3u
python3 src/gen_dashboard.py      # generate dashboard
```

## Tests

Regression tests cover the data-integrity invariants that used to break silently
(cross-channel source leakage, offline detection, merge-snapshot preservation,
m3u/dashboard ordering, probe timeouts). CI runs them before publishing any image.

```bash
pip install -r requirements.txt pytest
python3 -m pytest tests/ -v
```

## Configuration

Real carrier addresses, credentials and deployment paths come from `.env` in the project root
(never committed — see `.env.example`).

## Multicast gateway: udpxy vs msd_lite vs rtp2httpd

IPTV multicast can't be consumed directly by most players/networks, so a gateway converts
multicast RTP/UDP into HTTP unicast. Three common choices — all work with this project
(the m3u output only needs `http://<host>:<port>/rtp/<multicast>`, a de-facto standard URL
shape shared by all three):

| | **udpxy** | **msd_lite** | **rtp2httpd** |
|---|---|---|---|
| Multicast → HTTP | ✅ | ✅ | ✅ |
| Maturity | Oldest, everywhere | Mature, widely packaged | Newer, actively developed |
| Performance | Single-threaded, simple | Multi-threaded, efficient | epoll + multi-worker + zero-copy |
| **FCC (fast channel change)** | ❌ | ❌ | ✅ Telecom/ZTE/Fiberhome + Huawei |
| **RTSP → HTTP (catchup/VOD)** | ❌ | ❌ | ✅ |
| FEC / RTP reordering | ❌ | ❌ | ✅ (Reed-Solomon FEC, sliding-window reorder) |
| Custom HTTP headers (CORS) | ❌ | ✅ via `headersList` | ✅ via `--cors-allow-origin` |
| Status page / web player | ❌ | Basic status | ✅ `/status` + `/player` + snapshots |
| Dependencies | Tiny C | Tiny C | Tiny C (zero deps) |
| OpenWRT package | ✅ | ✅ | ✅ (may need manual ipk) |

**Recommendation** — any of them is fine for plain multicast→HTTP. Prefer **rtp2httpd** if you
want faster channel switching (FCC), catchup over HTTP, or better resilience on lossy links;
its URL format is a drop-in replacement for udpxy/msd_lite, so existing playlists keep working.
**msd_lite** remains a solid, battle-tested choice (and supports custom headers such as CORS,
which web players need).

CORS matters if you play the streams in a **browser-based** player: the gateway response needs
`Access-Control-Allow-Origin`, otherwise the fetch is blocked cross-origin.
- msd_lite: add `<header>Access-Control-Allow-Origin: *</header>` to `headersList` in its config
- rtp2httpd: `option cors_allow_origin '*'` (UCI) or `--cors-allow-origin '*'`

### FCC (fast channel change) with rtp2httpd

FCC asks a carrier FCC server for a unicast burst (IDR frame + initial data) so playback starts
immediately, then transparently switches to multicast. Append the FCC server to the URL:

```
http://<gateway>:<port>/rtp/<multicast>:<port>?fcc=<fcc_server_ip>:<fcc_port>
```

Find your FCC server by capturing set-top-box traffic: look for **RTCP packets with payload
type 205 (Generic RTP Feedback), FMT=5 (RTCP-SR-REQ)** — the destination is the FCC server.
Gateways that don't understand `?fcc=` simply ignore the parameter, so the same playlist stays
compatible. In this project the FCC server is configured via `FCC_SERVER` in `.env` and applied
by `gen_m3u.py --fcc`.

## Advanced: native multicast to LAN clients on OpenWRT

By default multicast is transcoded to HTTP unicast by the gateway, which means the router does
one stream per viewer — a CPU bottleneck. On OpenWRT you can let LAN devices (IINA, APTV, …)
**receive the multicast RTP directly** (`rtp://@<multicast>`), bypassing the gateway. The trick
is an IGMP proxy that forwards multicast from the IPTV upstream interface to the LAN bridge on demand.

> Replace `<IPTV_iface>` / `<multicast source subnet>` with your own values.
> Multicast only works **inside the same LAN**; VPNs (WireGuard/Tailscale) do not forward
> multicast, so remote clients still need the HTTP gateway.

**1. Install igmpproxy** (better suited to IPv4 IPTV than the bundled omcproxy)
```sh
opkg update && opkg install igmpproxy
```

**2. Configure igmpproxy: upstream = IPTV interface, downstream = LAN** (`/etc/config/igmpproxy`)
```
config igmpproxy
    option quickleave 1

config phyint
    option network   <IPTV_iface>    # multicast upstream (e.g. the IPTV VLAN interface)
    option direction upstream
    list   altnet    <source subnet> # allowed multicast source subnet, e.g. x.x.0.0/16
config phyint
    option network   lan             # LAN downstream
    option direction downstream
```
Use the **real interface / VLAN sub-interface** upstream — not the bridge (`br-lan`).

**3. ⚠️ Critical: allow multicast forwarding in the firewall (the easiest thing to miss)**

If the IPTV zone's `forward` policy is `REJECT/DROP` (a common default), forwarded multicast UDP
is silently dropped — the symptom is that `ip_mr_vif` shows the downstream counter increasing
(the kernel thinks it forwarded) while nothing is captured on the device or physical port.
**Add a rule allowing IPTV→LAN multicast UDP:**
```
config rule
    option name   'Allow-IPTV-Multicast-to-LAN'
    option src    '<IPTV_zone>'
    option dest   'lan'
    option proto  'udp'
    option dest_ip '224.0.0.0/4'
    option target 'ACCEPT'
```

**4. IGMP snooping on the LAN bridge**
```sh
uci set network.@device[0].igmp_snooping='0'   # br-lan device; 0=flood (verified working here), 1=snooping needs a querier
uci commit network
```
(`1` delivers multicast only to ports that joined — cleaner for Wi-Fi clients. `0` floods, which
also works on a gigabit LAN. Either way only groups actually being watched are pulled upstream.)

**5. Reload and verify**
```sh
/etc/init.d/igmpproxy restart; /etc/init.d/firewall reload; /etc/init.d/network reload
ip mroute show   # while watching, expect (source, group) Iif:<IPTV_iface> Oifs:lan State:resolved
```
LAN devices can then open `rtp://@<multicast>` directly.

**Troubleshooting** (in this order, evidence first):
- Client sends no IGMP join → check the client. On macOS, OrbStack / Docker Desktop create virtual
  bridges that confuse the host's multicast interface selection; turning off
  *"Allow access to container domains & IPs"* in OrbStack restores it (no need to quit Docker).
- No `resolved` entry in `ip mroute show` → igmpproxy upstream / altnet / scope. Some carriers use
  organization-local groups (`233.x`), while igmpproxy defaults to proxying global scope only.
- `resolved` but nothing arrives / nothing on the physical port → **almost certainly the firewall
  forward policy (step 3)**.
- High-concurrency probing drops out: a device can only receive a handful of simultaneous multicast
  streams (carrier CPAR rate limits, snooping table pressure). ~4 concurrent streams is the sweet spot.

## The three generated playlists

The pipeline emits three m3u files for different playback scenarios:

| m3u | Multicast source | Unicast / catchup | Best for |
|-----|------------------|-------------------|----------|
| `iptv.m3u` standard | `http://<gateway>/rtp/…` (+`?fcc=` when configured) | catchup channels use the unicast source + `catchup` tags | remote / Tailscale / native players with catchup (e.g. APTV) |
| `iptv_direct.m3u` direct | `rtp://@…` (received natively) | same as standard | LAN only, lowest latency, no transcoding (e.g. IINA) |
| `iptv_compat.m3u` compat | `http://<gateway>/rtp/…` (+`?fcc=`) | channels that have a multicast source fall back to it (no catchup); pure-unicast channels keep rtsp | players that only speak multicast-over-HTTP and not rtsp (e.g. browser-based players) |

Corresponding `gen_m3u.py` flags: `--multicast-mode msd|direct` selects the multicast URL form,
`--prefer-multicast` produces the compat variant, `--fcc <ip:port>` adds FCC.

## License

Personal project, for study and reference only.
