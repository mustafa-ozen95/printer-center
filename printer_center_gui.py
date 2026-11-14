#!/usr/bin/env python3
"""Printer Center: desktop interface for WSD network scanners (GTK3).

Colors come from the system theme (light/dark theme and accent color are kept);
fixed green/yellow/red is used only for the status dots.
"""

import datetime as dt
import getpass
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import printer_center as core  # noqa: E402
import i18n  # noqa: E402
from i18n import _, N_  # noqa: E402

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

log = logging.getLogger("gui")

APP_ID = "io.github.mustafa_ozen95.PrinterCenter"
SERVICE = "printer-center.service"
COLOR_CHOICES = [(N_("Color"), "color"), (N_("Gray"), "gray")]
FORMATS = [("PDF", "pdf"), ("JPEG", "jpg")]
# Document card: (label, id, extension, one file per page)
DOC_FORMATS = [("PDF", "pdf", ".pdf", False), (N_("PDF, one file per page"), "pdfs", ".pdf", True),
               ("JPEG", "jpg", ".jpg", True), ("PNG", "png", ".png", True), ("TIFF", "tif", ".tif", True)]
# quality id -> (JPEG quality, highest resolution); "original" keeps the scanner's image
DOC_QUALITIES = [(N_("Original"), "original"), (N_("High"), "high"),
                 (N_("Medium (smaller file)"), "medium"), (N_("Small (for e-mail)"), "small")]
