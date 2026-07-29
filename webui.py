#!/usr/bin/env python3
"""A page for the shelf: chapter names typed next to the tracks they belong to, on the phone.

The CLI can do everything this does, but supplying chapter names through it means redirecting
`names` into a file, editing fifty-four lines somewhere else, and remembering to pass --names to
the build. That last part is the whole reason this exists — and so is being able to *hear* track
twelve, because a file called "12 - CH12.mp3" tells you nothing about which chapter it is.

    ./webui.py                  # http://127.0.0.1:8610
    ./serve.sh                  # and the same page over HTTPS on the phone

Two ways of leaning on audiobook.py, and the split is the point:

  * its read-only functions are imported, so what a preview promises is what the download writes —
    the chapter names come out of `filename()` itself rather than a second copy of it;
  * the work runs as a subprocess of the CLI. Every failure in there is sys.exit(a sentence) and
    every step is print(flush=True), so shelling out gets resuming, progress and error messages
    for nothing. Nothing about the CLI had to change to make this page work.

Standard library only, like the script it drives — no venv, no install, nothing to build.

    AUDIOBOOK_ROOT   the shelf (default: audiobooks/ beside this file)
    AUDIOBOOK_PORT   default 8610
"""
import glob
import html
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import audiobook

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.expanduser(os.environ.get("AUDIOBOOK_ROOT")
                                          or os.path.join(HERE, "audiobooks")))
PORT = int(os.environ.get("AUDIOBOOK_PORT", "8610"))
PAGE = os.path.join(HERE, "index.html")
CACHE = os.path.join(ROOT, ".webui.json")
CHUNK = 1 << 16
MAX_UPLOAD = 25 * 1024 * 1024      # a cover, not an album
LOG_CAP = 2 * 1024 * 1024          # a runaway guard; a whole book is about 40 kB of output
JOB_LIMIT = 6 * 3600               # the CLI's own timeouts are 7200s each, with no total
# mimetypes doesn't know .m4b, and the two obvious guesses are both wrong: video/mp4 makes Safari
# open a video player for an audiobook, and octet-stream makes it refuse to play it at all.
MIME = {".m4b": "audio/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
        ".opus": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav", ".aac": "audio/aac",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
# The scratch audiobook.py leaves inside a book folder while it works. Cleared at startup, since a
# job doesn't outlive this process — but never *.mp3.part, which is where a download resumes from.
LEFTOVERS = ("*.m4b.part", "concat.txt", "chapters.txt", ".announcement*", ".cover-src",
             "cover.jpg.tmp", "names.txt.tmp")


def say(*args):
    print(*args, file=sys.stderr, flush=True)


# ----------------------------------------------------------------- the shelf on disk

def one_component(name):
    """A single flat name, and nothing that could climb out of a directory.

    Every path here is <root>/<book>/<file> — two flat components — so requiring both to be plain
    names makes ".." structurally impossible before any filesystem call happens. The realpath
    check in book_file is then a second line rather than the only one.

    Leading dots go too, which conveniently also hides this module's own .webui.json and the
    CLI's .announcement scratch from ever being a book or a served file.
    """
    return (bool(name) and name not in (".", "..") and not name.startswith(".")
            and "/" not in name and "\\" not in name and "\0" not in name
            and os.path.normpath(name) == name)


def book_dir(book_id):
    """The folder of one book, or None."""
    if not one_component(book_id):
        return None
    path = os.path.join(ROOT, book_id)
    return path if os.path.isdir(path) else None


def book_file(book_id, name):
    """One file inside one book, or None.

    dirname != root rather than a prefix test: nesting is refused outright, so a symlink pointing
    out of the folder can't be served. A symlinked *book folder* still works, because book_dir
    doesn't resolve — which is the case a series living on another disk actually needs.
    """
    folder = book_dir(book_id)
    if not folder or not one_component(name):
        return None
    path = os.path.realpath(os.path.join(folder, name))
    if os.path.dirname(path) != os.path.realpath(folder) or not os.path.isfile(path):
        return None
    return path


def is_track(name):
    """What `pack` would treat as a chapter — the same predicate, because if this list disagrees
    with the CLI's then every chapter name is against the wrong track."""
    low = name.lower()
    return (low.endswith(audiobook.AUDIO) and not name.startswith(".")
            and not low.endswith((".m4b", ".part")))


def listing(folder):
    """One scandir: (tracks in playing order, the .m4b, the cover). -> ([(name, bytes)], (name,
    bytes) | None, (name, mtime_ms) | None)"""
    tracks, m4b, cover = {}, None, None
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return [], None, None
    for e in entries:
        if not e.is_file():
            continue
        low = e.name.lower()
        if is_track(e.name):
            tracks[e.name] = e.stat().st_size
        elif low.endswith(".m4b"):
            # Newest wins. There should only ever be one, but a rebuild under a different title
            # would leave the old one behind and the new one is the answer.
            if not m4b or e.stat().st_mtime > m4b[2]:
                m4b = (e.name, e.stat().st_size, e.stat().st_mtime)
        elif e.name == "cover.jpg":
            cover = (e.name, int(e.stat().st_mtime * 1000))
    ordered = [(n, tracks[n]) for n in audiobook.in_order(tracks)]
    return ordered, (m4b[:2] if m4b else None), cover


def title_of(book_id, m4b):
    """The book's title without asking ffprobe. build_m4b names the file from the title, sanitised
    (audiobook.py:532), so the stem *is* the title — for a book that has one. Otherwise the folder
    name, which is what `pack` would have called it anyway."""
    if m4b:
        return os.path.splitext(m4b[0])[0]
    return book_id.replace("_", " ").strip() or book_id


def runtime(seconds):
    """"3 h 36 m", or "4 m" for something short."""
    if not seconds:
        return ""
    hours, minutes = divmod(int(round(seconds / 60)), 60)
    return f"{hours} h {minutes} m" if hours else f"{minutes} m"


# ----------------------------------------------------------------- ffprobe, and not asking twice

cache_lock = threading.Lock()
_cache = None


def cache():
    global _cache
    with cache_lock:
        if _cache is None:
            try:
                with open(CACHE) as f:
                    _cache = json.load(f)
            except (OSError, ValueError):
                _cache = {}
        return _cache


def cache_save():
    """Atomically, the way speech-webui writes its own state: a poll must never read half a file.
    Everything in here is derived, so losing it costs one ffprobe per book."""
    with cache_lock:
        tmp = CACHE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(_cache, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CACHE)
        except OSError as e:
            say(f"couldn't write {CACHE}: {e}")


def probe(path, chapters=False):
    """duration, tags and — for an .m4b — the whole chapter list, in one ffprobe.

    One call rather than one per track: -show_chapters on the finished audiobook answers the same
    question that measuring 477 MP3s would, in 190ms instead of a minute.
    """
    cmd = ["ffprobe", "-v", "error", "-show_format", "-of", "json", path]
    if chapters:
        cmd.insert(-1, "-show_chapters")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    fmt = data.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    try:
        seconds = float(fmt.get("duration") or 0)
    except ValueError:
        seconds = 0.0
    return {"seconds": seconds, "title": tags.get("title", ""), "author": tags.get("artist", ""),
            "chapters": [(c.get("tags") or {}).get("title", "")
                         for c in (data.get("chapters") or [])]}


def probe_track(path):
    """The artist, and any pictures inside the file, in one ffprobe.

    One call for both, because a book with no cover.jpg needs both answers and the tracks are
    where they live — a publisher's MP3s usually carry the artwork the CLI would otherwise have
    had to download.
    """
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                              "-of", "json", path],
                             capture_output=True, text=True, timeout=120)
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"author": "", "art": []}
    tags = {k.lower(): v for k, v in ((data.get("format") or {}).get("tags") or {}).items()}
    art = [{"index": s.get("index"), "codec": s.get("codec_name"),
            "width": s.get("width") or 0, "height": s.get("height") or 0,
            "what": (s.get("tags") or {}).get("comment", "")}
           for s in (data.get("streams") or [])
           if s.get("codec_type") == "video" and (s.get("width") or 0) > 0]
    return {"author": tags.get("artist", ""), "art": art}


