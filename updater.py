#!/usr/bin/env python3
"""Printer Center updater (runs as root; needs neither git nor credentials).

Lists the public repository's vX.Y.Z tags over HTTPS (certificate verified), compares the newest one with the installed version and, if newer, fetches exactly that
tag's files (a shallow pack, decoded here) and installs them with `install.sh --files`.
State is written to /var/lib/printer-center/update.json (read by the app and the service).

  updater.py            check; install a newer version unless a user turned automatic installation off
  updater.py --install  check and install now (the app's "Install now" button)
  updater.py --check    check only
  updater.py --force    reinstall even if the version is the same
"""

import argparse
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zlib

CONFIG_FILE = "/etc/printer-center/update.conf"
STATE_FILE = "/var/lib/printer-center/update.json"
INSTALLED_CORE = "/opt/printer-center/printer_center.py"
LOCK_FILE = "/run/printer-center-update.lock"
MAX_PACK_SIZE = 50 * 1024 * 1024


def emit(message):
    print(message, flush=True)  # goes to the systemd journal


def read_config():
    config = {"repo": ""}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except OSError:
        pass
    m = re.search(r"([\w.-]+/[\w.-]+?)(?:\.git)?$", config["repo"])
    config["repo"] = m.group(1) if m else ""
    return config


def auto_install_allowed():
    """False if any user of this computer unticked "Install updates automatically"
    ([client] auto_update = no in ~/.config/printer-center/settings.ini)."""
    import configparser
    for path in glob.glob("/home/*/.config/printer-center/settings.ini"):
        c = configparser.ConfigParser(interpolation=None)
        try:
            c.read(path, encoding="utf-8")
            if not c.getboolean("client", "auto_update", fallback=True):
                return False
        except (configparser.Error, ValueError, OSError):
            continue
    return True


def version_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


def installed_version():
    try:
        with open(INSTALLED_CORE, encoding="utf-8") as f:
            m = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def write_state(**fields):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    event = fields.pop("event", None)
    d.update(fields)
    if event:
        d["history"] = ([{"time": dt.datetime.now().isoformat(timespec="seconds"), "event": event}]
                        + d.get("history", []))[:15]
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# Minimal read-only git client (smart HTTP, protocol v0): list tags, fetch one tag.
# The repository is public, so no credentials are used or stored.
# --------------------------------------------------------------------------

class PktReader:
    def __init__(self, stream):
        self.stream = stream

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.stream.read(n - len(data))
            if not chunk:
                raise RuntimeError("repository connection closed")
            data += chunk
        return data

    def read_pkt(self):
        size = int(self._read(4), 16)
        return None if size == 0 else self._read(size - 4)

    def read_until_flush(self):
        while True:
            pkt = self.read_pkt()
            if pkt is None:
                return
            yield pkt


def pkt_line(line):
    if line is None:
        return b"0000"
    data = line.encode() + b"\n"
    return b"%04x" % (len(data) + 4) + data


def _request(url, data=None):
    headers = {"User-Agent": "git/2.0 printer-center"}
    if data is not None:
        headers.update({"Content-Type": "application/x-git-upload-pack-request",
                        "Accept": "application/x-git-upload-pack-result"})
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60)


class UploadPack:
    def __init__(self, config):
        self.url = "https://github.com/%s.git" % config["repo"]
        self.refs, self.caps = {}, set()
        with _request(self.url + "/info/refs?service=git-upload-pack") as y:
            reader = PktReader(y)
            if not (reader.read_pkt() or b"").startswith(b"# service=git-upload-pack"):
                raise RuntimeError("not a git repository: %s" % self.url)
            for _ in reader.read_until_flush():
                pass
            first = True
            for line in reader.read_until_flush():
                line = line.rstrip(b"\n")
                if first:
                    line, _, caps = line.partition(b"\0")
                    self.caps = set(caps.decode().split())
                    first = False
                sha, _, name = line.decode().partition(" ")
                self.refs[name] = sha

    def fetch(self, sha):
        caps = [c for c in ("side-band-64k", "ofs-delta", "shallow", "no-progress") if c in self.caps]
        body = (pkt_line("want %s %s agent=printer-center" % (sha, " ".join(caps))) + pkt_line("deepen 1")
                + pkt_line(None) + pkt_line("done"))
        pack = bytearray()
        with _request(self.url + "/git-upload-pack", body) as y:
            reader = PktReader(y)
            for _ in reader.read_until_flush():  # shallow/unshallow lines
                pass
            while True:
                pkt = reader.read_pkt()
                if pkt is None:
                    break
                if pkt.startswith(b"NAK") or pkt.startswith(b"ACK"):
                    continue
                if "side-band-64k" in caps:
                    band, pkt = pkt[0], pkt[1:]
                    if band == 3:
                        raise RuntimeError("server error: %s" % pkt.decode(errors="replace").strip())
                    if band != 1:
                        continue
                pack += pkt
                if len(pack) > MAX_PACK_SIZE:
                    raise RuntimeError("package too large")
        return bytes(pack)


