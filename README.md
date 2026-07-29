# archive-audiobook

Fetch an audiobook off archive.org — as a folder of numbered MP3s ready for BookPlayer, or as one
`.m4b` with chapter marks, and optionally straight into Proton Drive.

archive.org is where the recordings live. LibriVox is where most of the good ones come from: its
volunteers read public-domain books and upload every one to the Internet Archive. So `search`
looks in their collection, while `get` and `m4b` will take any item at all.

Standard library only. No install, no venv, no dependencies.

```bash
./audiobook.py search the time machine
./audiobook.py pack "~/Audiobooks/Some Book" --names names.txt
./audiobook.py get time_machine_ms_librivox
./audiobook.py m4b time_machine_ms_librivox
./audiobook.py m4b time_machine_ms_librivox --upload
./audiobook.py m4b https://archive.org/download/time_machine_ms_librivox
./audiobook.py m4b time_machine_ms_librivox --dir ~/Audiobooks --format "64Kbps MP3" --bitrate 48k
```

Anywhere an identifier is wanted, an archive.org URL will do — `/details/`, `/download/` or
`/metadata/`, with or without a file after it, since the identifier sits in the same place in all
of them. A URL naming one track still fetches the whole recording.

`search` matches words against the title and prints the identifier, the runtime, the title and the
author. It looks in `librivoxaudio` because archive.org holds everything: the same words unfiltered
come back as film trailers and scanned copies of the book, and audio alone still returns radio
plays, a remastered film score and podcast episodes. `--collection` looks elsewhere, and `--collection any` widens it to
everything archive.org files as audio — as do `all` and `audio`, since those are the obvious
guesses and none of them is a collection anything actually sits in.

A search that matches nothing says so, and says where it looked. Printing nothing at all reads as
"no such book" when it's just as likely to be a collection that doesn't exist. `get` takes one of those
identifiers and downloads it to `audiobooks/<identifier>/`. `m4b` does the same and then builds
one audiobook file out of the tracks.

## The .m4b

A folder of MP3s plays fine, but one `.m4b` is what an audiobook player expects: a single file
that remembers where you were, with a chapter list, artwork and the author's name in it. It needs
ffmpeg.

One chapter per track, named as the track is, with each chapter ending exactly where the next
begins so a rounding error can't leave a hole. Lengths are measured with ffprobe rather than taken
from the listing, whose lengths are rounded strings. The item's artwork is embedded if it has any.

AAC at 64 kbps mono — the source is one voice, so the channels carry the same thing, and 188 MB of
MP3 came out at 104 MB for three and a half hours. `--bitrate` if you disagree.

The tracks are kept alongside it. Building writes to a `.part` and renames when it's whole, so an
interrupted encode never leaves something that looks like an audiobook.

## A folder you already have

Not every audiobook is on archive.org. `pack` makes an `.m4b` out of audio you already have,
fetching nothing:

```bash
./audiobook.py pack "~/Audiobooks/Some Book" --names names.txt --upload
```

The order is the file order, sorted the way a person numbers things — 2 before 10, which plain
sorting gets wrong the moment a folder isn't zero-padded. Chapter names are the file names unless
`--names` says otherwise, the title is the folder's name unless `--title` does, and the author
comes off the first track's tags unless `--author` does. Its own `.m4b` and any half-downloaded
`.part` are left out, so running it twice doesn't pack the last result into the next one.

## Saying the title