def best_art(art):
    """Which of a track's pictures is the book's cover, or None.

    Publishers attach more than one. Two seen in the wild: an 800x1200 cover alongside a 400x400
    series emblem, and a 1877x2851 cover alongside a 500x386 photograph of the CD case with the
    disc beside it.

    Landscape loses first, which is what rules out the CD-case shot, and then the biggest wins,
    which is what rules out the symbol. The ID3 picture type is deliberately not the deciding
    vote — it is what calls that symbol "Cover (front)" while the real cover says "Other" — but it
    breaks a tie, since between two pictures of the same shape and size it is the only thing left
    to go on.
    """
    if not art:
        return None
    return max(art, key=lambda a: (a["height"] >= a["width"],
                                   a["width"] * a["height"],
                                   "front" in a["what"].lower()))


def cache_key(tracks, m4b, folder, cover):
    """What would make a measurement stale: how many tracks, how big the .m4b, whether there's a
    cover, and when the folder last changed."""
    return [len(tracks), m4b[1] if m4b else 0, int(os.path.getmtime(folder)), bool(cover)]


def cached_for(book_id, tracks, m4b, folder, cover):
    """What's already known about this book, or {} — never an ffprobe.

    The shelf uses this and only this, which is what keeps it at a few milliseconds for a hundred
    books. A cache entry from before the book changed is treated as no entry at all rather than
    shown: a blank author is honest, and warm_cache fills it back in a moment later.
    """
    have = cache().get(book_id)
    try:
        return have if have and have.get("key") == cache_key(tracks, m4b, folder, cover) else {}
    except OSError:
        return {}


