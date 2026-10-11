#!/usr/bin/env python3
"""
nodewatch - watch AllStar nodes/hubs, EchoLink nodes and general servers,
and email (and optionally push-notify) when one goes down or comes back.

Standard library only. Python 3.11+ (or 3.8+ with `pip install tomli`).

Usage:
  python3 nodewatch.py -c config.toml --setup      menu: add nodes/servers, email setup (start here)
  python3 nodewatch.py -c config.toml              run forever (use with systemd)
  python3 nodewatch.py -c config.toml --once       one check cycle (use with cron)
  python3 nodewatch.py -c config.toml --check      print status of everything, send nothing
  python3 nodewatch.py -c config.toml --test-email send a test message and exit
  python3 nodewatch.py --probe 12345                look up a node / IP / URL and suggest checks
"""

import argparse
import getpass
import html
import json
import os
import random
import re
import secrets
import shutil
import signal
import smtplib
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        sys.exit("nodewatch needs Python 3.11+, or run: pip install tomli")

VERSION = "1.1"
USER_AGENT = f"nodewatch/{VERSION} (amateur radio node monitor)"
ALLSTAR_API = "https://stats.allstarlink.org/api/stats/{node}"
ECHOLINK_LOGINS = "https://www.echolink.org/logins.jsp"

UP, DOWN, UNKNOWN = "UP", "DOWN", "UNKNOWN"


# --------------------------------------------------------------------------- utils

def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def http_get(url, timeout=20, verify=True):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read().decode("utf-8", "replace")


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- IAX2 poke

def iax2_poke(host, port=4569, timeout=3.0, tries=2):
    """
    Send an IAX2 POKE to an Asterisk/AllStar node and wait for a reply.
    Any IAX2 full frame addressed back to our call number (normally a PONG)
    means Asterisk is alive and reachable on that port.
    Returns round-trip time in ms, or None if nothing came back.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except socket.gaierror:
        return None
    family, _, _, _, addr = infos[0]
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(tries):
            src_call = random.randint(1, 0x7FFF)
            # Full frame: F|src call, R|dst call, timestamp, oseq, iseq, type=IAX(6), subclass=POKE(0x1e)
            pkt = struct.pack("!HHIBBBB", 0x8000 | src_call, 0, 0, 0, 0, 6, 0x1E)
            start = time.monotonic()
            try:
                sock.sendto(pkt, addr)
            except OSError:
                return None
            deadline = start + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, _ = sock.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError:
                    break
                if len(data) < 12:
                    continue
                (w0, w1) = struct.unpack("!HH", data[:4])
                if not (w0 & 0x8000):
                    continue
                if (w1 & 0x7FFF) != src_call:
                    continue
                return round((time.monotonic() - start) * 1000, 1)
        return None
    finally:
        sock.close()


# --------------------------------------------------------------------------- checks

def stats_last_time(stats):
    """Unix time of a node's last stats report (0 if none)."""
    sdata = stats.get("data") or {}
    try:
        last = int(sdata.get("time") or 0)
    except (TypeError, ValueError):
        last = 0
    if not last and stats.get("updated_at"):
        try:
            last = int(datetime.fromisoformat(
                stats["updated_at"].replace("Z", "+00:00")).timestamp())
        except ValueError:
            last = 0
    return last


def count_links(sdata):
    linked = sdata.get("linkedNodes")
    if linked is None:
        linked = sdata.get("links") or []
    return len(linked) if isinstance(linked, list) else 0


def check_allstar(t, ctx):
    """AllStar node or hub. Uses the AllStarLink stats API and/or a direct IAX2 poke."""
    node = str(t["node"]).strip()
    methods = t.get("checks", ctx["defaults"]["allstar_checks"])
    stale_min = float(t.get("stale_minutes", ctx["defaults"]["allstar_stale_minutes"]))
    min_links = t.get("min_links")
    now = time.time()

    results = []          # list of (status, detail)
    api_node_ip = None
    api_node_port = 4569

    if "stats" in methods or ("iax" in methods and not t.get("iax_host")) or min_links is not None:
        try:
            code, body = http_get(ALLSTAR_API.format(node=node))
            data = json.loads(body)
        except urllib.error.HTTPError as e:
            data = None
            api_err = f"stats API HTTP {e.code}"
        except Exception as e:  # network, JSON, timeout
            data = None
            api_err = f"stats API error: {e.__class__.__name__}"
        else:
            api_err = None

        if data is None:
            if "stats" in methods:
                results.append((UNKNOWN, api_err))
        else:
            nrec = data.get("node") or {}
            api_node_ip = nrec.get("ipaddr")
            api_node_port = int(nrec.get("port") or 4569)
            stats = data.get("stats") or {}
            last = stats_last_time(stats)
            n_links = count_links(stats.get("data") or {})

            if "stats" in methods:
                if not stats or not last:
                    results.append((DOWN, "no stats on record for this node"))
                else:
                    age = now - last
                    if age > stale_min * 60:
                        results.append((DOWN, f"last stats report {fmt_duration(age)} ago"))
                    else:
                        results.append((UP, f"stats {int(age // 60)}m old, {n_links} linked"))

            if min_links is not None and stats and last and (now - last) <= stale_min * 60:
                if n_links < int(min_links):
                    results.append((DOWN, f"only {n_links} linked node(s), expected at least {min_links}"))

    if "iax" in methods:
        host = t.get("iax_host") or api_node_ip
        port = int(t.get("iax_port") or (api_node_port if not t.get("iax_host") else 4569))
        if not host:
            results.append((UNKNOWN, "no IP for IAX poke (set iax_host)"))
        else:
            rtt = iax2_poke(host, port)
            if rtt is None:
                results.append((DOWN, f"no IAX2 reply from {host}:{port}"))
            else:
                results.append((UP, f"IAX2 reply {rtt}ms"))

    return combine(results)


def get_echolink_list(ctx):
    """Fetch and parse the EchoLink logins page once per cycle."""
    if "echolink" in ctx["cache"]:
        return ctx["cache"]["echolink"]
    result = {"ok": False, "error": None, "by_call": {}, "by_node": {}, "as_of": None}
    try:
        _, body = http_get(ECHOLINK_LOGINS, timeout=30)
    except Exception as e:
        result["error"] = f"EchoLink list unavailable: {e.__class__.__name__}"
        ctx["cache"]["echolink"] = result
        return result

    m = re.search(r"As of\s+(\d{1,2} \w{3} \d{4} \d{1,2}:\d{2}:\d{2})\s*UTC", body)
    if m:
        try:
            result["as_of"] = datetime.strptime(m.group(1), "%d %b %Y %H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass

    def clean(cell):
        cell = re.sub(r"<[^>]+>", " ", cell)
        return re.sub(r"\s+", " ", html.unescape(cell)).strip()

    # innermost rows only (the page uses nested layout tables)
    for row in re.findall(r"<tr[^>]*>((?:(?!<tr).)*?)</tr>", body, re.S | re.I):
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) < 5:
            continue
        call, location, status, _tm, node = cells[-5:]
        if not re.fullmatch(r"\d{1,7}", node) or not call:
            continue
        entry = {"call": call.upper(), "location": location, "status": status.upper(), "node": node}
        result["by_call"][call.upper()] = entry
        result["by_node"][node] = entry

    if len(result["by_node"]) < 200:
        result["error"] = f"EchoLink list looks incomplete ({len(result['by_node'])} stations parsed)"
    else:
        result["ok"] = True
    ctx["cache"]["echolink"] = result
    return result


def check_echolink(t, ctx):
    lst = get_echolink_list(ctx)
    if not lst["ok"]:
        return UNKNOWN, lst["error"]
    stale = float(ctx["defaults"]["echolink_list_stale_minutes"])
    if lst["as_of"] and time.time() - lst["as_of"] > stale * 60:
        return UNKNOWN, f"EchoLink list is stale (as of {fmt_time(lst['as_of'])})"

    entry = None
    if t.get("node"):
        entry = lst["by_node"].get(str(t["node"]).strip())
    if entry is None and t.get("callsign"):
        entry = lst["by_call"].get(str(t["callsign"]).strip().upper())
    if entry is None:
        return DOWN, "not logged in to EchoLink"
    if entry["status"] in ("ON", "BUSY"):
        return UP, f"{entry['call']} #{entry['node']} {entry['status']}"
    return DOWN, f"{entry['call']} status {entry['status']}"


