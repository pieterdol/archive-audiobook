#!/usr/bin/env python3
"""Fetch an audiobook off archive.org — as numbered MP3s, or as one .m4b with chapter marks.

archive.org is where the recordings are. LibriVox is where most of the good ones come from —
its volunteers read public-domain books and upload every one to the Internet Archive — so
`search` looks in their collection, while `get` and `m4b` will take any item at all.

    ./audiobook.py search the time machine
    ./audiobook.py get time_machine_ms_librivox
    ./audiobook.py m4b time_machine_ms_librivox --upload
    ./audiobook.py m4b https://archive.org/download/time_machine_ms_librivox

Two calls do the work: /metadata/<id> lists an item's files, /download/<id>/<file> fetches one.
LibriVox recordings are public domain; the licence of the item it found is printed before
anything is downloaded, so an item that turns out not to be can be left alone.

Standard library only, so it runs with any python3 — the app's venv is not needed.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ARCHIVE = "https://archive.org"
AGENT = {"User-Agent": "archive-audiobook (personal use)"}
# Best first. VBR is what LibriVox uploads; the fixed-rate ones are archive.org's derivatives,
# and 64 kbps of a single voice reading is perfectly listenable at a third of the size.
FORMATS = ["VBR MP3", "128Kbps MP3", "64Kbps MP3"]
# What `search` looks in unless told otherwise. archive.org holds everything, so the same words
# unfiltered come back as film trailers and scanned copies of the book; even audio alone returns
# radio plays, a remastered film score and podcast episodes. librivoxaudio is the shelf of books
# read aloud by volunteers, which is the one worth searching. "any" widens it to all audio.
COLLECTION = "librivoxaudio"
# Ways of saying "don't scope it to a collection". "audio" is in here because it's the obvious
# guess and it is *not* a collection anything sits in directly — asking for it found nothing at
# all, which reads as "no such book".
ANY = {"any", "all", "audio", "*", ""}
# What archive.org answers when it's had enough of you. 460 is its own, undocumented and not a
# broken file — the same URL serves in full a few seconds later, which is how a book fell over
# on track 15 of 17. 429 and the 5xx family mean the same thing here.
BUSY = {429, 460, 500, 502, 503, 504}
TRIES = 5
BACKOFF = 4          # seconds, doubling: 4, 8, 16, 32
PAUSE = 1            # between tracks, since a book is a couple of hundred megabytes in one go
# Proton Drive's CLI, and where a book goes in it. The folder has to exist already — this
# uploads, it doesn't build a tree.
PROTON = os.path.expanduser("~/.local/bin/proton-drive")
PROTON_DEST = os.environ.get("PROTON_DEST", "/my-files/Audiobooks")
# An item's identifier sits in the same place in every URL archive.org has for it.
_ITEM_URL = re.compile(r"archive\.org/(?:download|details|metadata|embed|stream|serve)/([^/?#]+)")


def _wait(seconds, why):
    print(f"    … {why}, waiting {seconds}s", flush=True)
    time.sleep(seconds)


def open_url(url, timeout=60, extra=None):
    """The response, waiting out a refusal rather than giving up on it.

    Retry-After is honoured where it's sent; where it isn't the wait doubles. A code that isn't
    about being busy — a 404 for a file that has moved — is raised at once, since waiting won't
    make it appear.
    """
    for attempt in range(TRIES):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=AGENT | (extra or {})), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code not in BUSY or attempt == TRIES - 1:
                raise
            delay = int(e.headers.get("Retry-After") or 0) or BACKOFF * 2 ** attempt
            _wait(delay, f"archive.org said {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == TRIES - 1:
                raise
            _wait(BACKOFF * 2 ** attempt, f"{type(e).__name__}")


def get_json(url):
    with open_url(url) as r:
        return json.load(r)


def search_query(words, collection=COLLECTION):
    """The lucene query: some words of a title, inside one collection or across all audio."""
    scope = ("mediatype:audio" if not collection or collection.strip().lower() in ANY
             else f"collection:{collection}")
    return f'{scope} AND title:("{" ".join(words)}")'


def search(words, rows=8, collection=COLLECTION):
    """Items matching some words of a title. Only the search is scoped to LibriVox; everything
    downstream takes any item archive.org has."""
    url = (f"{ARCHIVE}/advancedsearch.php?q={urllib.parse.quote(search_query(words, collection))}"
           "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=creator&fl%5B%5D=runtime"
           f"&rows={rows}&output=json")
    return get_json(url)["response"]["docs"]


def identifier_of(text):
    """The item's identifier, from itself or from any archive.org URL that names one.

    Every page of an item has it in the same place — /details/ for the page you land on,
    /download/ for the file listing, /metadata/ for the JSON — so the path after that word is
    what's wanted. A URL pointing at one file inside the item still means the whole book: this
    fetches recordings, not tracks.
    """
    text = (text or "").strip().strip("<>")
    found = _ITEM_URL.search(text)
    if found:
        return urllib.parse.unquote(found.group(1))
    if "/" in text or text.lower().startswith("http"):
        sys.exit(f"that doesn't name an archive.org item: {text}")
    return text


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

    Plenty of readers number their titles too — "01 - Introduction" — and prefixing that gives
    "01 01 - Introduction". The leading number comes off, but only when it is this track's own:
    a chapter called "1984" keeps its name.
    """
    title = re.sub(r"[^\w \-.,'()]+", " ", str(f.get("title") or "")).strip()
    title = re.sub(r"\s{2,}", " ", title)
    already = re.match(r"(\d{1,3})(?!\d)\s*[-–—.:)]*\s*", title)
    if already and int(already.group(1)) in (index, track_number(f)):
        title = title[already.end():].strip()
    stem = f"{index:02d} {title}" if title else f"{index:02d} {f['name'].rsplit('.', 1)[0]}"
    return stem[:120] + os.path.splitext(f["name"])[1]


