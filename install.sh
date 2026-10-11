#!/bin/bash
# nodewatch installer.  From the unpacked nodewatch folder run:   sudo ./install.sh
# Running it again later upgrades the program and keeps your config.
#
# Options:  --dir /some/folder   install somewhere other than /opt/nodewatch
#           --no-setup           don't open the setup menu at the end
set -euo pipefail

DIR=/opt/nodewatch
SETUP=1
while [ $# -gt 0 ]; do
    case "$1" in
        --dir)      DIR="${2:?--dir needs a folder}"; shift 2 ;;
        --no-setup) SETUP=0; shift ;;
        -h|--help)  sed -n '2,8p' "$0"; exit 0 ;;
        *)          echo "unknown option: $1 (try --help)"; exit 1 ;;
    esac
done
DIR="${DIR%/}"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:   sudo ./install.sh"; exit 1
fi
step() { printf '\n== %s\n' "$*"; }

pkg_install() {
    if command -v apt-get >/dev/null; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null; then dnf install -y -q "$@"
    elif command -v pacman >/dev/null; then pacman -Sy --noconfirm --needed "$@"
    else echo "Please install these yourself, then run install.sh again: $*"; exit 1
    fi
}

step "Checking what's needed"
if ! command -v python3 >/dev/null; then
    echo "Installing Python 3 ..."; pkg_install python3
fi
PY="$(command -v python3)"
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
    echo "nodewatch needs Python 3.8 or newer; this system has $("$PY" -V 2>&1)."; exit 1
fi
if ! "$PY" -c 'import tomllib' 2>/dev/null && ! "$PY" -c 'import tomli' 2>/dev/null; then
    echo "Installing the TOML reader for older Python ..."
    if command -v apt-get >/dev/null; then pkg_install python3-tomli
    else "$PY" -m pip install tomli || pkg_install python-tomli
    fi
fi
if ! command -v ping >/dev/null; then
    echo "Installing ping ..."
    if command -v apt-get >/dev/null; then pkg_install iputils-ping; else pkg_install iputils; fi
fi
echo "Python: $("$PY" -V 2>&1)   ping: ok"

step "Installing to $DIR"
UPGRADE=0
[ -f "$DIR/config.toml" ] && UPGRADE=1
if ! id nodewatch >/dev/null 2>&1; then
    NOLOGIN="$(command -v nologin || echo /bin/false)"
    useradd -r -M -s "$NOLOGIN" -d "$DIR" nodewatch
    echo "created system user 'nodewatch' (can't log in; the monitor runs as this user)"
fi
mkdir -p "$DIR"
if [ "$SRC" != "$DIR" ]; then
    install -m 644 "$SRC/nodewatch.py" "$SRC/config.example.toml" "$SRC/README.md" "$DIR/"
fi
chown -R nodewatch:nodewatch "$DIR"
[ -f "$DIR/config.toml" ] && chmod 600 "$DIR/config.toml"

sed -e "s#@DIR@#$DIR#g" -e "s#@PYTHON@#$PY#g" "$SRC/nodewatch-cmd" > /usr/local/bin/nodewatch
chmod 755 /usr/local/bin/nodewatch
echo "added the 'nodewatch' command"

if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
    sed -e "s#@DIR@#$DIR#g" -e "s#@PYTHON@#$PY#g" "$SRC/nodewatch.service" \
        | grep -v '^# Template\|^# /etc/systemd' > /etc/systemd/system/nodewatch.service
    systemctl daemon-reload
    echo "installed the nodewatch service (it starts at boot once you've set it up)"
    if [ "$UPGRADE" = 1 ]; then
        systemctl enable nodewatch >/dev/null 2>&1
        systemctl restart nodewatch
        echo "upgraded and restarted - your config was kept"
    fi
else
    echo "No systemd here, so no service was installed. To check every 2 minutes from cron instead, add:"
    echo "  */2 * * * * nodewatch $PY $DIR/nodewatch.py -c $DIR/config.toml --once"
    echo "to /etc/crontab (set interval_seconds to match)."
fi

step "Done"
if [ "$UPGRADE" = 1 ]; then
    echo "nodewatch upgraded to $("$PY" "$DIR/nodewatch.py" --version). Open the menu any time with:  sudo nodewatch"
    exit 0
fi
echo "Next: the setup menu will ask for your email account and your node numbers."
echo "You can always come back to it with:   sudo nodewatch"
if [ "$SETUP" = 1 ] && [ -t 0 ]; then
    read -r -p "Press Enter to start setup (or Ctrl-C to do it later) ... " _
    exec /usr/local/bin/nodewatch setup
fi