TYPES = {1: b"commit", 2: b"tree", 3: b"blob", 4: b"tag"}


def _apply_delta(base, delta):
    def varint(pos):
        value = shift = 0
        while True:
            c = delta[pos]
            pos += 1
            value |= (c & 0x7F) << shift
            shift += 7
            if not c & 0x80:
                return value, pos
    src_size, pos = varint(0)
    dst_size, pos = varint(pos)
    if src_size != len(base):
        raise RuntimeError("delta base size mismatch")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = size = 0
            for i in range(4):
                if op & (1 << i):
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            out += base[offset:offset + (size or 0x10000)]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise RuntimeError("invalid delta")
    if len(out) != dst_size:
        raise RuntimeError("delta result size mismatch")
    return bytes(out)


def parse_pack(pack):
    """Return {sha: (type, data)} for every object in a pack (ofs/ref deltas resolved)."""
    if pack[:4] != b"PACK" or int.from_bytes(pack[4:8], "big") not in (2, 3):
        raise RuntimeError("invalid pack")
    if hashlib.sha1(pack[:-20]).digest() != pack[-20:]:
        raise RuntimeError("pack checksum mismatch")
    count = int.from_bytes(pack[8:12], "big")
    pos, by_offset, by_sha, pending = 12, {}, {}, []
    for _ in range(count):
        start = pos
        c = pack[pos]
        pos += 1
        kind, size, shift = (c >> 4) & 7, c & 15, 4
        while c & 0x80:
            c = pack[pos]
            pos += 1
            size |= (c & 0x7F) << shift
            shift += 7
        base = None
        if kind == 6:
            c = pack[pos]
            pos += 1
            ofs = c & 0x7F
            while c & 0x80:
                c = pack[pos]
                pos += 1
                ofs = ((ofs + 1) << 7) | (c & 0x7F)
            base = ("ofs", start - ofs)
        elif kind == 7:
            base = ("ref", pack[pos:pos + 20].hex())
            pos += 20
        d = zlib.decompressobj()
        data = d.decompress(pack[pos:])
        if not d.eof or len(data) != size:
            raise RuntimeError("corrupt object in pack")
        pos = len(pack) - len(d.unused_data)
        if base is None:
            if kind not in TYPES:
                raise RuntimeError("unknown object type %d in pack" % kind)
            sha = hashlib.sha1(TYPES[kind] + b" %d\0" % len(data) + data).hexdigest()
            by_offset[start] = by_sha[sha] = (TYPES[kind], data)
        else:
            pending.append((start, base, data))
    while pending:
        progress = False
        for item in list(pending):
            start, (how, ref), delta = item
            base = by_offset.get(ref) if how == "ofs" else by_sha.get(ref)
            if base is None:
                continue
            data = _apply_delta(base[1], delta)
            obj = (base[0], data)
            sha = hashlib.sha1(base[0] + b" %d\0" % len(data) + data).hexdigest()
            by_offset[start] = by_sha[sha] = obj
            pending.remove(item)
            progress = True
        if not progress:
            raise RuntimeError("unresolved delta in pack")
    return by_sha


