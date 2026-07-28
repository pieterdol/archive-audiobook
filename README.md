# librivox

Fetch a LibriVox recording off archive.org as a folder of numbered MP3s, ready to import into
BookPlayer or anything else that plays a folder in name order.

Standard library only. No install, no venv, no dependencies.

```bash
./librivox.py search the time machine
./librivox.py get time_machine_ms_librivox
./librivox.py get time_machine_ms_librivox --dir ~/Audiobooks --format "64Kbps MP3"
```

`search` matches words against the title within archive.org's LibriVox collection and prints the
identifier, the runtime, the title and the reader's source author. `get` takes one of those
identifiers and downloads it to `audiobooks/<identifier>/`.

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
