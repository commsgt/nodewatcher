#!/bin/bash
# Removes the nodewatch service and command.  Run:  sudo ./uninstall.sh
set -u
if [ "$(id -u)" -ne 0 ]; then echo "Please run with sudo:   sudo ./uninstall.sh"; exit 1; fi

DIR="$(sed -n 's/^DIR="\(.*\)"$/\1/p' /usr/local/bin/nodewatch 2>/dev/null)"
DIR="${DIR:-/opt/nodewatch}"

if command -v systemctl >/dev/null; then
    systemctl disable --now nodewatch 2>/dev/null
    rm -f /etc/systemd/system/nodewatch.service
    systemctl daemon-reload
fi
rm -f /usr/local/bin/nodewatch
echo "Service and 'nodewatch' command removed."

if [ -d "$DIR" ]; then
    read -r -p "Also delete $DIR, including your config (email password, node list)? [y/N] " a
    if [ "${a,,}" = y ] || [ "${a,,}" = yes ]; then
        rm -rf "$DIR"
        userdel nodewatch 2>/dev/null
        echo "Deleted $DIR and the nodewatch user."
    else
        echo "Kept $DIR."
    fi
fi