def checkout(objects, sha, target):
    """Write the tree of a tag/commit into target; returns the tag message."""
    kind, data = objects[sha]
    message = ""
    if kind == b"tag":
        head, _, message = data.decode(errors="replace").partition("\n\n")
        sha = re.search(r"^object ([0-9a-f]{40})$", head, re.M).group(1)
        kind, data = objects[sha]
    if kind != b"commit":
        raise RuntimeError("tag does not point to a commit")
    tree = re.search(rb"^tree ([0-9a-f]{40})$", data, re.M).group(1).decode()

    def write_tree(tree_sha, path):
        kind, data = objects[tree_sha]
        pos = 0
        while pos < len(data):
            sp = data.index(b" ", pos)
            nul = data.index(b"\0", sp)
            mode, name = data[pos:sp].decode(), data[sp + 1:nul].decode()
            child = data[nul + 1:nul + 21].hex()
            pos = nul + 21
            if "/" in name or name in (".", ".."):
                raise RuntimeError("unsafe path in tree: %s" % name)
            dest = os.path.join(path, name)
            if mode == "40000":
                os.makedirs(dest, exist_ok=True)
                write_tree(child, dest)
            elif mode in ("100644", "100755"):
                with open(dest, "wb") as f:
                    f.write(objects[child][1])
                os.chmod(dest, 0o755 if mode == "100755" else 0o644)
            # symlinks / submodules are ignored

    write_tree(tree, target)
    return message.strip()


def list_tags(config):
    up = UploadPack(config)
    return {m.group(1): sha for name, sha in up.refs.items()
            if (m := re.fullmatch(r"refs/tags/(v\d+\.\d+\.\d+)", name))}


def install(config, tag, sha):
    up = UploadPack(config)
    pack = up.fetch(sha)
    objects = parse_pack(pack)
    tmp = tempfile.mkdtemp(prefix="printer-center-")
    try:
        notes = checkout(objects, sha, tmp)
        with open(os.path.join(tmp, "printer_center.py"), encoding="utf-8") as f:
            m = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M)
        if not m or version_tuple(m.group(1)) != version_tuple(tag):
            raise RuntimeError("tag %s does not match VERSION in package (%s)" % (tag, m and m.group(1)))
        r = subprocess.run(["sh", os.path.join(tmp, "install.sh"), "--files"],
                           capture_output=True, text=True, timeout=600)
        emit(r.stdout.strip())
        if r.returncode != 0:
            raise RuntimeError("install.sh failed: %s" % (r.stderr.strip() or r.stdout.strip())[-300:])
        return notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Printer Center updater")
    ap.add_argument("--check", action="store_true", help="check only, do not install")
    ap.add_argument("--install", action="store_true", help="install a newer version even if automatic "
                                                          "installation is turned off")
    ap.add_argument("--force", action="store_true", help="reinstall the same version")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root")

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        emit("Another update is running.")
        return 0

    config = read_config()
    current = installed_version()
    now = dt.datetime.now().isoformat(timespec="seconds")
    if not config["repo"]:
        write_state(current=current, last_check=now, status="not_configured",
                    error="No repository configured")
        emit("Not configured: %s has no repo" % CONFIG_FILE)
        return 1
    try:
        write_state(current=current, status="checking", error="")
        tags = list_tags(config)
        if not tags:
            raise RuntimeError("no vX.Y.Z tags in the repository")
        last = max(tags, key=version_tuple)
        newer = version_tuple(last) > version_tuple(current)
        emit("Installed %s, latest release %s" % (current, last))
        manual_only = not (args.install or args.force or auto_install_allowed())
        if manual_only and newer:
            emit("Automatic installation is turned off; waiting for the user to install %s" % last)
        if not (newer or args.force) or args.check or manual_only:
            write_state(current=current, latest=last, last_check=now,
                        status="update_available" if newer else "up_to_date", error="")
            return 0
        write_state(status="installing", latest=last)
        notes = install(config, last, tags[last])
        summary = notes.splitlines()[0][:200] if notes else ""
        write_state(current=installed_version(), latest=last, last_check=now, last_install=now,
                    status="installed", error="", notes=notes[:1000],
                    event="%s -> %s installed%s" % (current, last, ": " + summary if summary else ""))
        emit("Installed: %s" % last)
        return 0
    except Exception as e:
        write_state(current=current, last_check=now, status="error", error=str(e)[:300],
                    event="Error: %s" % str(e)[:200])
        emit("Error: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
