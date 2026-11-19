#!/bin/sh
# Printer Center installer — run as root.
#   sudo sh install.sh --user USER --printer PRINTER_IP [--name PANEL_NAME] [--port PORT]
#                      [--repo OWNER/REPO]
#   sudo sh install.sh --files                      refresh program files only (used by the updater)
#   sudo sh install.sh --user USER --printer PRINTER_IP --uninstall
set -eu

USER_NAME= ; PRINTER= ; PANEL_NAME= ; PORT=53580 ; UNINSTALL= ; FILES_ONLY=
DEFAULT_REPO=mustafa-ozen95/printer-center ; REPO= ; REPO_SET=
while [ $# -gt 0 ]; do
	case "$1" in
		-u|--user) USER_NAME=$2; shift 2 ;;
		-p|--printer) PRINTER=$2; shift 2 ;;
		-n|--name) PANEL_NAME=$2; shift 2 ;;
		--port) PORT=$2; shift 2 ;;
		--repo) REPO=$2; REPO_SET=1; shift 2 ;;
		--files) FILES_ONLY=1; shift ;;
		--uninstall) UNINSTALL=1; shift ;;
		*) echo "Unknown option: $1" >&2; exit 2 ;;
	esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
SRC_DIR=$(cd "$(dirname "$0")" && pwd)
APP=/opt/printer-center
CONF=/etc/printer-center