def measured(book_id, tracks, m4b, folder, cover=True):
    """What ffprobe says about this book, from the cache when the book hasn't changed.

    Keyed on what would make the answer stale — how many tracks there are, how big the .m4b is,
    whether there's a cover, when the folder last changed — so a rebuild re-probes and a page
    refresh doesn't.
    """
    have = cached_for(book_id, tracks, m4b, folder, cover)
    if have:
        return have
    found = {"key": cache_key(tracks, m4b, folder, cover),
             "seconds": 0, "author": "", "chapters": [], "art": None}
    if m4b:
        got = probe(os.path.join(folder, m4b[0]), chapters=True)
        found.update(seconds=got.get("seconds") or 0, author=got.get("author") or "",
                     chapters=got.get("chapters") or [])
    # The tracks get looked at when the author is still unknown — the runtime isn't, because the
    # build measures its own tracks and ffprobing 477 files for a number nobody needs is a minute
    # of waiting — or when there's no cover, since that's where one may be hiding.
    if tracks and (not found["author"] or not cover):
        got = probe_track(os.path.join(folder, tracks[0][0]))
        found["author"] = found["author"] or got["author"]
        if not cover:
            found["art"] = best_art(got["art"])
            if not found["art"] and len(tracks) > 1:
                # An introduction or a publisher's note is sometimes the one track with nothing
                # attached to it, so don't take track one's word for the whole book.
                middle = tracks[len(tracks) // 2][0]
                found["art"] = best_art(probe_track(os.path.join(folder, middle))["art"])
                if found["art"]:
                    found["art"]["file"] = middle
        if found["art"] and "file" not in found["art"]:
            found["art"]["file"] = tracks[0][0]
    with cache_lock:
        _cache[book_id] = found
    cache_save()
    return found


# ----------------------------------------------------------------- the two payloads

def card(book_id):
    """One book on the shelf. No subprocesses at all: scandir, stat, and the .m4b's own filename.
    Stays milliseconds whether the shelf holds six books or two hundred."""
    folder = os.path.join(ROOT, book_id)
    tracks, m4b, cover = listing(folder)
    known = cached_for(book_id, tracks, m4b, folder, bool(cover))
    said_title, said_author = read_about(folder)
    return {"id": book_id, "title": said_title or title_of(book_id, m4b),
            "author": said_author or known.get("author", ""),
            "tracks": len(tracks), "mb": round(sum(s for _n, s in tracks) / 1e6, 1),
            "cover": bool(cover), "cover_v": cover[1] if cover else 0,
            "m4b": m4b[0] if m4b else None,
            "m4b_mb": round(m4b[1] / 1e6, 1) if m4b else 0,
            "runtime": runtime(known.get("seconds")),
            # There's no cover.jpg, but the tracks are carrying one. Known from the cache, so the
            # shelf can offer it without an ffprobe per book.
            "art": bool(not cover and known.get("art")),
            "names": os.path.exists(os.path.join(folder, "names.txt")),
            "job": job_here(book_id)}


def shelf():
    try:
        found = [e.name for e in os.scandir(ROOT) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return []
    return sorted((card(n) for n in found), key=lambda b: b["title"].lower())


def detail(book_id):
    """Everything the book's own page needs, and at most one ffprobe to get it."""
    folder = book_dir(book_id)
    if not folder:
        return None
    tracks, m4b, cover = listing(folder)
    known = measured(book_id, tracks, m4b, folder, bool(cover))
    names, source = names_now(folder, tracks, known.get("chapters") or ())
    art = None if cover else known.get("art")
    said_title, said_author = read_about(folder)
    title = said_title or title_of(book_id, m4b)
    author = said_author or known.get("author", "")
    return {
        "id": book_id, "title": title, "author": author,
        # What's been typed, as opposed to what was worked out — so the form shows an empty field
        # rather than the guess it would fall back to, and clearing it means "go back to guessing".
        "about": {"title": said_title, "author": said_author,
                  "folder_title": title_of(book_id, m4b),
                  "found_author": known.get("author", "")},
        # The opening reads the title without the number it's filed under, so show which it is
        # before somebody spends ten minutes finding out.
        "spoken": {"title": audiobook.spoken(title), "author": author},
        "cover": bool(cover), "cover_v": cover[1] if cover else 0,
        "art": art and {"width": art["width"], "height": art["height"], "file": art["file"]},
        "mb": round(sum(s for _n, s in tracks) / 1e6, 1),
        "runtime": runtime(known.get("seconds")),
        "m4b": m4b and {"name": m4b[0], "mb": round(m4b[1] / 1e6, 1),
                        "runtime": runtime(known.get("seconds")),
                        "chapters": known.get("chapters") or []},
        # Building under a new title writes a new file and leaves the old one looking finished.
        "stale": stale_m4bs(folder, m4b),
        "tracks": [{"n": i, "file": n, "stem": stem_name(n, i),
                    "mb": round(s / 1e6, 1)}
                   for i, (n, s) in enumerate(tracks, start=1)],
        "names": {"source": source, "names": names},
        "job": job_here(book_id),
    }


def stale_m4bs(folder, keep):
    """Any .m4b that isn't the current one. Renaming a book means the next build writes a file
    under the new name, and ffmpeg has no reason to remove the old one — so it sits there, the same
    size and the same age it always was, looking exactly like the finished article."""
    if not keep:
        return []
    others = [p for p in glob.glob(os.path.join(glob.escape(folder), "*.m4b"))
              if os.path.basename(p) != keep[0]]
    return [{"name": os.path.basename(p), "mb": round(os.path.getsize(p) / 1e6, 1)}
            for p in sorted(others)]


# ----------------------------------------------------------------- chapter names

def stem_name(name, n):
    """A track's filename as a chapter name: "01 Introduction.mp3" -> "Introduction".

    The number comes off only when it is this track's own position, which is the same guard
    filename() uses (audiobook.py:163) and for the same reason — a chapter called "1984" keeps its
    name. A book whose tracks all begin "00 - " keeps that too, since 00 isn't track seven; find
    and replace is the tool for those, and quietly cutting the wrong three characters off
    fifty-four names is worse than leaving them.
    """
    stem = os.path.splitext(name)[0]
    found = re.match(r"(\d{1,3})(?!\d)\s*[-–—.:)]*\s*", stem)
    if found and int(found.group(1)) == n:
        return stem[found.end():].strip() or stem
    return stem


def names_now(folder, tracks, chapters=()):
    """The names to put in the form, and where they came from.

    Always exactly one per track. That's what makes the form fixed-length, and it's what makes
    read_names' refusal unreachable: the count can't be wrong if there's nowhere to type a
    fifty-fifth name.

    In order of how much somebody meant it: names.txt was typed here, the .m4b's chapter list was
    what the book was last built with, and the filenames are what the download happened to
    produce.
    """
    stems = [stem_name(n, i) for i, (n, _s) in enumerate(tracks, start=1)]
    path = os.path.join(folder, "names.txt")
    if not os.path.exists(path):
        if len(chapters) == len(stems) and any(c.strip() for c in chapters):
            return list(chapters), "the .m4b"
        return stems, "tracks"
    try:
        with open(path) as f:
            found = [line.strip() for line in f
                     if line.strip() and not line.lstrip().startswith("#")]
    except OSError:
        return stems, "tracks"
    if len(found) != len(stems):
        # An out-of-date names.txt — the book was re-fetched at a different track count, say.
        # Show what's there as far as it goes and fall back to the stems, rather than refusing to
        # open the page over a file the form is about to overwrite anyway.
        found = (found + stems[len(found):])[:len(stems)]
        return found, "stale"
    return found, "names.txt"


# The title and the author, when the folder's name isn't the answer. Plain text and one thing per
# line, like names.txt, and like names.txt the CLI doesn't go looking for it — the page passes what
# it says on the command line, so `pack` behaves the same whether it was typed here or there.
ABOUT = "about.txt"


def read_about(folder):
    """-> (title, author), either of them "" if it isn't set. Blank lines and # comments skipped,
    so the file can explain itself."""
    try:
        with open(os.path.join(folder, ABOUT)) as f:
            said = [line.strip() for line in f
                    if line.strip() and not line.lstrip().startswith("#")]
    except OSError:
        return "", ""
    return (said + ["", ""])[0], (said + ["", ""])[1]


def write_about(folder, title, author):
    """about.txt, atomically. Written only when there's something to say — clearing both fields
    takes the file away again and hands the title back to the folder's own name."""
    path = os.path.join(folder, ABOUT)
    title = re.sub(r"\s+", " ", str(title or "").replace("\0", "")).strip()[:200]
    author = re.sub(r"\s+", " ", str(author or "").replace("\0", "")).strip()[:200]
    if not title and not author:
        if os.path.exists(path):
            os.remove(path)
        return "", ""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("# what this book is called, and who wrote it — one per line, title first\n")
        f.write(title + "\n")
        if author:
            f.write(author + "\n")
    os.replace(tmp, path)
    return title, author


def safe_name(text, n):
    """One line that read_names will read back exactly as given.

    It skips blank lines and lines starting with #, so a name it would skip must be impossible to
    save — a dropped line shifts every chapter after it. A chapter genuinely called "#1" comes out
    "1", which is mildly lossy and much better than finding out at minute nine of an encode.
    """
    text = re.sub(r"\s+", " ", str(text or "").replace("\0", "")).strip()
    text = text.lstrip("#").strip()
    return text or f"Chapter {n}"


def write_names(folder, names):
    """names.txt, atomically, and read back before it counts.

    The round-trip is the test that matters: writing it, re-reading it under read_names' own rules
    and checking the count survived means a lost line fails here — with the form still on the
    screen — instead of two hundred megabytes later.
    """
    tidy = [safe_name(t, i) for i, t in enumerate(names, start=1)]
    path = os.path.join(folder, "names.txt")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"# {len(tidy)} tracks — one name per line, in playing order\n")
        f.writelines(name + "\n" for name in tidy)
    with open(tmp) as f:
        back = [line.strip() for line in f
                if line.strip() and not line.lstrip().startswith("#")]
    if back != tidy:
        os.remove(tmp)
        raise ValueError(f"{len(back)} of {len(tidy)} names survived being written — not saved")
    os.replace(tmp, path)
    return tidy


# ----------------------------------------------------------------- covers

# A magic-byte allowlist, not a content type and not a filename. It's here to stop ffmpeg being
# handed an MP4 and cheerfully extracting frame one, or an MP3 and extracting the art inside it —
# both of those "work", and both are the wrong picture. ffmpeg is the real validator after this.
MAGIC = ((0, b"\xff\xd8\xff"), (0, b"\x89PNG\r\n\x1a\n"), (0, b"GIF87a"), (0, b"GIF89a"),
         (0, b"BM"), (0, b"II*\x00"), (0, b"MM\x00*"), (8, b"WEBP"), (4, b"ftypheic"),
         (4, b"ftypmif1"), (4, b"ftypavif"))


def looks_like_image(raw):
    return any(raw[at:at + len(sig)] == sig for at, sig in MAGIC)


def make_cover(folder, source):
    """`source` — ffmpeg input arguments — as the cover.jpg build_m4b embeds.

    Every flag earns its place. -pix_fmt because mjpeg refuses RGBA and a PNG with alpha is one of
    the things that arrives here. -frames:v 1 because an animated GIF otherwise comes out as an
    image sequence. The scale because build_m4b embeds the cover with -c:v copy, so a 4000px
    picture goes into the .m4b at 4000px and onto the phone. And .tmp then replace, so a convert
    that fails doesn't destroy the cover that was already there.
    """
    out = os.path.join(folder, "cover.jpg")
    tmp = out + ".tmp"
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *source,
                            "-frames:v", "1", "-vf", "scale=w='min(1400,iw)':h=-1",
                            "-pix_fmt", "yuvj420p", "-q:v", "3",
                            # Named outright, because the .tmp name can't imply either of them —
                            # the same reason build_m4b has to pass -f ipod for its .m4b.part.
                            "-c:v", "mjpeg", "-f", "image2", tmp],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not os.path.exists(tmp):
            # ffmpeg's complaint about a truncated JPEG runs to several lines of thread numbers
            # and memory addresses, none of which say anything to somebody who just picked the
            # wrong file. It goes to the log; the page gets the short version.
            say("ffmpeg couldn't convert the cover:\n" + (r.stderr or "").strip()[-600:])
            raise ValueError("ffmpeg couldn't read it — is the file complete?")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return int(os.path.getmtime(out) * 1000)