def download(url, path, size):
    """One track, skipping it when it's already here whole — an interrupted run resumes by
    being run again.

    A part-written track carries on where it stopped: archive.org serves byte ranges, so the
    megabytes already here are asked for again only if the server ignores the range — which is
    also how a connection that dies mid-body is picked up, since each attempt starts from
    whatever is on disk.
    """
    if os.path.exists(path) and size and os.path.getsize(path) == size:
        return False
    tmp = path + ".part"
    for attempt in range(TRIES):
        try:
            have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            resuming = bool(size and 0 < have < size)
            r = open_url(url, 300, {"Range": f"bytes={have}-"} if resuming else None)
            # 206 means it honoured the range; a 200 is the whole file again, so start over
            # rather than append the beginning of it to the middle.
            resuming = resuming and getattr(r, "status", 200) == 206
            if resuming:
                print(f"    … carrying on from {round(have / 1e6, 1)} MB", flush=True)
            with r, open(tmp, "ab" if resuming else "wb") as out:
                while chunk := r.read(1 << 16):
                    out.write(chunk)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == TRIES - 1:
                raise
            _wait(BACKOFF * 2 ** attempt, f"{type(e).__name__} part way through")
            continue
        if not size or os.path.getsize(tmp) == size:
            os.replace(tmp, path)         # so a half-written file is never mistaken for done
            return True
        os.remove(tmp)          # came back the wrong length: no salvaging it, fetch it clean
        if attempt < TRIES - 1:
            _wait(BACKOFF * 2 ** attempt, "that came back the wrong length")
    sys.exit(f"{os.path.basename(path)} kept arriving the wrong length — try again later")