# For every logged-in user: reload user units and (re)start the service if it is running
for_user_sessions() {
	for run in /run/user/*; do
		[ -S "$run/bus" ] && [ "$(basename "$run")" -ge 1000 ] || continue
		u=$(id -nu "$(basename "$run")" 2>/dev/null) || continue
		runuser -u "$u" -- env XDG_RUNTIME_DIR="$run" systemctl --user daemon-reload || true
		runuser -u "$u" -- env XDG_RUNTIME_DIR="$run" systemctl --user "$1" printer-center.service || true
	done
}

# Remove the previous "wsd-tarama" installation (renamed to printer-center in 2.0)
migrate_legacy() {
	[ -d /opt/wsd-tarama ] || [ -f /etc/systemd/user/wsd-tarama.service ] || return 0
	for run in /run/user/*; do
		[ -S "$run/bus" ] || continue
		u=$(id -nu "$(basename "$run")" 2>/dev/null) || continue
		runuser -u "$u" -- env XDG_RUNTIME_DIR="$run" systemctl --user stop wsd-tarama.service 2>/dev/null || true
		pkill -u "$u" -f "/opt/wsd-tarama/tarama_merkezi.py" 2>/dev/null || true
	done
	for home in /home/*; do
		rm -f "$home/.config/systemd/user/default.target.wants/wsd-tarama.service" \
		      "$home"/.config/autostart/*.TaramaMerkezi-tepsi.desktop
		for d in "$home"/Masaüstü "$home"/Desktop; do rm -f "$d"/*.TaramaMerkezi.desktop; done
	done
	systemctl disable --now wsd-tarama-guncelle.timer >/dev/null 2>&1 || true
	rm -f /etc/systemd/user/wsd-tarama.service /etc/systemd/system/wsd-tarama-guncelle.service \
	      /etc/systemd/system/wsd-tarama-guncelle.timer /etc/polkit-1/rules.d/50-wsd-tarama-guncelle.rules \
	      /usr/share/applications/*.TaramaMerkezi.desktop
	install -d -m 0755 "$CONF"
	rm -rf /opt/wsd-tarama /etc/wsd-tarama /var/lib/wsd-tarama
	echo "Previous wsd-tarama installation migrated."
}

install_files() {
	install -d "$APP" /var/lib/printer-center
	install -d -m 0755 "$CONF"
	install -m 0644 "$SRC_DIR/printer_center.py" "$SRC_DIR/i18n.py" "$APP/"
	install -m 0755 "$SRC_DIR/printer_center_gui.py" "$SRC_DIR/updater.py" "$APP/"
	install -m 0644 "$SRC_DIR/printer-center.service" /etc/systemd/user/
	install -m 0644 "$SRC_DIR/printer-center-update.service" "$SRC_DIR/printer-center-update.timer" \
		"$SRC_DIR/printer-center-install.service" /etc/systemd/system/
	install -m 0644 "$SRC_DIR/io.github.mustafa_ozen95.PrinterCenter.desktop" /usr/share/applications/
	command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
	# polkit >= 106 (JS rules): the "Check for updates" button needs no password; older polkit asks for admin
	if [ -d /etc/polkit-1/rules.d ]; then
		install -m 0644 "$SRC_DIR/50-printer-center-update.rules" /etc/polkit-1/rules.d/
	fi
	fix_pdf_fonts || echo "Warning: the PDF font fix could not be applied (will retry with the next update)"
	allow_lan_queue
	configure_updates  # before the timer starts: its first run may be immediate
	systemctl daemon-reload
	systemctl enable --now printer-center-update.timer >/dev/null 2>&1 || true
	echo "Program files installed: version $(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC_DIR/printer_center.py")"
}

# PDFs that do not embed their TrueType fonts (e.g. Arial as CID/Identity-H, common in
# reports) print as empty tables: Ghostscript, which converts PDF for driverless printers,
# substitutes a font whose glyph numbers do not match. Install the real fonts and map them.
fix_pdf_fonts() {
	map=/etc/ghostscript/cidfmap.d/90-msttcorefonts-identity.conf
	fonts=/usr/share/fonts/truetype/msttcorefonts
	[ -d /etc/ghostscript/cidfmap.d ] && command -v apt-get >/dev/null || return 0
	[ -s "$map" ] && [ -f "$fonts/Arial.ttf" ] && return 0
	if [ ! -f "$fonts/Arial.ttf" ]; then
		echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections
		DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y ttf-mscorefonts-installer \
			>/dev/null 2>&1 || return 1
		[ -f "$fonts/Arial.ttf" ] || return 1
	fi
	: > "$map.part"
	for entry in "Arial ArialMT:Arial" "Arial,Bold Arial-BoldMT:Arial_Bold" \
		"Arial,Italic Arial-ItalicMT:Arial_Italic" "Arial,BoldItalic Arial-BoldItalicMT:Arial_Bold_Italic" \
		"TimesNewRoman TimesNewRomanPSMT:Times_New_Roman" "TimesNewRoman,Bold TimesNewRomanPS-BoldMT:Times_New_Roman_Bold" \
		"CourierNew CourierNewPSMT:Courier_New" "CourierNew,Bold CourierNewPS-BoldMT:Courier_New_Bold" \
		"Verdana:Verdana" "Verdana,Bold:Verdana_Bold"; do
		file="$fonts/${entry#*:}.ttf"
		[ -f "$file" ] || continue
		for name in ${entry%%:*}; do
			printf '/%s << /FileType /TrueType /Path (%s) /SubfontID 0 /CSI [(Identity) 0] >> ;\n' "$name" "$file" >> "$map.part"
		done
	done
	mv "$map.part" "$map"
	command -v update-gsfontmap >/dev/null && update-gsfontmap
	echo "PDF font fix installed: $map"
}

# Shared print queue: computers on the local network read each other's queue on the
# service port (tcp) and find each other with a broadcast on the same port (udp)
allow_lan_queue() {
	command -v ufw >/dev/null && ufw status | grep -q "Status: active" || return 0
	port=$(sed -n 's/^port *= *\([0-9][0-9]*\).*/\1/p' /home/*/.config/printer-center/settings.ini 2>/dev/null | head -1)
	port=${port:-$PORT}
	dev=$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
	[ -n "$dev" ] || return 0
	lan=$(ip -4 route show dev "$dev" scope link 2>/dev/null | awk '$1 ~ /\// {print $1; exit}')
	[ -n "$lan" ] || return 0
	ufw allow proto tcp from "$lan" to any port "$port" comment "printer-center queue" >/dev/null
	ufw allow proto udp from "$lan" to any port "$port" comment "printer-center discovery" >/dev/null
	echo "ufw: $lan -> tcp,udp/$port allowed (shared print queue)"
}

# Updates are read anonymously from the public repository over HTTPS: no git, no token
# and no key on the computers. Keys left by earlier versions are removed.
configure_updates() {
	install -d -m 0755 "$CONF"
	rm -f "$CONF/token" "$CONF/deploy_key" "$CONF/deploy_key.pub" "$CONF/known_hosts"
	# keep a repository chosen earlier with --repo (the updater runs --files without it)
	[ -n "$REPO_SET" ] || REPO=$(sed -n 's/^repo *= *//p' "$CONF/update.conf" 2>/dev/null | head -1)
	printf 'repo = %s\n' "${REPO:-$DEFAULT_REPO}" > "$CONF/update.conf"
	chmod 0644 "$CONF/update.conf"
}