def save_cover(folder, raw):
    """An uploaded picture, as the cover.

    The bytes are written dot-prefixed on purpose: `pack` skips dotfiles, and a crash halfway
    through an upload must not leave something that gets packed as track 55 of a 54-track book.
    """
    src = os.path.join(folder, ".cover-src")
    try:
        with open(src, "wb") as f:
            f.write(raw)
        return make_cover(folder, ["-i", src])
    finally:
        if os.path.exists(src):
            os.remove(src)


def take_artwork(book_id, folder):
    """-> (the new cover's version, what it says about it). (None, why not) if there wasn't one."""
    tracks, m4b, cover = listing(folder)
    if cover:
        return None, "it already has a cover.jpg"
    if not tracks:
        return None, "there's no audio in it"
    art = measured(book_id, tracks, m4b, folder, False).get("art")
    if not art:
        return None, "nothing is attached to its tracks"
    try:
        version = cover_from_tracks(book_id, folder, art)
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        return None, str(e) or "ffmpeg couldn't lift it out"
    with cache_lock:
        _cache.pop(book_id, None)      # there's a cover now, so the key has moved on
    cache_save()
    return version, f"{art['width']}×{art['height']} out of {art['file']}"


def cover_from_tracks(book_id, folder, art):
    """The artwork already inside the tracks, lifted out into cover.jpg.

    A publisher's MP3s carry the cover in an ID3 APIC frame, which is no use to a player looking
    at the folder and no use to build_m4b, which embeds `cover.jpg` and nothing else. It's the same
    picture either way — this just puts it where both of them will find it.

    -map is explicit because a file can hold several pictures and only one of them is the cover;
    see best_art. -an because otherwise ffmpeg hears an audio stream it could convert and tries to.
    """
    track = book_file(book_id, art.get("file") or "")
    if not track:
        raise ValueError("that track has gone")
    return make_cover(folder, ["-i", track, "-map", f"0:{art['index']}", "-an"])


# ----------------------------------------------------------------- archive.org, read-only

def parse_length(text):
    """archive.org writes a file's length either as seconds or as "12:34". Both, or 0."""
    text = str(text or "").strip()
    if not text:
        return 0.0
    if ":" in text:
        total = 0.0
        for part in text.split(":"):
            try:
                total = total * 60 + float(part or 0)
            except ValueError:
                return 0.0
        return total
    try:
        return float(text)
    except ValueError:
        return 0.0


def item_url(identifier, name):
    return f"{audiobook.ARCHIVE}/download/{identifier}/{urllib.parse.quote(name)}"


def preview(text, want=None):
    """What getting this item would produce, without getting any of it.

    The chapter names come out of audiobook.filename() itself, not a reimplementation of it, so
    what this promises is what lands on disk. Same for which format gets picked.
    """
    identifier = audiobook.identifier_of(text)
    meta = audiobook.get_json(f"{audiobook.ARCHIVE}/metadata/{identifier}")
    files = meta.get("files") or []
    if not files:
        raise ValueError(f"no such item: {identifier}")
    about = meta.get("metadata") or {}
    fmt, chosen = audiobook.tracks(files, want)
    if not chosen:
        have = sorted({f.get("format") or "?" for f in files})
        raise ValueError(f"no MP3s in {identifier} — it has {', '.join(have)}")
    have = {f.get("format") for f in files}
    picture = audiobook.cover_file(files)
    folder = os.path.join(ROOT, identifier)
    already = None
    if os.path.isdir(folder):
        tracks, m4b, _cover = listing(folder)
        already = {"folder": identifier, "have": len(tracks), "m4b": bool(m4b)}
    seconds = sum(parse_length(f.get("length")) for f in chosen)
    return {
        "identifier": identifier,
        "title": str(about.get("title") or identifier),
        "creator": str(about.get("creator") or ""),
        "date": str(about.get("date") or "")[:4],
        "licence_url": str(about.get("licenseurl") or ""),
        "format": fmt,
        "formats": [f for f in audiobook.FORMATS if f in have],
        "tracks": len(chosen),
        "mb": round(sum(int(f.get("size") or 0) for f in chosen) / 1e6),
        "runtime": runtime(seconds),
        "cover_url": item_url(identifier, picture["name"]) if picture else "",
        "page": f"{audiobook.ARCHIVE}/details/{identifier}",
        "already": already,
        "chapters": [{"n": i,
                      "name": os.path.splitext(audiobook.filename(i, f))[0][3:] or f["name"],
                      "mb": round(int(f.get("size") or 0) / 1e6, 1),
                      "url": item_url(identifier, f["name"])}
                     for i, f in enumerate(chosen, start=1)],
    }


# ----------------------------------------------------------------- jobs

jobs = {}
jobs_lock = threading.Lock()
running = {"id": None}
_counter = [0]

STAGES = ((re.compile(r"^Measuring \d+ tracks"), "measuring"),
          (re.compile(r"^Saying:"), "announcing"),
          (re.compile(r"^Encoding "), "encoding"),
          (re.compile(r"^Uploading "), "uploading"),
          (re.compile(r"^📤"), "done"))
TOTAL = re.compile(r"^(\d+) tracks\b")
TRACK = re.compile(r"^  [✓·] ")
ENCODE = re.compile(r"^Encoding ([\d.]+) h to AAC (\d+)k")


def job_here(book_id):
    """The job working on this book, small enough to put on a card."""
    with jobs_lock:
        for job in jobs.values():
            if job["book"] == book_id and job["status"] in ("queued", "running"):
                return {"id": job["id"], "status": job["status"], "now": job["now"]}
    return None


def new_job(kind, book, argv, label):
    _counter[0] += 1
    return {"id": f"j{_counter[0]}", "kind": kind, "book": book, "label": label, "argv": argv,
            "status": "queued", "stage": "starting", "now": "starting…", "log": "",
            "done": 0, "total": 0, "hours": 0.0, "bitrate": 0, "started": time.time(),
            "finished": None, "error": None, "result": None, "cancelled": False, "pid": None}


