#!/usr/bin/env python3
"""Developer tool: publish a release.

Publish:
  1. Bump VERSION in printer_center.py and commit.
  2. python3 release.py -m "What changed in this version"
  Creates the annotated tag vVERSION (its message carries the release notes), pushes it
  and creates a GitHub Release page. Installed computers fetch the tag anonymously over
  HTTPS on their daily check (or via the button in the app).

The GitHub credential is read from git's credential store, used in memory only and never
printed or written anywhere. Computers never receive it.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com/repos/"


def git(*args, input_text=None):
    r = subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, input=input_text,
                       text=True, timeout=120)
    if r.returncode != 0:
        sys.exit("git %s: %s" % (args[0], r.stderr.strip()))
    return r.stdout


def credentials():
    output = git("credential", "fill", input_text="protocol=https\nhost=github.com\n\n")
    fields = dict(s.split("=", 1) for s in output.splitlines() if "=" in s)
    if not fields.get("password"):
        sys.exit("No GitHub credential found (git credential)")
    return fields["password"]


def api(token, method, path, data=None, missing_ok=False):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(API + path, data=body, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "User-Agent": "printer-center-release", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as y:
            raw = y.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if missing_ok and e.code == 404:
            return None
        sys.exit("GitHub %s %s: %d %s" % (method, path, e.code, e.read()[:300].decode(errors="replace")))


def repository():
    remote = git("remote", "get-url", "origin").strip()
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", remote)
    if not m:
        sys.exit("origin is not a GitHub repository")
    return m.group(1)


def publish(repo, notes):
    with open(os.path.join(BASE_DIR, "printer_center.py"), encoding="utf-8") as f:
        version = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"', f.read(), re.M).group(1)
    tag = "v" + version
    if git("status", "--porcelain", "--untracked-files=no").strip():
        sys.exit("Uncommitted changes; commit first.")
    if git("tag", "--list", tag).strip():
        sys.exit("Tag %s already exists; bump VERSION." % tag)
    previous = git("tag", "--list", "v*", "--sort=-v:refname", "--merged", "HEAD").split()
    if not notes:
        interval = "%s..HEAD" % previous[0] if previous else "HEAD"
        notes = "\n".join("- " + s for s in git("log", "--format=%s", interval).splitlines())
    notes = notes or "Version " + version

    token = credentials()
    git("tag", "-a", tag, "-F", "-", input_text=notes + "\n")
    git("push", "-q", "origin", "HEAD")
    git("push", "-q", "origin", tag)
    if not api(token, "GET", "%s/releases/tags/%s" % (repo, tag), missing_ok=True):
        api(token, "POST", repo + "/releases", {"tag_name": tag, "name": "Version " + version, "body": notes})
    print("Published %s\nNotes:\n%s" % (tag, notes))


def main():
    ap = argparse.ArgumentParser(description="Publish a Printer Center release")
    ap.add_argument("-m", "--notes", help="release notes (default: commit subjects since the last tag)")
    args = ap.parse_args()
    publish(repository(), args.notes)


if __name__ == "__main__":
    main()
