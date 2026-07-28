# librivox

Fetch a LibriVox recording off archive.org as a folder of numbered MP3s, ready to import into
BookPlayer or anything else that plays a folder in name order.

Standard library only. No install, no venv, no dependencies.

```bash
./librivox.py search the time machine
./librivox.py get time_machine_ms_librivox
./librivox.py m4b time_machine_ms_librivox
./librivox.py m4b time_machine_ms_librivox --upload
./librivox.py m4b time_machine_ms_librivox --dir ~/Audiobooks --format "64Kbps MP3" --bitrate 48k
```

`search` matches words against the title within archive.org's LibriVox collection and prints the
identifier, the runtime, the title and the reader's source author. `get` takes one of those
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

A run that's interrupted resumes by being run again: a track already there at its full size is
skipped, and a partial download is written beside the real name until it's complete, so a
half-written file is never mistaken for a finished one.

## Licences

LibriVox recordings are public domain, and the item's stated licence is printed before anything
downloads — so one that turns out not to be can be left alone.

## Tests

The two GETs aren't tested; which files it picks and what it calls them are, since those decide
whether the book plays in order.

```bash
python3 -m pytest          # or any interpreter with pytest
```