def upload(paths, dest=None):
    """Put files in Proton Drive, so the book is on the phone without a cable.

    A conflict strategy is not optional: without one the CLI asks what to do about a file
    that's already there, and a script waiting on an answer nobody is there to give looks
    exactly like a hung upload. `replace` means fetching a book twice replaces it rather than
    leaving "The Time Machine (1).m4b" behind. Thumbnails are skipped — there's nothing to see
    in an audiobook.
    """
    dest = dest or PROTON_DEST
    if not os.path.exists(PROTON):
        sys.exit(f"{PROTON} isn't installed, and it's what uploads to Proton Drive")
    size = round(sum(os.path.getsize(p) for p in paths) / 1e6)
    print(f"\nUploading {len(paths)} file{'' if len(paths) == 1 else 's'}, {size} MB, "
          f"to {dest} …", flush=True)
    r = subprocess.run([PROTON, "filesystem", "upload", "-f", "replace", "-t", *paths, dest],
                       capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        sys.exit("proton-drive failed:\n" + ((r.stderr or r.stdout).strip() or "")[-600:])
    print("📤 Uploaded — it'll be on the phone shortly.")


def fetch(identifier, into, want=None):
    """Download the item's tracks. -> (what the item says about itself, [(path, title)])."""
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
    got = []
    for i, f in enumerate(chosen, start=1):
        name = filename(i, f)
        url = f"{ARCHIVE}/download/{identifier}/{urllib.parse.quote(f['name'])}"
        path = os.path.join(into, name)
        made = download(url, path, int(f.get("size") or 0))
        print(f"  {'✓' if made else '·'} {name}", flush=True)
        got.append((path, os.path.splitext(name)[0][3:] or name))
        if made and i < len(chosen):
            time.sleep(PAUSE)          # a book is a couple of hundred megabytes in one go
    if picture := cover_file(meta["files"]):
        url = f"{ARCHIVE}/download/{identifier}/{urllib.parse.quote(picture['name'])}"
        download(url, os.path.join(into, "cover.jpg"), int(picture.get("size") or 0))
    return about, got


def cover_file(files):
    """The item's artwork, or None. The full JPEG where there is one; the thumbnail is 180px
    and looks it on a phone."""
    for want in ("JPEG", "Item Tile", "JPEG Thumb"):
        for f in files:
            if f.get("format") == want and f.get("name", "").lower().endswith((".jpg", ".jpeg")):
                return f
    return None


def read_names(path, count):
    """Chapter names from a file, one per line, blank lines and # comments passed over.

    Open Library has no table of contents to fetch — nothing in eighty editions of four
    classics had one — and an item whose tracks are called "track 03" has nothing worth using
    either, so the way to fix a chapter list is to write it.

    The count has to match: names silently pairing off against the wrong tracks would be worse
    than refusing, and off-by-one is exactly what happens when a reader's intro is a track.
    """
    with open(path) as f:
        names = [line.strip() for line in f
                 if line.strip() and not line.lstrip().startswith("#")]
    if len(names) != count:
        sys.exit(f"{path} has {len(names)} names for {count} tracks — "
                 f"one per line, in playing order")
    return names


def seconds_of(path):
    """How long a track actually is, asked of the file rather than taken from the listing:
    the chapter marks are only as good as this, and the listing's length is a rounded string."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, timeout=120)
    try:
        return float(out.stdout.strip())
    except ValueError:
        sys.exit(f"ffprobe couldn't read {path}")


def concat_line(path):
    """One line of ffmpeg's concat list. It quotes with single quotes, so a quote inside the
    name has to be escaped or the path ends early — "15 The Time Traveller's Return.mp3"."""
    return "file '" + path.replace("'", r"'\''") + "'\n"


def chapter_meta(title, author, chapters):
    """FFMETADATA naming the whole and marking each track, in milliseconds from the start.

    A chapter ends where the next begins rather than at its own length, so a rounding error
    can't leave a gap that a player reads as a hole in the book.
    """
    lines = [";FFMETADATA1", f"title={title}", f"album={title}", f"artist={author}",
             "genre=Audiobook"]
    at = 0.0
    for name, length in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(at * 1000)}",
                  f"END={int((at + length) * 1000)}", f"title={name}"]
        at += length
    return "\n".join(lines) + "\n"


def build_m4b(about, got, into, bitrate="64k", names=None):
    """One .m4b out of the tracks: chapter marks, cover, and the metadata a player reads.

    Mono at 64 kbps. The source is one voice reading, so the channels carry the same thing and
    the bitrate is generous for speech — 188 MB of MP3 comes out around 95 MB, which matters on
    a phone.
    """
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} isn't installed, and it's what makes the .m4b")
    title = str(about.get("title") or "audiobook")
    out = os.path.join(into, re.sub(r"[^\w \-.,'()]+", " ", title).strip()[:120] + ".m4b")
    building = out + ".part"                   # renamed when whole, never half an audiobook
    print(f"\nMeasuring {len(got)} tracks…", flush=True)
    titles = read_names(names, len(got)) if names else [name for _p, name in got]
    chapters = [(title, seconds_of(path)) for title, (path, _n) in zip(titles, got)]
    listing = os.path.join(into, "concat.txt")
    metafile = os.path.join(into, "chapters.txt")
    with open(listing, "w") as f:
        f.writelines(concat_line(os.path.abspath(p)) for p, _ in got)
    with open(metafile, "w") as f:
        f.write(chapter_meta(title, str(about.get("creator") or ""), chapters))
    cover = os.path.join(into, "cover.jpg")
    cmd = ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", listing,
           "-i", metafile]
    if os.path.exists(cover):
        cmd += ["-i", cover]
    cmd += ["-map", "0:a", "-map_metadata", "1"]
    if os.path.exists(cover):
        # attached_pic is what makes a player show it as the book's artwork
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    # -f ipod names the muxer the .m4b extension would have picked; the .part name can't.
    cmd += ["-c:a", "aac", "-b:a", bitrate, "-ac", "1", "-movflags", "+faststart",
            "-f", "ipod", building]
    hours = round(sum(length for _n, length in chapters) / 3600, 1)
    print(f"Encoding {hours} h to AAC {bitrate} mono — a few minutes…", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0 or not os.path.exists(building):
        sys.exit("ffmpeg failed:\n" + (r.stderr or "")[-600:])
    os.replace(building, out)
    for scratch in (listing, metafile):
        os.remove(scratch)
    print(f"\n{out}\n{len(chapters)} chapters, {round(os.path.getsize(out) / 1e6)} MB")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    listed = sub.add_parser("names", help="print the chapter names, to edit and pass to m4b")
    listed.add_argument("identifier", help="the identifier, or any archive.org URL for it")
    listed.add_argument("--format", dest="fmt", help=f"one of {', '.join(FORMATS)}")
    found = sub.add_parser("search", help="find a recording by title")
    found.add_argument("words", nargs="+")
    found.add_argument("--collection", default=COLLECTION,
                       help=f"archive.org collection to look in (default: {COLLECTION}; "
                            f'"any" searches all audio)')
    for name, help_text in (("get", "download one, by the identifier search prints"),
                            ("m4b", "download it and make one .m4b with chapter marks")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("identifier", help="the identifier, or any archive.org URL for it")
        p.add_argument("--dir", default="audiobooks",
                       help="where to put it (default: audiobooks/)")
        p.add_argument("--format", dest="fmt", help=f"one of {', '.join(FORMATS)}")
        if name == "m4b":
            p.add_argument("--bitrate", default="64k", help="AAC bitrate (default: 64k mono)")
            p.add_argument("--names", metavar="FILE",
                           help="chapter names, one per line, in place of the tracks' own")
        p.add_argument("--upload", action="store_true",
                       help=f"put it in Proton Drive at {PROTON_DEST}")
        p.add_argument("--dest", help="a different Proton Drive folder")
    args = parser.parse_args(argv)
    if args.command == "names":
        meta = get_json(f"{ARCHIVE}/metadata/{identifier_of(args.identifier)}")
        _fmt, chosen = tracks(meta.get("files") or [], args.fmt)
        print(f"# {len(chosen)} tracks — one name per line, in playing order")
        for i, f in enumerate(chosen, start=1):
            print(os.path.splitext(filename(i, f))[0][3:] or f["name"])
    elif args.command == "search":
        found = search(args.words, collection=args.collection)
        for d in found:
            print(f"{d['identifier']:<40} {str(d.get('runtime') or '?'):>9}  "
                  f"{str(d.get('title'))[:38]:<40} {str(d.get('creator') or '')[:24]}")
        if not found:
            # Nothing printed at all reads as "no such book", when it is just as likely to be
            # a collection nothing is in — which is what --collection audio turned out to be.
            where = ("all audio" if not args.collection or args.collection.lower() in ANY
                     else f"collection:{args.collection}")
            print(f"Nothing in {where} with that in the title."
                  + ("" if where == "all audio" else "  Try --collection any."))
    else:
        identifier = identifier_of(args.identifier)
        into = os.path.join(os.path.expanduser(args.dir), identifier)
        about, got = fetch(identifier, into, args.fmt)
        if args.command == "m4b":
            book = build_m4b(about, got, into, args.bitrate, args.names)
            if args.upload:
                upload([book], args.dest)
        else:
            if args.upload:
                upload([p for p, _ in got], args.dest)
            print("\nDone. Import the folder into BookPlayer — it keeps the order "
                  "from the names.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl-C during a book is expected, not a fault: the tracks already here are kept and
        # the one in flight resumes from where it stopped.
        sys.exit("\nStopped. Run the same command to carry on where it left off.")
