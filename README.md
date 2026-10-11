# nodewatch

Watches AllStar nodes, AllStar hubs, EchoLink nodes and ordinary servers every
2 minutes. Emails you (and optionally pushes to your phone via ntfy) when
something goes down, reminds you if it stays down, and tells you when it's back.

Single Python file, standard library only. Needs Python 3.11+ (Ubuntu 24.04 /
Debian 12 have it). On older Python: `pip install tomli`.

## How each kind of thing is checked

| type | how | catches |
|---|---|---|
| `allstar` | stats.allstarlink.org: node must have reported stats in the last 15 min | Asterisk down, node offline, internet out at the node site |
| `allstar` + `checks = ["stats","iax"]` | also sends an IAX2 POKE straight to the node on UDP 4569 | node not reachable for incoming connects (port forward broken, firewall) |
| `hub` | same as allstar, plus optional `min_links` | hub up but nodes dropped off it |
| `echolink` | finds the callsign/node on echolink.org's live login list | node logged out of EchoLink, or showing OFF |
| `http` | fetches a URL, optional keyword must appear | XLX dashboard, websites |
| `tcp` | opens a TCP port | SSH, web, any TCP service |
| `ping` | ICMP ping | basic box-is-alive |

Notes:
- **IAX poke** only works if the node accepts IAX from the internet (normal for
  nodes that take incoming connects). If a node is behind NAT with no port forward,
  use `stats` only.
- If the AllStarLink or EchoLink status sites are down, you get a "can't check"
  notice after about 30 minutes, not a false DOWN alert.
- A thing must fail **2 checks in a row** (4 minutes) before you're alerted. Change
  `fail_threshold` to adjust.

## Install (Ubuntu/Debian, run as root or with sudo)

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/nodewatch nodewatch
sudo mkdir -p /opt/nodewatch
sudo cp nodewatch.py config.example.toml /opt/nodewatch/
sudo cp /opt/nodewatch/config.example.toml /opt/nodewatch/config.toml
sudo nano /opt/nodewatch/config.toml          # add your email + nodes
sudo chown -R nodewatch:nodewatch /opt/nodewatch
sudo chmod 600 /opt/nodewatch/config.toml
```

Test it:

```bash
cd /opt/nodewatch
sudo -u nodewatch python3 nodewatch.py -c config.toml --check        # shows status of everything
sudo -u nodewatch python3 nodewatch.py -c config.toml --test-email   # sends a test alert
```

Run it as a service:

```bash
sudo cp nodewatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nodewatch
journalctl -u nodewatch -f        # watch the log
```

After editing config.toml: `sudo systemctl restart nodewatch`.

## Email setup

- **Gmail:** turn on 2-step verification, then create an App Password
  (Google Account → Security → App passwords). Use that 16-character password,
  `smtp.gmail.com`, port 587, `starttls`.
- **Proton Mail:** regular Proton accounts don't offer SMTP. Paid plans can use an
  SMTP submission token (`smtp.protonmail.ch`, port 587). Otherwise send from a
  Gmail or other SMTP account *to* your Proton address.
- **Your web host's mail server** works too, using whatever SMTP settings your
  host gives you.

## Phone push notifications (optional, free)

Install the **ntfy** app, subscribe to a long random topic name such as
`radio-nodes-x7k2p9q`, and set `url = "https://ntfy.sh/radio-nodes-x7k2p9q"` in
the `[ntfy]` section. Down alerts arrive as high-priority pushes.

## Watch the watcher

If the server running nodewatch dies, nothing alerts you. Add one free external
check (UptimeRobot, or Healthchecks.io) pointed at that server.
