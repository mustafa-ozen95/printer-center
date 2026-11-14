#!/usr/bin/env python3
"""printer-center: WSD (WS-Scan) scanning core and background service for Linux.

- Scan from the PC: pull_scan()
- Scan from the panel to the PC: the service registers itself on the device as a
  destination; when that destination is chosen on the panel it fetches the document
  and saves it to the folder as PDF/JPEG.
Uses only the Python standard library.
"""

import argparse
import configparser
import contextlib
import datetime as dt
import fcntl
import http.server
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import i18n
from i18n import _, N_

NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wse": "http://schemas.xmlsoap.org/ws/2004/08/eventing",
    "wscn": "http://schemas.microsoft.com/windows/2006/08/wdp/scan",
}
A_SUBSCRIBE = NS["wse"] + "/Subscribe"
A_RENEW = NS["wse"] + "/Renew"
A_UNSUBSCRIBE = NS["wse"] + "/Unsubscribe"
A_SCAN_AVAILABLE = NS["wscn"] + "/ScanAvailableEvent"
A_CREATE_JOB = NS["wscn"] + "/CreateScanJob"
A_RETRIEVE = NS["wscn"] + "/RetrieveImage"
A_ELEMENTS = NS["wscn"] + "/GetScannerElements"
ANONYMOUS = NS["wsa"] + "/role/anonymous"

# Paper sizes in WS-Scan units (1/1000 inch)
PAPER_SIZES = {"A4": (8268, 11693), "A5": (5827, 8268), "Letter": (8500, 11000)}
COLOR_MODES = {"color": "RGB24", "gray": "Grayscale8"}
RESOLUTIONS = (75, 100, 150, 200, 300)

SETTINGS_PATH = os.path.expanduser("~/.config/printer-center/settings.ini")
STATE_PATH = os.path.expanduser("~/.cache/printer-center/state.json")
LOG_DIR = os.path.expanduser("~/.local/state/printer-center")

# Version: bump this when releasing and push a vX.Y.Z tag with the same name (updater.py compares the two)
VERSION = "2.4.1"
UPDATE_STATE_PATH = "/var/lib/printer-center/update.json"
UPDATE_SERVICE = "printer-center-update.service"
INSTALL_SERVICE = "printer-center-install.service"

log = logging.getLogger("printer-center")


def settings_parser():
    """ConfigParser that also accepts Turkish booleans (evet/hayır)."""
    c = configparser.ConfigParser()
    # Turkish boolean words are kept on purpose for compatibility with old settings files
    c.BOOLEAN_STATES = dict(c.BOOLEAN_STATES, evet=True, hayir=False,
                            **{"hayır": False, "açık": True, "kapalı": False})
    return c


LEGACY_SETTINGS_PATH = os.path.expanduser("~/.config/wsd-tarama/ayar.ini")
_LEGACY_SECTIONS = {"cihaz": "device", "istemci": "client"}
_LEGACY_KEYS = {"adres": "address", "ad": "name", "klasor": "folder", "masaustu_bildirimi": "desktop_notifications",
                "renk": "color", "cozunurluk": "resolution", "kagit": "paper", "bicim": "format"}
_LEGACY_VALUES = {"renkli": "color", "gri": "gray", "evet": "yes", "hayir": "no", "hayır": "no"}


def migrate_legacy_settings(path=SETTINGS_PATH, legacy=LEGACY_SETTINGS_PATH):
    """Convert the pre-2.0 settings file (wsd-tarama/ayar.ini, Turkish keys) once."""
    if os.path.exists(path) or not os.path.exists(legacy):
        return False
    old, new = settings_parser(), configparser.ConfigParser()
    old.read(legacy, encoding="utf-8")
    for section in old.sections():
        target = _LEGACY_SECTIONS.get(section) or re.sub(r"^profil:", "profile:", section)
        new[target] = {_LEGACY_KEYS.get(k, k): _LEGACY_VALUES.get(v.strip().lower(), v)
                       for k, v in old.items(section, raw=True) if k not in old.defaults()}
    if new.has_section("client"):
        new["client"].setdefault("language", "auto")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        new.write(f)
    log.info("Migrated legacy settings %s -> %s", legacy, path)
    return True


# --------------------------------------------------------------------------
# SOAP
# --------------------------------------------------------------------------

def friendly_error(error):
    """Short, translated description of an error for the screen; technical details go to the log.
    Errors joined with "; " (one per data source) are described once each."""
    lines = str(error).strip().splitlines()
    parts = []
    for raw in re.split(r";\s*", lines[0] if lines else ""):
        low = raw.lower()
        if isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in low or "timeout" in low:
            text = _("no response from the device")
        elif "refused" in low:
            text = _("the device refused the connection")
        elif "no route" in low or "unreachable" in low:
            text = _("no network route to the device")
        elif "name or service" in low or "name resolution" in low or "nodename" in low:
            text = _("the device address could not be resolved")
        else:
            text = re.sub(r"^\w*(Error|Exception): ", "", raw.strip())
            text = re.sub(r"^<urlopen error (.*)>$", r"\1", text).strip("<> ")
        if text and text not in parts:
            parts.append(text)
    return ("; ".join(parts) or error.__class__.__name__)[:160]


def envelope(to, action, body, extra_header=""):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="{soap}" xmlns:wsa="{wsa}" xmlns:wse="{wse}" xmlns:wscn="{wscn}">'
        "<soap:Header><wsa:MessageID>urn:uuid:{mid}</wsa:MessageID><wsa:To>{to}</wsa:To>"
        "<wsa:ReplyTo><wsa:Address>{anon}</wsa:Address></wsa:ReplyTo>"
        "<wsa:Action>{action}</wsa:Action>{extra}</soap:Header>"
        "<soap:Body>{body}</soap:Body></soap:Envelope>"
    ).format(to=escape(to), action=action, mid=uuid.uuid4(), anon=ANONYMOUS,
             extra=extra_header, body=body, **NS).encode("utf-8")


class SoapError(Exception):
    pass