# Desktop shortcut + start in the notification area (system tray) at login
user_shortcuts() {
	desktop=$(runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" xdg-user-dir DESKTOP 2>/dev/null || true)
	[ -n "$desktop" ] && [ "$desktop" != "$HOME_DIR" ] || desktop="$HOME_DIR/Desktop"
	runuser -u "$USER_NAME" -- mkdir -p "$desktop" "$HOME_DIR/.config/autostart"
	shortcut="$desktop/io.github.mustafa_ozen95.PrinterCenter.desktop"
	install -m 0755 -o "$USER_NAME" "$SRC_DIR/io.github.mustafa_ozen95.PrinterCenter.desktop" "$shortcut"
	install -m 0644 -o "$USER_NAME" "$SRC_DIR/io.github.mustafa_ozen95.PrinterCenter-tray.desktop" "$HOME_DIR/.config/autostart/"
	# Let Nemo (Cinnamon) / Caja (MATE) treat the launcher as trusted
	if [ -S "$RUN_DIR/bus" ]; then
		runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="$RUN_DIR" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN_DIR/bus" \
			gio set "$shortcut" metadata::trusted true 2>/dev/null || true
	fi
	echo "Desktop shortcut: $shortcut"
}

start_tray() {
	[ -S "$RUN_DIR/bus" ] || return 0
	pgrep -u "$USER_NAME" -f "printer_center_gui.py" >/dev/null && return 0
	display=$(runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="$RUN_DIR" systemctl --user show-environment 2>/dev/null \
		| sed -n 's/^DISPLAY=//p')
	[ -n "$display" ] || return 0
	runuser -u "$USER_NAME" -- env DISPLAY="$display" XDG_RUNTIME_DIR="$RUN_DIR" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN_DIR/bus" HOME="$HOME_DIR" \
		setsid /usr/bin/python3 "$APP/printer_center_gui.py" --tray >/dev/null 2>&1 &
	echo "Printer Center started in the notification area."
}

if [ -n "$FILES_ONLY" ]; then
	install_files
	for_user_sessions try-restart
	exit 0
fi

[ -n "$USER_NAME" ] && [ -n "$PRINTER" ] || { echo "--user and --printer are required" >&2; exit 2; }
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
[ -n "$HOME_DIR" ] || { echo "No such user: $USER_NAME" >&2; exit 1; }
WANTS="$HOME_DIR/.config/systemd/user/default.target.wants/printer-center.service"
RUN_DIR="/run/user/$(id -u "$USER_NAME")"

if [ -n "$UNINSTALL" ]; then
	rm -f "$WANTS" "$HOME_DIR/.config/autostart/io.github.mustafa_ozen95.PrinterCenter-tray.desktop"
	if [ -S "$RUN_DIR/bus" ]; then
		runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="$RUN_DIR" systemctl --user stop printer-center.service || true
	fi
	command -v ufw >/dev/null && ufw delete allow proto tcp from "$PRINTER" to any port "$PORT" >/dev/null 2>&1 || true
	if command -v ufw >/dev/null; then
		ufw status numbered 2>/dev/null | grep -q "printer-center \(queue\|discovery\)" &&
			for n in $(ufw status numbered | grep "printer-center \(queue\|discovery\)" | sed 's/^\[ *\([0-9]*\)\].*/\1/' | sort -rn); do
				ufw --force delete "$n" >/dev/null
			done
	fi
	echo "Uninstalled for $USER_NAME (settings, $APP and the update timer were kept)."
	exit 0
fi

migrate_legacy
install_files

SETTINGS="$HOME_DIR/.config/printer-center/settings.ini"
if [ ! -f "$SETTINGS" ] && [ ! -f "$HOME_DIR/.config/wsd-tarama/ayar.ini" ]; then
	runuser -u "$USER_NAME" -- mkdir -p "$HOME_DIR/.config/printer-center"
	sed -e "s#PRINTER_IP#$PRINTER#" -e "s#^port = .*#port = $PORT#" \
	    -e "0,/^name =.*/s##name = $PANEL_NAME#" "$SRC_DIR/settings.example.ini" > "$SETTINGS"
	chown "$USER_NAME": "$SETTINGS"
	echo "Settings created: $SETTINGS"
fi

runuser -u "$USER_NAME" -- mkdir -p "$(dirname "$WANTS")"
ln -sf /etc/systemd/user/printer-center.service "$WANTS"
chown -h "$USER_NAME": "$WANTS"

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
	ufw allow proto tcp from "$PRINTER" to any port "$PORT" comment "printer-center" >/dev/null
	echo "ufw: $PRINTER -> tcp/$PORT allowed"
fi

if [ -S "$RUN_DIR/bus" ]; then
	runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="$RUN_DIR" systemctl --user daemon-reload
	runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="$RUN_DIR" systemctl --user restart printer-center.service
	echo "Service started (user is logged in)."
else
	echo "Service will start when $USER_NAME logs in."
fi

user_shortcuts
start_tray