def note(job, line):
    """One line of the CLI's output, and what it says about where the job is.

    Nothing here invents a progress protocol — it reads the output the CLI already prints. `now`
    is simply the last thing said, which is always the right sentence to show: "… carrying on from
    12.4 MB", "… archive.org said 503, waiting 8s", "Encoding 3.4 h to AAC 64k mono".
    """
    with jobs_lock:
        if len(job["log"]) < LOG_CAP:
            job["log"] += line
        text = line.strip()
        if not text:
            return
        job["now"] = text
        if found := TOTAL.match(text):
            job["total"] = int(found.group(1))
        if TRACK.match(line):
            job["done"] += 1
            job["stage"] = "downloading"
        for pattern, stage in STAGES:
            if pattern.match(text):
                job["stage"] = stage
        if found := ENCODE.match(text):
            job["hours"], job["bitrate"] = float(found.group(1)), int(found.group(2))


def encode_percent(job):
    """How far the encode is, from the size of the .part it's writing.

    build_m4b runs ffmpeg with capture_output=True, so its progress is swallowed until it
    finishes — the last thing the page would otherwise hear is "a few minutes…". A constant
    bitrate makes the file size a clock. Never claim past 99 from an estimate.
    """
    if job["stage"] != "encoding" or not (job["hours"] and job["bitrate"]):
        return 0
    folder = book_dir(job["book"]) if job["book"] else None
    if not folder:
        return 0
    parts = glob.glob(os.path.join(folder, "*.m4b.part"))
    if not parts:
        return 0
    try:
        expect = job["hours"] * 3600 * job["bitrate"] * 1000 / 8
        return min(99, int(os.path.getsize(parts[0]) / expect * 100)) if expect else 0
    except OSError:
        return 0


def job_view(job, since=0):
    """The job, plus only the log the caller hasn't seen.

    A `since` past the end means the caller is talking about a different run of this server — jobs
    live in memory, and with a hand-rolled server you *will* restart it mid-encode. Say so rather
    than sending a negative slice.
    """
    with jobs_lock:
        log, reset = job["log"], False
        if since > len(log) or since < 0:
            since, reset = 0, True
        view = {k: job[k] for k in ("id", "kind", "book", "label", "status", "stage", "now",
                                    "done", "total", "error", "result", "cancelled")}
        view.update(log=log[since:], next=len(log), reset=reset,
                    seconds=round((job["finished"] or time.time()) - job["started"]))
    view["percent"] = encode_percent(job)
    return view


def argv_get(spec):
    """audiobook.py m4b <identifier> --dir <root> …"""
    argv = [sys.executable, "-u", os.path.join(HERE, "audiobook.py"),
            "m4b", spec["identifier"], "--dir", ROOT]
    if spec.get("format"):
        argv += ["--format", spec["format"]]
    if spec.get("announce"):
        argv += ["--announce", "--voice", spec.get("voice") or "af_heart"]
    if spec.get("upload"):
        argv += ["--upload"]
    return argv


def argv_build(spec, folder, title, author):
    """audiobook.py pack <folder> --title … --author … [--names …]

    --title and --author are never left off, and that isn't cosmetic. `m4b` names its output from
    archive.org's metadata while `pack` names it from the folder — which is why the folder called
    thetimemachineversion_7_2105_librivox holds "The Time Machine (Version 7).m4b". Rebuilding
    without them writes a second, differently-named audiobook beside the first and leaves the old
    one sitting there looking finished.
    """
    argv = [sys.executable, "-u", os.path.join(HERE, "audiobook.py"),
            "pack", folder, "--title", title, "--author", author]
    if os.path.exists(os.path.join(folder, "names.txt")):
        argv += ["--names", os.path.join(folder, "names.txt")]
    if spec.get("bitrate"):
        argv += ["--bitrate", spec["bitrate"]]
    if spec.get("announce"):
        argv += ["--announce", "--voice", spec.get("voice") or "af_heart"]
    if spec.get("upload"):
        argv += ["--upload"]
    return argv


def start_job(kind, book, argv, label):
    """One heavy job at a time, and it refuses rather than queues.

    For a page with one person looking at it, a queue that does something in ten minutes is worse
    than an answer now that names what's in the way. And a second job in the same folder isn't
    merely wasteful: build_m4b writes concat.txt and chapters.txt under fixed names, so two of
    them would write each other's chapter list into the audiobook.
    """
    with jobs_lock:
        current = jobs.get(running["id"] or "")
        if current and current["status"] in ("queued", "running"):
            return None, current
        for job in jobs.values():
            if job["book"] == book and job["status"] in ("queued", "running"):
                return None, job
        job = new_job(kind, book, argv, label)
        jobs[job["id"]] = job
        running["id"] = job["id"]
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job, None


def run_job(job):
    """The CLI, with its output pumped into the job.

    -u because stdout is block-buffered once it's a pipe and not every print in there passes
    flush. stderr folded in because sys.exit(message) writes there, and a stdout-only pipe stops
    mid-sentence with no reason given. No stdin, so nothing can sit waiting on a prompt nobody is
    there to answer. And its own session, because ffmpeg and kokoro-tts are grandchildren:
    killing just the python would leave a twenty-minute encode running with nobody watching.
    """
    say(f"[{job['id']}] {' '.join(job['argv'][2:])}")
    try:
        child = subprocess.Popen(job["argv"], cwd=HERE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 text=True, errors="replace", bufsize=1, start_new_session=True)
    except OSError as e:
        finish(job, "error", f"couldn't start audiobook.py: {e}")
        return
    with jobs_lock:
        job.update(status="running", pid=child.pid)
    watchdog = threading.Timer(JOB_LIMIT, lambda: kill(job))
    watchdog.daemon = True
    watchdog.start()
    try:
        for line in child.stdout:
            note(job, line)
            say(f"[{job['id']}] {line.rstrip()}")
        code = child.wait()
    finally:
        watchdog.cancel()
        child.stdout.close()
    if code == 0:
        finish(job, "done", None)
    elif code < 0 or job["cancelled"]:
        if job["cancelled"]:
            # The CLI's own words for this, because they're the true ones: nothing is lost.
            finish(job, "cancelled", "Stopped. Start it again to carry on where it left off.")
        else:
            finish(job, "error", f"the job was killed (signal {-code}) — out of memory, perhaps")
    elif code == 2:
        say(f"[{job['id']}] argparse refused: {job['argv']}")
        finish(job, "error", "audiobook.py refused the arguments — that's a bug in this page")
    else:
        tail = [l for l in job["log"].splitlines() if l.strip()][-6:]
        finish(job, "error", "\n".join(tail)[-600:] or f"audiobook.py exited {code}")


def finish(job, status, error):
    with jobs_lock:
        job.update(status=status, error=error, finished=time.time())
        if running["id"] == job["id"]:
            running["id"] = None
    if status == "done" and job["book"]:
        folder = book_dir(job["book"])
        # The printed path is for reading; the folder is for knowing. Re-scan it rather than
        # parsing the last line, and let the cache notice the book changed.
        if folder:
            _tracks, m4b, _cover = listing(folder)
            with jobs_lock:
                job["result"] = m4b[0] if m4b else None
    say(f"[{job['id']}] {status}" + (f": {error}" if error else ""))


def kill(job):
    """The whole process group, then harder. Safe by design: a part-written track resumes from the
    byte it stopped at, and the finished .m4b is only ever moved into place whole."""
    with jobs_lock:
        pid, alive = job["pid"], job["status"] == "running"
        job["cancelled"] = True
    if not (pid and alive):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    harder = threading.Timer(5, lambda: _sigkill(pid))
    harder.daemon = True
    harder.start()
    return True


