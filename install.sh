#!/bin/sh
# Installs sdmult + its GUI as a background service on a Raspberry Pi
# (Raspberry Pi OS or Ubuntu). Run from the unpacked folder:
#     sudo ./install.sh            install / update
#     sudo ./install.sh --new-key  install / update and replace the access key
#     sudo ./install.sh --remove   uninstall (saved images are kept)
set -eu
PORT="${SDMULT_PORT:-8765}"
DEST=/opt/sdmult
IMAGES=/var/lib/sdmult
CONF=/etc/sdmult
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo $0 $*"; exit 1; fi
USER_NAME="${SUDO_USER:-}"
USER_HOME=""
[ -n "$USER_NAME" ] && USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now sdmult-gui.service 2>/dev/null || true
    rm -f /etc/systemd/system/sdmult-gui.service /usr/local/bin/sdmult /usr/local/bin/sdmult-gui
    rm -rf "$DEST" "$CONF"
    [ -n "$USER_HOME" ] && rm -f "$USER_HOME/.local/share/applications/sdmult.desktop" "$USER_HOME/Desktop/sdmult.desktop"
    systemctl daemon-reload
    echo "Removed. Saved images are still in $IMAGES."
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 is missing"; exit 1; }
command -v lsblk >/dev/null || { echo "lsblk (util-linux) is missing"; exit 1; }

install -d -m 755 "$DEST" "$IMAGES"
install -d -m 700 "$CONF"
for f in sdmult.py sdmult_gui.py sdmult_gui.html README.md; do install -m 644 "$SRC/$f" "$DEST/$f"; done
chmod 755 "$DEST/sdmult.py" "$DEST/sdmult_gui.py"
ln -sf "$DEST/sdmult.py" /usr/local/bin/sdmult
ln -sf "$DEST/sdmult_gui.py" /usr/local/bin/sdmult-gui

if [ ! -f "$CONF/gui.env" ] || [ "${1:-}" = "--new-key" ]; then
    KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
    printf 'SDMULT_KEY=%s\nSDMULT_PORT=%s\n' "$KEY" "$PORT" > "$CONF/gui.env"
    chmod 600 "$CONF/gui.env"
fi
. "$CONF/gui.env"

cat > /etc/systemd/system/sdmult-gui.service <<EOF
[Unit]
Description=SD card duplicator GUI (localhost:${SDMULT_PORT})
After=local-fs.target

[Service]
EnvironmentFile=$CONF/gui.env
# the key comes from SDMULT_KEY in the EnvironmentFile, so it never shows in `ps`
ExecStart=/usr/bin/python3 $DEST/sdmult_gui.py --host 127.0.0.1 --port \${SDMULT_PORT} --images $IMAGES
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable sdmult-gui.service >/dev/null
systemctl restart sdmult-gui.service

URL="http://localhost:${SDMULT_PORT}/#${SDMULT_KEY}"
if [ -n "$USER_HOME" ]; then
    APPS="$USER_HOME/.local/share/applications"
    install -d -o "$USER_NAME" "$APPS"
    cat > "$APPS/sdmult.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SD Duplicator
Comment=Copy one SD card to many
Exec=xdg-open $URL
Icon=media-flash
Terminal=false
Categories=Utility;System;
EOF
    chown "$USER_NAME" "$APPS/sdmult.desktop"
    chmod 700 "$APPS/sdmult.desktop"          # holds the key: owner only
    if [ -d "$USER_HOME/Desktop" ]; then
        install -m 700 -o "$USER_NAME" "$APPS/sdmult.desktop" "$USER_HOME/Desktop/sdmult.desktop"
    fi
fi

sleep 1
if systemctl is-active --quiet sdmult-gui.service; then
    echo "Installed. GUI running at: $URL"
    echo "The part after # is the access key. Keep it private; don't paste it into chats."
    echo "Look for 'SD Duplicator' in the app menu. Command line: sudo sdmult list"
else
    echo "Installed, but the GUI service did not start. See: journalctl -u sdmult-gui -n 30"
    exit 1
fi
