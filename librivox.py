#!/usr/bin/env python3
"""Fetch a LibriVox recording off archive.org, as a folder of MP3s for BookPlayer.

Nothing to do with the app — it makes no audio and touches no library. It's here because
getting a human-read public-domain audiobook onto the phone is the other half of the same
job, and a folder of numbered tracks is what BookPlayer imports.

    ./librivox.py search the time machine
    ./librivox.py get time_machine_ms_librivox
    ./librivox.py get time_machine_ms_librivox --dir ~/Audiobooks --format 64Kbps MP3

Two calls do the work: /metadata/<id> lists an item's files, /download/<id>/<file> fetches one.
LibriVox recordings are public domain; the licence of the item it found is printed before
anything is downloaded, so an item that turns out not to be can be left alone.

Standard library only, so it runs with any python3 — the app's venv is not needed.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ARCHIVE = "https://archive.org"
AGENT = {"User-Agent": "librivox-fetch (personal use)"}
# Best first. VBR is what LibriVox uploads; the fixed-rate ones are archive.org's derivatives,
# and 64 kbps of a single voice reading is perfectly listenable at a third of the size.
FORMATS = ["VBR MP3", "128Kbps MP3", "64Kbps MP3"]


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=60) as r:
        return json.load(r)


def search(words, rows=8):
    """Items in the LibriVox collection matching some words of a title."""
    query = f'collection:librivoxaudio AND title:("{" ".join(words)}")'
    url = (f"{ARCHIVE}/advancedsearch.php?q={urllib.parse.quote(query)}"
           "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=creator&fl%5B%5D=runtime"
           f"&rows={rows}&output=json")
    return get_json(url)["response"]["docs"]


def track_number(f):
    """The track's own number. Archive writes it "3/12", and a missing one sorts last."""
    m = re.match(r"\s*(\d+)", str(f.get("track") or ""))
    return int(m.group(1)) if m else 10_000


def tracks(files, want=None):
    """The MP3s of one item, in playing order, in the best format it has.

    One format only — an item carries the same reading three times over, and mixing them would
    download the book twice and play some chapters twice.
    """
    have = {f.get("format") for f in files}
    for fmt in ([want] if want else FORMATS):
        if fmt in have:
            chosen = [f for f in files if f.get("format") == fmt]
            return fmt, sorted(chosen, key=lambda f: (track_number(f), f["name"]))
    return "", []


def filename(index, f):
    """A name that sorts in playing order and says what the track is.

    Numbered from the position in the list rather than from the item's own track numbers,
    which are sometimes missing and occasionally start at zero, and zero-padded because a
    player sorting names alphabetically puts 10 before 2.
    """
    title = re.sub(r"[^\w \-.,'()]+", " ", str(f.get("title") or "")).strip()
    title = re.sub(r"\s{2,}", " ", title)
    stem = f"{index:02d} {title}" if title else f"{index:02d} {f['name'].rsplit('.', 1)[0]}"
    return stem[:120] + os.path.splitext(f["name"])[1]


def download(url, path, size):
    """One track, skipping it when it's already here whole — an interrupted run resumes by
    being run again."""
    if os.path.exists(path) and size and os.path.getsize(path) == size:
        return False
    tmp = path + ".part"
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=300) as r, \
            open(tmp, "wb") as out:
        while chunk := r.read(1 << 16):
            out.write(chunk)
    os.replace(tmp, path)                 # so a half-written file is never mistaken for done
    return True


def fetch(identifier, into, want=None):
    meta = get_json(f"{ARCHIVE}/metadata/{identifier}")
    if not meta.get("files"):
        sys.exit(f"no such item: {identifier}")
    about = meta.get("metadata", {})
    fmt, chosen = tracks(meta["files"], want)
    if not chosen:
        sys.exit(f"no MP3s in {identifier} — it has {sorted({f.get('format') for f in meta['files']})}")
    total = sum(int(f.get("size") or 0) for f in chosen)
    print(f"{about.get('title', identifier)} — {about.get('creator', 'unknown')}")
    print(f"{about.get('licenseurl') or 'no licence stated'}")
    print(f"{len(chosen)} tracks, {fmt}, {round(total / 1e6)} MB → {into}\n")
    os.makedirs(into, exist_ok=True)
    for i, f in enumerate(chosen, start=1):
        name = filename(i, f)
        url = f"{ARCHIVE}/download/{identifier}/{urllib.parse.quote(f['name'])}"
        made = download(url, os.path.join(into, name), int(f.get("size") or 0))
        print(f"  {'✓' if made else '·'} {name}")
    print(f"\nDone. Import the folder into BookPlayer — it keeps the order from the names.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    found = sub.add_parser("search", help="find a recording by title")
    found.add_argument("words", nargs="+")
    got = sub.add_parser("get", help="download one, by the identifier search prints")
    got.add_argument("identifier")
    got.add_argument("--dir", default="audiobooks", help="where to put it (default: audiobooks/)")
    got.add_argument("--format", dest="fmt", help=f"one of {', '.join(FORMATS)}")
    args = parser.parse_args(argv)
    if args.command == "search":
        for d in search(args.words):
            print(f"{d['identifier']:<40} {str(d.get('runtime') or '?'):>9}  "
                  f"{str(d.get('title'))[:38]:<40} {str(d.get('creator') or '')[:24]}")
    else:
        fetch(args.identifier, os.path.join(os.path.expanduser(args.dir), args.identifier),
              args.fmt)


if __name__ == "__main__":
    main()
