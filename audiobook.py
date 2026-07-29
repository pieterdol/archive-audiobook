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
import glob
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
    if os.path.isdir(os.path.expanduser(text)):
        sys.exit(f'"{text}" is a folder of your own — that\'s: audiobook.py pack "{text}"')
    if not re.fullmatch(r"[\w.-]+", text):
        # Anything else would go into a URL and come back as a stack trace about control
        # characters, which says nothing about what went wrong.
        sys.exit(f"that isn't an archive.org identifier or URL: {text}")
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


AUDIO = (".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav", ".aac")


def in_order(names):
    """Sorted the way a person numbers things: 2 before 10, which plain sorting gets wrong the
    moment a folder isn't zero-padded."""
    return sorted(names, key=lambda n: [int(p) if p.isdigit() else p.lower()
                                        for p in re.split(r"(\d+)", n)])


def tag_of(path, key):
    """One metadata tag off a file, or "" — for taking the author off the tracks themselves."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", f"format_tags={key}",
                          "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def pack(folder, names=None, title=None, author=None, bitrate="64k", opening=False,
         voice="af_heart"):
    """One .m4b out of a folder of audio you already have.

    Nothing is fetched: these are your files. The order is the file order, naturally sorted; the
    chapter names are the file names unless --names says otherwise; the title is the folder's
    name and the author comes off the first track's tags. Every one of those is a guess, so it
    prints what it is about to do before it spends ten minutes encoding.
    """
    folder = os.path.expanduser(folder).rstrip("/")
    if not os.path.isdir(folder):
        sys.exit(f"no such folder: {folder}")
    # Dotfiles are never somebody's chapter, and they are this script's scratch: an
    # announcement left behind by a killed run was picked up as track 55 of a 54-track book.
    files = in_order([f for f in os.listdir(folder) if f.lower().endswith(AUDIO)
                      and not f.startswith(".") and not f.lower().endswith((".m4b", ".part"))])
    if not files:
        sys.exit(f"no audio in {folder}")
    got = [(os.path.join(folder, f), os.path.splitext(f)[0]) for f in files]
    about = {"title": title or os.path.basename(os.path.abspath(folder)),
             "creator": author or tag_of(got[0][0], "artist") or ""}
    print(f"{about['title']} — {about['creator'] or 'nobody named'}")
    print(f"{len(got)} tracks from {folder}")
    return build_m4b(about, got, folder, bitrate, names, opening, voice)


def tags_of(path, keys=("album", "track", "title")):
    """Several tags off one file, as a dict — one ffprobe rather than one per tag."""
    out = subprocess.run(["ffprobe", "-v", "error",
                          "-show_entries", "format_tags=" + ",".join(keys),
                          "-of", "default=nw=1", path],
                         capture_output=True, text=True, timeout=60)
    found = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition("=")
        found[key.removeprefix("TAG:").lower()] = value.strip()
    return found


def sorted_plan(folder):
    """-> ({album: [(from, to)]}, [files with no album]).

    A folder that a whole series downloaded into is one flat pile, and the files themselves say
    which book each belongs to: album names it, track numbers it within it. Nothing here reads a
    filename, because the tags are what the publisher wrote and the names are what the download
    happened to produce.
    """
    tidy = lambda s: re.sub(r"\s{2,}", " ", re.sub(r"[^\w \-.,'()]+", " ", s or "")).strip()
    found, loose = {}, []
    for name in in_order(f for f in os.listdir(folder)
                         if f.lower().endswith(AUDIO) and not f.startswith(".")):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        tag = tags_of(path)
        album = tidy(tag.get("album"))
        if not album:
            loose.append(name)
            continue
        number = re.match(r"\s*(\d+)", tag.get("track") or "")
        at = int(number.group(1)) if number else 10_000
        title = tidy(tag.get("title"))
        stem = f"{at:02d} {title}" if number and title else title or os.path.splitext(name)[0]
        found.setdefault(album, []).append((at, name, stem[:120] + os.path.splitext(name)[1]))
    # In track order within each book, which is the order they'll be packed in — the filenames
    # a download happened to produce say nothing about it.
    books = {album: [(name, stem) for _at, name, stem in sorted(rows)]
             for album, rows in found.items()}
    return books, loose


def sort_folder(folder, apply=False):
    """Split a flat pile into one folder per book. Shows what it would do unless told to do it:
    moving two hundred files on a guess is not something to find out afterwards."""
    folder = os.path.expanduser(folder).rstrip("/")
    if not os.path.isdir(folder):
        sys.exit(f"no such folder: {folder}")
    books, loose = sorted_plan(folder)
    if not books:
        sys.exit(f"nothing in {folder} says which book it belongs to")
    for album, moves in sorted(books.items()):
        clashes = len(moves) - len({stem for _was, stem in moves})
        print(f"{album}  ({len(moves)} tracks"
              + (f", {clashes} sharing a name" if clashes else "") + ")")
        for was, becomes in moves[:2]:
            print(f"    {was}\n      → {album}/{becomes}")
        if len(moves) > 2:
            print(f"    … and {len(moves) - 2} more")
    if loose:
        print(f"\n{len(loose)} file(s) with no album tag, left where they are: "
              f"{', '.join(loose[:3])}{' …' if len(loose) > 3 else ''}")
    if not apply:
        print("\nThat's the plan. Add --apply to move them.")
        return books
    for album, moves in books.items():
        into = os.path.join(folder, album)
        os.makedirs(into, exist_ok=True)
        for was, becomes in moves:
            target = os.path.join(into, becomes)
            if os.path.exists(target):
                print(f"  already there, left alone: {album}/{becomes}")
                continue
            os.rename(os.path.join(folder, was), target)
    print(f"\nMoved into {len(books)} folder(s). Each one is ready for: audiobook.py pack")
    return books


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


KOKORO = "kokoro-tts"       # ~/.local/bin, Kokoro-82M on the CPU, faster than realtime
# What separates the spoken opening, in seconds of real silence — the same numbers speech-webui
# announces its own books with. Punctuation buys about a third of a second, which isn't enough
# to read as "that was the title, this is the author": the title needs room to land, and the
# author needs more, or chapter one starts on top of it.
TITLE_PAUSE = 0.7
AUTHOR_PAUSE = 1.6
# A number in front of a title is a place on a shelf, not part of the name.
_SHELVED = re.compile(r"^(\d{1,3})(?!\d)\s*[-–—.:)]*\s+")


def spoken(title):
    """The title as it should be read out: without the number it is filed under.

    A folder called "01 The First Book" is how a series stays in order — a player sorting names
    alphabetically needs it, and the .m4b keeps it — but handed to Kokoro it opens the book by
    announcing its own position on the shelf. It says "zero one", too, not "one".

    This is the same thing filename() does to a track whose title starts with its own number, for
    the same reason. Four digits are left alone, so a book called 1984 or 2001 keeps its name; a
    title that really does begin with a small number — 12 Angry Men — loses it, and --title is the
    way to say so.
    """
    return _SHELVED.sub("", title).strip() or title.strip()


def stream_of(path, key):
    """One property of a file's audio stream — its rate or its channel count."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                          "-show_entries", f"stream={key}", "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def announcement(phrases, into, like, voice="af_heart"):
    """A spoken opening, made here rather than found in the recording.

    Plenty of audiobooks start straight into chapter one, and on a shelf of them that's a file
    you have to remember rather than recognise. Kokoro reads the title, then the author.

    `phrases` is [(what to say, silence after it)]. The silence is real rather than punctuation,
    and it is the point of the thing: without it the two run together and the prologue lands on
    top of the author's name.

    Everything is given the tracks' own sample rate and channel count, because ffmpeg's concat
    refuses a list whose inputs disagree — Kokoro's 24 kHz mono against a 44.1 kHz recording.
    """
    if not shutil.which(KOKORO):
        sys.exit(f"{KOKORO} isn't installed, and it's what would say the title")
    rate = stream_of(like, "sample_rate") or "44100"
    channels = stream_of(like, "channels") or "1"
    pieces = []
    for n, (text, after) in enumerate(phrases):
        raw = os.path.join(into, f".announcement-{n}.wav")
        part = os.path.join(into, f".announcement-{n}.mp3")
        print(f'Saying: "{text}" then {after}s', flush=True)
        spoken = subprocess.run([KOKORO, text, "-o", raw, "-v", voice],
                                capture_output=True, text=True, timeout=600)
        if spoken.returncode != 0 or not os.path.exists(raw):
            sys.exit(f"{KOKORO} failed:\n" + (spoken.stderr or spoken.stdout)[-400:])
        # apad rather than a silence file of its own: it re-encodes this clip, so the padding
        # can't disagree with what Kokoro produced and break the concat.
        shaped = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", raw,
                                 "-af", f"apad=pad_dur={after}", "-ar", rate, "-ac", channels,
                                 "-b:a", "128k", part], capture_output=True, text=True,
                                timeout=600)
        os.remove(raw)
        if shaped.returncode != 0:
            sys.exit("ffmpeg couldn't pad the announcement:\n" + shaped.stderr[-400:])
        pieces.append(part)
    if len(pieces) == 1:
        return pieces[0]
    # Joined into one file so the opening is one chapter mark rather than a title chapter and
    # a nameless one after it. Same parameters throughout, so this is a copy, not an encode.
    listing = os.path.join(into, ".announcement.txt")
    said = os.path.join(into, ".announcement.mp3")
    with open(listing, "w") as f:
        f.writelines(concat_line(os.path.abspath(p)) for p in pieces)
    joined = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat",
                             "-safe", "0", "-i", listing, "-c", "copy", said],
                            capture_output=True, text=True, timeout=600)
    for scratch in pieces + [listing]:
        os.remove(scratch)
    if joined.returncode != 0:
        sys.exit("ffmpeg couldn't join the announcement:\n" + joined.stderr[-400:])
    return said