def _sigkill(pid):
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def warm_cache():
    """Measure every book once, in the background, at startup.

    The shelf itself runs no subprocesses, which is what keeps it at six milliseconds — but it
    means the author and the runtime are blank until somebody opens each book. One ffprobe per
    book, off the request path, fills them in before you've finished looking at the first page.
    """
    try:
        books = [e.name for e in os.scandir(ROOT) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return
    began = time.time()
    for book_id in books:
        folder = os.path.join(ROOT, book_id)
        tracks, m4b, cover = listing(folder)
        try:
            measured(book_id, tracks, m4b, folder, bool(cover))
        except OSError:
            pass
    say(f"measured {len(books)} book(s) in {time.time() - began:.1f}s")


def sweep_leftovers():
    """The scratch a killed run leaves behind, cleared at startup — jobs don't outlive this
    process, so anything still here belongs to nobody. Never *.mp3.part: that one is a resume
    point, and deleting it means downloading the book again."""
    gone = 0
    for entry in glob.glob(os.path.join(ROOT, "*")):
        if not os.path.isdir(entry):
            continue
        for pattern in LEFTOVERS:
            for scratch in glob.glob(os.path.join(entry, pattern)):
                try:
                    os.remove(scratch)
                    gone += 1
                except OSError:
                    pass
    if gone:
        say(f"cleared {gone} leftover file(s) from an interrupted run")


# ----------------------------------------------------------------- ranges

def byte_range(header, size):
    """(first, last) inclusive, None for "all of it", or "bad" for a 416.

    Safari opens every audio element with "bytes=0-1" and will not play a file that answers 200
    to it, so this is not optional. Something malformed or multi-part is *ignored* rather than
    refused — RFC 9110 allows that, and a browser sending a header we don't understand is better
    off with a playable file than an error.
    """
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].strip()
    if "," in spec or "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    if (first and not first.isdigit()) or (last and not last.isdigit()):
        return None
    if not first:                                   # bytes=-N, the last N bytes
        want = int(last or 0)
        if want <= 0 or size <= 0:
            return "bad"
        return (max(0, size - want), size - 1)
    start = int(first)
    if size <= 0 or start >= size:
        return "bad"
    if not last:
        return (start, size - 1)
    end = int(last)
    if end < start:
        return "bad"
    return (start, min(end, size - 1))


# ----------------------------------------------------------------- the server