def check_ping(t, ctx):
    host = t["host"]
    count = str(t.get("count", 3))
    flag = "-6" if ":" in host else "-4"
    try:
        p = subprocess.run(["ping", flag, "-c", count, "-W", "2", host],
                           capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return UNKNOWN, "ping command not found"
    except subprocess.TimeoutExpired:
        return DOWN, "ping timed out"
    if p.returncode == 0:
        m = re.search(r"= [\d.]+/([\d.]+)/", p.stdout)
        return UP, f"ping ok{(' ' + m.group(1) + 'ms') if m else ''}"
    return DOWN, "no ping reply"


def check_tcp(t, ctx):
    host, port = t["host"], int(t["port"])
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=float(t.get("timeout", 5))):
            pass
    except OSError as e:
        return DOWN, f"TCP {port} failed: {e.strerror or e.__class__.__name__}"
    return UP, f"TCP {port} open {round((time.monotonic() - start) * 1000)}ms"


def check_http(t, ctx):
    url = t["url"]
    try:
        code, body = http_get(url, timeout=float(t.get("timeout", 15)),
                              verify=not t.get("ignore_cert", False))
    except urllib.error.HTTPError as e:
        return DOWN, f"HTTP {e.code}"
    except Exception as e:
        return DOWN, f"HTTP failed: {e.__class__.__name__}"
    kw = t.get("keyword")
    if kw and kw not in body:
        return DOWN, f"HTTP {code} but keyword '{kw}' missing"
    return UP, f"HTTP {code}"


CHECKS = {
    "allstar": check_allstar,
    "hub": check_allstar,       # hubs are AllStar nodes; use min_links if you care about links
    "echolink": check_echolink,
    "ping": check_ping,
    "tcp": check_tcp,
    "http": check_http,
}

REQUIRED = {"allstar": ["node"], "hub": ["node"], "ping": ["host"],
            "tcp": ["host", "port"], "http": ["url"]}


def combine(results):
    """All listed checks must be UP. Any DOWN -> DOWN. Otherwise UNKNOWN if any unknown."""
    if not results:
        return UNKNOWN, "no checks configured"
    downs = [d for s, d in results if s == DOWN]
    if downs:
        return DOWN, "; ".join(downs)
    unknowns = [d for s, d in results if s == UNKNOWN]
    if unknowns:
        return UNKNOWN, "; ".join(unknowns)
    return UP, "; ".join(d for _, d in results)


# --------------------------------------------------------------------------- notify