def build_m4b(about, got, into, bitrate="64k", names=None, opening=False,
              voice="af_heart"):
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
    if opening:
        # After the names are counted, so a --names file still describes the book's own tracks
        # and not this. One chapter mark for the lot of it: it's an opening, not a chapter.
        author = about.get("creator")
        phrases = ([(spoken(title), TITLE_PAUSE)]
                   + ([(f"by {author}", AUTHOR_PAUSE)] if author else []))
        said = announcement(phrases, into, got[0][0], voice)
        got, titles = [(said, title)] + list(got), [title] + list(titles)
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
    for scratch in [listing, metafile] + glob.glob(os.path.join(into, ".announcement*")):
        if os.path.exists(scratch):
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
    tidied = sub.add_parser("sort", help="split a folder of mixed books into one folder each")
    tidied.add_argument("folder")
    tidied.add_argument("--apply", action="store_true", help="actually move them")
    packed = sub.add_parser("pack", help="make an .m4b from a folder of audio you already have")
    packed.add_argument("folder")
    packed.add_argument("--names", metavar="FILE",
                        help="chapter names, one per line, in place of the file names")
    packed.add_argument("--title", help="default: the folder's name")
    packed.add_argument("--author", help="default: whatever the first track's tags say")
    packed.add_argument("--bitrate", default="64k", help="AAC bitrate (default: 64k mono)")
    packed.add_argument("--announce", action="store_true",
                   help="open with the title and author, spoken by Kokoro")
    packed.add_argument("--voice", default="af_heart",
                   help="Kokoro voice for --announce (default: af_heart)")
    packed.add_argument("--upload", action="store_true",
                        help=f"put it in Proton Drive at {PROTON_DEST}")
    packed.add_argument("--dest", help="a different Proton Drive folder")
    for name, help_text in (("get", "download one, by the identifier search prints"),
                            ("m4b", "download it and make one .m4b with chapter marks")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("identifier", help="the identifier, or any archive.org URL for it")
        p.add_argument("--dir", default="audiobooks",
                       help="where to put it (default: audiobooks/)")
        p.add_argument("--format", dest="fmt", help=f"one of {', '.join(FORMATS)}")
        if name == "m4b":
            p.add_argument("--bitrate", default="64k", help="AAC bitrate (default: 64k mono)")
            p.add_argument("--announce", action="store_true",
                           help="open with the title and author, spoken by Kokoro")
            p.add_argument("--voice", default="af_heart",
                           help="Kokoro voice for --announce (default: af_heart)")
            p.add_argument("--names", metavar="FILE",
                           help="chapter names, one per line, in place of the tracks' own")
        p.add_argument("--upload", action="store_true",
                       help=f"put it in Proton Drive at {PROTON_DEST}")
        p.add_argument("--dest", help="a different Proton Drive folder")
    args = parser.parse_args(argv)
    if args.command == "sort":
        sort_folder(args.folder, args.apply)
    elif args.command == "pack":
        book = pack(args.folder, args.names, args.title, args.author, args.bitrate,
                    args.announce, args.voice)
        if args.upload:
            upload([book], args.dest)
    elif args.command == "names":
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
            book = build_m4b(about, got, into, args.bitrate, args.names,
                             args.announce, args.voice)
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