`--announce` opens the book with its title and author, read by
[Kokoro](https://github.com/hexgrad/kokoro) on the CPU — for recordings that start straight into
chapter one, which on a shelf of them is a file you have to remember rather than recognise. It's
one chapter mark, so it's one tap to skip:

```
0.00s → 4.39s  A Test Book        ← the title, 0.7s, the author, 1.6s
4.39s → 6.43s  01 Chapter 1
```

The silence between them is real, not punctuation, and it's the point: a full stop buys about a
third of a second, which isn't long enough to read as *that was the title, this is the author* —
and without the pause after the author, chapter one lands on top of the name. The two numbers are
the ones speech-webui announces its own books with.

```bash
./audiobook.py pack "~/Audiobooks/Some Book" --announce --voice bm_george
```

Needs the `kokoro-tts` command; without it, it says so rather than quietly skipping. The line is
given the tracks' own sample rate and channel count, because ffmpeg's concat refuses a list whose
inputs disagree — Kokoro's 24 kHz mono against a 44.1 kHz recording. And it's added after a
`--names` file is counted, so that file still describes the book's own tracks.

## Chapter names

The chapter list comes from each track's own title, which for a LibriVox reading is usually
right. Where it isn't — tracks called `track 03`, or an item with no titles at all — write them:

```bash
./audiobook.py names some_item > names.txt   # what it would use now, one per line
$EDITOR names.txt
./audiobook.py m4b some_item --names names.txt
```

Blank lines and `#` comments are passed over. The count has to match the tracks: names quietly
pairing off against the wrong ones would be worse than refusing, and off-by-one is exactly what
happens when a reader's introduction is its own track.

Open Library isn't an option for this, in case it looks like one — it carries descriptions, not
tables of contents. Nothing in eighty editions of four classics had one.

## The page

`./webui.py` is the same thing with somewhere to type. It's the shelf as a grid of covers, and
opening a book gives you one row per track: the track's number, which is also a play button, and
the name you're giving it.

```bash
./webui.py                  # http://127.0.0.1:8610
./serve.sh                  # and the same page over HTTPS on the phone, once
```

Standard library only, like the script it drives. Nothing to install, nothing to build — the whole
interface is one `index.html`, read fresh on every request so a home-screen shortcut can't pin an
old copy of it.

Editing `names.txt` by hand works, but you can't hear anything while you do it, and a track called
`12 - CH12.mp3` doesn't tell you which chapter it is. So every row plays its own track, the filename
shows underneath a name once the two differ, and Save writes `names.txt` — which is cheap, so it
doesn't encode anything. Building is a separate button, because that's minutes of CPU you might
not want to start with your thumb.

For a book whose tracks are all called the same thing, the tools behind ⚙ are quicker than typing:
paste the whole list at once, fill `Chapter {n}` across every row, or replace one repeated scrap of
filename with another everywhere. An empty row refuses to save, and says why — the build counts lines, so a
blank one would shift every chapter after it. Anything typed but not yet saved is kept in the
browser, so a phone that gets backgrounded and killed doesn't lose fifty-four names.

**Adding one** searches archive.org, and shows you what you'd be getting before you commit to two
hundred megabytes: the licence, the size, the format it would take, and the chapter names it would
end up with — from `filename()` itself, so the preview can't disagree with the download. Every
chapter has a ▶ that streams straight from archive.org, because LibriVox readers vary and some
books change reader partway through. Nothing touches the disk until you press Get.

**The covers** can be replaced from the page, with any image — a PNG with transparency, a HEIC off
the phone — and ffmpeg converts it to the `cover.jpg` the `.m4b` embeds, scaled down if it's huge,
since the embed is a straight copy and a 4000px cover would go onto the phone at 4000px.

A book with no `cover.jpg` usually isn't missing its cover at all: a publisher's MP3s carry it in
an ID3 frame, where a player looking at the folder can't see it and `build_m4b` can't embed it.
The page offers to lift it out — per book, or for every book on the shelf at once, which after
sorting a series is all of them. It's one ffmpeg per book and no re-encoding, so twenty books is a
couple of seconds.

Which picture, though, is a real question: files carry more than one. One book here holds its cover
*and* a small square series emblem; another holds its cover *and* a photograph of the CD case with
the disc beside it. Landscape loses first, which rules out the box shot, and then the biggest wins,
which rules out the emblem. The ID3 picture type deliberately doesn't get the deciding vote — it's
what labels that emblem "Cover (front)" while the real cover says "Other" — but it breaks a tie,
since between two pictures of the same shape and size there's nothing else to go on.

The work is `audiobook.py` itself, run as a subprocess. So everything above about resuming still
holds: stopping a job is safe, and starting it again carries on from the byte it stopped at. The
log you see is the CLI's own output. One job at a time, and a second one is refused rather than
queued, with a message saying what's in the way.

`AUDIOBOOK_ROOT` is the shelf, `audiobooks/` by default. `AUDIOBOOK_PORT` is 8610.

It binds `127.0.0.1` and reaches the phone through `tailscale serve`, the way speech-webui does.
HTTPS isn't cosmetic there: Safari withholds the Media Session API from an insecure origin, and
that's the lock-screen artwork and the skip buttons. Both apps will happily run ffmpeg at the same
time on the same cores, so it's worth not starting a book and a narration together.

## Proton Drive

`--upload` puts the result in Proton Drive, which is how it reaches the phone without a cable:
the `.m4b` from `m4b`, or the tracks from `get`. It shells out to `~/.local/bin/proton-drive`,
which has to be logged in already (`proton-drive auth login`).

`/my-files/Audiobooks` by default — the folder must exist, since this uploads rather than builds
a tree. `--dest` or `PROTON_DEST` sends it somewhere else.

A conflict strategy is always passed, and that isn't cosmetic: without one the CLI *asks* what to
do about a file that's already there, and a script waiting for an answer nobody is there to give
looks exactly like a hung upload. It's `replace`, so fetching a book twice replaces it rather than
leaving `The Time Machine (1).m4b` behind. Thumbnails are skipped; there's nothing to see in an
audiobook.

## What it does with the files

An archive.org item carries the same reading in several formats — LibriVox uploads VBR, and
archive derives fixed-rate copies. Only one is taken: mixing them would download the book twice
and play every chapter twice. VBR first, then 128 kbps, then 64 kbps, which for a single voice
reading is perfectly listenable at a third of the size. `--format` overrides the choice.

Tracks are named `01 Chapter 01.mp3`, numbered by position and zero-padded, because a player
sorting names alphabetically puts 10 before 2 — and numbered from the position rather than from
the item's own track numbers, which are sometimes missing and occasionally start at zero.

## Stopping and carrying on

Ctrl-C is fine. Run the same command again and it picks up: a track already there at its full
size is skipped, and the one that was in flight carries on from the byte it stopped at, since
archive.org serves ranges. Nothing is downloaded twice.

A partial track is written beside the real name as `.part` until it's whole, so a half-written
file is never mistaken for a finished one, and a track that arrives the wrong length is thrown
away and fetched again rather than renamed into place.

## Licences

LibriVox recordings are public domain, and the item's stated licence is printed before anything
downloads — so one that turns out not to be can be left alone.

## Tests

The two GETs aren't tested; which files it picks and what it calls them are, since those decide
whether the book plays in order.

The page's own tests go nowhere near a socket or a subprocess either. The range parser is the one
worth having — Safari opens every audio element with `bytes=0-1` and refuses to play a file that
answers 200 to it, so getting it wrong is silent. Then what a path is allowed to be, since a book
id is a folder name somebody typed into a URL, and the argv it builds, because a rebuild that
forgets `--title` writes a second audiobook beside the first and leaves the stale one looking
finished. `index.html` is checked as text: every id the script reaches for exists, the tags
balance, no input is small enough to make iOS zoom on focus, and every URL it fetches is one the
server actually answers.

```bash
python3 -m pytest          # or any interpreter with pytest
```

From anywhere, too — `conftest.py` puts the script on the path, which it needs since there's no
package to install.