def soap_send(url, action, body, extra_header="", timeout=30):
    """Send a SOAP request; return (content type, raw response)."""
    request = urllib.request.Request(
        url, data=envelope(url, action, body, extra_header),
        headers={"Content-Type": "application/soap+xml", "User-Agent": "WSDAPI",
                 "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as y:
            return y.headers.get("Content-Type", ""), y.read()
    except urllib.error.HTTPError as h:
        raise SoapError("HTTP %d: %s" % (h.code, error_summary(h.read()))) from None


def error_summary(raw):
    try:
        root = ET.fromstring(raw)
        parts = [e.text.strip() for e in root.iter()
                    if e.text and e.text.strip() and local(e.tag) in ("Value", "Text", "Subcode")]
        return " | ".join(parts)[:300] or raw[:200].decode("utf-8", "replace")
    except ET.ParseError:
        return raw[:200].decode("utf-8", "replace")


def local(make_label):
    return make_label.rsplit("}", 1)[-1]


def find_target(root, name):
    for e in root.iter():
        if local(e.tag) == name:
            return e
    return None


def xml_text(root, name, default=None):
    e = find_target(root, name)
    return e.text.strip() if e is not None and e.text else default


def decode_mtom(content_type, raw):
    """Decode a multipart/related response: (soap_xml, [binary parts])."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if "multipart" not in content_type.lower() or not m:
        return raw, []
    limit = b"--" + m.group(1).encode()
    xml, binaries = None, []
    for widget in raw.split(limit)[1:]:
        if widget.startswith(b"--"):
            break
        title, __, body = widget.partition(b"\r\n\r\n")
        if body.endswith(b"\r\n"):
            body = body[:-2]
        b = title.lower()
        if xml is None and (b"xop+xml" in b or b"soap+xml" in b):
            xml = body
        else:
            binaries.append(body)
    return xml or b"", binaries


# --------------------------------------------------------------------------
# Scan job
# --------------------------------------------------------------------------

def ticket_xml(name, color, dpi, paper):
    width, height = PAPER_SIZES[paper]
    return (
        "<wscn:ScanTicket><wscn:JobDescription><wscn:JobName>printer-center</wscn:JobName>"
        "<wscn:JobOriginatingUserName>{name}</wscn:JobOriginatingUserName>"
        "<wscn:JobInformation>printer-center</wscn:JobInformation></wscn:JobDescription>"
        "<wscn:DocumentParameters><wscn:Format>jfif</wscn:Format>"
        "<wscn:ImagesToTransfer>0</wscn:ImagesToTransfer>"
        "<wscn:InputSize><wscn:InputMediaSize><wscn:Width>{g}</wscn:Width>"
        "<wscn:Height>{y}</wscn:Height></wscn:InputMediaSize></wscn:InputSize>"
        "<wscn:InputSource>Platen</wscn:InputSource>"
        "<wscn:MediaSides><wscn:MediaFront><wscn:ColorProcessing>{r}</wscn:ColorProcessing>"
        "<wscn:Resolution><wscn:Width>{d}</wscn:Width><wscn:Height>{d}</wscn:Height>"
        "</wscn:Resolution><wscn:ScanRegion><wscn:ScanRegionXOffset>0</wscn:ScanRegionXOffset>"
        "<wscn:ScanRegionYOffset>0</wscn:ScanRegionYOffset>"
        "<wscn:ScanRegionWidth>{g}</wscn:ScanRegionWidth>"
        "<wscn:ScanRegionHeight>{y}</wscn:ScanRegionHeight></wscn:ScanRegion>"
        "</wscn:MediaFront></wscn:MediaSides>"
        "</wscn:DocumentParameters></wscn:ScanTicket>"
    ).format(name=escape(name), g=width, y=height, r=color, d=dpi)


BUSY_WAIT = 300  # seconds a scan waits in line while the device is busy with another job


class ScanCancelled(Exception):
    pass


def _sleep(seconds, cancel):
    """Sleep; raise ScanCancelled as soon as cancel (threading.Event) is set."""
    if cancel is not None and cancel.wait(seconds):
        raise ScanCancelled()
    if cancel is None:
        time.sleep(seconds)


@contextlib.contextmanager
def scan_slot(on_wait=None, cancel=None):
    """Scan queue on this computer: the app and the service take turns (file lock)."""
    path = os.path.join(os.path.dirname(STATE_PATH), "scan.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        started = time.time()
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if on_wait:
                    on_wait("local", int(time.time() - started))
                _sleep(1, cancel)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def do_scan(device, ticket, scan_identifier=None, token=None, wait=BUSY_WAIT, on_wait=None, cancel=None):
    """Create a scan job and retrieve the images; return a list of JPEG bytes.
    scan_identifier/token are only given for scans started from the panel.
    While the device is busy (e.g. another computer is scanning) the request waits in line for up
    to `wait` seconds, calling on_wait("device", seconds_waited); `cancel` aborts the wait.
    on_wait("scanning", ...) is called once the device has accepted the job."""
    extra = ""
    if scan_identifier:
        extra = ("<wscn:ScanIdentifier>%s</wscn:ScanIdentifier>"
              "<wscn:DestinationToken>%s</wscn:DestinationToken>"
              % (escape(scan_identifier), escape(token or "")))
    request = "<wscn:CreateScanJobRequest>%s%s</wscn:CreateScanJobRequest>" % (extra, ticket)
    started = time.time()
    while True:
        if cancel is not None and cancel.is_set():
            raise ScanCancelled()
        try:
            __, raw = soap_send(device, A_CREATE_JOB, request)
            break
        except SoapError as e:
            if time.time() - started > wait or not re.search(r"NotAccepting|Busy|Unavailable", str(e)):
                raise
            log.info("Device busy, waiting in line: %s", e)
            if on_wait:
                on_wait("device", int(time.time() - started))
            _sleep(5, cancel)
    if on_wait:
        on_wait("scanning", int(time.time() - started))
    root = ET.fromstring(raw)
    job_no, job_token = xml_text(root, "JobId"), xml_text(root, "JobToken")
    log.info("Scan job %s started", job_no)
    images = []
    for n in range(1, 51):
        body = ("<wscn:RetrieveImageRequest><wscn:DocumentDescription>"
                 "<wscn:DocumentName>IMAGE%03d.JPG</wscn:DocumentName></wscn:DocumentDescription>"
                 "<wscn:JobId>%s</wscn:JobId><wscn:JobToken>%s</wscn:JobToken>"
                 "</wscn:RetrieveImageRequest>" % (n, escape(job_no), escape(job_token)))
        try:
            kind, raw = soap_send(device, A_RETRIEVE, body, timeout=180)
        except SoapError as e:
            if images and "NoImagesAvailable" in str(e):
                break
            raise
        __, binaries = decode_mtom(kind, raw)
        if not binaries:
            break
        images.append(max(binaries, key=len))
        break  # this device only has a flatbed: one page per job
    if not images:
        raise RuntimeError(_("No image received from the device"))
    return images


def pull_scan(device, color="RGB24", dpi=300, paper="A4", name="Linux", on_wait=None, cancel=None):
    """Scan started from the PC (single page); waits in the scan queue if needed."""
    with scan_slot(on_wait, cancel):
        return do_scan(device, ticket_xml(name, color, dpi, paper), on_wait=on_wait, cancel=cancel)[0]


# --------------------------------------------------------------------------
# JPEG -> PDF (without extra packages)
# --------------------------------------------------------------------------

def jpeg_info(data):
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height, data[i + 9]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError(_("Could not read the JPEG size"))


def pdf_text(text):
    """PDF text string (UTF-16BE with byte order mark, hex)."""
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">"


def jpegs_to_pdf(jpegs, dpi, title=None):
    objects = []

    def append_block(b):
        objects.append(b)
        return len(objects)

    catalog = append_block(None)
    pages = append_block(None)
    page_no = []
    for j in jpegs:
        width, height, component = jpeg_info(j)
        color = b"/DeviceGray" if component == 1 else b"/DeviceRGB"
        w_pt, y_pt = width * 72.0 / dpi, height * 72.0 / dpi
        image = append_block(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                     b"/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\n"
                     b"stream\n" % (width, height, color, len(j)) + j + b"\nendstream")
        stream = b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (w_pt, y_pt)
        content = append_block(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_no.append(append_block(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages, w_pt, y_pt, image, content)))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages
    now = dt.datetime.now().astimezone()
    offset = now.strftime("%z") or "+0000"
    info = append_block(b"<< /Producer %s /CreationDate (D:%s%s'%s')%s >>" % (
        pdf_text("Printer Center " + VERSION), now.strftime("%Y%m%d%H%M%S").encode(),
        offset[:3].encode(), offset[3:].encode(), b" /Title " + pdf_text(title) if title else b""))
    objects[pages - 1] = (b"<< /Type /Pages /Kids [%s] /Count %d >>"
                              % (b" ".join(b"%d 0 R" % n for n in page_no), len(page_no)))
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for no, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += b"%d 0 obj\n" % no + obj + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for k in offsets:
        output += b"%010d 00000 n \n" % k
    output += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, catalog, info, xref))
    return bytes(output)


def write_file(path, data):
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def unique_path(folder, prefix, ext):
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(folder, "%s_%s%s" % (prefix, timestamp, ext))
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s_%s_%d%s" % (prefix, timestamp, n, ext))
        n += 1
    return path


# --------------------------------------------------------------------------
# Device status (the printer's web interface; read-only)
# --------------------------------------------------------------------------

def device_status(device_url):
    host = urllib.parse.urlparse(device_url).hostname
    url = "http://%s/sws/app/information/home/home.json" % host
    with urllib.request.urlopen(url, timeout=8) as y:
        t = y.read().decode("utf-8", "replace")
    def pick(key, section=None):
        source = t
        if section:
            m = re.search(r"%s\s*:\s*\{(.*?)\}" % section, t, re.S)
            source = m.group(1) if m else ""
        m = re.search(r"\b%s\s*:\s*\"?([^\",\n]*)\"?" % key, source)
        return m.group(1).strip() if m else ""
    toner = pick("remaining", "toner_black")
    return {
        "model": pick("model_name"),
        "name": pick("host_name"),
        "ip": pick("ip_addr"),
        "status": re.sub(r"\s+", " ", pick("status1")).strip(),
        "toner": int(toner) if toner.isdigit() else None,
    }


# --------------------------------------------------------------------------
# SNMP v2c (read-only; Printer MIB / Host Resources MIB)
# --------------------------------------------------------------------------

def _ber_length(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _ber(make_label, content):
    return bytes([make_label]) + _ber_length(len(content)) + content


def _ber_int(n):
    return _ber(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _ber_oid(oid):
    p = [int(x) for x in oid.strip(".").split(".")]
    body = bytes([40 * p[0] + p[1]])
    for x in p[2:]:
        widget = [x & 0x7F]
        x >>= 7
        while x:
            widget.insert(0, 0x80 | (x & 0x7F))
            x >>= 7
        body += bytes(widget)
    return _ber(0x06, body)


def _ber_decode(data, i=0):
    """(tag, content, next_position)"""
    make_label = data[i]
    n = data[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(data[i:i + k], "big")
        i += k
    return make_label, data[i:i + n], i + n


def _ber_sequence(data):
    i, result = 0, []
    while i < len(data):
        e, inner, i = _ber_decode(data, i)
        result.append((e, inner))
    return result


def _oid_str(inner):
    p = [inner[0] // 40, inner[0] % 40]
    x = 0
    for b in inner[1:]:
        x = (x << 7) | (b & 0x7F)
        if not b & 0x80:
            p.append(x)
            x = 0
    return ".".join(map(str, p))


def _snmp_value(e, inner):
    if e in (0x02, 0x41, 0x42, 0x43, 0x46):  # Integer, Counter, Gauge, TimeTicks
        return int.from_bytes(inner, "big", signed=(e == 0x02))
    if e == 0x04:
        return inner
    if e == 0x06:
        return _oid_str(inner)
    if e == 0x40 and len(inner) == 4:
        return ".".join(str(b) for b in inner)
    if e in (0x80, 0x81, 0x82, 0x05):  # noSuchObject, noSuchInstance, endOfMibView, NULL
        return None
    return inner


def snmp_request(ip, oids, following=False, community="public", timeout=3):
    """GET (or GETNEXT) -> [(oid, value)]"""
    request_id = int(time.time() * 1000) & 0x7FFFFFFF
    vb = b"".join(_ber(0x30, _ber_oid(o) + b"\x05\x00") for o in oids)
    pdu = _ber(0xA1 if following else 0xA0,
               _ber_int(request_id) + _ber_int(0) + _ber_int(0) + _ber(0x30, vb))
    package = _ber(0x30, _ber_int(1) + _ber(0x04, community.encode()) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(package, (ip, 161))
        while True:
            reply, __ = s.recvfrom(65535)
            __, inner, __ = _ber_decode(reply)
            parts = _ber_sequence(inner)
            __, pdu_body = parts[2]
            fields = _ber_sequence(pdu_body)
            if int.from_bytes(fields[0][1], "big") != request_id:
                continue
            result = []
            for __, v in _ber_sequence(fields[3][1]):
                (__, oid), (e, d) = _ber_sequence(v)
                result.append((_oid_str(oid), _snmp_value(e, d)))
            return result
    finally:
        s.close()


def snmp_walk(ip, root, limit=200, **kw):
    result, oid = [], root
    while len(result) < limit:
        r = snmp_request(ip, [oid], following=True, **kw)
        if not r or not r[0][0].startswith(root + ".") or r[0][0] == oid:
            break
        result.append(r[0])
        oid = r[0][0]
    return result


# --------------------------------------------------------------------------
# Detailed device report (SyncThru web JSON + SNMP Printer MIB + IPP)
# --------------------------------------------------------------------------

SWS = "/sws/app/information/"

# hrPrinterDetectedErrorState bits (RFC 3805): (key, title, advice, level)
ERROR_BITS = [
    ("paper_low", N_("Paper low"), N_("Add paper to the tray."), "warning"),
    ("paper_out", N_("Out of paper"), N_("Load paper into the tray."), "error"),
    ("toner_low", N_("Toner low"), N_("Get a replacement cartridge."), "warning"),
    ("toner_out", N_("Out of toner"), N_("Replace the cartridge."), "error"),
    ("cover_open", N_("Cover open"), N_("Close the device covers."), "error"),
    ("jam", N_("Paper jam"), N_("Carefully pull the jammed paper out of the tray and the rear/front cover."), "error"),
    ("offline", N_("Device offline"), N_("Turn the device off and on; check the panel message."), "error"),
    ("service", N_("Service required"), N_("Note the error code on the panel and report it to technical service."), "error"),
    ("tray_missing", N_("Paper tray not inserted"), N_("Push the tray all the way in."), "error"),
    ("output_missing", N_("Output tray not inserted"), N_("Check the output tray."), "warning"),
    ("cartridge_missing", N_("Cartridge not installed"), N_("Install the toner cartridge."), "error"),
    ("output_nearly_full", N_("Output tray almost full"), N_("Remove the printed pages."), "warning"),
    ("output_full", N_("Output tray full"), N_("Remove the printed pages."), "error"),
    ("tray_empty", N_("Paper tray empty"), N_("Load paper into the tray."), "error"),
    ("maintenance_due", N_("Periodic maintenance overdue"), N_("Call technical service for maintenance."), "warning"),
]
HR_DEVICE_STATUS = {1: N_("Unknown"), 2: N_("Running"), 3: N_("Warning"), 4: N_("Testing"), 5: N_("Down")}
HR_PRINTER_STATUS = {1: N_("Other"), 2: N_("Unknown"), 3: N_("Idle"), 4: N_("Printing"), 5: N_("Warming up")}
ALERT_SEVERITY = {1: N_("other"), 3: N_("critical"), 4: N_("warning"), 5: N_("warning (contextual)")}
ALERT_GROUPS = {5: N_("General"), 6: N_("Cover"), 8: N_("Paper input"), 9: N_("Output"), 11: N_("Toner/cartridge"),
               12: N_("Toner/cartridge"), 13: N_("Paper path"), 14: N_("Channel"), 15: N_("Interpreter"),
               30: N_("Scanner"), 31: N_("Document feeder")}
INPUT_STATUS = {-1: N_("Unlimited"), -2: N_("Unknown"), -3: N_("Paper present")}
PAPER_CODES = {1: "Letter", 2: "Legal", 3: "Folio", 4: "A4", 5: "Executive", 6: "A5", 7: "B5",
              8: "Statement", 9: "Oficio"}


def sws_json(xml_text):
    """Convert SyncThru's JS object with unquoted keys to JSON."""
    t = re.sub(r'([{,\[]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', xml_text)
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return json.loads(t)


def sws_fetch(ip, path, timeout=6):
    with urllib.request.urlopen("http://%s%s%s" % (ip, SWS, path), timeout=timeout) as y:
        return sws_json(y.read().decode("utf-8", "replace"))


def _text(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip()
    return "" if v is None else str(v).strip()


def _date(yyyymmdd):
    m = re.fullmatch(r"(\d{4})(\d\d)(\d\d)", _text(yyyymmdd))
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def device_reachable(ip, ports=(80, 631, 8018), timeout=3):
    """Quick check before the detailed report: True if any of the device's TCP ports answers.
    Probes run in parallel, so an unreachable device is reported within `timeout` seconds."""
    answered = threading.Event()

    def probe(port):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                answered.set()
        except OSError:
            pass
    threads = [threading.Thread(target=probe, args=(p,), daemon=True) for p in ports]
    for t in threads:
        t.start()
    deadline = time.time() + timeout + 0.5
    for t in threads:
        t.join(max(0, deadline - time.time()))
        if answered.is_set():
            return True
    return answered.is_set()


def build_device_report(device_url):
    """Detailed report from all sources. Each source is independent; if one fails the others still arrive.
    'problems': [{"token","title","advice","level","source"}]. User-visible texts are translated."""
    ip = urllib.parse.urlparse(device_url).hostname or device_url
    if not device_reachable(ip):
        raise TimeoutError("device %s did not answer on any port (timed out)" % ip)
    r = {"ip": ip, "time": time.time(), "problems": [], "source_errors": {}, "supplies": [], "maintenance": [],
         "trays": [], "counters": [], "info": {}, "security": []}

    def attempt(name, f):
        try:
            return f()
        except Exception as e:
            r["source_errors"][name] = "%s: %s" % (e.__class__.__name__, e)
            return None

    ev = attempt("home", lambda: sws_fetch(ip, "home/home.json")) or {}
    supplies = attempt("supplies", lambda: sws_fetch(ip, "supplies/supplies.json")) or {}
    counter = attempt("counters", lambda: sws_fetch(ip, "counters/counters.json")) or {}
    warn = attempt("activealert", lambda: sws_fetch(ip, "activealert/activealert.json")) or {}
    network = attempt("tcpip", lambda: sws_fetch(ip, "currentsettings/network/tcpip/tcpip_info.json")) or {}
    protocols = attempt("protocol", lambda: sws_fetch(ip, "currentsettings/network/protocol/protocol_info.json")) or {}

    # --- SNMP
    oids = {"uptime": "1.3.6.1.2.1.1.3.0", "hr_device": "1.3.6.1.2.1.25.3.2.1.5.1",
              "hr_printer": "1.3.6.1.2.1.25.3.5.1.1.1", "error_bit": "1.3.6.1.2.1.25.3.5.1.2.1",
              "firmware": "1.3.6.1.4.1.236.11.5.1.1.1.2.0",
              "nic_firmware": "1.3.6.1.4.1.236.11.5.1.12.2.16.0",
              "cartridge_date": "1.3.6.1.4.1.253.8.53.20.2.1.9.2.2.57.1"}
    snmp = attempt("snmp", lambda: dict(zip(oids, (v for __, v in snmp_request(ip, list(oids.values()))))))
    snmp = snmp or {}
    display_text = attempt("snmp_display", lambda: [_text(v) for __, v in snmp_walk(ip, "1.3.6.1.2.1.43.16.5.1.2", 8)])
    alert_table = attempt("snmp_alert", lambda: snmp_walk(ip, "1.3.6.1.2.1.43.18.1.1", 400)) or []
    inputs = attempt("snmp_input", lambda: {
        k: snmp_walk(ip, o, 20) for k, o in (("name", "1.3.6.1.2.1.43.8.2.1.18"), ("level", "1.3.6.1.2.1.43.8.2.1.10"),
                                             ("capacity", "1.3.6.1.2.1.43.8.2.1.9"))}) or {}
    ipp = attempt("ipp", lambda: next((g for g in ipp_request("ipp://%s/ipp/print" % ip, 0x000B, [
        (0x44, "requested-attributes", ["printer-state", "printer-state-reasons", "printer-state-message"])
    ])[1] if g.get("_group") == 4), {})) or {}

    # --- Identity and overall status
    identity = ev.get("identity", {})
    status_ = ev.get("status", {})
    r["model"] = identity.get("model_name") or ""
    r["name"] = identity.get("host_name") or ""
    r["serial"] = identity.get("serial_num") or counter.get("GXI_SYS_SERIAL_NUM") or ""
    r["display"] = [re.sub(r"\s{2,}", " · ", x) for x in (display_text or [re.sub(r"\s+", " ", _text(status_.get("status1"))),
                                        _text(status_.get("status2"))]) if x]
    r["status"] = re.sub(r"\s+", " ", r["display"][0]).strip() if r["display"] else ""
    r["hr_device"] = _(HR_DEVICE_STATUS.get(snmp.get("hr_device") or status_.get("hrDeviceStatus"), ""))
    r["hr_printer"] = _(HR_PRINTER_STATUS.get(snmp.get("hr_printer"), ""))
    r["running_time"] = (snmp.get("uptime") or 0) / 100.0 or None

    # --- Faults: SNMP error bits
    bits = snmp.get("error_bit")
    if isinstance(bits, bytes):
        for i, (key, title, advice, level) in enumerate(ERROR_BITS):
            if i // 8 < len(bits) and bits[i // 8] & (0x80 >> (i % 8)):
                r["problems"].append({"token": key, "title": _(title), "advice": _(advice),
                                      "level": level, "source": "SNMP"})
    # Printer MIB alert table
    rows = {}
    for oid, v in alert_table:
        p = oid.split(".")
        rows.setdefault(p[-1], {})[int(p[-3])] = v
    for no, s in sorted(rows.items()):
        description = _text(s.get(8))
        if not description:
            continue
        severity = s.get(2)
        r["problems"].append({
            "token": "alert_%s" % no, "title": description,
            "advice": _("%s · %s · code %s") % (
                _(ALERT_GROUPS[s.get(4)]) if s.get(4) in ALERT_GROUPS else _("group %s") % s.get(4),
                _(ALERT_SEVERITY[severity]) if severity in ALERT_SEVERITY else _("severity %s") % severity,
                s.get(7)),
            "level": "error" if severity == 3 else "warning", "source": "Printer MIB"})
    # Active alerts from the web interface
    for k in warn.get("recordData") or []:
        text = " ".join(_text(x) for x in (k.values() if isinstance(k, dict) else k
                                              if isinstance(k, list) else [k]) if _text(x))
        if text and not any(text in s["title"] for s in r["problems"]):
            r["problems"].append({"token": "web_%s" % text[:20], "title": text, "advice": "",
                                  "level": "warning", "source": "Web"})
    # IPP state reasons
    reasons = ipp.get("printer-state-reasons")
    for s in (reasons if isinstance(reasons, list) else [reasons]):
        s = _text(s)
        if s and s != "none" and not s.endswith("-report") and not r["problems"]:
            r["problems"].append({"token": "ipp_" + s, "title": s, "advice": "",
                                  "level": "error" if s.endswith("-error") else "warning", "source": "IPP"})
    r["ipp_message"] = _text(ipp.get("printer-state-message"))

    # --- Supplies: toner
    cartridge_date = _date(snmp.get("cartridge_date"))
    for color, name in (("black", _("Black toner cartridge")), ("cyan", _("Cyan toner")),
                     ("magenta", _("Magenta toner")), ("yellow", _("Yellow toner"))):
        t = supplies.get("toner_" + color) or ev.get("toner_" + color) or {}
        if not t.get("opt"):
            continue
        remaining, capacity, printed = t.get("remaining"), t.get("capa") or 0, t.get("cnt")
        item = {"name": name, "percent": remaining, "part_no": t.get("id") or "", "serial": t.get("serial") or "",
               "capacity": capacity, "printed": printed, "error": _text(t.get("newError")),
               "installed_on": cartridge_date.isoformat() if color == "black" and cartridge_date else None}
        if isinstance(remaining, int) and capacity:
            item["pages_left"] = int(capacity * remaining / 100)
            if item["installed_on"] and printed:
                days = max(1, (dt.date.today() - cartridge_date).days)
                logs = printed / days
                item["pages_per_day"] = logs
                item["days_left"] = int(item["pages_left"] / logs) if logs > 0 else None
        if isinstance(remaining, int) and remaining <= 10 and not any(s["token"].startswith("toner") for s in r["problems"]):
            r["problems"].append({"token": "toner_low", "title": _("%s %d%%") % (name, remaining),
                                  "advice": _("Get a replacement cartridge (%s).") % (t.get("id") or ""),
                                  "level": "error" if remaining <= 3 else "warning", "source": "Web"})
        if item["error"]:
            r["problems"].append({"token": "cartridge_error", "title": "%s: %s" % (name, item["error"]),
                                  "advice": "", "level": "error", "source": "Web"})
        r["supplies"].append(item)
    for color, name in (("black", _("Imaging unit (drum)")),):
        d = supplies.get("drum_" + color) or {}
        if d.get("opt"):
            r["supplies"].append({"name": name, "percent": d.get("remaining"), "part_no": d.get("id") or "",
                              "serial": d.get("serial") or "", "capacity": d.get("capa"), "printed": d.get("cnt")})

    # --- Maintenance parts (page counter / lifetime)
    consumed, top = supplies.get("consume") or {}, supplies.get("consume_opt") or {}
    for key, name in (("fuser_kit", _("Fuser unit")), ("btr_kit", _("Transfer roller (BTR)")),
                        ("forward_roller_tray1", _("Tray 1 feed roller")),
                        ("roller_tray1", _("Tray 1 roller")), ("retard_roller_mp", _("Manual feed roller"))):
        fmt_number, maximum = consumed.get(key), top.get(key + "_max") or 0
        if fmt_number:
            r["maintenance"].append({"name": name, "count": fmt_number, "lifetime": maximum,
                               "percent": max(0, 100 - int(100 * fmt_number / maximum)) if maximum else None})
            if maximum and fmt_number >= maximum:
                r["problems"].append({"token": "maint_" + key, "title": _("%s has reached end of life") % name,
                                      "advice": _("Should be replaced if print quality drops."), "level": "warning",
                                      "source": "Web"})

    # --- Trays
    names = [_text(v) for __, v in inputs.get("name", [])]
    levels = [v for __, v in inputs.get("level", [])]
    capacities = [v for __, v in inputs.get("capacity", [])]
    for i in range(1, 6):
        t = ev.get("tray%d" % i) or {}
        if not t.get("opt"):
            continue
        level = levels[i - 1] if i - 1 < len(levels) else None
        if isinstance(level, int) and level >= 0:
            level_text = _("Empty") if level == 0 else _("%d sheets") % level
        else:
            level_text = _(INPUT_STATUS[level]) if level in INPUT_STATUS else "—"
        # The device reports "Tray N"; translate the word "Tray"
        r["trays"].append({"name": (names[i - 1] if i - 1 < len(names) else "Tray %d" % i).replace("Tray", _("Tray")),
                              "paper": PAPER_CODES.get(t.get("paper_size1"), str(t.get("paper_size1") or "")),
                              "capacity": t.get("capa") or (capacities[i - 1] if i - 1 < len(capacities) else None),
                              "level": level_text, "neutral": level == 0})

    # --- Counters
    for key, name in (("GXI_BILLING_TOTAL_IMP_CNT", _("Total impressions")),
                        ("GXI_BLACK_PRINTED_IMPRESSIONS", _("Prints")),
                        ("GXI_BLACK_COPIED_IMPRESSIONS", _("Copies")),
                        ("GXI_NETWORK_SCANNING_IMAGES_SENT", _("Network scanning")),
                        ("GXI_2SIDED_SHEETS", _("Duplex")),
                        ("GXI_EMBEDDED_FAX_IMAGES_SENT", _("Faxes sent"))):
        if key in counter and (counter[key] or key != "GXI_EMBEDDED_FAX_IMAGES_SENT"):
            r["counters"].append((name, counter[key]))

    # --- Information (keys are display labels)
    b = r["info"]
    b["Model"] = r["model"]
    b[_("Serial number")] = r["serial"]
    b[_("Network name")] = r["name"]
    fw = _text(snmp.get("firmware"))
    b[_("Software (firmware)")] = re.sub(r"\s{2,}", " · ", fw)
    b[_("Network card firmware")] = _text(snmp.get("nic_firmware"))
    b[_("IP address")] = network.get("GSI_TCPIP_IP_ADDR") or identity.get("ip_addr") or ip
    b[_("Subnet / gateway")] = " / ".join(x for x in (network.get("GSI_TCPIP_SUBNET"), network.get("GSI_TCPIP_GATEWAY")) if x)
    b[_("IP assignment")] = {1: _("Static"), 2: "BOOTP", 3: "DHCP"}.get(network.get("GSI_TCPIP_IP_ASSIGN_METHOD"), "")
    b[_("MAC address")] = identity.get("mac_addr") or ""
    memo = ((ev.get("capability") or {}).get("ram") or {}).get("capa")
    b[_("Memory")] = "%d MB" % (memo // 1024) if isinstance(memo, int) and memo else ""
    if r["running_time"]:
        s = int(r["running_time"])
        b[_("Uptime")] = (_("%d d %d h %d min") % (s // 86400, s % 86400 // 3600, s % 3600 // 60)
                          if s >= 86400 else _("%d h %d min") % (s // 3600, s % 3600 // 60))
    enabled_protocols = [name for key, name in (("GSI_IPP_ENABLE", "IPP"), ("GSI_LPD_ENABLE", "LPD"),
                                   ("GSI_IP9100_ENABLE", "RAW 9100"), ("GSI_WSD_ENABLE", "WSD"),
                                   ("GSI_SNMP_ENABLE", "SNMP"), ("GSI_MDNS_ENABLE", "Bonjour"),
                                   ("GSI_SSDP_ENABLE", "SSDP")) if protocols.get(key) == 1]
    b[_("Enabled protocols")] = ", ".join(enabled_protocols)
    r["info"] = {k: v for k, v in b.items() if v}

    # --- Security
    if ev.get("GXI_ADMIN_WUI_HAS_DEFAULT_PASS") == 1:
        r["security"].append((_("Web admin password is still the factory default"),
                              _("Anyone on the network can change the device settings. "
                                "Change the administrator password in the web admin.")))

    # --- Overall level
    if not ev and not snmp:
        r["level"] = "error"
    elif any(s["level"] == "error" for s in r["problems"]):
        r["level"] = "error"
    elif r["problems"]:
        r["level"] = "warning"
    else:
        r["level"] = "ok"
    return r


def problem_signature(report):
    return {s["token"]: s for s in (report or {}).get("problems", [])}


def problem_diff(previous, now):
    """(new_problems, resolved) — previous/now: problem_signature() dicts or None."""
    if previous is None:
        previous = {}
    return ([s for k, s in now.items() if k not in previous],
            [s for k, s in previous.items() if k not in now])


def monitor_device(settings, notify, shutdown, interval=60):
    """Inside the service: polls the device regularly, writes the report to the state file
    and sends a desktop notification on a new fault."""
    previous, unreachable, wait = None, False, interval
    while not shutdown.is_set():
        try:
            report = build_device_report(settings.device)
            if report.get("level") == "error" and not report.get("model"):
                raise OSError("; ".join(report["source_errors"].values())[:200] or _("no response"))
            if unreachable:
                log.info("Device reachable again")
            unreachable = False
            now = problem_signature(report)
            new, resolved = problem_diff(previous, now)
            for s in new:
                log.warning("Device problem: %s (%s)", s["title"], s["source"])
                notify(_("Printer: %s") % s["title"], s.get("advice") or report.get("status") or "",
                       "printer-error")
            for s in resolved:
                log.info("Problem resolved: %s", s["title"])
            previous = now
            write_state(device_report=report)
        except Exception as e:
            if not unreachable:
                log.warning("Cannot monitor device: %s", friendly_error(e))
                notify(_("Printer unreachable"), str(e)[:120], "printer-error")
            unreachable = True
            write_state(device_report={"level": "error", "time": time.time(), "access_error": str(e)[:200]})
        # unreachable: poll less often (2, 4, then every 5 minutes) until it answers again
        wait = min(wait * 2, 300) if unreachable else interval
        shutdown.wait(wait)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

class Profile:
    def __init__(self, key, b, client_name):
        self.context = key
        self.name = (b.get("name", "") or client_name).strip()[:15]
        self.color_name = b.get("color", "color").lower()
        self.color = COLOR_MODES.get(self.color_name, "RGB24")
        self.dpi = b.getint("resolution", 300)
        self.paper = b.get("paper", "A4")
        self.file_format = b.get("format", "pdf").lower()
        if self.paper not in PAPER_SIZES:
            raise ValueError(_("unknown paper: %s") % self.paper)
        if self.file_format not in ("pdf", "jpg"):
            raise ValueError(_("format must be pdf or jpg"))


class Settings:
    def __init__(self, path=SETTINGS_PATH):
        migrate_legacy_settings(path)
        c = settings_parser()
        read_ok = c.read(path, encoding="utf-8")
        self.language = c.get("client", "language", fallback="auto").strip() or "auto"
        i18n.set_language(self.language)
        if not read_ok:
            raise SystemExit(_("Could not read the settings file: %s") % path)
        self.device = c.get("device", "address").strip()
        self.client_name = (c.get("client", "name", fallback="").strip()
                            or socket.gethostname()).upper()[:15]
        self.port = c.getint("client", "port", fallback=53580)
        self.notify_ip = c.get("client", "ip", fallback="").strip()
        self.folder = os.path.expanduser(c.get("client", "folder", fallback="").strip() or "~/" + _("Scans"))
        self.notify_user = c.getboolean("client", "desktop_notifications", fallback=True)
        self.profiles = {}
        for section in c.sections():
            if section.startswith("profile:"):
                key = re.sub(r"[^A-Za-z0-9]", "", section.split(":", 1)[1]) or "Scan"
                self.profiles[key] = Profile(key, c[section], self.client_name)
        if not self.profiles:
            self.profiles["Scan"] = Profile("Scan", c["DEFAULT"], self.client_name)
        if not self.notify_ip:
            self.notify_ip = local_ip(self.device)


def local_ip(device_url):
    host = urllib.parse.urlparse(device_url).hostname
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_state_lock = threading.Lock()


def write_state(**fields):
    with _state_lock:
        d = read_state()
        d.update(fields)
        d["update"] = time.time()
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".%d.part" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, default=str)
        os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# Subscription (scan from the panel to the PC)
# --------------------------------------------------------------------------

class Subscription:
    DURATION = "PT1H"
    REFRESH_INTERVAL = 600  # seconds

    def __init__(self, settings):
        self.settings = settings
        self.identity = None
        self.admin = settings.device
        self.tokens = {}
        self.stop = threading.Event()

    @property
    def notify_address(self):
        return "http://%s:%d/printer-center/%s" % (self.settings.notify_ip, self.settings.port,
                                               self.settings.client_name)

    def subscribe(self):
        targets = "".join(
            "<wscn:ScanDestination><wscn:ClientDisplayName>{name}</wscn:ClientDisplayName>"
            "<wscn:ClientContext>{bg}</wscn:ClientContext></wscn:ScanDestination>".format(
                name=escape(p.name), bg=escape(p.context))
            for p in self.settings.profiles.values())
        body = (
            "<wse:Subscribe><wse:EndTo><wsa:Address>{n}</wsa:Address></wse:EndTo>"
            '<wse:Delivery Mode="{wse}/DeliveryModes/Push"><wse:NotifyTo>'
            "<wsa:Address>{n}</wsa:Address></wse:NotifyTo></wse:Delivery>"
            "<wse:Expires>{s}</wse:Expires>"
            '<wse:Filter Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/Action">{a}</wse:Filter>'
            "<wscn:ScanDestinations>{h}</wscn:ScanDestinations></wse:Subscribe>"
        ).format(n=escape(self.notify_address), wse=NS["wse"], s=self.DURATION,
                 a=A_SCAN_AVAILABLE, h=targets)
        __, raw = soap_send(self.settings.device, A_SUBSCRIBE, body)
        root = ET.fromstring(raw)
        self.identity = xml_text(root, "Identifier")
        direction = find_target(root, "SubscriptionManager")
        self.admin = xml_text(direction, "Address", self.settings.device) if direction is not None else self.settings.device
        self.tokens = {}
        for dr in root.iter():
            if local(dr.tag) == "DestinationResponse":
                self.tokens[xml_text(dr, "ClientContext")] = xml_text(dr, "DestinationToken")
        names = [p.name for p in self.settings.profiles.values()]
        log.info("Registered on device: %s; notify %s", ", ".join(names), self.notify_address)
        write_state(registered=True, error="", names=names, recorded_at=time.time())

    def _identity_header(self):
        return "<wse:Identifier>%s</wse:Identifier>" % escape(self.identity or "")

    def refresh(self):
        body = "<wse:Renew><wse:Expires>%s</wse:Expires></wse:Renew>" % self.DURATION
        soap_send(self.admin, A_RENEW, body, self._identity_header())
        write_state(registered=True, error="")

    def cancel(self):
        if self.identity:
            try:
                soap_send(self.admin, A_UNSUBSCRIBE, "<wse:Unsubscribe/>",
                            self._identity_header(), timeout=5)
                log.info("Registration removed from device")
            except Exception as e:
                log.warning("Could not remove registration: %s", e)
            self.identity = None
        write_state(registered=False, error="", running=False)

    def loop(self):
        delay, retry = 0, 0  # retry > 0 while the device cannot be reached
        while not self.stop.wait(delay):
            try:
                if self.identity:
                    try:
                        self.refresh()
                    except (SoapError, OSError) as e:
                        log.warning("Renew failed (%s), registering again", e)
                        self.identity = None
                        self.subscribe()
                else:
                    self.subscribe()
                delay, retry = self.REFRESH_INTERVAL, 0
            except (SoapError, OSError, ET.ParseError) as e:
                # logged once per outage; retries get slower (1, 2, 4, then every 5 minutes)
                if not retry:
                    log.warning("Could not reach device for panel scanning: %s; retrying quietly", friendly_error(e))
                write_state(registered=False, error=_("Could not reach the device: %s") % friendly_error(e))
                retry = min(retry * 2, 300) if retry else 60
                delay = retry


class PanelScanner:
    """Scans requested from the device panel are queued and handled one at a time.
    The queue is published in the state file ("scan_queue") for the app."""

    def __init__(self, settings, subscription):
        self.settings = settings
        self.subscription = subscription
        self.queue = queue.Queue()
        self.pending = []  # [{"id", "target", "time", "status"}] in order
        self.pending_lock = threading.Lock()
        threading.Thread(target=self.worker, daemon=True).start()

    def publish(self):
        with self.pending_lock:
            write_state(scan_queue=[dict(i) for i in self.pending])

    def set_status(self, item, status):
        with self.pending_lock:
            item["status"] = status
        self.publish()

    def enqueue(self, context, scan_identifier):
        p = self.settings.profiles.get(context)
        if p is None:
            log.warning("Unknown destination: %r", context)
            return
        item = {"id": uuid.uuid4().hex[:8], "target": p.name, "time": time.time(), "status": "waiting"}
        with self.pending_lock:
            self.pending.append(item)
            position = len(self.pending)
        log.info("Scan queued: %s (position %d)", p.name, position)
        self.publish()
        self.queue.put((item, p, scan_identifier))

    def worker(self):
        while True:
            item, p, scan_identifier = self.queue.get()
            try:
                self.scan(item, p, scan_identifier)
            finally:
                with self.pending_lock:
                    self.pending = [i for i in self.pending if i["id"] != item["id"]]
                self.publish()

    def scan(self, item, p, scan_identifier):
        def on_wait(reason, seconds):
            status = {"device": "busy", "local": "waiting"}.get(reason, "scanning")
            if item["status"] != status:
                self.set_status(item, status)
        try:
            with scan_slot(on_wait):
                ticket = ticket_xml(p.name, p.color, p.dpi, p.paper)
                images = do_scan(self.settings.device, ticket, scan_identifier,
                                 self.subscription.tokens.get(p.context, ""), on_wait=on_wait)
            self.save(p, images)
        except Exception as e:
            log.exception("Scan failed: %s", e)
            write_state(error=_("Scan failed: %s") % e)
            self.notify(_("Scan failed"), str(e)[:200])

    def save(self, p, images):
        os.makedirs(self.settings.folder, exist_ok=True)
        if p.file_format == "pdf":
            paths = [unique_path(self.settings.folder, _("Scanned document"), ".pdf")]
            items_data = [jpegs_to_pdf(images, p.dpi)]
        else:
            paths = [unique_path(self.settings.folder, _("Scanned document"), ".jpg") for __ in images]
            items_data = images
        for path, data in zip(paths, items_data):
            write_file(path, data)
            log.info("Saved: %s (%d KB)", path, len(data) // 1024)
        last = read_state().get("recent_scans", [])
        last = [{"path": y, "time": time.time(), "target": p.name} for y in paths] + last
        write_state(recent_scans=last[:20], error="")
        self.notify(_("Scan received"), os.path.basename(paths[0]))

    def notify(self, title, body, icon="scanner"):
        """Desktop notification. Callers pass icon="printer-error" for printer faults."""
        if not self.settings.notify_user or not shutil.which("notify-send"):
            return
        try:
            subprocess.run(["notify-send", "-i", icon or "scanner", "-a", _("Printer Center"), title, body],
                           timeout=5, check=False)
        except Exception:
            pass


def handler_class(scanner):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "printer-center"

        def log_message(self, file_format, *args):
            log.debug("HTTP %s - %s", self.client_address[0], file_format % args)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 1 << 20))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                log.warning("Unparsable notification: %s", self.client_address[0])
                return
            action = xml_text(root, "Action", "")
            if action.endswith("ScanAvailableEvent"):
                context = xml_text(root, "ClientContext", "")
                identity = xml_text(root, "ScanIdentifier", "")
                log.info("Scan request from panel: destination=%s, source=%s", context,
                         self.client_address[0])
                scanner.enqueue(context, identity)
            elif action.endswith("SubscriptionEnd"):
                log.warning("Device ended the subscription, will register again")
                scanner.subscription.identity = None
                scanner.subscription.stop.set()
            else:
                log.debug("Ignored notification: %s", action)

    return Handler


def watch_updates(notify, shutdown, interval=300):
    """Notify the user about a new version installed by the daily updater (root)."""
    marker = os.path.join(os.path.dirname(STATE_PATH), "update-notified")
    shutdown.wait(20)
    while not shutdown.is_set():
        try:
            with open(UPDATE_STATE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            try:
                with open(marker, encoding="utf-8") as f:
                    notified = f.read().strip()
            except OSError:
                notified = None
            last = d.get("last_install") or ""
            if last and last != notified:
                new = (dt.datetime.now() - dt.datetime.fromisoformat(last)).days < 2
                if notified is not None or new:
                    notes = (d.get("notes") or "").strip()
                    log.info("Update installed: %s", d.get("current"))
                    notify(_("Printer Center updated"),
                           _("Version %s installed.") % d.get("current") + ("\n" + notes[:300] if notes else ""),
                           "system-software-update")
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(last)
            latest = d.get("latest")
            if d.get("status") == "update_available" and latest != getattr(watch_updates, "_offered", None):
                watch_updates._offered = latest
                log.info("New version available: %s (automatic installation is off)", latest)
                notify(_("New Printer Center version"),
                       _("Version %s is available. Install it from Settings → About and updates.")
                       % (latest or "").lstrip("v"), "system-software-update")
            if d.get("status") == "error" and d.get("error") != getattr(watch_updates, "_last_error", None):
                log.warning("Automatic update failed: %s", d.get("error"))
            watch_updates._last_error = d.get("error") if d.get("status") == "error" else None
        except (OSError, ValueError):
            pass
        shutdown.wait(interval)


def run_service(settings_path):
    settings = Settings(settings_path)
    subscription = Subscription(settings)
    scanner = PanelScanner(settings, subscription)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", settings.port), handler_class(scanner))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Listening on 0.0.0.0:%d, folder %s", settings.port, settings.folder)
    write_state(running=True, pid=os.getpid())

    shutdown = threading.Event()

    def on_signal(*_):
        shutdown.set()
        subscription.stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=monitor_device, args=(settings, scanner.notify, shutdown), daemon=True).start()
    threading.Thread(target=watch_updates, args=(scanner.notify, shutdown), daemon=True).start()
    while not shutdown.is_set():
        subscription.stop.clear()
        subscription.loop()
        if not shutdown.is_set():
            time.sleep(2)
    subscription.cancel()
    server.shutdown()


# --------------------------------------------------------------------------
# Logging (service and GUI write separate files; the GUI merges and shows them)
# --------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(source)s] %(message)s"


class _SourceFilter(logging.Filter):
    def __init__(self, source):
        super().__init__()
        self.source = source

    def filter(self, record):
        record.source = self.source
        return True


LOG_KEEP_DAYS = 7


def setup_logging(source, verbose=False, console=True):
    """source: e.g. 'service' or 'gui'. ~/.local/state/printer-center/<source>.log, a new file every
    day; files older than LOG_KEEP_DAYS days are deleted automatically."""
    import logging.handlers
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_format = logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S")
    log_filter = _SourceFilter(source)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        for name in os.listdir(LOG_DIR):  # size-rotated files of versions before 2.4
            if re.match(r"^%s\.log\.\d$" % source, name):
                os.remove(os.path.join(LOG_DIR, name))
        file_path = logging.handlers.TimedRotatingFileHandler(
            os.path.join(LOG_DIR, source + ".log"), when="midnight",
            backupCount=LOG_KEEP_DAYS - 1, encoding="utf-8")
        file_path.setFormatter(file_format)
        file_path.addFilter(log_filter)
        root.addHandler(file_path)
    except OSError as e:
        print("Could not open log file:", e, file=sys.stderr)
    if console:
        k = logging.StreamHandler(sys.stdout)
        k.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(k)


def clear_logs():
    """Delete the log history. Open log files are emptied (the processes keep writing to them)."""
    for name in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, name)
        if name.endswith(".log"):
            open(path, "w").close()
        elif ".log." in name:
            os.remove(path)


def read_logs(limit=1000):
    """Merge all log files by time: [(time, level, source, message)]."""
    rows = []
    try:
        names = sorted(os.listdir(LOG_DIR))
    except OSError:
        return []
    pattern = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)? (\w+)\s+\[(\w+)\] (.*)$")
    for name in names:
        if ".log" not in name:
            continue
        try:
            with open(os.path.join(LOG_DIR, name), encoding="utf-8", errors="replace") as f:
                last = None
                for row in f:
                    row = row.rstrip("\n")
                    m = pattern.match(row)
                    if m:
                        last = list(m.groups())
                        rows.append(last)
                    elif last is not None:  # multi-line trace (traceback)
                        last[3] += "\n" + row
        except OSError:
            continue
    rows.sort(key=lambda x: x[0])
    return [tuple(x) for x in rows[-limit:]]


# --------------------------------------------------------------------------
# IPP (print queue: local CUPS and the device's own job list)
# --------------------------------------------------------------------------

IPP_JOB_STATES = {3: N_("Pending"), 4: N_("Held"), 5: N_("Processing"), 6: N_("Stopped"),
                 7: N_("Canceled"), 8: N_("Error"), 9: N_("Completed")}
IPP_JOB_ATTRIBUTES = ("job-id", "job-name", "job-originating-user-name", "job-state",
                   "job-state-reasons", "time-at-creation", "time-at-completed", "job-k-octets",
                   "job-impressions-completed", "job-media-sheets-completed",
                   "job-originating-host-name", "job-printer-uri")


class IppError(Exception):
    pass


def _ipp_attr(make_label, name, value):
    if isinstance(value, bool):
        v = b"\x01" if value else b"\x00"
    elif isinstance(value, int):
        v = struct.pack(">i", value)
    else:
        v = value.encode("utf-8")
    return bytes([make_label]) + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(v)) + v


def ipp_request(printer_uri, operation, attributes=(), timeout=10):
    """Simple IPP/1.1 client. attributes: [(tag, name, value | [values])].
    Returns: (status_code, [ {name: value | [values]} ... ] job/printer groups)."""
    u = urllib.parse.urlparse(printer_uri)
    http_url = "http://%s:%d%s" % (u.hostname, u.port or 631, u.path)
    body = struct.pack(">BBHI", 1, 1, operation, int(time.time()) & 0x7FFFFFFF) + b"\x01"
    body += _ipp_attr(0x47, "attributes-charset", "utf-8")
    body += _ipp_attr(0x48, "attributes-natural-language", i18n.language())
    body += _ipp_attr(0x45, "printer-uri", printer_uri)
    for make_label, name, value in attributes:
        values = value if isinstance(value, (list, tuple)) else [value]
        for i, d in enumerate(values):
            body += _ipp_attr(make_label, name if i == 0 else "", d)
    body += b"\x03"
    request = urllib.request.Request(http_url, body, {"Content-Type": "application/ipp"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as y:
            raw = y.read()
    except urllib.error.HTTPError as e:
        raise IppError("HTTP %s" % e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise IppError(str(getattr(e, "reason", e))) from None
    if len(raw) < 8:
        raise IppError(_("Invalid IPP response"))
    status = struct.unpack(">H", raw[2:4])[0]
    groups, group, name, i = [], None, "", 8
    while i < len(raw):
        t = raw[i]
        i += 1
        if t == 0x03:
            break
        if t < 0x10:
            group = {"_group": t}
            groups.append(group)
            continue
        n = struct.unpack(">H", raw[i:i + 2])[0]
        i += 2
        new_name = raw[i:i + n].decode("utf-8", "replace")
        i += n
        n = struct.unpack(">H", raw[i:i + 2])[0]
        i += 2
        v = raw[i:i + n]
        i += n
        if t in (0x21, 0x23) and len(v) == 4:
            v = struct.unpack(">i", v)[0]
        elif t == 0x22:
            v = bool(v and v[0])
        elif t >= 0x30:
            v = v.decode("utf-8", "replace")
        else:
            v = None
        if group is None:
            continue
        if new_name:
            name = new_name
            group[name] = v
        elif name in group:  # additional value
            group[name] = (group[name] if isinstance(group[name], list) else [group[name]]) + [v]
    return status, groups


def ipp_jobs(printer_uri, which="not-completed", limit=50, user=None):
    """Job list (newest first). which: 'not-completed' | 'completed'."""
    attr = [(0x44, "which-jobs", which), (0x21, "limit", limit),
           (0x44, "requested-attributes", list(IPP_JOB_ATTRIBUTES))]
    if user:
        attr.insert(0, (0x42, "requesting-user-name", user))
    status, groups = ipp_request(printer_uri, 0x000A, attr)
    if status >= 0x0400:
        raise IppError(_("IPP status 0x%04x") % status)
    jobs = []
    for g in groups:
        if g.get("_group") != 2 or "job-id" not in g:
            continue
        status_ = g.get("job-state")
        reason = g.get("job-state-reasons")
        jobs.append({
            "id": g["job-id"],
            "name": g.get("job-name") or _("(untitled)"),
            "user": g.get("job-originating-user-name") or "",
            "machine": g.get("job-originating-host-name") or "",
            "status": status_,
            "status_text": _(IPP_JOB_STATES[status_]) if status_ in IPP_JOB_STATES else str(status_),
            "reason": reason if isinstance(reason, list) else ([reason] if reason else []),
            "time": g.get("time-at-creation") if isinstance(g.get("time-at-creation"), int) else None,
            "completed": g.get("time-at-completed") if isinstance(g.get("time-at-completed"), int) else None,
            "kbytes": g.get("job-k-octets"),
            "page": g.get("job-impressions-completed") or g.get("job-media-sheets-completed"),
        })
    jobs.sort(key=lambda x: x["id"], reverse=True)
    return jobs


def ipp_cancel(printer_uri, job_id, user):
    status, groups = ipp_request(printer_uri, 0x0008, [
        (0x21, "job-id", int(job_id)), (0x42, "requesting-user-name", user)])
    if status >= 0x0400:
        message = next((g.get("status-message") for g in groups if g.get("status-message")), "")
        raise IppError(message or _("IPP status 0x%04x") % status)


def device_ipp_uri(device_url):
    return "ipp://%s/ipp/print" % urllib.parse.urlparse(device_url).hostname


def local_queues(device_url):
    """CUPS queues that use the device's IP: [(name, cups_uri)]."""
    ip = urllib.parse.urlparse(device_url).hostname or ""
    try:
        r = subprocess.run(["lpstat", "-v"], capture_output=True, text=True, timeout=10,
                           env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired):
        return []
    result = []
    for row in r.stdout.splitlines():
        m = re.match(r"device for (\S+): (\S+)", row)
        if m and ip and re.search(r"[/@]%s([:/]|$)" % re.escape(ip), m.group(2)):
            result.append((m.group(1), "ipp://localhost/printers/" + m.group(1)))
    return result


def local_cancel(queue, job_id=None):
    """Cancel a CUPS job with 'cancel' (CUPS checks permission via the local socket)."""
    cmd = ["cancel", "%s-%s" % (queue, job_id)] if job_id else ["cancel", "-u", os.environ.get("USER", ""), queue]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise IppError((r.stderr or r.stdout).strip() or _("cancel failed"))


def main():
    ap = argparse.ArgumentParser(description="WSD scan-from-panel-to-PC service")
    ap.add_argument("--settings", dest="settings", default=SETTINGS_PATH)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging("service", args.verbose)
    run_service(args.settings)


if __name__ == "__main__":
    main()
