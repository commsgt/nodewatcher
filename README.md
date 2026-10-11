# nodewatch

Keeps an eye on your **AllStar nodes and hubs**, **EchoLink nodes**, and **servers**
(XLX reflectors, Supermon/Allmon dashboards, web sites, anything with an IP address).
Checks every 2 minutes and **emails you** (and optionally pushes to your phone) when
something goes down, reminds you if it stays down, and tells you when it's back.

You don't need to know IP addresses or ports: type your node numbers and the setup
menu looks everything up and tests it for you.

## Install (about 5 minutes)

On a Linux computer that stays on: a Raspberry Pi, a VPS, a home server, or the node
itself (Debian, Ubuntu, Raspberry Pi OS, ASL3, Fedora or Arch).

```bash
tar xzf nodewatch-1.1.tar.gz
cd nodewatch
sudo ./install.sh
```

The installer sets everything up and opens the setup menu, which walks you through:

1. **Email** - which account sends the alerts and who gets them (step-by-step
   instructions for Gmail, Yahoo, iCloud, Proton and others, then a test email)
2. **AllStar nodes and hubs** - just type the node numbers, e.g. `12345 23456 34567`.
   For each one it looks up the node on AllStarLink, tests it, and picks the right checks.
3. **Anything else** - EchoLink callsigns, servers by IP address, web dashboards.
   For a server it pings it, finds open ports and recognises web dashboards
   (XLX, Supermon, Allmon, AllScan, AllTune, Pi-Star, WPSD ...) and suggests checks.
4. **Start watching** - runs all checks once, then starts the service, which also starts at boot.

## Everyday use

```bash
sudo nodewatch                  # setup menu: add / remove things, email, phone push, timing
sudo nodewatch status           # check everything right now
sudo nodewatch probe 12345      # look up anything without adding it (node, IP or web address)
sudo nodewatch test-email       # send a test alert
sudo nodewatch log              # watch the live log
sudo nodewatch restart          # after editing the config by hand
```

The menu:

```
   1) Add AllStar nodes or hubs
   2) Add an EchoLink node
   3) Add a server / IP address  (finds ping, ports and web pages for you)
   4) Add a web page by its address
   5) See / remove / turn off things being watched
   6) Check everything now
   7) Email setup and test
   8) Phone push alerts (free ntfy app)
   9) Timing: how often to check, how fast to alert
  10) Start / restart / stop the service, view the log
```

## What the alerts look like

```
Subject: [nodewatch] DOWN: 12345 W1ABC Springfield 146.940

DOWN: 12345 W1ABC Springfield 146.940
  last stats report 22m ago; no IAX2 reply from 203.0.113.25:4569
  since 2026-10-11 14:02:10
```

Then `STILL DOWN` every 6 hours while it stays down, and `BACK UP` when it recovers.
Something has to fail **2 checks in a row** (4 minutes) before you're alerted, so a
single hiccup doesn't wake you up. Change this under menu option 9.

## How things are checked

| kind | how | catches |
|---|---|---|
| AllStar node / hub, **stats** | the node must have reported to stats.allstarlink.org in the last 15 min | Asterisk down, node offline, internet out at the site |
| AllStar node / hub, **iax** | sends an IAX2 "poke" straight to the node's registered IP and port | node not reachable for connections (port forward broken, firewall) |
| hub with **min_links** | counts the nodes linked to the hub | hub up but its nodes dropped off |
| EchoLink | finds the callsign / node on echolink.org's live login list | node logged out of EchoLink, or showing OFF |
| web page | loads a URL and looks for a piece of text | XLX dashboard (xlxd stopped), Supermon, web sites |
| TCP port | connects to a port | SSH, any TCP service |
| ping | ICMP ping | box is alive |

The setup menu picks stats, IAX or both for each node, depending on what that node
answers to. Nodes behind NAT with no port forward can't be poked, so they get stats only.

If AllStarLink's or EchoLink's status sites are down, you get a "CAN'T CHECK" notice
after about 30 minutes instead of false DOWN alerts.

## Email tips

- **Make a separate free Gmail account just for sending alerts**, so your personal
  password is never stored on the server. The menu shows exactly how to get the app password it needs.
- You can send to several people, and to phone text gateways such as `5551234567@vtext.com`.
- The first test email may land in spam. Mark it "not spam".
- Phone push (menu option 8) uses the free **ntfy** app and needs no account.

## Good to know

- **Watch the watcher:** if the computer running nodewatch goes down, nothing
  alerts you. Add a free external check (UptimeRobot or Healthchecks.io) for it,
  or run nodewatch somewhere other than your nodes.
- The config is `/opt/nodewatch/config.toml`. It's plain text with comments, so you
  can edit it by hand (`sudo nodewatch edit`). `config.example.toml` shows every option.
  It holds your email password, so it's readable only by root and the nodewatch user.
- Please keep the check interval at 60 seconds or more, to be kind to the free
  AllStarLink and EchoLink status sites.
- **Upgrading:** unpack the new version and run `sudo ./install.sh` again. Your config is kept.
- **Uninstalling:** `sudo ./uninstall.sh`.
- No systemd? The installer prints a cron line to use instead.
- Needs Python 3.8+ (3.11+ preferred) and nothing else. It uses only Python's standard library.
