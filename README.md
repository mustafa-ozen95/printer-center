# Printer Center

Network scanner and printer management tool for the Linux desktop. Works with WSD
(WS-Scan) multifunction devices; tested with a Xerox WorkCentre 3025. It covers what the
vendor software (Easy Printer Manager / Scan2PC) offers on Windows, and more.
The interface is available in Turkish and English (operating system language by default,
selectable in Settings).

*Türkçe özet:* Linux için ağ tarayıcısı ve yazıcı yönetim aracı. Camdan ve panelden
tarama, yazdırma kuyruğu, cihaz/toner/arıza durumu, günlük ve otomatik güncelleme.
Arayüz Türkçe ve İngilizce; dil sistemden alınır, Ayarlar'dan değiştirilebilir.

## Features

- **Scan:** multi-page scanning from the glass, preview, rotate and reorder pages, save as PDF/JPEG.
- **Scan from the device panel:** choosing *Scan → WSD → computer name* on the device
  drops the document into the scan folder and shows a desktop notification (background service).
- **Print queue:** local CUPS jobs and all jobs on the device; cancel, restart a stopped queue.
- **Device status:** SNMP error bits and alert table, toner (part no., serial, estimated
  days left), maintenance kit life, trays, counters, security checks. New faults raise a
  desktop notification.
- **Logs:** service and app events are kept in `~/.local/state/printer-center/`
  (1 MB × 3, rotated) and can be filtered and copied in the app.
- **Notification area:** the app starts in the system tray at login; the icon follows the
  printer state. Closing the window keeps it running in the tray.
- **Automatic updates** from this repository's release tags, with a desktop notification after each update.

No extra packages: Python 3 standard library + GTK 3 (PyGObject). WSD, IPP and SNMP
clients are built in.

## Supported printers

The app talks to the device with open standards, so most features work on any network
multifunction printer that supports them. Extended status details use the SyncThru web
interface found on Samsung-built devices.

| Feature | Requirement | Devices |
|---|---|---|
| Scan from the glass | WSD scanning (WS-Scan) | Most network MFPs with Windows "WSD scan" support |
| Scan from the device panel | WS-Scan scan events | Same as above, if the panel offers *Scan → WSD / PC* |
| Printing, print queue | IPP / IPP Everywhere (CUPS) | Nearly all network printers made in the last ~10 years |
| Basic status, alerts, toner level | SNMP Printer MIB (RFC 3805) | Most network printers (SNMP v1/v2c, community `public`) |
| Detailed status (toner part no. and serial, estimated days left, counters, network and security checks) | Samsung SyncThru web interface | Samsung-built devices: Xerox Phaser 3020/3260, WorkCentre 3025/3215/3225, Samsung Xpress / ProXpress |

**Tested:** Xerox WorkCentre 3025 (all features).
Other models are expected to work as listed but have not been tested yet. Reports of
working or failing models are welcome as issues.

*Türkçe:* WSD tarama ve IPP yazdırma destekleyen ağ yazıcılarının çoğuyla çalışır; ayrıntılı
durum (toner parça/seri no, kalan gün, sayaçlar) Samsung SyncThru arayüzlü cihazlarda
(Xerox Phaser 3020/3260, WorkCentre 3025/3215/3225, Samsung Xpress/ProXpress) gelir.
Test edilen model: Xerox WorkCentre 3025.

## Installation

```sh
sudo sh install.sh --user USER --printer PRINTER_IP [--name PANEL_NAME] [--port PORT]
sudo sh install.sh --user USER --printer PRINTER_IP --uninstall
```

The installer puts the program in `/opt/printer-center/`, the user service in
`/etc/systemd/user/`, creates `~/.config/printer-center/settings.ini` from
`settings.example.ini`, adds a desktop shortcut, a menu entry and a tray autostart entry.
A previous `wsd-tarama` installation (and its Turkish-keyed `ayar.ini`) is migrated
automatically. Scan-to-PC needs the device to reach the computer on TCP `PORT`
(default 53580); if ufw is active a rule is added.

Printing needs no driver: a CUPS IPP Everywhere queue is enough.

## Updates

No git, token or key is needed on the computers. `printer-center-update.timer` runs daily
(10 min after boot, ±1 h) and lists this public repository's `vX.Y.Z` tags anonymously over
HTTPS (certificate verified). If one is newer than the installed version it fetches that tag
(a built-in, read-only git client), checks that its `VERSION` matches, installs it with
`install.sh --files`, restarts the services and notifies the user with the tag message as
release notes. Nothing is sent from the computer. *Settings → About and updates* can check
on demand. State: `/var/lib/printer-center/update.json`, details:
`journalctl -u printer-center-update`.

Publishing a release (maintainer):

```sh
# bump VERSION in printer_center.py, commit, then:
python3 release.py -m "What changed"
```

The script creates the annotated tag (its message carries the notes), pushes it and adds a
Release page. Commits without a tag never reach the computers.

## Translations

Source strings are English. Turkish texts live in `i18n.py` (`TR` dictionary). Wrap new
user-visible strings in `_()`, or `N_()` for module-level tables.

## Privacy

Scans, logs and settings stay on the computer; the app talks only to the printer on the
local network and, for updates, reads this repository. The repository contains no device
address, personal settings, scans or logs (`.gitignore`). Document contents and passwords
are never logged.

## License

[MIT](LICENSE)