class Handler(BaseHTTPRequestHandler):
    # Keep-alive, which means every single response needs a truthful Content-Length: get one
    # wrong and the browser hangs on the *next* request down that socket, so the symptom turns up
    # nowhere near the cause.
    protocol_version = "HTTP/1.1"
    server_version = "archive-audiobook"
    sys_version = ""
    # An idle keep-alive socket holds a thread. Without this, a phone that goes to sleep in the
    # middle of a session keeps one forever.
    timeout = 65
    disable_nagle_algorithm = True

    # -------------------------------------------------- plumbing

    def log_message(self, fmt, *args):
        pass            # said properly in respond(), and without the polls

    def log_error(self, fmt, *args):
        pass

    def handle_one_request(self):
        """A client that goes away mid-response is the normal case here, not a fault: every seek
        in a 95 MB audiobook abandons one 206 and opens another. It can surface from writing the
        body, from writing the headers, or from reading the next request line — and anything not
        caught here reaches ThreadingHTTPServer, which prints a traceback about it."""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def refuse(self, code, message):
        """An error, with the socket shut afterwards. Anything unread in the body would otherwise
        be parsed as the next request line, which comes back as a 400 about nothing."""
        self.close_connection = True
        self.respond(code, b'{"error": ' + json.dumps(message).encode() + b"}",
                     "application/json; charset=utf-8", extra={"Connection": "close"})

    def respond(self, code, body, ctype, extra=None, log=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if code not in (204, 304):
            # Both are defined as bodyless, and a Content-Length on them is what some clients
            # sit waiting on.
            self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)
        if log:
            self.note(code, len(body))

    def note(self, code, sent, extra=""):
        if self.path.startswith("/api/jobs/"):
            return                  # a poll every 1.5s, and the log would be nothing else
        agent = self.headers.get("User-Agent") or ""
        phone = "iPhone" if "iPhone" in agent else "iPad" if "iPad" in agent else "pc"
        say(f"{self.command} {self.path} → {code} {sent}B {phone}{extra}")

    def json_out(self, data, code=200):
        self.respond(code, json.dumps(data).encode(), "application/json; charset=utf-8")

    def body(self):
        """The request body, or None having already refused it.

        http.server does not decode chunked transfer encoding, and rfile.read(n) on a chunked
        body with no Content-Length blocks until the socket times out — so refuse it and let the
        page send something with a length, which a Blob or a File always has.
        """
        if self.headers.get("Transfer-Encoding"):
            self.refuse(411, "send it with a Content-Length, not chunked")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.refuse(400, "bad Content-Length")
            return None
        if length > MAX_UPLOAD:
            self.refuse(413, f"that's {round(length / 1e6)} MB — "
                             f"the limit is {MAX_UPLOAD // 1024 // 1024} MB")
            return None
        return self.rfile.read(length) if length else b""

    def json_in(self):
        raw = self.body()
        if raw is None:
            return None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            self.refuse(400, "that wasn't JSON")
            return None

    # -------------------------------------------------- routing

    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("HEAD")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        split = urllib.parse.urlsplit(self.path)
        # Split first, unquote after: an encoded %2F inside somebody's filename can then never
        # forge a path segment of its own.
        parts = [urllib.parse.unquote(p) for p in split.path.split("/") if p]
        query = urllib.parse.parse_qs(split.query)
        one = lambda key, default="": (query.get(key) or [default])[0]
        try:
            if method in ("GET", "HEAD"):
                if not parts:
                    return self.send_page()
                if parts == ["favicon.ico"]:
                    return self.respond(204, b"", "image/x-icon", log=False)
                if parts == ["api", "books"]:
                    return self.json_out({"root": ROOT, "books": shelf()})
                if len(parts) == 3 and parts[0] == "api" and parts[1] == "books":
                    found = detail(parts[2])
                    return self.json_out(found) if found else self.refuse(404, "no such book")
                if parts == ["api", "search"]:
                    return self.search(one("q"), one("collection", audiobook.COLLECTION))
                if parts == ["api", "item"]:
                    return self.item(one("id"), one("format") or None)
                if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                    return self.job_out(parts[2], int(one("from", "0") or 0))
                if len(parts) == 3 and parts[0] == "file":
                    return self.send_file(parts[1], parts[2], download=bool(one("dl")))
                if len(parts) == 3 and parts[0] == "get":
                    return self.send_get_page(parts[1], parts[2])
            elif method == "POST":
                if len(parts) == 4 and parts[:2] == ["api", "books"]:
                    return self.book_post(parts[2], parts[3])
                if parts == ["api", "artwork"]:
                    return self.post_all_artwork()
                if parts == ["api", "get"]:
                    return self.start_get()
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                    job = jobs.get(parts[2])
                    if not job:
                        return self.refuse(404, "no such job")
                    return self.json_out({"ok": kill(job)})
        except SystemExit as e:
            # sys.exit is how audiobook.py reports every kind of bad input, and SystemExit is a
            # BaseException — so `except Exception` would let it kill this thread quietly and
            # leave the browser waiting on a socket that never answers.
            return self.refuse(400, str(e.code or "that didn't work"))
        except (ValueError, OSError) as e:
            return self.refuse(400, str(e) or e.__class__.__name__)
        except Exception as e:                                   # noqa: BLE001
            say(f"!! {self.command} {self.path}: {e.__class__.__name__}: {e}")
            return self.refuse(500, f"{e.__class__.__name__}: {e}")
        self.refuse(405 if method == "POST" else 404, f"nothing at {split.path}")

    # -------------------------------------------------- the page

    def send_page(self):
        """index.html, read fresh every time and never cached, so a home-screen shortcut can't
        pin an old copy of the UI."""
        try:
            with open(PAGE, "rb") as f:
                page = f.read()
        except OSError:
            return self.refuse(500, f"can't read {PAGE}")
        self.respond(200, page, "text/html; charset=utf-8",
                     {"Cache-Control": "no-store"})

    # -------------------------------------------------- books

    def book_post(self, book_id, what):
        folder = book_dir(book_id)
        if not folder:
            return self.refuse(404, "no such book")
        busy = job_here(book_id)
        if busy:
            # Including the cover, which is the one that looks harmless. ffmpeg already has the old
            # cover.jpg open, so replacing it now would build the book with the picture you just
            # replaced and say nothing about it.
            return self.refuse(409, f"something is working on this book: {busy['now']}")
        if what == "cover":
            return self.post_cover(book_id, folder)
        if what == "artwork":
            return self.post_artwork(book_id, folder)
        if what == "about":
            return self.post_about(book_id, folder)
        if what == "tidy":
            return self.post_tidy(book_id, folder)
        if what == "names":
            return self.post_names(book_id, folder)
        if what == "build":
            return self.post_build(book_id, folder)
        return self.refuse(404, f"nothing to post to {what}")

    def post_cover(self, book_id, folder):
        raw = self.body()
        if raw is None:
            return None
        if not raw:
            return self.refuse(400, "no image")
        if not looks_like_image(raw):
            # Not the content type and not the filename, because both are whatever the client
            # felt like saying, and ffmpeg will happily pull frame one out of a video.
            return self.refuse(400, "that doesn't look like an image")
        try:
            version = save_cover(folder, raw)
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            return self.refuse(400, str(e) or "couldn't read that image")
        cache()                         # so there is one; the key includes the folder's mtime
        with cache_lock:
            _cache.pop(book_id, None)
        self.json_out({"ok": True, "cover_v": version})

    def post_artwork(self, book_id, folder):
        """Lift the cover out of this book's own tracks."""
        version, why = take_artwork(book_id, folder)
        if not version:
            return self.refuse(400, why)
        self.json_out({"ok": True, "cover_v": version, "from": why})

    def post_all_artwork(self):
        """The same for every book that hasn't got a cover — which after sorting a series is all of
        them at once, and clicking into twenty books to press the same button is not an interface.

        Done in the request rather than as a job: it's one ffmpeg per book and no re-encoding, so
        twenty books is a couple of seconds. The reply says what it did to each.
        """
        with jobs_lock:
            busy = jobs.get(running["id"] or "")
        if busy and busy["status"] in ("queued", "running"):
            return self.refuse(409, f"wait for {busy['label']} to finish first")
        done, skipped = [], []
        for book in shelf():
            if book["cover"]:
                continue
            if not book["art"]:
                skipped.append({"id": book["id"], "why": "nothing attached to its tracks"})
                continue
            version, why = take_artwork(book["id"], os.path.join(ROOT, book["id"]))
            (done if version else skipped).append({"id": book["id"], "why": why})
        say(f"took artwork from the tracks of {len(done)} book(s)")
        self.json_out({"ok": True, "done": done, "skipped": skipped})

    def post_about(self, book_id, folder):
        """What the book is called, when the folder's name isn't it."""
        data = self.json_in()
        if data is None:
            return None
        title, author = write_about(folder, data.get("title"), data.get("author"))
        say(f"{book_id}: called {title or '(the folder)'} by {author or '(whatever the tags say)'}")
        self.json_out({"ok": True, "book": detail(book_id)})

    def post_tidy(self, book_id, folder):
        """Delete one .m4b that isn't the current one — the leftover from a title change.

        Named rather than swept: it's several hundred megabytes and the only copy, so the page has
        to have said which file and the answer has to come back naming it too.
        """
        data = self.json_in()
        if data is None:
            return None
        path = book_file(book_id, data.get("name") or "")
        if not path or not path.lower().endswith(".m4b"):
            return self.refuse(400, "no such audiobook file")
        _tracks, m4b, _cover = listing(folder)
        if m4b and os.path.basename(path) == m4b[0]:
            return self.refuse(400, "that's the current audiobook, not a leftover")
        mb = round(os.path.getsize(path) / 1e6)
        os.remove(path)
        say(f"{book_id}: removed the leftover {os.path.basename(path)} ({mb} MB)")
        self.json_out({"ok": True, "removed": os.path.basename(path), "mb": mb})

    def post_names(self, book_id, folder):
        data = self.json_in()
        if data is None:
            return None
        names = data.get("names")
        tracks, _m4b, _cover = listing(folder)
        if not isinstance(names, list):
            return self.refuse(400, "expected a list of names")
        if len(names) != len(tracks):
            return self.refuse(400, f"{len(names)} names for {len(tracks)} tracks — "
                                    "the list has to be one per track")
        saved = write_names(folder, names)
        self.json_out({"ok": True, "count": len(saved), "names": saved})

    def post_build(self, book_id, folder):
        data = self.json_in()
        if data is None:
            return None
        tracks, m4b, cover = listing(folder)
        if not tracks:
            return self.refuse(400, "there's no audio in this book to build from")
        known = measured(book_id, tracks, m4b, folder, bool(cover))
        said_title, said_author = read_about(folder)
        title = said_title or title_of(book_id, m4b)
        author = said_author or data.get("author") or known.get("author") or ""
        spec = {"announce": bool(data.get("announce")), "voice": voice_of(data),
                "bitrate": bitrate_of(data), "upload": bool(data.get("upload"))}
        argv = argv_build(spec, folder, title, author)
        job, busy = start_job("build", book_id, argv, title)
        if not job:
            return self.refuse(409, f"already working on {busy['label']}: {busy['now']}")
        self.json_out({"job": job_view(job)})

    # -------------------------------------------------- archive.org

    def search(self, words, collection):
        words = (words or "").split()
        if not words:
            return self.refuse(400, "nothing to search for")
        found = audiobook.search(words, rows=24, collection=collection)
        where = ("all audio" if not collection or collection.lower() in audiobook.ANY
                 else collection)
        self.json_out({"where": where, "results": [
            {"identifier": d.get("identifier"), "title": str(d.get("title") or ""),
             "creator": str(d.get("creator") or ""), "runtime": str(d.get("runtime") or ""),
             "have": os.path.isdir(os.path.join(ROOT, str(d.get("identifier") or "\0")))}
            for d in found if d.get("identifier")]})

    def item(self, text, want):
        if not (text or "").strip():
            return self.refuse(400, "no identifier")
        if want and want not in audiobook.FORMATS:
            return self.refuse(400, f"unknown format: {want}")
        self.json_out(preview(text, want))

    def start_get(self):
        data = self.json_in()
        if data is None:
            return None
        identifier = audiobook.identifier_of(data.get("identifier") or "")
        want = data.get("format") or None
        if want and want not in audiobook.FORMATS:
            return self.refuse(400, f"unknown format: {want}")
        spec = {"identifier": identifier, "format": want, "voice": voice_of(data),
                "announce": bool(data.get("announce")), "upload": bool(data.get("upload"))}
        job, busy = start_job("get", identifier, argv_get(spec), identifier)
        if not job:
            return self.refuse(409, f"already working on {busy['label']}: {busy['now']}")
        self.json_out({"job": job_view(job)})

    def job_out(self, jid, since):
        job = jobs.get(jid)
        if not job:
            return self.refuse(404, "no such job")
        self.json_out(job_view(job, since))

    # -------------------------------------------------- files

    def send_file(self, book_id, name, download=False):
        path = book_file(book_id, name)
        if not path:
            return self.refuse(404, "no such file")
        try:
            stat = os.stat(path)
        except OSError:
            return self.refuse(404, "no such file")
        size = stat.st_size
        ext = os.path.splitext(name)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        tag = f'"{stat.st_mtime_ns}-{size}"'
        modified = self.date_time_string(int(stat.st_mtime))
        headers = {"Accept-Ranges": "bytes", "ETag": tag, "Last-Modified": modified,
                   # Cacheable but revalidated, following what speech-webui found the hard way:
                   # no-store made Safari fetch the same file over and over during lock-screen
                   # playback, and a blind max-age happily served yesterday's rebuild.
                   "Cache-Control": ("private, max-age=86400" if ext in (".jpg", ".jpeg")
                                     else "private, max-age=0, must-revalidate")}
        if download:
            headers["Content-Disposition"] = \
                "attachment; filename*=UTF-8''" + urllib.parse.quote(name)

        if self.headers.get("If-None-Match") == tag or \
                self.headers.get("If-Modified-Since") == modified:
            return self.respond(304, b"", ctype, headers)

        want = byte_range(self.headers.get("Range"), size)
        if want == "bad":
            headers["Content-Range"] = f"bytes */{size}"
            return self.respond(416, b"", ctype, headers)
        first, last = want if want else (0, size - 1)
        length = max(0, last - first + 1)
        if want:
            headers["Content-Range"] = f"bytes {first}-{last}/{size}"

        self.send_response(206 if want else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return self.note(200, 0)
        sent = self.pump(path, first, length)
        # "sent 3 MB of 95 MB" and "sent 95 MB and threw them away" want opposite fixes, so the
        # log has to be able to tell them apart.
        self.note(206 if want else 200, sent, f" of {length}" if sent != length else "")

    def pump(self, path, first, length):
        sent, left = 0, length
        try:
            with open(path, "rb") as f:
                f.seek(first)
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break                   # it shrank under us; stop rather than spin
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True        # a seek, or a shut tab. Expected.
        except OSError:
            self.close_connection = True
        return sent

    def send_get_page(self, book_id, name):
        """The same download with a page wrapped round it, for the home-screen app.

        iOS opens a target="_blank" link in a browser view with no address bar. Handed the .m4b
        itself it has no page to show, so it renders blank and greys out both Share and Open in
        Safari: the file has arrived and there's nothing you can do with it. Handed a page, it
        shows the page and those two buttons stay live. Lifted from speech-webui, which found this
        out the hard way.
        """
        path = book_file(book_id, name)
        if not path:
            return self.refuse(404, "no such file")
        mb = os.path.getsize(path) / 1e6
        href = "/file/" + urllib.parse.quote(book_id) + "/" + urllib.parse.quote(name) + "?dl=1"
        page = f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(name)}</title>