QUALITY = {"high": (90, None), "medium": (75, 200), "small": (60, 150)}
EMPTY_HINT = N_("Place the document face down on the glass and press Scan.")
# strftime formats per language: (date, date + time, short date + time)
DATE_FORMATS = {"tr": ("%d.%m.%Y", "%d.%m.%Y %H:%M", "%d.%m %H:%M"),
                "en": ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%m-%d %H:%M")}

CSS = b"""
.pc-sidebar { background-color: alpha(@theme_fg_color, 0.035);
            border-right: 1px solid alpha(@theme_fg_color, 0.10); }
.pc-sidebar list, .pc-sidebar list row { background-color: transparent; }
.pc-sidebar list row { border-radius: 8px; margin: 2px 8px; padding: 9px 10px; }
.pc-sidebar list row:hover { background-color: alpha(@theme_fg_color, 0.06); }
.pc-sidebar list row:selected { background-color: @theme_selected_bg_color;
                              color: @theme_selected_fg_color; }
.pc-card { background-color: @theme_base_color; border-radius: 12px;
           border: 1px solid alpha(@theme_fg_color, 0.10); padding: 16px; }
.pc-card-title { font-weight: bold; }
.pc-page-title { font-size: 17pt; font-weight: bold; }
.pc-big-text { font-size: 14pt; font-weight: bold; }
.pc-dim { color: alpha(@theme_fg_color, 0.60); }
.pc-big { min-height: 50px; font-size: 13pt; font-weight: bold; border-radius: 10px; }
.pc-preview { background-color: alpha(@theme_fg_color, 0.05); border-radius: 10px; }
.pc-thumbs, .pc-thumbs.view { background-color: transparent; }
.pc-thumbs.view:selected, .pc-thumbs.view:selected:focus {
           background-color: alpha(@theme_selected_bg_color, 0.85); border-radius: 8px; }
.pc-dot { border-radius: 99px; min-width: 10px; min-height: 10px; }
.pc-dot.ok { background-color: #2ec27e; }
.pc-dot.warning { background-color: #e5a50a; }
.pc-dot.error { background-color: #e01b24; }
.pc-dot.neutral { background-color: alpha(@theme_fg_color, 0.30); }
.pc-badge { border-radius: 99px; padding: 3px 12px; font-weight: bold; font-size: 9pt; }
.pc-badge.ok { background-color: alpha(#2ec27e, 0.18); color: #26a269; }
.pc-badge.warning { background-color: alpha(#e5a50a, 0.20); color: #c88800; }
.pc-badge.error { background-color: alpha(#e01b24, 0.15); color: #e01b24; }
.pc-badge.neutral { background-color: alpha(@theme_fg_color, 0.08); }
.pc-icon-error { color: #e01b24; }
.pc-icon-warning { color: #c88800; }
.pc-icon-ok { color: #26a269; }
levelbar.pc-toner block { min-height: 10px; border-radius: 5px; }
.pc-recent row, .pc-job row { border-radius: 8px; }
.pc-log text { background-color: @theme_base_color; }
.pc-plain { background-color: transparent; }
.pc-counter { border-radius: 99px; padding: 0 7px; font-size: 8pt; font-weight: bold;
            background-color: @theme_selected_bg_color; color: @theme_selected_fg_color; }
.pc-winbtn { min-width: 26px; min-height: 26px; padding: 0; margin: 0 1px;
             border-radius: 99px; border: none; box-shadow: none; background: none;
             color: alpha(@theme_fg_color, 0.55); transition: all 120ms ease-out; }
.pc-winbtn image { -gtk-icon-transform: scale(0.85); }
.pc-winbtn:hover { background-color: alpha(@theme_fg_color, 0.09); color: @theme_fg_color; }
.pc-winbtn:active { background-color: alpha(@theme_fg_color, 0.16); }
.pc-winbtn.close:hover { background-color: #e01b24; color: #ffffff; }
.pc-winbtn.close:active { background-color: #c01c28; color: #ffffff; }
.pc-winbtns { margin-left: 6px; }
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def read_settings():
    core.migrate_legacy_settings()
    c = core.settings_parser()
    c.read(core.SETTINGS_PATH, encoding="utf-8")
    for b in ("device", "client"):
        if not c.has_section(b):
            c.add_section(b)
    profile = next((s for s in c.sections() if s.startswith("profile:")), None)
    if profile is None:
        profile = "profile:Color"
        c.add_section(profile)
    return c, profile


def write_settings(c):
    os.makedirs(os.path.dirname(core.SETTINGS_PATH), exist_ok=True)
    tmp = core.SETTINGS_PATH + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        c.write(f)
    os.replace(tmp, core.SETTINGS_PATH)


def device_address(ip):
    ip = ip.strip()
    if not ip:
        return ""
    if ip.startswith("http"):
        return ip
    return "http://%s:8018/wsd/scan" % ip


def ip_from_address(address):
    return urllib.parse.urlparse(address).hostname or "" if address else ""


def systemctl(*args):
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def read_update_state():
    try:
        with open(core.UPDATE_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def installed_version():
    """Version on disk (possibly newer, just installed); the running one is core.VERSION."""
    try:
        with open(core.__file__, encoding="utf-8") as f:
            m = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M)
        return m.group(1) if m else core.VERSION
    except OSError:
        return core.VERSION


def version_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


def date_format(kind=1):
    """strftime format for the UI language: 0 date, 1 date + time, 2 short date + time."""
    return DATE_FORMATS.get(i18n.language(), DATE_FORMATS["en"])[kind]


def format_date(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime(date_format())
    except (TypeError, ValueError):
        return ""


def open_path(path):
    try:
        Gio.AppInfo.launch_default_for_uri(
            path if "://" in path else Gio.File.new_for_path(path).get_uri(), None)
    except GLib.Error as e:
        print("Could not open:", e, file=sys.stderr)


def styled(widget, *names):
    ctx = widget.get_style_context()
    for name in names:
        ctx.add_class(name)
    return widget


def swap_class(widget, options, selected):
    ctx = widget.get_style_context()
    for s in options:
        ctx.remove_class(s)
    ctx.add_class(selected)


def make_label(text, *classes, **kw):
    kw.setdefault("xalign", 0)
    return styled(Gtk.Label(label=text, **kw), *classes)


def combo(options, selected):
    box = Gtk.ComboBoxText()
    for e, value in options:
        box.append(str(value), e)
    box.set_active_id(str(selected))
    if box.get_active() < 0:
        box.set_active(0)
    return box


def form(rows):
    g = Gtk.Grid(column_spacing=16, row_spacing=10)
    for i, (e, widget) in enumerate(rows):
        g.attach(make_label(e, "pc-dim"), 0, i, 1, 1)
        widget.set_hexpand(True)
        g.attach(widget, 1, i, 1, 1)
    return g


def card(title=None, *content, description=None, right=None, horizontal=False):
    k = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), "pc-card")
    if title or right:
        top = Gtk.Box(spacing=8)
        if title:
            title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_box.pack_start(make_label(title, "pc-card-title"), False, False, 0)
            if description:
                title_box.pack_start(make_label(description, "pc-dim", wrap=True), False, False, 0)
            top.pack_start(title_box, True, True, 0)
        if right:
            right.set_valign(Gtk.Align.CENTER)
            top.pack_end(right, False, False, 0)
        k.pack_start(top, False, False, 0)
    for widget in content:
        k.pack_start(widget, True, True, 0)
    return k


def icon_button(icon, label_text, *classes):
    d = Gtk.Button()
    inner = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
    inner.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
    inner.pack_start(Gtk.Label(label=label_text), False, False, 0)
    inner.show_all()  # buttons hidden with set_no_show_all() must still show their content
    d.add(inner)
    return styled(d, *classes)


def page_shell(title, *content):
    """Scrollable, centred settings page at most ~760 px wide."""
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=24)
    inner.pack_start(make_label(title, "pc-page-title"), False, False, 0)
    for widget in content:
        inner.pack_start(widget, False, False, 0)
    clamp = Gtk.Box(halign=Gtk.Align.FILL)
    clamp.set_center_widget(inner)
    inner.set_size_request(640, -1)
    scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.add(clamp)
    return scroller


def jpeg_pixbuf(data):
    loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
    loader.write(data)
    loader.close()
    return loader.get_pixbuf()


def pixbuf_jpeg(pb, quality=92):
    ok, data = pb.save_to_bufferv("jpeg", ["quality"], [str(quality)])
    return bytes(data)


def page_output(page, quality):
    """(pixbuf, jpeg, dpi) of a page for the chosen quality."""
    if quality not in QUALITY:
        return page["pb"], page["jpeg"], page["dpi"]
    jpeg_quality, max_dpi = QUALITY[quality]
    pb, dpi = page["pb"], page["dpi"]
    if max_dpi and dpi > max_dpi:
        f = max_dpi / dpi
        pb = pb.scale_simple(max(1, round(pb.get_width() * f)), max(1, round(pb.get_height() * f)),
                             GdkPixbuf.InterpType.HYPER)
        dpi = max_dpi
    return pb, pixbuf_jpeg(pb, jpeg_quality), dpi


def is_blank(pb):
    """True for an empty page: almost no dark pixels inside the margins (scanner dust is tolerated)."""
    w, h = pb.get_width(), pb.get_height()
    f = min(1.0, 800.0 / max(w, h))
    # nearest-neighbour keeps thin dark strokes dark (smoothing would turn text into light grey)
    small = pb.scale_simple(max(1, int(w * f)), max(1, int(h * f)), GdkPixbuf.InterpType.NEAREST)
    sw, sh, n, stride = small.get_width(), small.get_height(), small.get_n_channels(), small.get_rowstride()
    px = small.get_pixels()
    mx, my = int(sw * 0.06), int(sh * 0.06)
    dark = total = 0
    for y in range(my, sh - my):
        row = y * stride
        for x in range(mx, sw - mx):
            i = row + x * n
            total += 1
            if px[i] * 3 + px[i + 1] * 6 + px[i + 2] < 1600:  # luminance < 160
                dark += 1
    return total > 0 and dark < total * 0.0002  # ~0.02 %: one line of text is not blank


def safe_name(text, fallback):
    text = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', "-", text).strip(" .-")
    return text or fallback


def fmt_number(n):
    """Number with thousands separator: 15889 -> 15.889 (Turkish) / 15,889 (English)"""
    try:
        text = "{:,}".format(int(n))
    except (TypeError, ValueError):
        return str(n)
    return text.replace(",", ".") if i18n.language() == "tr" else text


def page_count(n):
    """'1 page' / '15,889 pages' with the number formatted for the UI language."""
    return (_("%s page") if n == 1 else _("%s pages")) % fmt_number(n)


def error_text(error):
    """Readable, translated error for the screen (details are logged where the error occurs)."""
    return core.friendly_error(error)


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

class ScanPage(Gtk.Box):
    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=16, margin=16)
        self.main_window = main_window
        self.pages = []  # {"jpeg": bytes, "pb": Pixbuf, "dpi": int}

        # --- Left column
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=False)
        left.set_size_request(290, -1)
        # scrolls on short screens (e.g. 1366x768) instead of making the window taller than the screen
        left_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                         propagate_natural_width=True)
        viewport = Gtk.Viewport(shadow_type=Gtk.ShadowType.NONE)
        styled(viewport, "pc-plain")
        viewport.add(left)
        left_scroll.add(viewport)
        self.pack_start(left_scroll, False, False, 0)

        self.color = combo([(_(e), v) for e, v in COLOR_CHOICES], "color")
        self.dpi = combo([("%d dpi" % d, d) for d in core.RESOLUTIONS], 300)
        self.paper = combo([(k, k) for k in core.PAPER_SIZES], "A4")
        left.pack_start(card(_("Scan settings"), form([
            (_("Color"), self.color), (_("Resolution"), self.dpi), (_("Paper"), self.paper)])), False, False, 0)

        self.scan_btn = icon_button("scanner-symbolic", _("Scan"), "suggested-action", "pc-big")
        self.scan_btn.connect("clicked", self.scan)
        left.pack_start(self.scan_btn, False, False, 0)
        self.job = None  # scan of this window while it is queued/running (shown on the Queue page)
        self.cancel_event = None
        self.cancel_btn = icon_button("process-stop-symbolic", _("Cancel"), "flat")
        self.cancel_btn.connect("clicked", self.cancel_scan)
        self.cancel_btn.set_no_show_all(True)
        left.pack_start(self.cancel_btn, False, False, 0)

        status = Gtk.Box(spacing=8, margin_start=4, margin_end=4)
        self.spinner = Gtk.Spinner()
        self.status = make_label(_("Ready."), "pc-dim", wrap=True, max_width_chars=30, width_chars=30)
        status.pack_start(self.spinner, False, False, 0)
        status.pack_start(self.status, True, True, 0)
        left.pack_start(status, False, False, 0)

        c, _profile = read_settings()
        doc = lambda key, default: c.get("client", key, fallback=default)
        self.file_format = combo([(_(e), v) for e, v, _x, _m in DOC_FORMATS], doc("doc_format", "pdf"))
        self.quality = combo([(_(e), v) for e, v in DOC_QUALITIES], doc("doc_quality", "original"))
        self.quality.set_tooltip_text(_("Original keeps the scanner's image. Lower quality makes the file "
                                        "smaller (lower resolution and stronger compression)."))
        self.file_name = Gtk.Entry(text=doc("doc_name", ""), placeholder_text=_("Scanned document"))
        self.file_name.set_tooltip_text(_("Date and time are added to the name"))
        self.skip_blank = Gtk.CheckButton(label=_("Skip blank pages"))
        self.skip_blank.set_active(doc("skip_blank", "no") == "yes")
        self.open_after = Gtk.CheckButton(label=_("Open after saving"))
        self.open_after.set_active(doc("open_after", "no") == "yes")
        for w, signal in ((self.file_format, "changed"), (self.quality, "changed"), (self.file_name, "changed"),
                          (self.skip_blank, "toggled"), (self.open_after, "toggled")):
            w.connect(signal, self.on_doc_option)

        self.save_btn = icon_button("document-save-symbolic", _("Save"), "suggested-action")
        self.save_btn.set_tooltip_text(_("Save to the scan folder"))
        self.save_btn.connect("clicked", self.save, False)
        self.save_as_btn = icon_button("document-save-as-symbolic", _("Save as…"))
        self.save_as_btn.connect("clicked", self.save, True)
        self.print_btn = icon_button("printer-symbolic", _("Print…"), "flat")
        self.print_btn.connect("clicked", self.print_pages)
        self.clear_btn = icon_button("edit-clear-all-symbolic", _("Clear all"), "flat")
        self.clear_btn.connect("clicked", self.clear_pages)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(form([(_("Format"), self.file_format), (_("Quality"), self.quality),
                             (_("File name"), self.file_name)]), False, False, 0)
        box.pack_start(self.skip_blank, False, False, 0)
        box.pack_start(self.open_after, False, False, 0)
        row = Gtk.Box(spacing=8, homogeneous=True, margin_top=4)
        row.pack_start(self.save_btn, True, True, 0)
        row.pack_start(self.save_as_btn, True, True, 0)
        box.pack_start(row, False, False, 0)
        row = Gtk.Box(spacing=8, homogeneous=True)
        row.pack_start(self.print_btn, True, True, 0)
        row.pack_start(self.clear_btn, True, True, 0)
        box.pack_start(row, False, False, 0)
        self.doc_summary = _("No pages yet")
        self.document_card = card(_("Document"), box, description=" ")
        left.pack_start(self.document_card, False, False, 0)

        # --- Right: preview card
        right = card()
        self.pack_start(right, True, True, 0)

        top = Gtk.Box(spacing=8)
        self.counter = make_label(_("Pages"), "pc-card-title")
        top.pack_start(self.counter, True, True, 0)
        self.tools = styled(Gtk.Box(), "linked")
        for icon, tooltip, func in (
                ("object-rotate-left-symbolic", _("Rotate left"), lambda b: self.rotate(90)),
                ("object-rotate-right-symbolic", _("Rotate right"), lambda b: self.rotate(270)),
                ("go-up-symbolic", _("Move up"), lambda b: self.move(-1)),
                ("go-down-symbolic", _("Move down"), lambda b: self.move(1)),
                ("user-trash-symbolic", _("Delete page"), lambda b: self.delete_page())):
            d = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            d.set_tooltip_text(tooltip)
            d.connect("clicked", func)
            self.tools.pack_start(d, False, False, 0)
        top.pack_end(self.tools, False, False, 0)
        right.pack_start(top, False, False, 0)

        body = Gtk.Box(spacing=12)
        right.pack_start(body, True, True, 0)

        self.items = Gtk.ListStore(GdkPixbuf.Pixbuf, str, int)  # thumbnail, label, page index
        self.icons = styled(Gtk.IconView(model=self.items, pixbuf_column=0, text_column=1),
                              "pc-thumbs")
        self.icons.set_item_width(100)
        self.icons.set_columns(1)
        self.icons.set_margin(4)
        self.icons.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.icons.set_reorderable(True)
        self.icons.connect("selection-changed", self.on_selection_changed)
        self.items.connect("row-deleted", self.on_reorder)
        self.thumb_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.thumb_scroll.set_size_request(136, -1)
        self.thumb_scroll.add(self.icons)
        body.pack_start(self.thumb_scroll, False, False, 0)

        self.preview_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        image = styled(Gtk.Image.new_from_icon_name("scanner-symbolic", Gtk.IconSize.DIALOG),
                      "pc-dim")
        image.set_pixel_size(96)
        empty.pack_start(image, False, False, 0)
        empty.pack_start(make_label(_("No scanned pages yet"), "pc-big-text", xalign=0.5),
                       False, False, 0)
        empty.pack_start(make_label(_(EMPTY_HINT) + "\n" + _("For a multi-page document press Scan "
                              "again after each page."), "pc-dim", xalign=0.5, wrap=True,
                              justify=Gtk.Justification.CENTER, max_width_chars=48),
                       False, False, 0)
        self.preview_stack.add_named(empty, "neutral")
        self.preview = Gtk.DrawingArea()
        self.preview.connect("draw", self.on_draw)
        self.preview_stack.add_named(self.preview, "page")
        area = styled(Gtk.Box(), "pc-preview")
        area.pack_start(self.preview_stack, True, True, 0)
        body.pack_start(area, True, True, 0)

        self.show_all()
        self.update_view()

    # --- scanning
    def scan(self, _widget):
        device = self.main_window.device()
        if not device:
            self.main_window.warn(_("First enter the printer's IP address in Settings."))
            self.main_window.navigate("settings")
            return
        color = core.COLOR_MODES[self.color.get_active_id()]
        dpi = int(self.dpi.get_active_id())
        paper = self.paper.get_active_id()
        self.scan_btn.set_sensitive(False)
        self.spinner.start()
        self.status.set_text(_("Scanning… (about 10–30 seconds)"))
        log.info("Scan started: %s, %d dpi, %s", color, dpi, paper)
        cancel = self.cancel_event = threading.Event()
        self.job = {"target": _("Scan from the glass"), "time": time.time(), "status": "scanning", "own": True}
        self.cancel_btn.show()

        def on_wait(reason, seconds):
            GLib.idle_add(self.waiting, reason, seconds)

        def work():
            started = time.time()
            try:
                data = core.pull_scan(device, color, dpi, paper, socket.gethostname(),
                                      on_wait=on_wait, cancel=cancel)
                log.info("Scan done: %d KB, %.1f s", len(data) // 1024, time.time() - started)
                GLib.idle_add(self.scanned, data, dpi, None)
            except core.ScanCancelled:
                log.info("Scan cancelled while waiting in line")
                GLib.idle_add(self.scanned, None, dpi, "cancelled")
            except Exception as e:
                log.error("Scan failed (%.1f s): %s", time.time() - started, e, exc_info=True)
                GLib.idle_add(self.scanned, None, dpi, e)

        threading.Thread(target=work, daemon=True).start()

    def waiting(self, reason, seconds):
        if not self.job:
            return
        self.job["status"] = {"device": "busy", "local": "waiting"}.get(reason, "scanning")
        if reason == "local":
            self.status.set_text(_("Another scan on this computer is in progress; waiting in line… (%d s)")
                                 % seconds)
        elif reason == "device":
            self.status.set_text(_("The device is busy with another job; waiting in line… (%d s)") % seconds)
        else:
            self.status.set_text(_("Scanning… (about 10–30 seconds)"))
            self.cancel_btn.hide()  # the device is scanning now; it cannot be stopped from here

    def cancel_scan(self, _widget):
        if self.cancel_event:
            self.cancel_event.set()
        self.cancel_btn.hide()

    def scanned(self, data, dpi, error):
        self.scan_btn.set_sensitive(True)
        self.spinner.stop()
        self.job = self.cancel_event = None
        self.cancel_btn.hide()
        if error == "cancelled":
            self.status.set_text(_("Scan cancelled."))
            return
        if error:
            self.status.set_text(_("Scan failed: %s") % error_text(error))
            self.main_window.warn(_("Scan failed.\n\n%s\n\nCheck that the device is on and not busy "
                                    "with another job, then try again.") % error_text(error))
            return
        self.add_page(data, dpi)
        self.status.set_text(_("Page %d added. Press Scan again for the next page.")
                             % len(self.pages))

    def add_page(self, data, dpi):
        self.pages.append({"jpeg": data, "pb": jpeg_pixbuf(data), "dpi": dpi})
        self.refresh_list(len(self.pages) - 1)

    # --- page list
    def thumb(self, pb):
        ratio = 88.0 / max(pb.get_width(), pb.get_height())
        return pb.scale_simple(max(1, int(pb.get_width() * ratio)),
                               max(1, int(pb.get_height() * ratio)), GdkPixbuf.InterpType.BILINEAR)

    def refresh_list(self, select=None):
        self.items.clear()
        for i, s in enumerate(self.pages):
            self.items.append([self.thumb(s["pb"]), _("Page %d") % (i + 1), i])
        if select is not None and self.pages:
            path = Gtk.TreePath.new_from_indices([max(0, min(select, len(self.pages) - 1))])
            self.icons.select_path(path)
            self.icons.scroll_to_path(path, False, 0, 0)
        self.update_view()
        self.preview.queue_draw()

    def update_view(self):
        n = len(self.pages)
        self.preview_stack.set_visible_child_name("page" if n else "neutral")
        self.thumb_scroll.set_visible(n > 0)
        self.tools.set_sensitive(n > 0)
        for b in (self.save_btn, self.save_as_btn, self.print_btn, self.clear_btn):
            b.set_sensitive(n > 0)
        self.counter.set_text(_("Pages (%d)") % n if n else _("Preview"))
        description = self.document_card.get_children()[0].get_children()[0].get_children()[1]
        one_file = not self.doc_format()[3]
        description.set_text((_("%s, saved as a single file") if one_file or n == 1 else
                              _("%s, saved as separate files")) % page_count(n) if n else _("No pages yet"))
        if not n and not self.spinner.get_property("active"):
            self.status.set_text(_("Ready."))

    def selected(self):
        s = self.icons.get_selected_items()
        return s[0].get_indices()[0] if s else None

    def on_selection_changed(self, *_args):
        self.preview.queue_draw()

    def on_reorder(self, *_args):
        # After drag and drop the ListStore order is authoritative
        if len(self.items) != len(self.pages):
            return
        try:
            new = [self.pages[r[2]] for r in self.items]
        except IndexError:
            return
        self.pages = new
        GLib.idle_add(self.refresh_list, self.selected())

    def rotate(self, angle):
        i = self.selected()
        if i is None:
            return
        s = self.pages[i]
        s["pb"] = s["pb"].rotate_simple(angle)
        s["jpeg"] = pixbuf_jpeg(s["pb"])
        self.refresh_list(i)

    def move(self, direction):
        i = self.selected()
        if i is None or not 0 <= i + direction < len(self.pages):
            return
        self.pages[i], self.pages[i + direction] = self.pages[i + direction], self.pages[i]
        self.refresh_list(i + direction)

    def delete_page(self):
        i = self.selected()
        if i is None:
            return
        del self.pages[i]
        self.refresh_list(i)

    def clear_pages(self, _widget):
        n = len(self.pages)
        if n and not self.main_window.ask(_("Delete the scanned page?") if n == 1
                                          else _("Delete the %d scanned pages?") % n):
            return
        self.pages = []
        self.refresh_list()

    def on_draw(self, area, cr):
        g, y = area.get_allocated_width(), area.get_allocated_height()
        i = self.selected()
        if i is None and self.pages:
            i = len(self.pages) - 1
        if i is None:
            return
        pb = self.pages[i]["pb"]
        ratio = min((g - 40) / pb.get_width(), (y - 40) / pb.get_height())
        if ratio <= 0:
            return
        tw, th = int(pb.get_width() * ratio), int(pb.get_height() * ratio)
        scaled = pb.scale_simple(max(1, tw), max(1, th), GdkPixbuf.InterpType.BILINEAR)
        x0, y0 = (g - tw) // 2, (y - th) // 2
        for k, a in ((6, 0.05), (4, 0.07), (2, 0.10)):  # soft shadow
            cr.set_source_rgba(0, 0, 0, a)
            cr.rectangle(x0 - k + 2, y0 - k + 4, tw + 2 * k - 4, th + 2 * k - 4)
            cr.fill()
        Gdk.cairo_set_source_pixbuf(cr, scaled, x0, y0)
        cr.paint()

    # --- document options, save and print
    def doc_format(self):
        fid = self.file_format.get_active_id()
        return next((f for f in DOC_FORMATS if f[1] == fid), DOC_FORMATS[0])

    def on_doc_option(self, *_args):
        self.update_view()
        c, _profile = read_settings()
        c.set("client", "doc_format", self.file_format.get_active_id() or "pdf")
        c.set("client", "doc_quality", self.quality.get_active_id() or "original")
        c.set("client", "doc_name", self.file_name.get_text().strip())
        c.set("client", "skip_blank", "yes" if self.skip_blank.get_active() else "no")
        c.set("client", "open_after", "yes" if self.open_after.get_active() else "no")
        try:
            write_settings(c)
        except OSError as e:
            log.error("Could not write settings: %s", e)

    def output_pages(self):
        """Pages to save or print, and how many blank pages were left out."""
        pages = self.pages
        if self.skip_blank.get_active():
            pages = [p for p in self.pages if not is_blank(p["pb"])] or self.pages[:1]
        return pages, len(self.pages) - len(pages)

    def save(self, _widget, ask):
        if not self.pages:
            return
        _label, fid, ext, per_page = self.doc_format()
        name = safe_name(self.file_name.get_text(), _("Scanned document"))
        folder = self.main_window.scan_folder()
        os.makedirs(folder, exist_ok=True)
        path = core.unique_path(folder, name, ext)
        if ask:
            d = Gtk.FileChooserDialog(title=_("Save scan"), transient_for=self.main_window,
                                      action=Gtk.FileChooserAction.SAVE)
            d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.ACCEPT)
            d.set_do_overwrite_confirmation(True)
            d.set_current_folder(folder)
            d.set_current_name(os.path.basename(path))
            if d.run() != Gtk.ResponseType.ACCEPT:
                d.destroy()
                return
            path = d.get_filename()
            d.destroy()
        root = path[:-len(ext)] if path.lower().endswith(ext) else path
        pages, skipped = self.output_pages()
        quality = self.quality.get_active_id()
        try:
            outputs = [page_output(p, quality) for p in pages]
            if fid == "pdf":
                files = [(root + ext, core.jpegs_to_pdf([o[1] for o in outputs], outputs[0][2],
                                                        title=os.path.basename(root)))]
            else:
                names = [root + ext] if len(outputs) == 1 else ["%s_%d%s" % (root, i + 1, ext)
                                                                for i in range(len(outputs))]
                files = []
                for target, (pb, jpeg, dpi) in zip(names, outputs):
                    if fid == "pdfs":
                        data = core.jpegs_to_pdf([jpeg], dpi, title=os.path.basename(target)[:-len(ext)])
                    elif fid == "jpg":
                        data = jpeg
                    else:  # lossless formats: the (possibly reduced) image itself
                        ok, data = pb.save_to_bufferv("png" if fid == "png" else "tiff", [], [])
                        data = bytes(data)
                    files.append((target, data))
            for target, data in files:
                core.write_file(target, data)
        except (OSError, GLib.Error, ValueError) as e:
            log.error("Could not save: %s", e)
            self.main_window.warn(_("Could not save:\n%s") % e)
            return
        paths = [f[0] for f in files]
        size = sum(len(f[1]) for f in files)
        log.info("Saved: %s (%d pages, %s, %s, %d blank skipped, %d bytes)",
                 paths[0], len(pages), fid, quality, skipped, size)
        text = _("Saved: %s") % os.path.basename(paths[0])
        if len(paths) > 1:
            text = _("Saved %d files: %s …") % (len(paths), os.path.basename(paths[0]))
        text += " (%s)" % GLib.format_size(size)
        if skipped:
            text += "\n" + (_("1 blank page skipped") if skipped == 1 else _("%d blank pages skipped") % skipped)
        self.status.set_text(text)
        self.main_window.notify_user(_("Saved"), paths[0])
        if self.open_after.get_active():
            open_path(paths[0])

    def print_pages(self, _widget):
        if not self.pages:
            return
        pages, skipped = self.output_pages()
        op = Gtk.PrintOperation()
        op.set_n_pages(len(pages))
        op.set_job_name(safe_name(self.file_name.get_text(), _("Scanned document")))
        op.set_embed_page_setup(True)
        setup = Gtk.PageSetup()
        paper = {"A4": Gtk.PAPER_NAME_A4, "A5": Gtk.PAPER_NAME_A5, "Letter": Gtk.PAPER_NAME_LETTER,
                 "Legal": Gtk.PAPER_NAME_LEGAL}.get(self.paper.get_active_id())
        if paper:
            setup.set_paper_size(Gtk.PaperSize.new(paper))
        op.set_default_page_setup(setup)

        def draw_page(_op, context, n):
            pb = pages[n]["pb"]
            cw, ch = context.get_width(), context.get_height()
            ratio = min(cw / pb.get_width(), ch / pb.get_height())
            cr = context.get_cairo_context()
            cr.translate((cw - pb.get_width() * ratio) / 2, 0)
            cr.scale(ratio, ratio)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()

        op.connect("draw-page", draw_page)
        try:
            result = op.run(Gtk.PrintOperationAction.PRINT_DIALOG, self.main_window)
        except GLib.Error as e:
            log.error("Could not print: %s", e)
            self.main_window.warn(_("Could not print:\n%s") % e.message)
            return
        if result == Gtk.PrintOperationResult.APPLY:
            log.info("Sent to printer: %d pages (%d blank skipped)", len(pages), skipped)
            self.status.set_text(_("Sent to the printer: %s") % page_count(len(pages)))


# --------------------------------------------------------------------------
# Scan from panel (status + recent scans)
# --------------------------------------------------------------------------

class PanelPage(Gtk.Box):
    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=16)
        self.main_window = main_window

        # Status card
        self.icon = Gtk.Image()
        self.icon.set_pixel_size(48)
        self.title = make_label("", "pc-big-text")
        self.status = make_label("", "pc-dim", wrap=True)
        self.status.set_use_markup(True)
        label_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        label_text.pack_start(self.title, False, False, 0)
        label_text.pack_start(self.status, False, False, 0)
        self.key = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch_handler = self.key.connect("notify::active", self.on_switch_changed)
        top = Gtk.Box(spacing=16)
        top.pack_start(self.icon, False, False, 0)
        top.pack_start(label_text, True, True, 0)
        top.pack_start(self.key, False, False, 0)
        self.pack_start(card(None, top), False, False, 0)

        # How to use + recent scans
        steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.step_name = None
        for i, source in enumerate((
                N_("Place the document face down on the device's glass."),
                N_("On the device panel press <b>Scan</b> and choose <b>WSD</b> (Computer)."),
                N_("Select <b>%s</b> from the list and start."),
                N_("The document arrives in the save folder and you get a notification."))):
            text = _(source)
            row = Gtk.Box(spacing=12)
            no = styled(Gtk.Label(label=str(i + 1), width_chars=2), "pc-badge", "neutral")
            no.set_valign(Gtk.Align.START)
            m = make_label("", wrap=True)
            has_name = "%s" in source  # decided on the English source, not the translation
            m.set_markup(text % "…" if has_name else text)
            if has_name:
                self.step_name = (m, text)
            row.pack_start(no, False, False, 0)
            row.pack_start(m, True, True, 0)
            steps.pack_start(row, False, False, 0)
        howto = card(_("How to use"), steps)
        howto.set_size_request(300, -1)
        howto.set_valign(Gtk.Align.START)

        self.last = styled(Gtk.ListBox(), "pc-recent")
        self.last.set_selection_mode(Gtk.SelectionMode.NONE)
        self.last.set_activate_on_single_click(True)
        self.last.connect("row-activated", lambda lb, r: getattr(r, "path", None) and open_path(r.path))
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(self.last)
        open_folder = icon_button("folder-open-symbolic", _("Open folder"), "flat")
        open_folder.connect("clicked", lambda b: open_path(self.main_window.scan_folder()))
        recent_card = card(_("Recent scans"), scroller, description=_("Click to open"), right=open_folder)

        body = Gtk.Box(spacing=16)
        body.pack_start(howto, False, False, 0)
        body.pack_start(recent_card, True, True, 0)
        self.pack_start(body, True, True, 0)

        self.refresh()
        GLib.timeout_add_seconds(5, self.refresh)

    def on_switch_changed(self, key, _pspec):
        if key.get_active():
            if not self.main_window.device():
                self.main_window.warn(_("First enter the printer's IP address in Settings."))
                self.main_window.navigate("settings")
                GLib.idle_add(self.refresh)
                return
            code, output = systemctl("enable", "--now", SERVICE)
        else:
            code, output = systemctl("disable", "--now", SERVICE)
        log.info("Scan from panel %s", "enabled" if key.get_active() else "disabled")
        if code != 0:
            log.error("Could not change the service: %s", output)
            self.main_window.warn(_("Could not change the service:\n%s") % output)
        GLib.timeout_add(1500, self.refresh)

    def refresh(self):
        enabled = systemctl("is-active", SERVICE)[0] == 0
        self.key.handler_block(self.switch_handler)
        self.key.set_active(enabled)
        self.key.handler_unblock(self.switch_handler)
        d = core.read_state()
        c, _profile = read_settings()
        name_ = ", ".join(d.get("names") or [c.get("client", "name", fallback="")
                                           or socket.gethostname().upper()[:15]])
        name = GLib.markup_escape_text(name_)
        if self.step_name:
            self.step_name[0].set_markup(self.step_name[1] % name)
        if not enabled:
            icon, title = "network-offline-symbolic", _("Scan from panel is off")
            label_text = _("When turned on, this computer appears on the device panel as <b>%s</b>.") % name
            level = "neutral"
        elif d.get("registered"):
            icon, title, level = "emblem-ok-symbolic", _("Scan from panel is ready"), "ok"
            label_text = _("This computer appears on the device panel as <b>%s</b>.") % name
            if d.get("error"):
                label_text += "\n<small>%s</small>" % (_("Last warning: %s")
                                                       % GLib.markup_escape_text(d["error"]))
        else:
            icon, title, level = "dialog-warning-symbolic", _("Could not register with the device"), "warning"
            label_text = GLib.markup_escape_text(d.get("error") or _("Connecting…"))
        self.icon.set_from_icon_name(icon, Gtk.IconSize.DIALOG)
        self.title.set_text(title)
        self.status.set_markup(label_text)
        self.main_window.set_panel_status(level)
        self.recent_list(d.get("recent_scans", []))
        return True

    def recent_list(self, records):
        signature = tuple(k.get("path") for k in records)
        if getattr(self, "_signature", None) == signature:
            return
        self._signature = signature
        for r in self.last.get_children():
            self.last.remove(r)
        if not records:
            empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=24)
            r = styled(Gtk.Image.new_from_icon_name("folder-documents-symbolic",
                                                   Gtk.IconSize.DIALOG), "pc-dim")
            r.set_pixel_size(48)
            empty.pack_start(r, False, False, 0)
            empty.pack_start(make_label(_("No scans from the panel yet."), "pc-dim", xalign=0.5),
                           False, False, 0)
            row = Gtk.ListBoxRow(activatable=False)
            row.add(empty)
            self.last.add(row)
        for k in records:
            r = Gtk.ListBoxRow()
            r.path = k["path"]
            box = Gtk.Box(spacing=12, margin=8)
            box.pack_start(Gtk.Image.new_from_icon_name(
                "application-pdf" if r.path.endswith(".pdf") else "image-x-generic",
                Gtk.IconSize.DND), False, False, 0)
            label_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            label_text.pack_start(make_label(os.path.basename(r.path),
                                   ellipsize=Pango.EllipsizeMode.END), False, False, 0)
            timestamp = dt.datetime.fromtimestamp(k.get("time", 0)).strftime(date_format())
            label_text.pack_start(make_label(timestamp, "pc-dim"), False, False, 0)
            box.pack_start(label_text, True, True, 0)
            r.add(box)
            r.set_sensitive(os.path.exists(r.path))
            self.last.add(r)
        self.last.show_all()


# --------------------------------------------------------------------------
# Device status
# --------------------------------------------------------------------------

class DevicePage(Gtk.ScrolledWindow):
    """Detailed device status: faults, supplies, maintenance, trays, counters, information."""

    def __init__(self, main_window):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.main_window = main_window
        self.previous = None
        self._running = False
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=16)
        self.add(inner)

        # --- Top summary
        icon = Gtk.Image.new_from_icon_name("printer", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(80)
        self.model = make_label("—", "pc-page-title", ellipsize=Pango.EllipsizeMode.END)
        self.footer = make_label("", "pc-dim", selectable=True)
        self.badge = styled(Gtk.Label(label=_("Unknown")), "pc-badge", "neutral")
        self.display_text = make_label("", "pc-dim", ellipsize=Pango.EllipsizeMode.END)
        rs = Gtk.Box(spacing=10)
        rs.pack_start(self.badge, False, False, 0)
        rs.pack_start(self.display_text, True, True, 0)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        for p in (self.model, self.footer, rs):
            info.pack_start(p, False, False, 0)
        self.toner = styled(Gtk.LevelBar(min_value=0, max_value=100), "pc-toner")
        for name, value in (("low", 10), ("high", 25), ("full", 100)):
            self.toner.add_offset_value(name, value)
        self.toner.set_size_request(230, -1)
        self.toner_label = make_label("—", "pc-big-text", xalign=1)
        self.toner_estimate = make_label("", "pc-dim")
        toner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        tb = Gtk.Box()
        tb.pack_start(make_label(_("Black toner"), "pc-dim"), True, True, 0)
        tb.pack_start(self.toner_label, False, False, 0)
        for p in (tb, self.toner, self.toner_estimate):
            toner_box.pack_start(p, False, False, 0)
        top = Gtk.Box(spacing=20)
        top.pack_start(icon, False, False, 0)
        top.pack_start(info, True, True, 0)
        top.pack_start(toner_box, False, False, 0)
        inner.pack_start(card(None, top), False, False, 0)

        # --- Faults
        self.last_check = make_label("", "pc-dim")
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text(_("Refresh now"))
        refresh.connect("clicked", lambda b: self.refresh(force=True))
        right = Gtk.Box(spacing=8)
        right.pack_start(self.last_check, False, False, 0)
        right.pack_start(refresh, False, False, 0)
        self.problems = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.pack_start(card(_("Warnings and faults"), self.problems,
                              description=_("Paper jam, cover, toner, tray and service alerts"), right=right),
                      False, False, 0)

        # --- Security (only when there are findings)
        self.security = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.security_card = card(_("Security"), self.security)
        self.security_card.set_no_show_all(True)
        inner.pack_start(self.security_card, False, False, 0)

        # --- Supplies + trays
        self.supplies = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.tray = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        row = Gtk.Box(spacing=16, homogeneous=False)
        row.pack_start(card(_("Supplies and maintenance"), self.supplies), True, True, 0)
        tray_card = card(_("Paper trays"), self.tray)
        tray_card.set_size_request(240, -1)
        tray_card.set_valign(Gtk.Align.START)
        row.pack_start(tray_card, False, False, 0)
        inner.pack_start(row, False, False, 0)

        # --- Counters
        self.counters = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=12,
                                    row_spacing=12, homogeneous=True, max_children_per_line=6)
        inner.pack_start(card(_("Counters"), self.counters,
                              description=_("Over the device's lifetime (all computers)")),
                      False, False, 0)

        # --- Information
        self.info = Gtk.Grid(column_spacing=24, row_spacing=6)
        inner.pack_start(card(_("Device information"), self.info), False, False, 0)

        # --- Tools
        buttons = Gtk.Box(spacing=8)
        for icon_, label_text, func in (
                ("web-browser-symbolic", _("Web management"), lambda b: self.web()),
                ("printer-symbolic", _("Printer settings"), lambda b: self.printers()),
                ("edit-copy-symbolic", _("Copy report"), lambda b: self.copy_report()),
                ("emblem-system-symbolic", _("Connection settings"),
                 lambda b: self.main_window.navigate("settings"))):
            d = icon_button(icon_, label_text)
            d.connect("clicked", func)
            buttons.pack_start(d, False, False, 0)
        inner.pack_start(card(_("Tools"), buttons), False, False, 0)

        self.report = None
        self.refresh(force=True)
        GLib.timeout_add_seconds(30, self.refresh)

    # --- data
    def refresh(self, force=False, notify=False):
        device = self.main_window.device()
        if not device:
            self.render(None, _("No printer address entered"), False)
            return True
        if self._running:
            return True
        self._running = True

        def work():
            try:
                # Window hidden (tray): reuse the background service's report (it polls every minute)
                # instead of querying the device a second time.
                r = None
                if not force and not self.main_window.get_visible():
                    cached = core.read_state().get("device_report") or {}
                    if time.time() - cached.get("time", 0) < 150:
                        if cached.get("access_error"):
                            raise OSError(cached["access_error"])
                        if "problems" in cached:
                            r = cached
                if r is None:
                    r = core.build_device_report(device)
                if not r.get("model") and r["source_errors"]:
                    raise OSError("; ".join(r["source_errors"].values())[:200])
                GLib.idle_add(self.render, r, None, notify)
            except Exception as e:
                GLib.idle_add(self.render, None, e, notify)

        threading.Thread(target=work, daemon=True).start()
        return True

    def render(self, r, error, notify):
        self._running = False
        previous_error, self._last_error_msg = getattr(self, "_last_error_msg", None), bool(error)
        if error and previous_error is not True:
            log.warning("Device unreachable: %s", error)
        elif not error and previous_error is True:
            log.info("Device reachable again")
        self.last_check.set_text(_("Last check %s") % dt.datetime.now().strftime("%H:%M:%S"))
        if error:
            self.badge.set_text(_("Unreachable"))
            swap_class(self.badge, ("ok", "warning", "error", "neutral"), "error")
            reason = error_text(error)
            self.display_text.set_text(reason[:1].upper() + reason[1:])
            self.main_window.show_device_status(None, _("Unreachable"), "error")
            self.render_problems([{"title": _("Cannot reach the device"), "level": "error", "source": "", "retry": True,
                                   "advice": _("Check that the device is on and connected to the network, "
                                               "and the IP address in Settings. (%s)") % reason}])
            if notify:
                self.main_window.warn(_("Could not reach the device:\n%s") % error_text(error))
            return
        self.report = r
        self.log_event(r)
        self.model.set_text(r.get("model") or _("Scanner"))
        self.footer.set_text(" · ".join(x for x in (r.get("name"), r.get("ip"),
                                                 r.get("serial") and "S/N " + r["serial"]) if x))
        n = len(r["problems"])
        badge = {"ok": _("No problems"), "warning": (_("%d warning") if n == 1 else _("%d warnings")) % n,
                 "error": _("Fault")}[r["level"]]
        self.badge.set_text(badge)
        swap_class(self.badge, ("ok", "warning", "error", "neutral"), r["level"])
        self.display_text.set_text(_("Panel: %s") % " · ".join(r["display"]) if r.get("display") else "")
        toner = next((s for s in r["supplies"] if "toner" in s["name"].lower()), None)
        if toner and isinstance(toner.get("percent"), int):
            self.toner.set_value(toner["percent"])
            self.toner_label.set_text(_("%d%%") % toner["percent"])
            self.toner_estimate.set_text(self.estimate_label(toner))
        self.render_problems(r["problems"])
        self.render_security(r.get("security") or [])
        self.render_supplies(r["supplies"], r["maintenance"])
        self.render_trays(r["trays"])
        self.render_counters(r["counters"])
        self.render_info(r["info"])
        status = r["problems"][0]["title"] if r["problems"] else (r.get("status") or _("Ready"))
        self.main_window.show_device_status(r.get("model"), status, r["level"])
        if notify:
            self.main_window.notify_user(_("Connection successful"), r.get("model") or "")

    def log_event(self, r):
        """Log new faults; if the service is not running also send a desktop notification."""
        now = core.problem_signature(r)
        new, resolved = core.problem_diff(self.previous, now)
        service_active = systemctl("is-active", SERVICE)[0] == 0
        for s in new:
            if not service_active:  # when the service runs, it logs and notifies
                log.warning("Device problem: %s (%s)", s["title"], s["source"])
                self.main_window.notify_user(_("Printer: %s") % s["title"], s.get("advice") or "")
        for s in resolved:
            if not service_active:
                log.info("Problem resolved: %s", s["title"])
        self.previous = now

    @staticmethod
    def estimate_label(t):
        if t.get("days_left") is None:
            return _("%s left") % page_count(t["pages_left"]) if t.get("pages_left") else ""
        g = t["days_left"]
        if g < 60:
            duration = (_("%d day") if g == 1 else _("%d days")) % g
        else:
            duration = _("%d months") % round(g / 30)
        return _("≈ %s · lasts %s") % (page_count(t["pages_left"]), duration)

    # --- drawing helpers
    @staticmethod
    def clear_children(container):
        for c in container.get_children():
            container.remove(c)

    def retry(self, button):
        button.set_sensitive(False)  # the row is redrawn when the check finishes (a few seconds)
        self.refresh(force=True)

    def render_problems(self, problems):
        self.clear_children(self.problems)
        if not problems:
            s = Gtk.Box(spacing=12)
            im = styled(Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.DND), "pc-icon-ok")
            s.pack_start(im, False, False, 0)
            k = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            k.pack_start(make_label(_("No problems"), "pc-card-title"), False, False, 0)
            k.pack_start(make_label(_("The device is working normally; no active warnings or faults "
                                      "were reported."), "pc-dim"),
                         False, False, 0)
            s.pack_start(k, True, True, 0)
            self.problems.pack_start(s, False, False, 0)
        for x in problems:
            s = Gtk.Box(spacing=12)
            im = Gtk.Image.new_from_icon_name(
                "dialog-error-symbolic" if x["level"] == "error" else "dialog-warning-symbolic",
                Gtk.IconSize.DND)
            styled(im, "pc-icon-" + x["level"])
            im.set_valign(Gtk.Align.START)
            s.pack_start(im, False, False, 0)
            k = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            k.pack_start(make_label(x["title"], "pc-card-title", wrap=True), False, False, 0)
            if x.get("advice"):
                k.pack_start(make_label(x["advice"], "pc-dim", wrap=True), False, False, 0)
            s.pack_start(k, True, True, 0)
            if x.get("source"):
                s.pack_start(styled(Gtk.Label(label=x["source"], valign=Gtk.Align.START), "pc-badge",
                                   x["level"]), False, False, 0)
            if x.get("retry"):
                b = icon_button("view-refresh-symbolic", _("Try again"))
                b.set_valign(Gtk.Align.CENTER)
                b.connect("clicked", lambda _b: self.retry(_b))
                s.pack_start(b, False, False, 0)
            self.problems.pack_start(s, False, False, 0)
        self.problems.show_all()

    def render_security(self, findings):
        self.clear_children(self.security)
        for title, description in findings:
            s = Gtk.Box(spacing=12)
            im = styled(Gtk.Image.new_from_icon_name("security-low-symbolic", Gtk.IconSize.DND), "pc-icon-warning")
            im.set_valign(Gtk.Align.START)
            s.pack_start(im, False, False, 0)
            k = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            k.pack_start(make_label(title, "pc-card-title", wrap=True), False, False, 0)
            k.pack_start(make_label(description, "pc-dim", wrap=True), False, False, 0)
            s.pack_start(k, True, True, 0)
            self.security.pack_start(s, False, False, 0)
        if findings:
            self.security_card.set_no_show_all(False)
            self.security_card.show_all()
        else:
            self.security_card.hide()

    def usage_bar(self, name, percent, right_text, detail=()):
        k = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top = Gtk.Box()
        top.pack_start(make_label(name, "pc-card-title"), True, True, 0)
        top.pack_start(make_label(right_text, "pc-card-title" if percent is not None else "pc-dim"), False, False, 0)
        k.pack_start(top, False, False, 0)
        if percent is not None:
            c = styled(Gtk.LevelBar(min_value=0, max_value=100, value=max(0, min(100, percent))), "pc-toner")
            for a, d in (("low", 10), ("high", 25), ("full", 100)):
                c.add_offset_value(a, d)
            k.pack_start(c, False, False, 0)
        if detail:
            g = Gtk.Grid(column_spacing=16, row_spacing=2)
            for i, (e, v) in enumerate(detail):
                g.attach(make_label(e, "pc-dim"), (i % 2) * 2, i // 2, 1, 1)
                g.attach(make_label(v, selectable=True), (i % 2) * 2 + 1, i // 2, 1, 1)
            k.pack_start(g, False, False, 0)
        return k

    def render_supplies(self, supplies, maintenance):
        self.clear_children(self.supplies)
        for t in supplies:
            detail = []
            if t.get("part_no"):
                detail.append((_("Part no."), t["part_no"]))
            if t.get("serial"):
                detail.append((_("Serial no."), t["serial"]))
            if t.get("capacity"):
                detail.append((_("Capacity"), page_count(t["capacity"])))
            if t.get("printed") is not None:
                detail.append((_("Printed with this cartridge"), page_count(t["printed"])))
            if t.get("installed_on"):
                detail.append((_("Installed on"),
                               dt.date.fromisoformat(t["installed_on"]).strftime(date_format(0))))
            if t.get("pages_per_day"):
                detail.append((_("Daily average"), _("%.1f pages") % t["pages_per_day"]))
            if t.get("pages_left") is not None:
                detail.append((_("Estimated remaining"), self.estimate_label(t).lstrip("≈ ")))
            percent = t.get("percent") if isinstance(t.get("percent"), int) else None
            self.supplies.pack_start(self.usage_bar(t["name"], percent, _("%d%%") % percent if percent is not None else "",
                                            detail), False, False, 0)
        if maintenance:
            self.supplies.pack_start(Gtk.Separator(), False, False, 0)
            self.supplies.pack_start(make_label(_("Maintenance parts (remaining life)"), "pc-dim"), False, False, 0)
        for b in maintenance:
            if b["lifetime"]:
                right = _("%d%%") % b["percent"]
                details = ((_("Usage"), _("%s / %s pages") % (fmt_number(b["count"]), fmt_number(b["lifetime"]))),)
            else:
                right, details = page_count(b["count"]), ()
            self.supplies.pack_start(self.usage_bar(b["name"], b["percent"], right, details), False, False, 0)
        self.supplies.show_all()

    def render_trays(self, trays):
        self.clear_children(self.tray)
        if not trays:
            self.tray.pack_start(make_label(_("No information"), "pc-dim"), False, False, 0)
        for t in trays:
            s = Gtk.Box(spacing=10)
            im = Gtk.Image.new_from_icon_name("document-new-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
            s.pack_start(im, False, False, 0)
            k = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            k.pack_start(make_label(t["name"], "pc-card-title"), False, False, 0)
            k.pack_start(make_label(" · ".join(x for x in (t["paper"], t["capacity"] and _("%s sheets") % t["capacity"]) if x),
                                "pc-dim"), False, False, 0)
            s.pack_start(k, True, True, 0)
            s.pack_start(styled(Gtk.Label(label=t["level"], valign=Gtk.Align.CENTER), "pc-badge",
                               "error" if t["neutral"] else "ok"), False, False, 0)
            self.tray.pack_start(s, False, False, 0)
        self.tray.show_all()

    def render_counters(self, counters):
        self.clear_children(self.counters)
        for name, value in counters:
            k = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            k.pack_start(make_label(fmt_number(value), "pc-page-title"), False, False, 0)
            k.pack_start(make_label(name, "pc-dim"), False, False, 0)
            self.counters.add(k)
        self.counters.show_all()

    def render_info(self, info):
        self.clear_children(self.info)
        for i, (k, v) in enumerate(info.items()):
            self.info.attach(make_label(k, "pc-dim"), (i % 2) * 2, i // 2, 1, 1)
            self.info.attach(make_label(str(v), selectable=True, ellipsize=Pango.EllipsizeMode.END,
                                     hexpand=True), (i % 2) * 2 + 1, i // 2, 1, 1)
        self.info.show_all()

    # --- actions
    def copy_report(self):
        r = self.report
        if not r:
            return
        rows = [_("Device report — %s") % dt.datetime.now().strftime(date_format()),
                _("Status: %s (%s)") % (r.get("status"), r["level"])]
        rows += [_("Problem: %s — %s") % (s["title"], s.get("advice", "")) for s in r["problems"]] \
            or [_("No problems")]
        for t in r["supplies"]:
            rows.append(_("%s: %s%% (%s)") % (t["name"], t.get("percent"), self.estimate_label(t)))
        for b in r["maintenance"]:
            rows.append(_("%s: %s / %s pages") % (b["name"], b["count"], b["lifetime"] or "?"))
        rows += ["%s: %s" % kv for kv in r["counters"]]
        rows += ["%s: %s" % kv for kv in r["info"].items()]
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("\n".join(rows), -1)
        self.main_window.notify_user(_("Device report copied"), _("Copied to the clipboard."))

    def web(self):
        ip = ip_from_address(self.main_window.device())
        if ip:
            open_path("http://%s/" % ip)

    def printers(self):
        for cmd in (["system-config-printer"], ["xdg-open", "http://localhost:631/printers/"]):
            try:
                subprocess.Popen(cmd)
                return
            except OSError:
                continue


# --------------------------------------------------------------------------
# Settings (printer IP + scan-from-panel preferences)
# --------------------------------------------------------------------------

class SettingsPage(Gtk.ScrolledWindow):
    def __init__(self, main_window):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.main_window = main_window
        c, profile = read_settings()
        p = c[profile]

        # Printer connection
        self.ip = Gtk.Entry(text=ip_from_address(c.get("device", "address", fallback="")),
                            placeholder_text=_("e.g. 192.168.1.50"))
        self.ip.connect("activate", lambda e: self.test_connection(None))
        test_connection = icon_button("network-transmit-receive-symbolic", _("Test connection"))
        test_connection.connect("clicked", self.test_connection)
        row = Gtk.Box(spacing=8)
        row.pack_start(self.ip, True, True, 0)
        row.pack_start(test_connection, False, False, 0)
        self.test_result = make_label("", "pc-dim", wrap=True)
        ctx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ctx.pack_start(form([(_("Printer IP address"), row)]), False, False, 0)
        ctx.pack_start(self.test_result, False, False, 0)
        printer_card = card(_("Printer / scanner"), ctx,
                            description=_("IP address of the multifunction device on the network "
                                          "(WSD scanning, port 8018)"))

        # Scan from panel
        self.name_entry = Gtk.Entry(max_length=15, text=c.get("client", "name", fallback="")
                            or socket.gethostname().upper()[:15])
        self.folder = Gtk.FileChooserButton(title=_("Save folder"),
                                            action=Gtk.FileChooserAction.SELECT_FOLDER)
        folder = os.path.expanduser((c.get("client", "folder", fallback="").strip() or "~/" + _("Scans")))
        os.makedirs(folder, exist_ok=True)
        self.folder.set_filename(folder)
        self.color = combo([(_(e), v) for e, v in COLOR_CHOICES], p.get("color", "color"))
        self.dpi = combo([("%d dpi" % d, d) for d in core.RESOLUTIONS], p.get("resolution", "300"))
        self.paper = combo([(k, k) for k in core.PAPER_SIZES], p.get("paper", "A4"))
        self.file_format = combo(FORMATS, p.get("format", "pdf"))
        self.notify_user = Gtk.Switch(halign=Gtk.Align.START)
        self.notify_user.set_active(c.getboolean("client", "desktop_notifications", fallback=True))
        panel_card = card(_("Scan from panel to PC"), form([
            (_("Name shown on the panel"), self.name_entry), (_("Save folder"), self.folder),
            (_("Color"), self.color), (_("Resolution"), self.dpi), (_("Paper"), self.paper),
            (_("File format"), self.file_format), (_("Desktop notification"), self.notify_user)]),
            description=_("For documents sent from the device panel with Scan → WSD"))

        # Appearance
        self.language = combo([(_(label) if code == "auto" else label, code) for code, label in i18n.LANGUAGES],
                              c.get("client", "language", fallback="auto"))
        appearance_card = card(_("Appearance"), form([(_("Language"), self.language)]),
                               description=_("Interface language; takes effect after restarting"))

        save = icon_button("document-save-symbolic", _("Save and apply"), "suggested-action")
        save.set_halign(Gtk.Align.END)
        save.connect("clicked", self.save)

        update_card = self.build_update_card()

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=24,
                     halign=Gtk.Align.CENTER)
        inner.set_size_request(620, -1)
        for widget in (printer_card, panel_card, appearance_card, save, update_card):
            inner.pack_start(widget, False, False, 0)
        self.add(inner)
        self.render_update()
        GLib.timeout_add_seconds(600, lambda: self.render_update() or True)

    # ---- Updates -------------------------------------------------------------

    def build_update_card(self):
        self.upd_badge = styled(Gtk.Label(label="…"), "pc-badge", "neutral")
        self.upd_version = make_label(core.VERSION)
        self.upd_last = make_label("—", "pc-dim")
        self.upd_message = make_label("", "pc-dim", wrap=True, selectable=True)
        self.upd_check_btn = icon_button("view-refresh-symbolic", _("Check for updates"))
        self.upd_check_btn.connect("clicked", self.check_for_updates)
        self.upd_restart_btn = icon_button("system-reboot-symbolic", _("Restart with the new version"),
                                       "suggested-action")
        self.upd_restart_btn.connect("clicked", lambda _b: self.main_window.relaunch())
        self.upd_restart_btn.set_no_show_all(True)
        self.upd_install_btn = icon_button("software-update-available-symbolic", _("Install now"),
                                           "suggested-action")
        self.upd_install_btn.connect("clicked", lambda b: self.check_for_updates(b, core.INSTALL_SERVICE))
        self.upd_install_btn.set_no_show_all(True)
        self.upd_spinner = Gtk.Spinner()
        buttons = Gtk.Box(spacing=8)
        for p in (self.upd_check_btn, self.upd_install_btn, self.upd_spinner, self.upd_restart_btn):
            buttons.pack_start(p, False, False, 0)
        c, _profile = read_settings()
        self.upd_auto = Gtk.CheckButton(label=_("Install updates automatically"))
        self.upd_auto.set_active(self.auto_update_enabled(c))
        self.upd_auto.set_tooltip_text(_("When off, new versions are only announced; you decide when to "
                                         "install them."))
        self.upd_auto.connect("toggled", self.on_auto_update_toggled)
        return card(_("About and updates"),
                    form([(_("Version"), self.upd_version), (_("Last check"), self.upd_last)]),
                    self.upd_auto, self.upd_message, buttons,
                    description=_("New versions are checked for in the repository every day"),
                    right=self.upd_badge)

    @staticmethod
    def auto_update_enabled(c):
        try:
            return c.getboolean("client", "auto_update", fallback=True)
        except ValueError:
            return True

    def on_auto_update_toggled(self, button):
        c, _profile = read_settings()
        if not c.has_section("client"):
            c.add_section("client")
        c["client"]["auto_update"] = "yes" if button.get_active() else "no"
        write_settings(c)
        log.info("Automatic update installation %s", "on" if button.get_active() else "off")
        self.render_update()

    def render_update(self):
        d = read_update_state()
        on_disk = installed_version()
        newer_installed = version_tuple(on_disk) > version_tuple(core.VERSION)
        self.upd_version.set_text(core.VERSION + ("  " + _("(installed: %s)") % on_disk if newer_installed else ""))
        self.upd_last.set_text(format_date(d.get("last_check")) or _("Not checked yet"))
        status = d.get("status")
        if newer_installed:
            badge, level = _("Restart needed"), "warning"
            message = _("Version %s has been installed. The program restarts with it by itself "
                        "once no scan is in progress and the scanned pages are saved.") % on_disk
        elif status == "error":
            badge, level, message = _("Error"), "error", _("Last check failed: %s") % d.get("error", "")
        elif status == "not_configured" or (not d and not os.path.exists("/etc/printer-center")):
            badge, level, message = (_("Off"), "neutral",
                                     _("Automatic updates are not configured on this computer."))
        elif status == "update_available":
            if self.upd_auto.get_active():
                text = _("Version %s is available in the repository; it will be installed at the next daily check.")
            else:
                text = _("Version %s is available. Press \"Install now\" to install it.")
            badge, level, message = _("New version"), "warning", text % (d.get("latest") or "").lstrip("v")
        elif status in ("checking", "installing"):
            badge, level, message = _("In progress"), "neutral", _("Checking for updates…")
        elif d:
            badge, level, message = _("Up to date"), "ok", _("You are using the latest version.")
        else:
            badge, level, message = _("Unknown"), "neutral", _("No update check has been made yet.")
        history = d.get("history") or []
        if history:
            message += "\n" + _("Last installation: %s — %s") % (format_date(history[0].get("time")),
                                                               history[0].get("event", ""))
        self.upd_badge.set_text(badge)
        swap_class(self.upd_badge, ("ok", "warning", "error", "neutral"), level)
        self.upd_message.set_text(message)
        self.upd_restart_btn.set_visible(newer_installed)
        self.upd_install_btn.set_visible(status == "update_available" and not newer_installed)
        return False

    def check_for_updates(self, _widget, unit=core.UPDATE_SERVICE):
        self.upd_check_btn.set_sensitive(False)
        self.upd_install_btn.set_sensitive(False)
        self.upd_spinner.start()
        self.upd_message.set_text(_("Installing the new version…") if unit == core.INSTALL_SERVICE
                                  else _("Checking the repository…"))

        def work():
            try:
                # System unit: no password with the polkit rule, otherwise asks for the admin password
                r = subprocess.run(["systemctl", "start", unit],
                                   capture_output=True, text=True, timeout=900)
                error = (r.stderr or r.stdout).strip() if r.returncode else ""
            except Exception as e:
                error = str(e)
            if error:
                log.warning("Could not start the update check: %s", error)
            log.info("Update checked: %s", read_update_state().get("status"))

            def done():
                self.upd_spinner.stop()
                self.upd_check_btn.set_sensitive(True)
                self.upd_install_btn.set_sensitive(True)
                self.render_update()
                if error and read_update_state().get("status") != "error":
                    self.upd_message.set_text(_("Could not start the check: %s") % error.splitlines()[-1])
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def save_ip(self):
        address = device_address(self.ip.get_text())
        c, _profile = read_settings()
        old = c.get("device", "address", fallback="")
        c["device"]["address"] = address
        write_settings(c)
        return address, old != address

    def test_connection(self, _widget):
        address, _changed = self.save_ip()
        if not address:
            self.test_result.set_text(_("Enter an IP address."))
            return
        self.test_result.set_text(_("Connecting…"))

        def work():
            try:
                d = core.device_status(address)
                m = _("✓ Connected: %s (%s)") % (d.get("model") or _("device"), d.get("status") or "")
            except Exception as e:
                m = _("✗ Could not connect: %s") % error_text(e)
                log.warning("Connection test failed (%s): %s", ip_from_address(address), e)
            GLib.idle_add(self.test_result.set_text, m)
            GLib.idle_add(lambda: self.main_window.device_page.refresh(force=True) and False)

        threading.Thread(target=work, daemon=True).start()

    def save(self, _widget):
        address, changed = self.save_ip()
        if not address:
            self.main_window.warn(_("Enter the printer's IP address."))
            return
        c, profile = read_settings()
        old_language = c.get("client", "language", fallback="auto")
        c["client"]["name"] = self.name_entry.get_text().strip().upper()[:15]
        c["client"]["folder"] = self.folder.get_filename() or "~/" + _("Scans")
        c["client"]["desktop_notifications"] = "yes" if self.notify_user.get_active() else "no"
        c["client"]["language"] = self.language.get_active_id() or "auto"
        c["client"].setdefault("port", "53580")
        c[profile]["name"] = ""
        c[profile]["color"] = self.color.get_active_id()
        c[profile]["resolution"] = self.dpi.get_active_id()
        c[profile]["paper"] = self.paper.get_active_id()
        c[profile]["format"] = self.file_format.get_active_id()
        write_settings(c)
        log.info("Settings saved (device %s, panel name %s)", ip_from_address(address), c["client"]["name"])
        if systemctl("is-active", SERVICE)[0] == 0:
            systemctl("restart", SERVICE)
        self.main_window.device_page.refresh(force=True)
        self.main_window.notify_user(_("Settings saved"), _("Name shown on the panel: %s")
                                     % (c["client"]["name"] or socket.gethostname().upper()))
        if c["client"]["language"] != old_language and self.main_window.ask(
                _("The language change takes effect after restarting. Restart now?")):
            self.main_window.relaunch()


# --------------------------------------------------------------------------
# Print queue
# --------------------------------------------------------------------------

JOB_ICON = {3: "alarm-symbolic", 4: "media-playback-pause-symbolic",
            5: "document-print-symbolic", 6: "dialog-warning-symbolic",
            7: "process-stop-symbolic", 8: "dialog-error-symbolic", 9: "emblem-ok-symbolic"}
JOB_LEVEL = {3: "neutral", 4: "warning", 5: "ok", 6: "error", 7: "neutral", 8: "error", 9: "ok"}
SCAN_STATES = {"waiting": (N_("Waiting"), "neutral"), "busy": (N_("Device busy"), "warning"),
               "scanning": (N_("Scanning"), "ok")}
PRINTER_STATES = {3: (N_("Idle"), "ok"), 4: (N_("Printing"), "ok"), 5: (N_("Stopped"), "error")}


class QueuePage(Gtk.ScrolledWindow):
    def __init__(self, main_window):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.main_window = main_window
        self.queue = None  # (name, cups_uri)
        self._running = False
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=16)

        # Scan queue: this window's scan + scans requested from the device panel (service)
        self.scans = styled(Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE), "pc-job")
        self.scan_count = 0
        inner.pack_start(card(_("Scan queue"), self.scans,
                              description=_("Scans waiting for the device, from this computer and from "
                                            "the device panel; one is scanned at a time")), False, False, 0)

        # Local queue status
        self.queue_badge = styled(Gtk.Label(label="…"), "pc-badge", "neutral")
        self.start_btn = icon_button("media-playback-start-symbolic", _("Start queue"))
        self.start_btn.connect("clicked", self.start_queue)
        self.start_btn.set_no_show_all(True)
        self.cancel_all_btn = icon_button("edit-clear-all-symbolic", _("Cancel all"), "flat")
        self.cancel_all_btn.connect("clicked", self.cancel_all)
        right = Gtk.Box(spacing=8)
        for p in (self.queue_badge, self.start_btn, self.cancel_all_btn):
            right.pack_start(p, False, False, 0)
        self.local = styled(Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE), "pc-job")
        self.local_card = card(_("Print jobs from this computer"), self.local,
                               description=_("Pending and recently completed print jobs"), right=right)
        inner.pack_start(self.local_card, False, False, 0)

        self.device = styled(Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE), "pc-job")
        self.device_card = card(_("Print jobs on the device"), self.device,
                                description=_("Jobs from all computers on the network that the device "
                                              "is processing now"))
        inner.pack_start(self.device_card, False, False, 0)
        self.add(inner)
        self.refresh()
        GLib.timeout_add_seconds(3, self.refresh)

    # --- data
    def refresh(self, force=False):
        self.render_scans()
        if self._running or not (force or self.get_mapped() or not getattr(self, "_first", False)):
            return True
        self._first = True
        device = self.main_window.device()
        if not device:
            return True
        self._running = True

        def work():
            result = {"queue": None, "printer": None, "local": [], "history": [], "device": [],
                     "local_error": None, "device_error": None}
            try:
                queues = core.local_queues(device)
                if queues:
                    name, uri = queues[0]
                    result["queue"] = (name, uri)
                    _status, groups = core.ipp_request(uri, 0x000B, [(0x44, "requested-attributes", [
                        "printer-state", "printer-state-message", "printer-is-accepting-jobs"])])
                    result["printer"] = next((g for g in groups if g.get("_group") == 4), {})
                    result["local"] = core.ipp_jobs(uri, "not-completed")
                    result["history"] = core.ipp_jobs(uri, "completed", limit=10)
            except Exception as e:
                result["local_error"] = error_text(e)
            try:
                result["device"] = core.ipp_jobs(core.device_ipp_uri(device), "not-completed")
            except Exception as e:
                result["device_error"] = error_text(e)
            GLib.idle_add(self.render, result)

        threading.Thread(target=work, daemon=True).start()
        return True

    def render(self, s):
        self._running = False
        self.queue = s["queue"]
        y = s["printer"] or {}
        level = "warning"
        if not self.queue:
            self.queue_badge.set_text(_("No queue"))
            swap_class(self.queue_badge, ("ok", "warning", "error", "neutral"), "warning")
        else:
            label_text, level = PRINTER_STATES.get(y.get("printer-state"), (N_("Unknown"), "neutral"))
            if y.get("printer-is-accepting-jobs") is False:
                label_text, level = N_("Not accepting jobs"), "error"
            self.queue_badge.set_text(_(label_text))
            self.queue_badge.set_tooltip_text("%s\n%s" % (self.queue[0], y.get("printer-state-message") or ""))
            swap_class(self.queue_badge, ("ok", "warning", "error", "neutral"), level)
        self.start_btn.set_visible(bool(self.queue) and level == "error")
        self.cancel_all_btn.set_sensitive(bool(s["local"]))

        if s["local_error"]:
            empty = _("Could not read the queue: %s") % s["local_error"]
        elif not self.queue:
            empty = _("No printer queue is set up for this device.")
        else:
            empty = _("No pending print jobs.")
        self.populate(self.local, s["local"], s["history"], empty, local=True)
        empty = (_("Could not reach the device: %s") % s["device_error"]) if s["device_error"] \
            else _("No pending jobs on the device.")
        self.populate(self.device, s["device"], [], empty, local=False)
        self.print_count = len(s["local"]) + len(s["device"])
        self.main_window.set_queue_count(self.print_count + self.scan_count)

    # --- scan queue
    def render_scans(self):
        d = core.read_state()
        jobs = [dict(j) for j in ([self.main_window.scan_page.job] if self.main_window.scan_page.job else [])]
        if d.get("running", True):  # stale entries of a stopped service are ignored
            jobs += [dict(j, own=False) for j in d.get("scan_queue") or []]
        recent = (d.get("recent_scans") or [])[:5]
        signature = (tuple((j.get("target"), j.get("time"), j.get("status")) for j in jobs)
                     + tuple(r.get("path") for r in recent))
        if self.scan_count != len(jobs):
            self.scan_count = len(jobs)
            self.main_window.set_queue_count(getattr(self, "print_count", 0) + self.scan_count)
        if getattr(self.scans, "signature", None) == signature:
            return
        self.scans.signature = signature
        for r in self.scans.get_children():
            self.scans.remove(r)
        if not jobs:
            r = Gtk.ListBoxRow(activatable=False)
            r.add(make_label(_("No scans waiting."), "pc-dim", margin=12, xalign=0.5))
            self.scans.add(r)
        for n, j in enumerate(jobs, 1):
            self.scans.add(self.scan_row(j, n))
        if recent:
            r = Gtk.ListBoxRow(activatable=False)
            r.add(make_label(_("Recently completed"), "pc-dim", margin_top=12, margin_start=8, margin_bottom=4))
            self.scans.add(r)
            for item in recent:
                self.scans.add(self.recent_scan_row(item))
        self.scans.show_all()

    def scan_row(self, j, position):
        r = Gtk.ListBoxRow(activatable=False)
        k = Gtk.Box(spacing=12, margin=8)
        k.pack_start(Gtk.Image.new_from_icon_name("scanner-symbolic", Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.pack_start(make_label(j.get("target") or "", ellipsize=Pango.EllipsizeMode.END), False, False, 0)
        parts = ["#%d" % position, _("This window") if j.get("own") else _("Device panel")]
        if j.get("time"):
            parts.append(dt.datetime.fromtimestamp(j["time"]).strftime(date_format(2)))
        text.pack_start(make_label(" · ".join(parts), "pc-dim"), False, False, 0)
        k.pack_start(text, True, True, 0)
        label, level = SCAN_STATES.get(j.get("status"), SCAN_STATES["waiting"])
        k.pack_start(styled(Gtk.Label(label=_(label), valign=Gtk.Align.CENTER), "pc-badge", level),
                     False, False, 0)
        if j.get("own") and j.get("status") != "scanning":
            b = styled(Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON),
                       "flat", "circular")
            b.set_valign(Gtk.Align.CENTER)
            b.set_tooltip_text(_("Cancel"))
            b.connect("clicked", self.main_window.scan_page.cancel_scan)
            k.pack_start(b, False, False, 0)
        r.add(k)
        return r

    def recent_scan_row(self, item):
        r = Gtk.ListBoxRow(activatable=False)
        k = Gtk.Box(spacing=12, margin=8)
        k.pack_start(Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        path = item.get("path") or ""
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.pack_start(make_label(os.path.basename(path), ellipsize=Pango.EllipsizeMode.MIDDLE), False, False, 0)
        parts = [x for x in (item.get("target"),) if x]
        if item.get("time"):
            parts.append(dt.datetime.fromtimestamp(item["time"]).strftime(date_format(2)))
        text.pack_start(make_label(" · ".join(parts), "pc-dim"), False, False, 0)
        k.pack_start(text, True, True, 0)
        if os.path.exists(path):
            b = styled(Gtk.Button.new_from_icon_name("document-open-symbolic", Gtk.IconSize.BUTTON),
                       "flat", "circular")
            b.set_valign(Gtk.Align.CENTER)
            b.set_tooltip_text(_("Open"))
            b.connect("clicked", lambda _b: open_path(path))
            k.pack_start(b, False, False, 0)
        r.add(k)
        return r

    def populate(self, items, active, history, empty_label, local):
        signature = tuple((i["id"], i["status"], i.get("page")) for i in active + history) + (empty_label,)
        if getattr(items, "signature", None) == signature:
            return
        items.signature = signature
        for r in items.get_children():
            items.remove(r)
        if not active:
            r = Gtk.ListBoxRow(activatable=False)
            r.add(make_label(empty_label, "pc-dim", margin=12, xalign=0.5))
            items.add(r)
        for i in active:
            items.add(self.row(i, local, cancel=True))
        if history:
            r = Gtk.ListBoxRow(activatable=False)
            r.add(make_label(_("Recently completed"), "pc-dim", margin_top=12, margin_start=8, margin_bottom=4))
            items.add(r)
            for i in history:
                items.add(self.row(i, local, cancel=False))
        items.show_all()

    def row(self, i, local, cancel):
        r = Gtk.ListBoxRow(activatable=False)
        k = Gtk.Box(spacing=12, margin=8)
        k.pack_start(Gtk.Image.new_from_icon_name(JOB_ICON.get(i["status"], "document-symbolic"),
                                                  Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        label_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label_text.pack_start(make_label(i["name"], ellipsize=Pango.EllipsizeMode.MIDDLE), False, False, 0)
        parts = ["#%s" % i["id"]]
        who = "@".join(x for x in (i["user"], i["machine"]) if x)
        if who:
            parts.append(who)
        z = i.get("completed") or i.get("time")
        if z:
            parts.append(dt.datetime.fromtimestamp(z).strftime(date_format(2)))
        if i.get("kbytes"):
            parts.append("%d KB" % i["kbytes"])
        if i.get("page"):
            parts.append(page_count(i["page"]))
        label_text.pack_start(make_label(" · ".join(parts), "pc-dim",
                               ellipsize=Pango.EllipsizeMode.END), False, False, 0)
        k.pack_start(label_text, True, True, 0)
        badge = styled(Gtk.Label(label=i["status_text"], valign=Gtk.Align.CENTER), "pc-badge",
                      JOB_LEVEL.get(i["status"], "neutral"))
        reason = [x for x in i["reason"] if x and x != "none"]
        if reason:
            badge.set_tooltip_text(", ".join(reason))
        k.pack_start(badge, False, False, 0)
        if cancel and i["status"] in (3, 4, 5, 6):
            d = styled(Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON),
                      "flat", "circular")
            d.set_valign(Gtk.Align.CENTER)
            d.set_tooltip_text(_("Cancel job"))
            d.connect("clicked", lambda b: self.cancel(i, local))
            k.pack_start(d, False, False, 0)
        r.add(k)
        return r

    # --- actions
    def cancel(self, i, local):
        if not self.main_window.ask(_("Cancel the job \"%s\"?") % i["name"]):
            return
        try:
            if local and self.queue:
                core.local_cancel(self.queue[0], i["id"])
            else:
                core.ipp_cancel(core.device_ipp_uri(self.main_window.device()), i["id"], getpass.getuser())
            log.info("Job cancelled: #%s %s (%s)", i["id"], i["name"], "local" if local else "device")
        except Exception as e:
            log.error("Could not cancel job #%s: %s", i["id"], e)
            self.main_window.warn(_("Could not cancel the job:\n%s") % error_text(e))
        self.refresh(force=True)

    def cancel_all(self, _widget):
        if not self.queue or not self.main_window.ask(_("Cancel all pending jobs of this computer?")):
            return
        try:
            core.local_cancel(self.queue[0])
            log.info("All pending local jobs cancelled")
        except Exception as e:
            log.error("Cancelling all jobs failed: %s", e)
            self.main_window.warn(_("Could not cancel:\n%s") % error_text(e))
        self.refresh(force=True)

    def start_queue(self, _widget):
        if not self.queue:
            return
        for cmd in (["cupsenable", self.queue[0]], ["cupsaccept", self.queue[0]]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                log.error("%s failed: %s", cmd[0], r.stderr.strip())
                self.main_window.warn(_("Could not start the queue (administrator rights may be "
                                        "required):\n%s") % r.stderr.strip())
                return
        log.info("Printer queue restarted: %s", self.queue[0])
        self.refresh(force=True)


# --------------------------------------------------------------------------
# Log
# --------------------------------------------------------------------------

class LogPage(Gtk.Box):
    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=16)
        self.main_window = main_window

        self.level_filter = combo([(_("All events"), "all"), (_("Warnings and errors"), "error")], "all")
        self.level_filter.connect("changed", lambda c: self.refresh(force=True))
        right = Gtk.Box(spacing=6)
        right.pack_start(self.level_filter, False, False, 0)
        for icon, tooltip, func in (
                ("view-refresh-symbolic", _("Refresh"), lambda b: self.refresh(force=True)),
                ("edit-copy-symbolic", _("Copy to clipboard"), lambda b: self.copy_report()),
                ("folder-open-symbolic", _("Open log folder"), lambda b: open_path(core.LOG_DIR))):
            d = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            d.set_tooltip_text(tooltip)
            d.connect("clicked", func)
            right.pack_start(d, False, False, 0)

        self.xml_text = styled(Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                        wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                        left_margin=8, right_margin=8, top_margin=8, bottom_margin=8),
                           "pc-log")
        buffer = self.xml_text.get_buffer()
        buffer.create_tag("time", foreground="#8a8a8a")
        buffer.create_tag("ERROR", foreground="#e01b24", weight=Pango.Weight.BOLD)
        buffer.create_tag("CRITICAL", foreground="#e01b24", weight=Pango.Weight.BOLD)
        buffer.create_tag("WARNING", foreground="#c88800", weight=Pango.Weight.BOLD)
        buffer.create_tag("INFO", foreground="#2e8bd6")
        buffer.create_tag("DEBUG", foreground="#8a8a8a")
        scroller = Gtk.ScrolledWindow()
        scroller.add(self.xml_text)
        self.summary = make_label("", "pc-dim")
        k = card(_("Event log"), scroller, description=_("Service and interface records · %s")
                 % core.LOG_DIR.replace(os.path.expanduser("~"), "~"), right=right)
        k.pack_start(self.summary, False, False, 0)
        self.pack_start(k, True, True, 0)
        self.scroller = scroller
        self.connect("map", lambda *_args: self.refresh(force=True))
        GLib.timeout_add_seconds(5, self.refresh)

    def rows(self):
        everything = core.read_logs()
        if self.level_filter.get_active_id() == "error":
            everything = [s for s in everything if s[1] in ("WARNING", "ERROR", "CRITICAL")]
        return everything

    def refresh(self, force=False):
        if not (force or self.get_mapped()):
            return True
        rows = self.rows()
        signature = (len(rows), rows[-1] if rows else None, self.level_filter.get_active_id())
        if not force and getattr(self, "_signature", None) == signature:
            return True
        self._signature = signature
        t = self.xml_text.get_buffer()
        t.set_text("")
        for timestamp, level, source, msg in rows:
            t.insert_with_tags_by_name(t.get_end_iter(), timestamp + "  ", "time")
            t.insert_with_tags_by_name(t.get_end_iter(), "%-7s " % level,
                                       level if level in ("ERROR", "CRITICAL", "WARNING", "INFO",
                                                            "DEBUG") else "INFO")
            t.insert_with_tags_by_name(t.get_end_iter(), "[%s] " % source, "time")
            t.insert(t.get_end_iter(), msg + "\n")
        if not rows:
            t.set_text(_("No records yet."))
        error = sum(1 for s in core.read_logs() if s[1] in ("ERROR", "CRITICAL"))
        self.summary.set_text(_("Showing %d records · %d errors in total") % (len(rows), error))
        GLib.idle_add(lambda: self.xml_text.scroll_to_iter(t.get_end_iter(), 0, False, 0, 1) and False)
        return True

    def copy_report(self):
        text = "\n".join("%s %s [%s] %s" % s for s in self.rows()[-300:])
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.main_window.notify_user(_("Log copied"), _("The last %d lines were copied to the clipboard.")
                                     % min(300, len(self.rows())))


# --------------------------------------------------------------------------
# Window and application
# --------------------------------------------------------------------------

SECTIONS = (("scan", "scanner-symbolic", N_("Scan")),
            ("panel", "send-to-symbolic", N_("Scan from Panel")),
            ("queue", "view-list-symbolic", N_("Queue")),
            ("device", "printer-symbolic", N_("Device")),
            ("logs", "utilities-terminal-symbolic", N_("Log")),
            ("settings", "emblem-system-symbolic", N_("Settings")))


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=_("Printer Center"))
        self.set_default_size(1040, 680)
        self.set_icon_name("scanner")

        # own light window buttons instead of the theme's title buttons
        self.header_bar = Gtk.HeaderBar(show_close_button=False, title=_("Printer Center"))
        self.set_titlebar(self.header_bar)
        buttons = styled(Gtk.Box(spacing=2, valign=Gtk.Align.CENTER), "pc-winbtns")
        self.max_image = Gtk.Image.new_from_icon_name("window-maximize-symbolic", Gtk.IconSize.MENU)
        for icon, tip, action, extra in (
                ("window-minimize-symbolic", _("Minimize"), lambda _b: self.iconify(), None),
                (self.max_image, _("Maximize"), lambda _b: self.toggle_maximize(), "maximize"),
                ("window-close-symbolic", _("Close"), lambda _b: self.close(), "close")):
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE, can_focus=False, tooltip_text=tip)
            b.add(icon if isinstance(icon, Gtk.Image) else Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU))
            styled(b, "pc-winbtn", *([extra] if extra else []))
            b.connect("clicked", action)
            buttons.pack_start(b, False, False, 0)
            if extra == "maximize":
                self.max_button = b
        self.header_bar.pack_end(buttons)
        self.connect("window-state-event", self.on_window_state)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=150)

        # Sidebar
        sidebar = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "pc-sidebar")
        sidebar.set_size_request(210, -1)
        self.menu = Gtk.ListBox(margin_top=8)
        self.menu_rows = {}
        for name, icon, label_text in SECTIONS:
            r = Gtk.ListBoxRow()
            r.name = name
            k = Gtk.Box(spacing=12)
            k.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
            k.pack_start(Gtk.Label(label=_(label_text), xalign=0), True, True, 0)
            if name == "panel":
                self.panel_dot = styled(Gtk.Box(valign=Gtk.Align.CENTER), "pc-dot", "neutral")
                k.pack_end(self.panel_dot, False, False, 0)
            if name == "device":
                self.device_menu_dot = styled(Gtk.Box(valign=Gtk.Align.CENTER), "pc-dot", "neutral")
                k.pack_end(self.device_menu_dot, False, False, 0)
            if name == "queue":
                self.queue_label = styled(Gtk.Label(valign=Gtk.Align.CENTER), "pc-counter")
                self.queue_label.set_no_show_all(True)
                k.pack_end(self.queue_label, False, False, 0)
            r.add(k)
            self.menu.add(r)
            self.menu_rows[name] = r
        self.menu.connect("row-selected", lambda lb, r: r and self.stack.set_visible_child_name(r.name))
        sidebar.pack_start(self.menu, True, True, 0)

        footer = Gtk.Box(spacing=10, margin=14)
        self.device_dot = styled(Gtk.Box(valign=Gtk.Align.CENTER), "pc-dot", "neutral")
        device_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.device_name = make_label(_("Device"), "pc-card-title", ellipsize=Pango.EllipsizeMode.END)
        self.device_label = make_label(_("Connecting…"), "pc-dim", ellipsize=Pango.EllipsizeMode.END)
        device_box.pack_start(self.device_name, False, False, 0)
        device_box.pack_start(self.device_label, False, False, 0)
        footer.pack_start(self.device_dot, False, False, 0)
        footer.pack_start(device_box, True, True, 0)
        sidebar.pack_end(footer, False, False, 0)

        self.device_page = DevicePage(self)
        self.scan_page = ScanPage(self)
        self.stack.add_named(self.scan_page, "scan")
        self.stack.add_named(PanelPage(self), "panel")
        self.stack.add_named(QueuePage(self), "queue")
        self.stack.add_named(self.device_page, "device")
        self.stack.add_named(LogPage(self), "logs")
        self.stack.add_named(SettingsPage(self), "settings")

        box = Gtk.Box()
        box.pack_start(sidebar, False, False, 0)
        box.pack_start(self.stack, True, True, 0)
        self.add(box)
        # window itself not made visible (tray mode starts hidden), but the title bar must be:
        # a hidden header bar leaves the window without title, minimize, maximize and close
        self.header_bar.show_all()
        box.show_all()
        self.scan_page.update_view()
        self.navigate("scan" if self.device() else "settings")
        self.connect("delete-event", self.on_delete)
        self._seen_version = None
        GLib.timeout_add_seconds(30, self.check_new_version)

    def toggle_maximize(self):
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def on_window_state(self, _w, event):
        self.iconified = bool(event.new_window_state & Gdk.WindowState.ICONIFIED)
        maximized = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
        self.max_image.set_from_icon_name("window-restore-symbolic" if maximized else "window-maximize-symbolic",
                                          Gtk.IconSize.MENU)
        self.max_button.set_tooltip_text(_("Restore") if maximized else _("Maximize"))
        return False

    def on_delete(self, *_args):
        app = self.get_application()
        if not app.tray:
            return False
        self.hide()
        if not getattr(self, "_tray_hint_shown", False):
            self._tray_hint_shown = True
            self.notify_user(_("Printer Center is running in the background"),
                             _("You can reopen it from the icon in the bottom-right corner."))
        return True

    def navigate(self, name):
        self.menu.select_row(self.menu_rows[name])

    def device(self):
        c, _profile = read_settings()
        return c.get("device", "address", fallback="").strip()

    def scan_folder(self):
        c, _profile = read_settings()
        return os.path.expanduser((c.get("client", "folder", fallback="").strip() or "~/" + _("Scans")))

    def show_device_status(self, model, status, level):
        if model:
            self.device_name.set_text(model)
        self.device_label.set_text(status)
        swap_class(self.device_dot, ("ok", "warning", "error", "neutral"), level)
        swap_class(self.device_menu_dot, ("ok", "warning", "error", "neutral"), level)
        self.header_bar.set_subtitle("%s · %s" % (model or self.device_name.get_text(), status))
        tray = self.get_application().tray
        if tray:
            tray.status(level, "%s: %s" % (model or self.device_name.get_text(), status))

    def set_queue_count(self, n):
        self.queue_label.set_text(str(n))
        self.queue_label.set_visible(n > 0)

    def set_panel_status(self, level):
        swap_class(self.panel_dot, ("ok", "warning", "error", "neutral"), level)

    def warn(self, message):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.OK, text=message)
        d.run()
        d.destroy()

    def ask(self, message):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO, text=message)
        answer = d.run()
        d.destroy()
        return answer == Gtk.ResponseType.YES

    def check_new_version(self):
        """After an update was installed: restart with the new version by itself, but only when idle
        (no scan running, no unsaved pages, no open dialog); otherwise try again later."""
        on_disk = installed_version()
        if version_tuple(on_disk) <= version_tuple(core.VERSION):
            return True
        if self._seen_version != on_disk:  # seen twice in a row: the installer has finished copying
            self._seen_version = on_disk
            return True
        sp = self.scan_page
        busy = (sp.job is not None or sp.spinner.get_property("active") or sp.pages
                or any(w.get_modal() and w.get_visible() for w in Gtk.Window.list_toplevels()))
        if busy:
            return True
        log.info("Version %s installed, restarting (running %s)", on_disk, core.VERSION)
        # a minimized window comes back in the tray, not popped up on the screen
        self.relaunch(tray=not self.get_visible() or getattr(self, "iconified", False))
        return False

    def relaunch(self, tray=False):
        log.info("Restarting the application")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)]
                 + (["--tray"] if tray else []))

    def notify_user(self, title, body):
        n = Gio.Notification.new(title)
        n.set_body(body)
        self.get_application().send_notification(None, n)


TRAY_ICONS = {"ok": "scanner", "warning": "dialog-warning", "error": "printer-error", "neutral": "scanner"}


class TrayIcon:
    """Notification area icon at the bottom right (Cinnamon/MATE: XApp, others: GtkStatusIcon)."""

    def __init__(self, app):
        self.app = app
        self.menu = Gtk.Menu()
        for label_text, work in ((_("Open Printer Center"), lambda: self.render()),
                                 (_("Scan"), lambda: self.render("scan")),
                                 (_("Queue"), lambda: self.render("queue")),
                                 (_("Device status"), lambda: self.render("device")),
                                 (None, None),
                                 (_("Open scans folder"), self.folder),
                                 (None, None),
                                 (_("Quit"), self.app.quit)):
            o = Gtk.SeparatorMenuItem() if label_text is None else Gtk.MenuItem(label=label_text)
            if work:
                o.connect("activate", lambda _m, f=work: f())
            self.menu.append(o)
        self.menu.show_all()
        try:
            gi.require_version("XApp", "1.0")
            from gi.repository import XApp
            self.icon = XApp.StatusIcon()
            self.icon.set_name("printer-center")
            self.icon.set_secondary_menu(self.menu)
            self.icon.connect("activate", lambda *_args: self.toggle())
            self.xapp = True
        except (ImportError, ValueError):
            self.icon = Gtk.StatusIcon()
            self.icon.connect("activate", lambda *_args: self.toggle())
            self.icon.connect("popup-menu", lambda s, d, z: self.menu.popup(
                None, None, Gtk.StatusIcon.position_menu, s, d, z))
            self.xapp = False
        self.status("neutral", None)

    def status(self, level, tooltip=None):
        self.icon.set_icon_name(TRAY_ICONS.get(level, "scanner"))
        name = _("Printer Center")
        self.icon.set_tooltip_text(name + "\n" + tooltip if tooltip else name)

    def toggle(self):
        p = self.app.main_window
        if p and p.is_visible() and p.is_active():
            p.hide()
        else:
            self.render()

    def render(self, section=None):
        p = self.app.get_main_window()
        if section:
            p.navigate(section)
        p.present()

    def folder(self):
        open_path(self.app.get_main_window().scan_folder())


class Application(Gtk.Application):
    def __init__(self, tray_mode=False):
        super().__init__(application_id=APP_ID)
        self.tray_mode = tray_mode
        self.tray = None
        self.main_window = None

    def get_main_window(self):
        if not self.main_window:
            self.main_window = MainWindow(self)
        return self.main_window

    def do_startup(self):
        try:
            i18n.set_language(read_settings()[0].get("client", "language", fallback="auto"))
        except Exception:
            i18n.set_language("auto")
        Gtk.Application.do_startup(self)
        core.setup_logging("gui", console=False)
        sys.excepthook = lambda t, d, trace: log.critical("Unexpected error: %s", d, exc_info=(t, d, trace))
        log.info("Printer Center started (language: %s)", i18n.language())
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        try:
            self.tray = TrayIcon(self)
            self.hold()  # keep the icon when the window is closed
        except Exception as e:
            log.warning("Could not create the notification area icon: %s", e)

    def do_activate(self):
        p = self.get_main_window()
        if self.tray_mode and self.tray:
            self.tray_mode = False  # only the first start is hidden; later activations show the window
            return
        p.present()


if __name__ == "__main__":
    tray_mode = "--tray" in sys.argv
    sys.exit(Application(tray_mode).run([a for a in sys.argv if a != "--tray"]))
