#!/usr/bin/env python3
"""
nodewatch - watch AllStar nodes/hubs, EchoLink nodes and general servers,
and email (and optionally push-notify) when one goes down or comes back.

Standard library only. Python 3.11+ (or 3.8+ with `pip install tomli`).

Usage:
  python3 nodewatch.py -c config.toml              run forever (use with systemd)
  python3 nodewatch.py -c config.toml --once       one check cycle (use with cron)
  python3 nodewatch.py -c config.toml --check      print status of everything, send nothing
  python3 nodewatch.py -c config.toml --test-email send a test message and exit
"""

import argparse
import html
import json
import os
import random
import re
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
from datetime import datetime, timezone
from email.message import EmailMessage

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        sys.exit("nodewatch needs Python 3.11+, or run: pip install tomli")

VERSION = "1.0"
USER_AGENT = f"nodewatch/{VERSION} (amateur radio node monitor)"
ALLSTAR_API = "https://stats.allstarlink.org/api/stats/{node}"
ECHOLINK_LOGINS = "https://www.echolink.org/logins.jsp"

UP, DOWN, UNKNOWN = "UP", "DOWN", "UNKNOWN"


# --------------------------------------------------------------------------- utils

def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
            sdata = stats.get("data") or {}
            last = 0
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
            linked = sdata.get("linkedNodes")
            if linked is None:
                linked = sdata.get("links") or []
            n_links = len(linked) if isinstance(linked, list) else 0

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
        code, body = http_get(url, timeout=float(t.get("timeout", 15)))
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


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    s = cfg.setdefault("settings", {})
    s.setdefault("interval_seconds", 120)
    s.setdefault("fail_threshold", 2)
    s.setdefault("unknown_threshold", 15)
    s.setdefault("remind_every_hours", 6)
    s.setdefault("allstar_checks", ["stats"])
    s.setdefault("allstar_stale_minutes", 15)
    s.setdefault("echolink_list_stale_minutes", 30)
    s.setdefault("state_file", os.path.join(os.path.dirname(os.path.abspath(path)), "nodewatch_state.json"))
    targets = cfg.get("target") or []
    if not targets:
        sys.exit("No [[target]] entries in config")
    names = set()
    for t in targets:
        if t.get("type") not in CHECKS:
            sys.exit(f"Unknown target type {t.get('type')!r} (use one of {', '.join(CHECKS)})")
        n = target_name(t)
        if n in names:
            sys.exit(f"Duplicate target name {n!r}; give each one a unique name")
        names.add(n)
    return cfg


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


def run_checks(cfg):
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


def main():
    ap = argparse.ArgumentParser(description="AllStar / EchoLink / server monitor with email alerts")
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit (for cron)")
    ap.add_argument("--check", action="store_true", help="print current status of every target, send nothing")
    ap.add_argument("--test-email", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

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