<style>
 body {{ margin:0; padding:28px 20px; background:#15131f; color:#e9e6f5; font-size:17px;
        line-height:1.5; font-family:-apple-system,system-ui,sans-serif; }}
 h1 {{ font-size:19px; margin:0 0 6px; word-break:break-word; }}
 .sz {{ color:#a49fc0; font-size:14px; margin-bottom:22px; }}
 a.dl {{ display:block; padding:16px; border-radius:12px; background:#2a2640;
         border:1px solid #332e4a; color:#e9e6f5; text-decoration:none; text-align:center; }}
 p {{ color:#a49fc0; font-size:14px; margin-top:22px; }}
</style>
<h1>{html.escape(name)}</h1>
<div class="sz">{mb:.1f} MB audiobook</div>
<a class="dl" href="{html.escape(href)}" download>⬇ Download it</a>
<p>If nothing happens, tap the compass at the bottom right to open this in Safari and try again.
BookPlayer takes it from the share sheet once it's in Files.</p>
"""
        self.respond(200, page.encode(), "text/html; charset=utf-8",
                     {"Cache-Control": "no-store"})


def voice_of(data):
    voice = str(data.get("voice") or "af_heart")
    if not re.fullmatch(r"[a-z]{2}_[a-z]+", voice):
        raise ValueError(f"that isn't a Kokoro voice: {voice}")
    return voice


def bitrate_of(data):
    rate = str(data.get("bitrate") or "")
    if rate and not re.fullmatch(r"\d{1,3}k", rate):
        raise ValueError(f"that isn't a bitrate: {rate}")
    return rate


class Server(ThreadingHTTPServer):
    # ThreadingHTTPServer collects its threads whether they're daemons or not, so a tidy
    # server_close() would join one that is 4 MB into a 95 MB transfer to a phone — and Ctrl-C
    # looks like a hang.
    block_on_close = False
    daemon_threads = True


def be_less_patient():
    """Shorten the retrying, for the previews this process fetches itself.

    open_url is five tries with 4/8/16/32s of sleeping and a 60s socket timeout — up to five
    minutes of patience. Exactly right for an unattended download at three in the morning, and
    unusable in something a browser is waiting on. The download runs as a subprocess with its own
    fresh import, so it keeps the full five.

    Done here rather than at import, because reaching into another module's globals from the top of
    this one would change audiobook.py's behaviour for anything that imports both — which is how
    its own test for waiting out a 460 started failing.
    """
    audiobook.TRIES, audiobook.BACKOFF = 2, 2


def main():
    be_less_patient()
    if not os.path.isdir(ROOT):
        sys.exit(f"no shelf at {ROOT} — set AUDIOBOOK_ROOT, or make it")
    if not os.path.exists(PAGE):
        sys.exit(f"{PAGE} is missing, and it's the whole interface")
    sweep_leftovers()
    books = len([e for e in os.scandir(ROOT) if e.is_dir() and not e.name.startswith(".")])
    say(f"{books} book(s) on the shelf at {ROOT}")
    say(f"http://127.0.0.1:{PORT}")
    threading.Thread(target=warm_cache, daemon=True).start()
    server = Server(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\nStopped.")


if __name__ == "__main__":
    main()