def send_email(cfg, subject, body):
    e = cfg.get("email") or {}
    if not e.get("smtp_host"):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = e["from_addr"]
    to = e["to"] if isinstance(e["to"], list) else [e["to"]]
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    security = e.get("security", "starttls").lower()
    port = int(e.get("smtp_port", 465 if security == "ssl" else 587))
    ctx = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(e["smtp_host"], port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(e["smtp_host"], port, timeout=30)
        if security == "starttls":
            server.starttls(context=ctx)
    try:
        if e.get("username"):
            server.login(e["username"], e["password"])
        server.send_message(msg)
    finally:
        server.quit()
    return True


def send_ntfy(cfg, title, body, urgent):
    n = cfg.get("ntfy") or {}
    if not n.get("url"):
        return False
    headers = {"Title": title.encode("ascii", "replace").decode(), "User-Agent": USER_AGENT,
               "Priority": "high" if urgent else "default",
               "Tags": "rotating_light" if urgent else "white_check_mark"}
    if n.get("token"):
        headers["Authorization"] = f"Bearer {n['token']}"
    req = urllib.request.Request(n["url"], data=body.encode(), headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=15).read()
    return True


def notify(cfg, events):
    """events: list of dicts {name, kind ('down','up','still_down','unknown'), detail, since}"""
    prefix = (cfg.get("email") or {}).get("subject_prefix", "[nodewatch]")
    n_down = sum(1 for e in events if e["kind"] in ("down", "still_down"))
    n_up = sum(1 for e in events if e["kind"] == "up")
    if len(events) == 1:
        e = events[0]
        label = {"down": "DOWN", "up": "BACK UP", "still_down": "STILL DOWN",
                 "unknown": "CAN'T CHECK"}[e["kind"]]
        subject = f"{prefix} {label}: {e['name']}"
    else:
        bits = []
        if n_down:
            bits.append(f"{n_down} down")
        if n_up:
            bits.append(f"{n_up} back up")
        other = len(events) - n_down - n_up
        if other:
            bits.append(f"{other} can't check")
        subject = f"{prefix} " + ", ".join(bits)

    lines = []
    now = time.time()
    for e in events:
        if e["kind"] == "down":
            lines.append(f"DOWN: {e['name']}\n  {e['detail']}\n  since {fmt_time(e['since'])}")
        elif e["kind"] == "still_down":
            lines.append(f"STILL DOWN: {e['name']} ({fmt_duration(now - e['since'])})\n  {e['detail']}")
        elif e["kind"] == "up":
            lines.append(f"BACK UP: {e['name']} (was down {fmt_duration(now - e['since'])})\n  {e['detail']}")
        else:
            lines.append(f"CAN'T CHECK: {e['name']}\n  {e['detail']}\n"
                         f"  (the status source isn't answering; the node itself may be fine)")
    body = "\n\n".join(lines) + f"\n\n-- nodewatch on {socket.gethostname()} at {fmt_time(now)}"

    urgent = n_down > 0
    for sender, args in ((send_email, (cfg, subject, body)), (send_ntfy, (cfg, subject, body, urgent))):
        try:
            if sender(*args):
                log(f"sent {sender.__name__[5:]}: {subject}")
        except Exception as ex:
            log(f"ERROR sending {sender.__name__[5:]}: {ex}")


# --------------------------------------------------------------------------- engine

def target_name(t):
    if t.get("name"):
        return t["name"]
    ident = t.get("node") or t.get("callsign") or t.get("host") or t.get("url")
    return f"{t['type']} {ident}"


def validate_config(cfg, path, allow_empty=False):
    """Fill in defaults and check targets. Raises ValueError with a readable message."""
    s = cfg.setdefault("settings", {})
    s.setdefault("interval_seconds", 120)
    s.setdefault("fail_threshold", 2)
    s.setdefault("unknown_threshold", 15)
    s.setdefault("remind_every_hours", 6)
    s.setdefault("allstar_checks", ["stats"])
    s.setdefault("allstar_stale_minutes", 15)
    s.setdefault("echolink_list_stale_minutes", 30)
    s.setdefault("state_file", os.path.join(os.path.dirname(os.path.abspath(path)), "nodewatch_state.json"))
    targets = cfg.setdefault("target", [])
    if not targets and not allow_empty:
        raise ValueError("No [[target]] entries in config (add some with: nodewatch setup)")
    names = set()
    for t in targets:
        if t.get("type") not in CHECKS:
            raise ValueError(f"Unknown target type {t.get('type')!r} (use one of {', '.join(CHECKS)})")
        missing = [k for k in REQUIRED.get(t["type"], []) if k not in t]
        if t["type"] == "echolink" and not (t.get("callsign") or t.get("node")):
            missing.append("callsign")
        if missing:
            raise ValueError(f"Target {target_name(t)!r} is missing {', '.join(missing)}")
        n = target_name(t)
        if n in names:
            raise ValueError(f"Duplicate target name {n!r}; give each one a unique name")
        names.add(n)
    return cfg


def load_config(path):
    try:
        with open(path, "rb") as f:
            return validate_config(tomllib.load(f), path)
    except FileNotFoundError:
        sys.exit(f"No config file at {path}. Create one with: nodewatch setup "
                 f"(or python3 nodewatch.py -c {path} --setup)")
    except (ValueError, tomllib.TOMLDecodeError) as e:
        sys.exit(f"Problem in {path}: {e}")


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def run_checks(cfg, on_result=None):
    ctx = {"defaults": cfg["settings"], "cache": {}}
    out = []
    for t in cfg["target"]:
        if t.get("enabled", True) is False:
            continue
        try:
            status, detail = CHECKS[t["type"]](t, ctx)
        except Exception as ex:
            status, detail = UNKNOWN, f"check error: {ex.__class__.__name__}: {ex}"
        out.append((target_name(t), status, detail))
        if on_result:
            on_result(target_name(t), status, detail)
        if t["type"] in ("allstar", "hub"):
            time.sleep(0.5)  # be gentle with the AllStarLink API
    return out


def cycle(cfg, state):
    s = cfg["settings"]
    now = time.time()
    events = []
    results = run_checks(cfg)
    live = set()
    for name, status, detail in results:
        live.add(name)
        st = state.get(name)
        first_run = st is None
        if first_run:
            st = state[name] = {"confirmed": None, "since": now, "candidate": status,
                                "candidate_since": now, "count": 0, "last_alert": 0, "detail": detail}
        if status == st["candidate"]:
            st["count"] += 1
        else:
            st["candidate"], st["count"], st["candidate_since"] = status, 1, now
        st["detail"] = detail
        st["last_check"] = now

        need = {UP: 1, DOWN: int(s["fail_threshold"]), UNKNOWN: int(s["unknown_threshold"])}[status]
        if st["confirmed"] is None and status == UP:
            need = 1
        if st["count"] >= need and st["confirmed"] != status:
            prev, prev_since = st["confirmed"], st["since"]
            st["confirmed"], st["since"] = status, st.get("candidate_since", now)
            if status == DOWN:
                events.append({"name": name, "kind": "down", "detail": detail, "since": st["since"]})
                st["last_alert"] = now
            elif status == UP and prev == DOWN:
                events.append({"name": name, "kind": "up", "detail": detail, "since": prev_since})
            elif status == UNKNOWN:
                events.append({"name": name, "kind": "unknown", "detail": detail, "since": now})
            log(f"{name}: {prev or 'new'} -> {status} ({detail})")
        elif st["confirmed"] == DOWN and status == DOWN:
            remind = float(s["remind_every_hours"])
            if remind > 0 and now - st.get("last_alert", 0) >= remind * 3600:
                events.append({"name": name, "kind": "still_down", "detail": detail, "since": st["since"]})
                st["last_alert"] = now

        mark = "  " if status == UP else "!!" if status == DOWN else "??"
        log(f"{mark} {name}: {status} - {detail}")

    for gone in set(state) - live:   # targets removed from config
        del state[gone]
    save_state(s["state_file"], state)
    if events:
        notify(cfg, events)


# --------------------------------------------------------------------------- probing (finding what to watch)

# (text that shows on the page, short name, note). First match wins, so specific ones go first.
KNOWN_PAGES = [
    ("Service uptime", "XLX dashboard", "this text only shows while xlxd is running"),
    ("Supermon", "Supermon", ""),
    ("Allmon3", "Allmon3", ""),
    ("Allmon", "Allmon", ""),
    ("AllScan", "AllScan", ""),
    ("Alltune", "AllTune", ""),
    ("WPSD", "WPSD dashboard", ""),
    ("Pi-Star", "Pi-Star dashboard", ""),
    ("YSFReflector", "YSF reflector", ""),
    ("FreeDMR", "FreeDMR dashboard", ""),
    ("HBlink", "HBlink dashboard", ""),
]
# extra paths tried on every web port (common AllStar web tools)
WEB_PATHS = ["/supermon/", "/allmon3/", "/allmon2/", "/allscan/", "/alltune/"]
COMMON_TCP = [(22, "SSH"), (222, "SSH (HamVoIP)"), (2222, "SSH"), (80, "web"), (443, "web"),
              (81, "web"), (82, "web"), (8080, "web"), (8443, "web"), (5038, "Asterisk manager"),
              (3000, "web app"), (10000, "Webmin"), (21, "FTP"), (23, "telnet"), (25, "mail")]
PORT_NAMES = dict(COMMON_TCP)
WEB_PORTS = {80: "http", 81: "http", 82: "http", 8080: "http", 443: "https", 8443: "https"}


def tcp_open(host, port, timeout=3.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def page_info(url):
    """Fetch a web page and work out a keyword that proves it's working."""
    info = {"url": url, "ok": False, "code": None, "title": "", "keyword": None, "short": None,
            "note": "", "ignore_cert": False, "error": None, "body": ""}
    body = None
    for verify in (True, False):
        try:
            code, body = http_get(url, timeout=12, verify=verify)
            break
        except urllib.error.HTTPError as e:
            info["code"], info["error"] = e.code, f"HTTP {e.code}"
            return info
        except urllib.error.URLError as e:
            if verify and isinstance(e.reason, ssl.SSLCertVerificationError):
                info["ignore_cert"] = True      # self-signed certificate: try again without checking it
                continue
            info["error"] = str(e.reason)
            return info
        except Exception as e:
            info["error"] = e.__class__.__name__
            return info
    if body is None:
        return info
    info.update(ok=True, code=code, body=body)
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    raw_title = m.group(1).strip() if m else ""
    info["title"] = re.sub(r"\s+", " ", html.unescape(raw_title))
    low, title_low = body.lower(), raw_title.lower()
    # what the title says wins; otherwise the first known name found anywhere on the page
    known = [k for k in KNOWN_PAGES if k[0].lower() in title_low] or KNOWN_PAGES
    for text, short, note in known:
        i = low.find(text.lower())
        if i >= 0:
            info.update(keyword=body[i:i + len(text)], short=short, note=note)
            break
    else:
        if raw_title and "\n" not in raw_title and "<" not in raw_title and len(raw_title) <= 80:
            info["keyword"] = raw_title
    return info


def probe_host(host, say=print):
    """Ping a server, look for open ports and web pages. Returns what was found."""
    r = {"host": host, "ping": False, "ping_detail": "", "ports": [], "iax": None, "pages": []}
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror:
        r["error"] = f"can't find a server called {host!r} (check the spelling)"
        say("  " + r["error"])
        return r
    say(f"Checking {host} (about 10 seconds) ...")
    with ThreadPoolExecutor(max_workers=24) as ex:
        f_ping = ex.submit(check_ping, {"host": host, "count": 2}, None)
        f_iax = ex.submit(iax2_poke, host, 4569)
        f_ports = {p: ex.submit(tcp_open, host, p) for p, _ in COMMON_TCP}
        status, r["ping_detail"] = f_ping.result()
        r["ping"] = status == UP
        r["ports"] = [p for p, _ in COMMON_TCP if f_ports[p].result()]
        r["iax"] = f_iax.result()

        urlhost = f"[{host}]" if ":" in host else host
        futures = []
        for p in r["ports"]:
            if p not in WEB_PORTS:
                continue
            scheme = WEB_PORTS[p]
            base = f"{scheme}://{urlhost}" + ("" if p in (80, 443) else f":{p}")
            futures.append((p, True, ex.submit(page_info, base + "/")))
            futures += [(p, False, ex.submit(page_info, base + path)) for path in WEB_PATHS]
        for port, is_root, f in futures:
            pg = f.result()
            pg["port"] = port
            if is_root or (pg["ok"] and pg["short"]):   # extra paths only count if we recognise them
                r["pages"].append(pg)

    say(f"  ping:        {'answers - ' + r['ping_detail'] if r['ping'] else 'no reply (many servers block ping; fine if a port or web check works)'}")
    if r["ports"]:
        say("  open ports:  " + ", ".join(f"{p} {PORT_NAMES[p]}" for p in r["ports"]))
    else:
        say("  open ports:  none of the common ones")
    if r["iax"] is not None:
        say(f"  AllStar:     answers IAX on UDP 4569 ({r['iax']}ms) - this is an AllStar/Asterisk box;"
            " add its nodes by node number")
    for pg in r["pages"]:
        if not pg["ok"]:
            say(f"  web:         {pg['url']} - {pg['error']}")
            continue
        what = f"  -> {pg['short']}" if pg["short"] else ""
        title = f'  "{pg["title"][:50]}"' if pg["title"] else ""
        say(f"  web:         {pg['url']}  HTTP {pg['code']}{title}{what}")
    return r


def allstar_record(node):
    """A node's record from AllStarLink, or None if there's no such node."""
    for attempt in range(3):
        try:
            _, body = http_get(ALLSTAR_API.format(node=node))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 and attempt < 2:      # rate limited: wait and retry
                time.sleep(10 * (attempt + 1))
                continue
            raise
        data = json.loads(body)
        return data if isinstance(data, dict) and data.get("node") else None
    return None


def probe_node(node, say=print):
    """Look up an AllStar node, test it, and recommend which checks to use."""
    node = str(node).strip()
    r = {"node": node, "found": False, "checks": None, "why": "", "links": 0}
    say(f"Looking up AllStar node {node} ...")
    try:
        data = allstar_record(node)
    except Exception as e:
        r["error"] = f"AllStarLink lookup failed ({e.__class__.__name__}) - try again in a minute"
        say("  " + r["error"])
        return r
    if data is None:
        r["error"] = "AllStarLink has no node with that number"
        say("  " + r["error"])
        return r
    r["found"] = True
    n = data["node"]
    srv = n.get("server") or {}
    freq = ""
    m = re.match(r"\s*([\d.]+)", str(n.get("node_frequency") or ""))
    if m:
        try:
            f = float(m.group(1))
            freq = f"{f:.3f}" if f else ""
        except ValueError:
            pass
    call = str(n.get("callsign") or n.get("User_ID") or "").strip()
    place = str(srv.get("Location") or srv.get("SiteName") or "").strip()
    r.update(callsign=call, location=place, freq=freq, ip=n.get("ipaddr"),
             port=int(n.get("port") or 4569), proxy=srv.get("proxy_ip"))
    r["suggest_name"] = " ".join(x for x in (node, call, place, freq) if x)
    desc = "  ".join(x for x in (call, place, f"{freq} MHz" if freq else "") if x)
    if srv.get("Server_Name"):
        desc += f"   (server {srv['Server_Name']})"
    say("  " + desc)

    now = time.time()
    if r["ip"]:
        say(f"  registered:  {r['ip']}:{r['port']}")
    if r["proxy"]:
        say(f"  uses an IAX proxy ({r['proxy']}) - a direct IAX check may not work")

    stats = data.get("stats") or {}
    last = stats_last_time(stats)
    r["links"] = count_links(stats.get("data") or {})
    fresh = bool(last) and now - last <= 15 * 60
    if not last:
        say("  stats:       none on record (stats reporting may be turned off on this node)")
    elif fresh:
        say(f"  stats:       reported {int((now - last) // 60)}m ago, {r['links']} node(s) linked")
    else:
        say(f"  stats:       last report {fmt_duration(now - last)} ago (old)")

    rtt = iax2_poke(r["ip"], r["port"]) if r["ip"] else None
    if r["ip"]:
        say(f"  IAX poke:    {'reply in ' + str(rtt) + 'ms' if rtt is not None else 'no reply'}")

    if fresh and rtt is not None:
        r["checks"], r["why"] = ["stats", "iax"], "reports stats and answers IAX, so both"
    elif fresh:
        r["checks"], r["why"] = ["stats"], ("doesn't answer IAX from the internet (normal behind NAT "
                                            "with no port forward), so stats only")
    elif rtt is not None:
        r["checks"], r["why"] = ["iax"], "no current stats, but answers IAX, so IAX only"
    else:
        r["why"] = "looks offline right now (no recent stats and no IAX reply)"
    return r


def probe_echolink(ident, say=print):
    """Find a callsign or node number on the EchoLink login list. Returns (entry, similar)."""
    say("Downloading the EchoLink login list ...")
    lst = get_echolink_list({"cache": {}})
    if not lst["ok"]:
        say("  " + lst["error"])
        return None, []
    key = ident.strip().upper()
    entry = lst["by_node"].get(key) if key.isdigit() else lst["by_call"].get(key)
    similar = []
    if entry is None and not key.isdigit():
        base = key.split("-")[0]
        similar = [e for c, e in sorted(lst["by_call"].items()) if c.split("-")[0] == base]
    if entry:
        say(f"  found: {entry['call']}  node #{entry['node']}  {entry['location']}  status {entry['status']}")
    elif similar:
        say("  not found exactly, but these are logged in: "
            + ", ".join(f"{e['call']} (#{e['node']})" for e in similar))
    else:
        say("  not logged in to EchoLink right now")
    return entry, similar


def probe_cli(what):
    """--probe: print what we find and a ready-to-paste [[target]] block."""
    what = what.strip()
    blocks = []
    if re.fullmatch(r"\d{4,7}", what):
        r = probe_node(what)
        if r["found"]:
            print(f"  -> {r['why']}")
            t = {"type": "allstar", "name": r["suggest_name"], "node": int(what),
                 "checks": r["checks"] or ["stats"]}
            if not r["checks"]:
                t["enabled"] = False
            blocks.append(t)
    elif re.match(r"https?://", what, re.I):
        pg = page_info(what)
        if pg["ok"]:
            print(f"  HTTP {pg['code']}  \"{pg['title'][:60]}\"")
            t = {"type": "http", "name": pg["short"] or pg["title"][:40] or what, "url": what}
            if pg["keyword"]:
                t["keyword"] = pg["keyword"]
            if pg["ignore_cert"]:
                t["ignore_cert"] = True
            blocks.append(t)
        else:
            print(f"  couldn't load it: {pg['error']}")
    else:
        r = probe_host(what)
        if not r.get("error"):
            blocks = [t for sel, _, t in server_options(r, what)[0] if sel]
    if blocks:
        print("\nSuggested config (paste into config.toml, or use: nodewatch setup):\n")
        print("\n".join(render_target(t) for t in blocks))


def server_options(r, name):
    """Turn probe_host results into (options, ...) where options = [[selected, label, target], ...]."""
    host = r["host"]
    opts = []
    if r["ping"]:
        opts.append([True, f"ping ({r['ping_detail']})", {"type": "ping", "name": f"{name} ping", "host": host}])
    have_web = False
    for pg in r["pages"]:
        if not pg["ok"]:
            continue
        label = pg["short"] or ("web" if pg["port"] in (80, 443) else f"web port {pg['port']}")
        t = {"type": "http", "name": f"{name} {label}", "url": pg["url"]}
        if pg["keyword"]:
            t["keyword"] = pg["keyword"]
        if pg["ignore_cert"]:
            t["ignore_cert"] = True
        desc = f"web page {pg['url']}" + (f' must show "{pg["keyword"]}"' if pg["keyword"] else "")
        if pg["note"]:
            desc += f" ({pg['note']})"
        opts.append([True, desc, t])
        have_web = True
    for p in r["ports"]:
        if p in WEB_PORTS and have_web:
            continue
        opts.append([False, f"TCP port {p} ({PORT_NAMES[p]})",
                     {"type": "tcp", "name": f"{name} {PORT_NAMES[p]} port {p}", "host": host, "port": p}])
    if not any(o[0] for o in opts):
        for o in opts:
            if o[2]["type"] == "tcp":
                o[0] = True
                break
    return opts, have_web


# --------------------------------------------------------------------------- config file editing

NEW_CONFIG = """\
# nodewatch config - made by `nodewatch setup`. You can edit it by hand too;
# run `sudo systemctl restart nodewatch` afterwards.
# Keep this file private (chmod 600): it holds your email password.

[settings]
interval_seconds = 120        # how often to check everything
fail_threshold = 2            # failed checks in a row before a DOWN alert
unknown_threshold = 15        # checks before a "can't check" notice (status site down, etc.)
remind_every_hours = 6        # re-alert while something stays down (0 = never)
allstar_checks = ["stats"]    # default for AllStar targets that don't list their own checks
allstar_stale_minutes = 15    # stats older than this = down
echolink_list_stale_minutes = 30

[email]
smtp_host = ""

[ntfy]
url = ""
token = ""


# ---------------------------------------------------------------- targets
# One [[target]] block per thing to watch. "name" is what shows in alerts.
"""

TARGET_LINE = re.compile(r"^\s*\[\[\s*target\s*\]\]\s*(#.*)?$")
TABLE_LINE = re.compile(r"^\s*\[")
KEY_ORDER = ["type", "name", "enabled", "node", "callsign", "host", "port", "url",
             "keyword", "ignore_cert", "checks", "min_links"]


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False).replace("\x7f", "\\u007f")


def render_target(t, comment=None):
    lines = [f"# {c}" for c in (comment or "").splitlines()]
    lines.append("[[target]]")
    for k in sorted(t, key=lambda k: KEY_ORDER.index(k) if k in KEY_ORDER else 99):
        lines.append(f"{k} = {toml_value(t[k])}")
    return "\n".join(lines) + "\n"


class ConfigFile:
    """Edits config.toml as text, so hand-written comments and layout survive."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                self.text = f.read()
            self.exists = True
        except FileNotFoundError:
            self.text, self.exists = NEW_CONFIG, False

    def cfg(self):
        return validate_config(tomllib.loads(self.text), self.path, allow_empty=True)

    def targets(self):
        return self.cfg()["target"]

    def _commit(self, text):
        validate_config(tomllib.loads(text), self.path, allow_empty=True)  # never save a broken file
        folder = os.path.dirname(os.path.abspath(self.path))
        if self.exists:
            st = os.stat(self.path)
            mode, uid, gid = st.st_mode & 0o777, st.st_uid, st.st_gid
        else:
            st = os.stat(folder)
            mode, uid, gid = 0o600, st.st_uid, st.st_gid
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        try:
            os.chown(tmp, uid, gid)
        except PermissionError:
            pass
        os.replace(tmp, self.path)
        self.text, self.exists = text, True

    def _spans(self, lines):
        """[start, header, end] line ranges of each [[target]] block (start includes comments above it)."""
        heads = [i for i, l in enumerate(lines) if TARGET_LINE.match(l)]
        spans = []
        for n, h in enumerate(heads):
            start, floor = h, (heads[n - 1] + 1 if n else 0)
            while start > floor and lines[start - 1].lstrip().startswith("#"):
                start -= 1
            spans.append([start, h, len(lines)])
        for n, sp in enumerate(spans):
            end = spans[n + 1][0] if n + 1 < len(spans) else len(lines)
            for j in range(sp[1] + 1, end):
                if TABLE_LINE.match(lines[j]):
                    end = j
                    break
            sp[2] = end
        return spans

    def _table(self, lines, name):
        pat = re.compile(r"^\s*\[\s*" + re.escape(name) + r"\s*\]\s*(#.*)?$")
        for i, l in enumerate(lines):
            if pat.match(l):
                end = len(lines)
                for j in range(i + 1, len(lines)):
                    if TABLE_LINE.match(lines[j]):
                        end = j
                        break
                # leave blank lines and comments that introduce the next table alone
                while end > i + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
                    end -= 1
                return i, end
        return None

    def add_target(self, t, comment=None):
        text = self.text if self.text.endswith("\n") else self.text + "\n"
        if not text.endswith("\n\n"):
            text += "\n"
        self._commit(text + render_target(t, comment))

    def remove_targets(self, idxs):
        lines = self.text.splitlines(keepends=True)
        spans = self._spans(lines)
        for i in sorted(set(idxs), reverse=True):
            del lines[spans[i][0]:spans[i][2]]
        self._commit("".join(lines))

    def set_enabled(self, idx, on):
        lines = self.text.splitlines(keepends=True)
        _, h, e = self._spans(lines)[idx]
        for j in range(h + 1, e):
            if re.match(r"^\s*enabled\s*=", lines[j]):
                lines[j] = f"enabled = {toml_value(on)}\n"
                break
        else:
            lines.insert(h + 1, f"enabled = {toml_value(on)}\n")
        self._commit("".join(lines))

    def set_table(self, name, items):
        """Replace (or add) a whole [name] table. items: [(key, value), ...]; key "#" = comment line."""
        body = [f"[{name}]\n"] + [f"# {v}\n" if k == "#" else f"{k} = {toml_value(v)}\n" for k, v in items]
        lines = self.text.splitlines(keepends=True)
        rng = self._table(lines, name)
        if rng:
            lines[rng[0]:rng[1]] = body
        else:
            spans = self._spans(lines)
            at = spans[0][0] if spans else len(lines)
            lines[at:at] = body + ["\n"]
        self._commit("".join(lines))

    def set_setting(self, key, value):
        lines = self.text.splitlines(keepends=True)
        rng = self._table(lines, "settings")
        if not rng:
            lines[0:0] = ["[settings]\n", "\n"]
            rng = (0, 1)
        pat = re.compile(r"^(\s*" + re.escape(key) + r"\s*=\s*)([^#\n]*?)(\s*#.*)?$")
        for j in range(rng[0] + 1, rng[1]):
            m = pat.match(lines[j].rstrip("\n"))
            if m:
                pad = (m.group(1) + toml_value(value)).ljust(30)
                lines[j] = (pad + "# " + m.group(3).strip().lstrip("#").strip() if m.group(3)
                            else m.group(1) + toml_value(value)) + "\n"
                break
        else:
            lines.insert(rng[1], f"{key} = {toml_value(value)}\n")
        self._commit("".join(lines))


# --------------------------------------------------------------------------- setup menu

def ask(prompt, default=None):
    d = f" [{default}]" if default not in (None, "") else ""
    ans = input(f"{prompt}{d}: ").strip()
    return ans if ans else ("" if default is None else str(default))


def ask_yes(prompt, default=True):
    while True:
        ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def ask_number(prompt, default, lo=0):
    while True:
        ans = ask(prompt, default)
        try:
            v = float(ans)
            if v >= lo:
                return int(v) if v == int(v) else v
        except ValueError:
            pass
        print(f"  please enter a number{f' of at least {lo}' if lo else ''}")


def choose(prompt, options, default=None):
    """Numbered list. Returns index, or None for Enter (when no default) / 0."""
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        ans = ask(prompt, None if default is None else default + 1)
        if not ans or ans == "0":
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans) - 1


def heading(text):
    print(f"\n=== {text} " + "=" * max(3, 66 - len(text)))


EMAIL_PROVIDERS = [
    ("Gmail (recommended)", {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "security": "starttls"}, """\
Gmail needs an "App Password". Your normal Gmail password will NOT work.
  Tip: make a new Gmail account just for alerts (like yourcall.alerts@gmail.com),
  so your personal account's password is never on this server.
  1. Sign in to that Gmail account and turn on 2-Step Verification:
       https://myaccount.google.com/signinoptions/twosv
  2. Make an app password (call it "nodewatch"):
       https://myaccount.google.com/apppasswords
  3. Google shows 16 letters like  abcd efgh ijkl mnop  - copy them."""),
    ("Yahoo Mail", {"smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "security": "ssl"}, """\
Yahoo needs an app password. Your normal password will NOT work.
  1. Go to https://login.yahoo.com/account/security
  2. Click "Generate app password", call it "nodewatch", and copy it."""),
    ("iCloud Mail", {"smtp_host": "smtp.mail.me.com", "smtp_port": 587, "security": "starttls"}, """\
iCloud needs an app-specific password. Your Apple password will NOT work.
  1. Go to https://account.apple.com -> Sign-In and Security -> App-Specific Passwords
  2. Make one called "nodewatch" and copy it.
  Use your full @icloud.com address as the username."""),
    ("Proton Mail (paid plans only)", {"smtp_host": "smtp.protonmail.ch", "smtp_port": 587,
                                       "security": "starttls"}, """\
Only paid Proton plans can send this way.
  In Proton Mail: Settings -> IMAP/SMTP -> SMTP tokens -> Generate token.
  Use the address the token is for, and the token as the password.
  Free Proton account? Use Gmail to SEND, and put your Proton address in the "to" list."""),
    ("Other / my own mail server", None, """\
You need your mail provider's SMTP settings: server name, port, and whether it uses
STARTTLS (usually port 587) or SSL (usually port 465). Your web host or ISP lists them."""),
]


class Setup:
    MENU = [
        ("Add AllStar nodes or hubs", "add_allstar"),
        ("Add an EchoLink node", "add_echolink"),
        ("Add a server / IP address  (finds ping, ports and web pages for you)", "add_server"),
        ("Add a web page by its address", "add_url"),
        ("See / remove / turn off things being watched", "manage"),
        ("Check everything now", "check_now"),
        ("Email setup and test", "email_setup"),
        ("Phone push alerts (free ntfy app)", "ntfy_setup"),
        ("Timing: how often to check, how fast to alert", "timing"),
        ("Start / restart / stop the service, view the log", "service_menu"),
    ]

    def __init__(self, path):
        self.cf = ConfigFile(path)
        self.svc = os.environ.get("NODEWATCH_SERVICE", "nodewatch")
        self.changed = False
        self.is_root = hasattr(os, "geteuid") and os.geteuid() == 0

    # ---- helpers
    def targets(self):
        return self.cf.targets()

    def save(self, fn, *args, **kw):
        try:
            getattr(self.cf, fn)(*args, **kw)
        except PermissionError:
            print(f"  ! can't write {self.cf.path} - run this with sudo")
            return False
        except (ValueError, tomllib.TOMLDecodeError) as e:
            print(f"  ! not saved: {e}")
            return False
        self.changed = True
        return True

    def unique_name(self, name):
        names = {target_name(t) for t in self.targets()}
        base, n = name, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        return name

    def add(self, t, comment=None):
        t["name"] = self.unique_name(t["name"])
        if self.save("add_target", t, comment):
            print(f"  + added: {t['name']}{'  (turned off for now)' if t.get('enabled') is False else ''}")

    # ---- service
    def systemd(self):
        if not shutil.which("systemctl"):
            return False
        return subprocess.run(["systemctl", "cat", self.svc], capture_output=True).returncode == 0

    def service_state(self):
        if not self.systemd():
            return "not installed as a service"
        p = subprocess.run(["systemctl", "is-active", self.svc], capture_output=True, text=True)
        return {"active": "running", "inactive": "stopped"}.get(p.stdout.strip(), p.stdout.strip())

    def restart(self):
        if not self.systemd():
            print("  The nodewatch service isn't installed (run install.sh), so there's nothing to restart.")
            return
        if not self.is_root:
            print(f"  Run:  sudo systemctl restart {self.svc}")
            return
        cfg = self.cf.cfg()
        if not cfg["target"]:
            print("  Add something to watch first.")
            return
        subprocess.run(["systemctl", "enable", self.svc], capture_output=True)
        subprocess.run(["systemctl", "restart", self.svc])
        time.sleep(3)
        state = self.service_state()
        print(f"  service is {state}")
        if state != "running":
            subprocess.run(["journalctl", "-u", self.svc, "-n", "15", "--no-pager"])
        self.changed = False

    # ---- screens
    def header(self):
        cfg = self.cf.cfg()
        ts = cfg["target"]
        off = sum(1 for t in ts if t.get("enabled", True) is False)
        e = cfg.get("email") or {}
        if e.get("smtp_host"):
            to = e.get("to") or []
            to = to if isinstance(to, list) else [to]
            email = f"from {e.get('username') or e.get('from_addr')} to {len(to)} address(es)"
        else:
            email = "NOT SET UP"
        push = "on" if (cfg.get("ntfy") or {}).get("url") else "off"
        s = cfg["settings"]
        heading(f"nodewatch {VERSION} setup")
        print(f"  config:    {os.path.abspath(self.cf.path)}")
        print(f"  watching:  {len(ts)} thing(s)" + (f", {off} turned off" if off else "")
              + f"  - checked every {fmt_duration(s['interval_seconds']) if s['interval_seconds'] >= 60 else str(s['interval_seconds']) + 's'}")
        print(f"  email:     {email}")
        print(f"  phone:     push alerts {push}")
        print(f"  service:   {self.service_state()}" + ("  (restart to use your changes)" if self.changed else ""))
        print()

    def run(self, first_time=False):
        try:
            if first_time:
                self.wizard()
            while True:
                self.header()
                for i, (label, _) in enumerate(self.MENU, 1):
                    print(f"  {i:2}) {label}")
                print("   0) Quit")
                ans = input("\nChoose a number: ").strip().lower()
                if not ans:
                    continue
                if ans in ("0", "q", "quit", "exit"):
                    break
                if ans.isdigit() and 1 <= int(ans) <= len(self.MENU):
                    try:
                        getattr(self, self.MENU[int(ans) - 1][1])()
                    except KeyboardInterrupt:
                        print("\n  (cancelled)")
        except (KeyboardInterrupt, EOFError):
            print()
        self.finish()

    def finish(self):
        if not self.changed:
            return
        try:
            state = self.service_state()
            if state == "running":
                if ask_yes("\nRestart nodewatch now so your changes take effect?", True):
                    self.restart()
            elif self.systemd() and self.cf.targets():
                if ask_yes("\nStart nodewatch now?", True):
                    self.restart()
        except (KeyboardInterrupt, EOFError):
            print(f"\nRemember to run: sudo systemctl restart {self.svc}")

    def wizard(self):
        heading("Welcome to nodewatch")
        print("""This will walk you through four steps:
  1. Email - where alerts are sent from, and who gets them
  2. Your AllStar nodes and hubs (just type the node numbers)
  3. Anything else: EchoLink nodes, servers, web dashboards
  4. Start watching
You can come back to this menu any time with:  sudo nodewatch""")
        heading("Step 1 of 4: Email")
        self.email_setup()
        heading("Step 2 of 4: AllStar nodes and hubs")
        while True:
            self.add_allstar()
            if not ask_yes("\nAdd more AllStar nodes?", False):
                break
        heading("Step 3 of 4: Anything else?")
        while True:
            i = choose("What else? (Enter when done)", ["EchoLink node", "Server / IP address",
                                                        "Web page by address", "Done"])
            if i is None or i == 3:
                break
            [self.add_echolink, self.add_server, self.add_url][i]()
        heading("Step 4 of 4: Start watching")
        if not self.targets():
            print("Nothing to watch yet - use the menu below to add things.")
            return
        if ask_yes("Run every check once now to see how it looks?", True):
            self.check_now()
        if self.systemd():
            if self.is_root and ask_yes("Start nodewatch now (and every time this computer boots)?", True):
                self.restart()
        else:
            print(f"To run it: python3 {os.path.abspath(sys.argv[0])} -c {os.path.abspath(self.cf.path)}")

    # ---- adding things
    def add_allstar(self):
        print("\nType AllStar node numbers. Several at once is fine (spaces or commas between).")
        print("Don't know them? Search your callsign at https://stats.allstarlink.org")
        nodes = re.findall(r"\d+", ask("Node numbers (Enter to go back)"))
        if not nodes:
            return
        have = {str(t.get("node")) for t in self.targets() if t["type"] in ("allstar", "hub")}
        for i, node in enumerate(nodes):
            print()
            if node in have:
                print(f"{node} is already being watched - skipped.")
                continue
            if i:
                time.sleep(1)    # be polite to AllStarLink
            r = probe_node(node)
            if not r["found"]:
                continue
            t = {"type": "allstar", "name": "", "node": int(node), "checks": r["checks"]}
            comment = None
            if r["checks"]:
                print(f"  -> will check: {' + '.join(r['checks'])}  ({r['why']})")
            else:
                print(f"  -> {r['why']}")
                if not ask_yes("  Add it anyway, turned off until it's back on the air?", False):
                    continue
                t.update(checks=["stats"], enabled=False)
                comment = f"{r['why']} when added on {datetime.now():%Y-%m-%d}. Set enabled = true once it's back."
            t["name"] = ask("  Name to show in alerts", r["suggest_name"])
            if ask_yes("  Is this a hub?", r["links"] >= 3):
                t["type"] = "hub"
                ml = ask("  Alert if fewer than how many nodes are linked? (Enter = don't watch links)", "")
                if ml.isdigit() and int(ml) > 0:
                    t["min_links"] = int(ml)
            self.add(t, comment)
            have.add(node)

    def add_echolink(self):
        ident = ask("\nEchoLink callsign (like W1ABC-R) or node number (Enter to go back)").upper()
        if not ident:
            return
        entry, similar = probe_echolink(ident)
        if entry is None and similar:
            i = choose("Which one? (Enter for none of these)", [f"{e['call']} #{e['node']} {e['location']}"
                                                               for e in similar])
            entry = similar[i] if i is not None else None
        if entry is None:
            if not ask_yes(f"Add {ident} anyway? (it will show DOWN until it logs in)", False):
                return
            t = {"type": "echolink", "name": f"EchoLink {ident}"}
            t["node" if ident.isdigit() else "callsign"] = int(ident) if ident.isdigit() else ident
        else:
            t = {"type": "echolink", "name": f"EchoLink {entry['call']} #{entry['node']}",
                 "callsign": entry["call"], "node": int(entry["node"])}
        t["name"] = ask("  Name to show in alerts", t["name"])
        self.add(t)

    def add_server(self):
        host = ask("\nServer IP address or name, like 203.0.113.10 or xlx.example.com (Enter to go back)")
        if not host:
            return
        if re.match(r"https?://", host, re.I):
            return self.add_url(host)
        r = probe_host(host)
        if r.get("error"):
            return
        print()
        name = ask("Name for this server (used in alerts)", host)
        opts, _ = server_options(r, name)
        if not opts:
            print("Nothing answered (no ping, no common ports).")
            port = ask("If you know a TCP port it uses, type it (Enter to skip)")
            if port.isdigit():
                self.add({"type": "tcp", "name": f"{host} port {port}", "host": host, "port": int(port)})
            return
        print("\nSuggested checks:")
        for t in self.pick(opts, host, name):
            self.add(t)

    def pick(self, opts, host, name):
        while True:
            for i, (sel, label, _) in enumerate(opts, 1):
                print(f"  [{'x' if sel else ' '}] {i}) {label}")
            ans = input("Type a number to switch it on/off, p and a number to add a port (p 5038),\n"
                        "or press Enter to add the [x] ones: ").strip().lower()
            if not ans:
                return [t for sel, _, t in opts if sel]
            m = re.fullmatch(r"p\s*(\d+)", ans)
            if m:
                p = int(m.group(1))
                print(f"  port {p} is {'open' if tcp_open(host, p) else 'NOT answering right now'}")
                opts.append([True, f"TCP port {p}", {"type": "tcp", "name": f"{name} port {p}", "host": host, "port": p}])
                continue
            for n in re.findall(r"\d+", ans):
                if 1 <= int(n) <= len(opts):
                    opts[int(n) - 1][0] = not opts[int(n) - 1][0]

    def add_url(self, url=None):
        url = url or ask("\nWeb address, like http://example.com:81/supermon/ (Enter to go back)")
        if not url:
            return
        if not re.match(r"https?://", url, re.I):
            url = "http://" + url
        print("Loading it ...")
        pg = page_info(url)
        if pg["ok"]:
            print(f"  HTTP {pg['code']}" + (f'  title "{pg["title"][:60]}"' if pg["title"] else "")
                  + (f"  -> looks like {pg['short']}" + (f" ({pg['note']})" if pg["note"] else "") if pg["short"] else ""))
            if pg["ignore_cert"]:
                print("  (it has a self-signed certificate; nodewatch will accept that)")
        else:
            print(f"  couldn't load it: {pg['error']}")
            if not ask_yes("Add it anyway?", False):
                return
        print("Pick some text that is only on the page when it's working (a missing word = DOWN).")
        kw = ask('Text to look for (Enter = suggested, "-" = just check the page loads)', pg["keyword"] or "")
        if kw == "-":
            kw = ""
        if kw and pg["ok"] and kw not in pg["body"]:
            print(f'  "{kw}" is not on the page right now (it\'s case-sensitive) - it would show DOWN.')
            if not ask_yes("  Use it anyway?", False):
                return
        default = pg["short"] or pg["title"][:40] or re.sub(r"^https?://", "", url).split("/")[0]
        t = {"type": "http", "name": ask("Name to show in alerts", default), "url": url}
        if kw:
            t["keyword"] = kw
        if pg["ignore_cert"]:
            t["ignore_cert"] = True
        self.add(t)

    # ---- list / remove
    @staticmethod
    def describe(t):
        if t["type"] in ("allstar", "hub"):
            return f"node {t['node']} ({'+'.join(t.get('checks') or ['default'])})" + (
                f", min {t['min_links']} links" if t.get("min_links") else "")
        if t["type"] == "echolink":
            return " ".join(str(x) for x in (t.get("callsign"), t.get("node") and f"#{t['node']}") if x)
        if t["type"] == "tcp":
            return f"{t['host']} port {t['port']}"
        if t["type"] == "http":
            return t["url"] + (f'  "{t["keyword"]}"' if t.get("keyword") else "")
        return t.get("host", "")

    def manage(self):
        while True:
            ts = self.targets()
            print()
            if not ts:
                print("Nothing is being watched yet.")
                return
            for i, t in enumerate(ts, 1):
                on = "OFF" if t.get("enabled", True) is False else "on "
                print(f" {i:3}  {on} {t['type']:8} {target_name(t)[:38]:38}  {self.describe(t)[:60]}")
            print("\n  t 3 = test #3    o 3 = turn #3 off / back on    r 3 = remove #3    (several: r 3 5 7)")
            ans = input("  What to do? (Enter to go back): ").strip().lower()
            if not ans:
                return
            m = re.fullmatch(r"([tor])\s*([\d ,]+)", ans)
            nums = [int(n) - 1 for n in re.findall(r"\d+", m.group(2))] if m else []
            nums = [n for n in nums if 0 <= n < len(ts)]
            if not nums:
                print("  ? try something like:  t 3")
                continue
            act = m.group(1)
            if act == "t":
                ctx = {"defaults": self.cf.cfg()["settings"], "cache": {}}
                for n in nums:
                    status, detail = CHECKS[ts[n]["type"]](ts[n], ctx)
                    print(f"  {status:8} {target_name(ts[n])}: {detail}")
                input("  (Enter to continue)")
            elif act == "o":
                for n in nums:
                    on = ts[n].get("enabled", True) is False
                    if self.save("set_enabled", n, on):
                        print(f"  {target_name(ts[n])} turned {'on' if on else 'off'}")
            elif ask_yes("  Remove " + ", ".join(target_name(ts[n]) for n in nums) + "?", False):
                if self.save("remove_targets", nums):
                    print("  removed")

    def check_now(self):
        cfg = self.cf.cfg()
        if not cfg["target"]:
            print("\nNothing to check yet.")
            return
        print("\nChecking everything (AllStar lookups take a moment each) ...")
        res = run_checks(cfg, lambda n, s, d: print(f"  {s:8} {n[:40]:40} {d}", flush=True))
        bad = [r for r in res if r[1] != UP]
        print(f"\n{len(res) - len(bad)} of {len(res)} OK." + (" Things marked DOWN for 2 checks in a row get"
                                                             " an alert once the service is running." if bad else ""))

    # ---- email / push / timing
    def email_setup(self):
        e = dict(self.cf.cfg().get("email") or {})
        if e.get("smtp_host"):
            print(f"Now sending from {e.get('username')} via {e['smtp_host']} to {', '.join(e.get('to') or [])}")
            i = choose("What do you want to do? (Enter to go back)",
                       ["Send a test email", "Change who gets alerts", "Set up a different sending account"])
            if i is None:
                return
            if i == 0:
                return self.test_email()
            if i == 1:
                return self.email_recipients(e)
        print("""
nodewatch sends alerts from an email account you already have (or a new free one).
It needs that account's address and an "app password" to sign in and send.""")
        i = choose("Which kind of account will SEND the alerts? (Enter to skip email for now)",
                   [p[0] for p in EMAIL_PROVIDERS])
        if i is None:
            return
        name, server, help_text = EMAIL_PROVIDERS[i]
        print("\n" + help_text + "\n")
        new = {}
        if server:
            new.update(server)
        else:
            new["smtp_host"] = ask("SMTP server name, like mail.example.com", e.get("smtp_host"))
            sec = choose("Security", ["STARTTLS (usually port 587)", "SSL/TLS (usually port 465)",
                                      "none (not recommended)"], 0)
            new["security"] = ["starttls", "ssl", "none"][sec or 0]
            new["smtp_port"] = ask_number("Port", {"starttls": 587, "ssl": 465, "none": 25}[new["security"]], 1)
        while True:
            new["username"] = ask("Sending email address", e.get("username") if e.get("smtp_host") == new["smtp_host"] else None)
            if "@" in new["username"] or not server:
                break
            print("  please type the full email address")
        pw = getpass.getpass("App password (typing is hidden; Enter keeps the old one): ").strip()
        if not pw and e.get("password") and e.get("username") == new["username"]:
            pw = e["password"]
        if server and i in (0, 1, 2):
            pw = pw.replace(" ", "")          # app passwords are shown with spaces
        if not pw:
            print("  No password given - email not saved.")
            return
        new["password"] = pw
        new["from_addr"] = f"nodewatch <{new['username']}>" if "@" in new["username"] else new["username"]
        new["to"] = e.get("to") or ([new["username"]] if "@" in new["username"] else [])
        new["subject_prefix"] = e.get("subject_prefix", "[nodewatch]")
        self.email_recipients(new, save_only_if_changed=False)

    def email_recipients(self, e, save_only_if_changed=True):
        cur = e.get("to") or []
        cur = cur if isinstance(cur, list) else [cur]
        print("\nWho should get the alerts? You can list several, and phone text gateways work too")
        print("(e.g. 5551234567@vtext.com for Verizon).")
        while True:
            raw = ask("Email addresses, separated by commas", ", ".join(cur))
            to = [a.strip() for a in re.split(r"[,\s;]+", raw) if a.strip()]
            bad = [a for a in to if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a)]
            if to and not bad:
                break
            print(f"  that doesn't look right: {', '.join(bad) or 'need at least one address'}")
        if save_only_if_changed and to == cur:
            return
        e["to"] = to
        keys = ["smtp_host", "smtp_port", "security", "username", "password", "from_addr", "to", "subject_prefix"]
        items = [(k, e[k]) for k in keys if k in e] + [(k, v) for k, v in e.items() if k not in keys]
        if self.save("set_table", "email", items):
            print("  email settings saved")
            if ask_yes("Send a test email now?", True):
                self.test_email()

    def test_email(self):
        cfg = self.cf.cfg()
        e = cfg.get("email") or {}
        print(f"Sending a test to {', '.join(e.get('to') or [])} ...")
        try:
            send_email(cfg, f"{e.get('subject_prefix', '[nodewatch]')} TEST - email alerts work",
                       "If you got this, nodewatch can email you.\n\n"
                       f"-- nodewatch on {socket.gethostname()} at {fmt_time(time.time())}")
        except smtplib.SMTPAuthenticationError:
            print("  ! The mail server said the username or password is wrong.\n"
                  "    For Gmail / Yahoo / iCloud it must be an APP password, not your normal password.")
        except (socket.gaierror, socket.timeout, ConnectionError, OSError) as ex:
            print(f"  ! Couldn't talk to the mail server ({ex}).\n"
                  "    Check the server name and port, and that this computer is online.")
        except Exception as ex:
            print(f"  ! Sending failed: {ex}")
        else:
            print("  Sent! Check the inbox (and the spam folder - mark it 'not spam' so alerts get through).")

    def ntfy_setup(self):
        n = self.cf.cfg().get("ntfy") or {}
        print("""
Free phone push alerts with the ntfy app (no account needed):
  1. Install "ntfy" from the Google Play Store, F-Droid or the Apple App Store.
  2. In the app tap +, and subscribe to the topic name below.
  Anyone who knows the topic name can see your alerts, so keep it long and random.""")
        if n.get("url"):
            print(f"\nNow sending to: {n['url']}")
            i = choose("What do you want to do? (Enter to go back)",
                       ["Send a test push", "Use a different topic", "Turn phone push off"])
            if i is None:
                return
            if i == 0:
                return self.test_ntfy()
            if i == 2:
                if self.save("set_table", "ntfy", [("url", ""), ("token", "")]):
                    print("  phone push turned off")
                return
        topic = "nodewatch-" + secrets.token_hex(6)
        ans = ask("\nTopic name (Enter to use this random one, or type your own)", topic)
        url = ans if ans.startswith("http") else "https://ntfy.sh/" + re.sub(r"[^A-Za-z0-9_-]", "", ans)
        print(f"\n  Subscribe to this in the app:  {url.rsplit('/', 1)[-1]}")
        if self.save("set_table", "ntfy", [("url", url), ("token", n.get("token", ""))]):
            input("  Press Enter once you've subscribed, and a test will be sent ...")
            self.test_ntfy()

    def test_ntfy(self):
        try:
            send_ntfy(self.cf.cfg(), "nodewatch test", "Phone push alerts are working.", False)
            print("  Sent - it should pop up on your phone in a few seconds.")
        except Exception as ex:
            print(f"  ! Sending failed: {ex}")

    def timing(self):
        s = self.cf.cfg()["settings"]
        print("\nPress Enter to keep a value.")
        new = {
            "interval_seconds": ask_number("Seconds between checks (120 = every 2 min; keep 60+ to be kind "
                                           "to AllStarLink)", s["interval_seconds"], 30),
            "fail_threshold": ask_number("Failed checks in a row before an alert", s["fail_threshold"], 1),
            "remind_every_hours": ask_number("Re-send an alert every how many hours while still down "
                                             "(0 = never)", s["remind_every_hours"]),
        }
        mins = new["interval_seconds"] * new["fail_threshold"] / 60
        for k, v in new.items():
            if v != s[k] and not self.save("set_setting", k, v):
                return
        print(f"  You'll hear about an outage after about {mins:g} minutes.")

    def service_menu(self):
        print(f"\nThe service is {self.service_state()}.")
        i = choose("What do you want to do? (Enter to go back)",
                   ["Start / restart (do this after changes)", "Stop", "Show the last 40 log lines",
                    "Follow the live log (Ctrl-C to stop)"])
        if i is None:
            return
        if not self.systemd():
            print("  The service isn't installed - run install.sh first.")
            return
        if i == 0:
            self.restart()
        elif i == 1:
            subprocess.run(["systemctl", "stop", self.svc])
            print(f"  stopped (it will start again at boot; to stop that too: sudo systemctl disable {self.svc})")
        elif i == 2:
            subprocess.run(["journalctl", "-u", self.svc, "-n", "40", "--no-pager"])
        else:
            try:
                subprocess.run(["journalctl", "-u", self.svc, "-f", "-n", "20"])
            except KeyboardInterrupt:
                pass


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="AllStar / EchoLink / server monitor with email alerts")
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("--setup", "--menu", action="store_true",
                    help="menu to add nodes/servers and set up email (start here)")
    ap.add_argument("--probe", metavar="WHAT",
                    help="look up an AllStar node number, IP/host name or URL and suggest checks")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit (for cron)")
    ap.add_argument("--check", action="store_true", help="print current status of every target, send nothing")
    ap.add_argument("--test-email", action="store_true", help="send a test notification and exit")
    ap.add_argument("--version", action="version", version=f"nodewatch {VERSION}")
    args = ap.parse_args()

    if args.probe:
        probe_cli(args.probe)
        return

    if args.setup:
        first = not os.path.exists(args.config)
        try:
            Setup(args.config).run(first_time=first)
        except (ValueError, tomllib.TOMLDecodeError) as e:
            sys.exit(f"Problem in {args.config}: {e}\nFix it by hand (nano {args.config}) and try again.")
        return

    cfg = load_config(args.config)

    if args.test_email:
        notify(cfg, [{"name": "TEST - nodewatch is set up", "kind": "up",
                      "detail": "If you got this, email/ntfy alerts are working.", "since": time.time()}])
        return

    if args.check:
        for name, status, detail in run_checks(cfg):
            print(f"{status:8} {name:30} {detail}")
        return

    state = load_state(cfg["settings"]["state_file"])
    if args.once:
        cycle(cfg, state)
        return

    stop = {"flag": False}

    def handle(sig, _frm):
        stop["flag"] = True
        log("stopping")
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    interval = int(cfg["settings"]["interval_seconds"])
    log(f"nodewatch {VERSION} started, {len(cfg['target'])} targets, every {interval}s")
    while not stop["flag"]:
        started = time.monotonic()
        try:
            cycle(cfg, state)
        except Exception as ex:
            log(f"ERROR in cycle: {ex.__class__.__name__}: {ex}")
        while not stop["flag"] and time.monotonic() - started < interval:
            time.sleep(1)


if __name__ == "__main__":
    main()
