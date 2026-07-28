"""Choosing the tracks of an archive.org item, naming them, and waiting out a refusal.

No test goes near the network: which files it picks and what it calls them are decided from a
file listing, and the retrying is tested against a urlopen that answers however the test says.
"""
import email.message
import subprocess
import urllib.error

import pytest

import audiobook

VBR = [{"name": "book_03_wells.mp3", "format": "VBR MP3", "track": "3/3", "title": "Chapter 03",
        "size": "300"},
       {"name": "book_01_wells.mp3", "format": "VBR MP3", "track": "1/3", "title": "Chapter 01",
        "size": "100"},
       {"name": "book_02_wells.mp3", "format": "VBR MP3", "track": "2/3", "title": "Chapter 02",
        "size": "200"}]
SMALL = [dict(f, format="64Kbps MP3") for f in VBR]


class TestSearching:
    """archive.org holds everything, so an unfiltered search for a book's title comes back as
    film trailers and scanned copies of it."""

    def test_it_looks_in_the_librivox_collection_by_default(self):
        q = audiobook.search_query(["the", "time", "machine"])
        assert q == 'collection:librivoxaudio AND title:("the time machine")'

    def test_another_collection(self):
        assert audiobook.search_query(["x"], "oldtimeradio").startswith("collection:oldtimeradio")

    @pytest.mark.parametrize("widen", ["any", "all", "audio", "Any", "*", "", None])
    def test_widening_it_still_means_audio(self, widen):
        """Or the answers are films and texts, which no amount of MP3-picking will fix.

        "audio" is among the words because it's the obvious guess, and it is not a collection
        anything sits in directly — asked for as one it found nothing at all."""
        assert audiobook.search_query(["x"], widen).startswith("mediatype:audio")


class TestNamingTheItem:
    """A URL is what you have in your hand after finding a recording; the identifier is buried
    in it, in the same place on every page archive.org has for an item."""

    @pytest.mark.parametrize("text", [
        "time_machine_ms_librivox",
        "https://archive.org/download/time_machine_ms_librivox",
        "https://archive.org/download/time_machine_ms_librivox/",
        "https://archive.org/details/time_machine_ms_librivox",
        "https://archive.org/metadata/time_machine_ms_librivox",
        "archive.org/details/time_machine_ms_librivox?start=3#play",
        "  https://archive.org/download/time_machine_ms_librivox  ",
    ])
    def test_every_way_of_naming_the_same_item(self, text):
        assert audiobook.identifier_of(text) == "time_machine_ms_librivox"

    def test_a_url_pointing_at_one_track_still_means_the_book(self):
        """This fetches recordings, not tracks."""
        assert audiobook.identifier_of(
            "https://archive.org/download/an_item/track_01.mp3") == "an_item"

    def test_an_escaped_character_comes_back(self):
        assert audiobook.identifier_of("https://archive.org/details/an%20item") == "an item"

    @pytest.mark.parametrize("text", [
        "https://example.com/download/an_item",
        "https://archive.org/search?query=wells",
        "some/path/somewhere",
    ])
    def test_something_that_names_no_item(self, text):
        with pytest.raises(SystemExit):
            audiobook.identifier_of(text)


class TestChoosingTheTracks:
    def test_playing_order_not_listing_order(self):
        _fmt, got = audiobook.tracks(VBR)
        assert [f["title"] for f in got] == ["Chapter 01", "Chapter 02", "Chapter 03"]

    def test_the_best_format_it_has(self):
        assert audiobook.tracks(VBR + SMALL)[0] == "VBR MP3"
        assert audiobook.tracks(SMALL)[0] == "64Kbps MP3"

    def test_one_format_only(self):
        """An item carries the same reading three times over; taking two would download the
        book twice and play every chapter twice."""
        _fmt, got = audiobook.tracks(VBR + SMALL)
        assert len(got) == 3

    def test_a_format_can_be_asked_for(self):
        fmt, got = audiobook.tracks(VBR + SMALL, want="64Kbps MP3")
        assert fmt == "64Kbps MP3" and len(got) == 3

    def test_a_format_it_hasnt_got(self):
        assert audiobook.tracks(VBR, want="128Kbps MP3") == ("", [])

    def test_an_item_with_no_audio(self):
        assert audiobook.tracks([{"name": "cover.jpg", "format": "JPEG"}]) == ("", [])

    def test_a_track_with_no_number_goes_last(self):
        odd = VBR + [{"name": "outro.mp3", "format": "VBR MP3", "title": "Outro"}]
        _fmt, got = audiobook.tracks(odd)
        assert got[-1]["title"] == "Outro"


class TestNaming:
    def test_numbered_from_the_position_and_padded(self):
        """A player sorting names alphabetically puts 10 before 2."""
        assert audiobook.filename(2, VBR[2]) == "02 Chapter 02.mp3"
        assert audiobook.filename(10, VBR[2]).startswith("10 ")

    def test_a_title_that_would_make_a_bad_filename(self):
        f = {"name": "x.mp3", "title": "Part 1/2: \"A Notice\"\tand more"}
        assert audiobook.filename(1, f) == "01 Part 1 2 A Notice and more.mp3"

    @pytest.mark.parametrize("index,title,want", [
        (1, "01 - Introduction", "01 Introduction.mp3"),
        (7, "07 The Morlocks", "07 The Morlocks.mp3"),
        (3, "3. In the Dark", "03 In the Dark.mp3"),
    ])
    def test_a_title_that_numbers_itself_is_not_numbered_twice(self, index, title, want):
        """Plenty of readers number their own titles, which gave "01 01 - Introduction"."""
        assert audiobook.filename(index, {"name": "x.mp3", "title": title}) == want

    @pytest.mark.parametrize("index,title", [
        (3, "1984"),                       # a title that is a number, and not this track's
        (2, "20,000 Leagues"),
        (5, "12 Angry Men"),
    ])
    def test_a_number_that_is_the_title_stays(self, index, title):
        out = audiobook.filename(index, {"name": "x.mp3", "title": title})
        assert out == f"{index:02d} {title}.mp3"

    def test_no_title_falls_back_to_the_file(self):
        assert audiobook.filename(7, {"name": "book_07_wells.mp3"}) == "07 book_07_wells.mp3"

    def test_the_extension_survives_a_long_title(self):
        f = {"name": "x.mp3", "title": "word " * 60}
        assert audiobook.filename(1, f).endswith(".mp3") and len(audiobook.filename(1, f)) <= 124


class TestWaitingOutARefusal:
    """archive.org answers 460 when it's had enough — its own code, undocumented, and not a
    broken file: the same URL serves in full a few seconds later."""

    def answers(self, monkeypatch, *replies):
        """urlopen gives back the next reply each call: a status code to raise, an exception
        to raise, or a value to return."""
        seen = []

        def urlopen(request, timeout=None):
            reply = replies[len(seen)]
            seen.append(request.full_url)
            if isinstance(reply, int):
                raise urllib.error.HTTPError(request.full_url, reply, "no",
                                             email.message.Message(), None)
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(audiobook.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(audiobook.time, "sleep", lambda s: None)
        return seen

    def test_it_waits_and_tries_again(self, monkeypatch):
        seen = self.answers(monkeypatch, 460, 460, "the file")
        assert audiobook.open_url("https://archive.org/x") == "the file"
        assert len(seen) == 3

    def test_a_dropped_connection_counts_as_busy(self, monkeypatch):
        self.answers(monkeypatch, ConnectionResetError(), "the file")
        assert audiobook.open_url("https://archive.org/x") == "the file"

    def test_it_gives_up_in_the_end(self, monkeypatch):
        self.answers(monkeypatch, *([503] * audiobook.TRIES))
        with pytest.raises(urllib.error.HTTPError):
            audiobook.open_url("https://archive.org/x")

    def test_a_missing_file_is_not_waited_for(self, monkeypatch):
        """Waiting won't make a 404 appear, and four doubling pauses to find that out is four
        pauses too many."""
        seen = self.answers(monkeypatch, 404, "never reached")
        with pytest.raises(urllib.error.HTTPError):
            audiobook.open_url("https://archive.org/x")
        assert len(seen) == 1

    def test_retry_after_is_honoured(self, monkeypatch):
        waits = []
        monkeypatch.setattr(audiobook.time, "sleep", waits.append)
        headers = email.message.Message()
        headers["Retry-After"] = "30"

        def urlopen(request, timeout=None):
            if not waits:
                raise urllib.error.HTTPError(request.full_url, 429, "slow down", headers, None)
            return "the file"

        monkeypatch.setattr(audiobook.urllib.request, "urlopen", urlopen)
        assert audiobook.open_url("https://archive.org/x") == "the file"
        assert waits == [30]                    # not the backoff it would have chosen


class TestTheM4b:
    """One file with chapter marks, which is what a player wants of an audiobook. ffmpeg does
    the encoding; what's tested is what it's told."""

    def test_a_quote_in_a_name_does_not_end_the_path(self):
        """ffmpeg's concat list quotes with single quotes, and a reader called a chapter
        "The Time Traveller's Return"."""
        line = audiobook.concat_line("/books/15 The Traveller's Return.mp3")
        assert line == "file '/books/15 The Traveller'\\''s Return.mp3'\n"

    def test_an_ordinary_name_is_quoted_plainly(self):
        assert audiobook.concat_line("/books/01 Intro.mp3") == "file '/books/01 Intro.mp3'\n"

    def test_the_marks_run_end_to_end(self):
        meta = audiobook.chapter_meta("A Book", "A Reader",
                                     [("One", 60.0), ("Two", 30.5), ("Three", 9.5)])
        starts = [l for l in meta.splitlines() if l.startswith("START=")]
        ends = [l for l in meta.splitlines() if l.startswith("END=")]
        assert starts == ["START=0", "START=60000", "START=90500"]
        assert ends == ["END=60000", "END=90500", "END=100000"]

    def test_no_gap_between_chapters(self):
        """A chapter ends where the next starts, so rounding can't leave a hole in the book."""
        meta = audiobook.chapter_meta("A Book", "", [("One", 1.0004), ("Two", 1.0004),
                                                    ("Three", 1.0004)])
        starts = [l.removeprefix("START=") for l in meta.splitlines() if l.startswith("START=")]
        ends = [l.removeprefix("END=") for l in meta.splitlines() if l.startswith("END=")]
        assert ends[:-1] == starts[1:]

    def test_it_says_whose_book_it_is(self):
        meta = audiobook.chapter_meta("A Book", "A Reader", [("One", 1.0)])
        assert "title=A Book" in meta and "artist=A Reader" in meta
        assert "genre=Audiobook" in meta

    def test_the_artwork_is_the_full_one_not_the_thumbnail(self):
        files = [{"name": "__ia_thumb.jpg", "format": "JPEG Thumb"},
                 {"name": "cover.jpg", "format": "JPEG"}]
        assert audiobook.cover_file(files)["name"] == "cover.jpg"

    def test_an_item_with_no_artwork(self):
        assert audiobook.cover_file([{"name": "a.mp3", "format": "VBR MP3"}]) is None

    def test_a_png_named_as_a_jpeg_entry_is_not_taken(self):
        """The spectrograms archive generates are PNGs filed under a JPEG-ish format."""
        assert audiobook.cover_file([{"name": "spectrogram.png", "format": "JPEG"}]) is None


class TestNamingTheChapters:
    """An item whose tracks are called "track 03" makes a useless chapter list, and Open Library
    has no table of contents to fetch — nothing in eighty editions of four classics had one. So
    the names get written by hand."""

    def names_file(self, tmp_path, text):
        path = tmp_path / "names.txt"
        path.write_text(text)
        return str(path)

    def test_one_per_line(self, tmp_path):
        path = self.names_file(tmp_path, "Opening\nThe Long Middle\nHow It Ends\n")
        assert audiobook.read_names(path, 3) == ["Opening", "The Long Middle", "How It Ends"]

    def test_blank_lines_and_comments_are_passed_over(self, tmp_path):
        path = self.names_file(tmp_path, "# three tracks\n\nOne\n\n  # note\nTwo\nThree\n")
        assert audiobook.read_names(path, 3) == ["One", "Two", "Three"]

    def test_a_count_that_does_not_match_is_refused(self, tmp_path):
        """Names pairing off against the wrong tracks silently would be worse than refusing,
        and off by one is exactly what happens when a reader's intro is a track."""
        path = self.names_file(tmp_path, "Only\nTwo\n")
        with pytest.raises(SystemExit) as e:
            audiobook.read_names(path, 3)
        assert "2 names for 3 tracks" in str(e.value)

    def test_they_keep_the_order_they_are_written_in(self, tmp_path):
        path = self.names_file(tmp_path, "Third\nFirst\nSecond\n")
        assert audiobook.read_names(path, 3)[0] == "Third"


class TestUploading:
    """What proton-drive is told. The upload itself is its business."""

    def ran(self, monkeypatch, tmp_path, returncode=0):
        book = tmp_path / "A Book.m4b"
        book.write_bytes(b"\0" * 2048)
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, "", "no")

        monkeypatch.setattr(audiobook.os.path, "exists", lambda p: True)
        monkeypatch.setattr(audiobook.subprocess, "run", run)
        return str(book), calls

    def test_it_goes_to_the_audiobooks_folder(self, monkeypatch, tmp_path):
        book, calls = self.ran(monkeypatch, tmp_path)
        audiobook.upload([book])
        assert calls[0][-1] == "/my-files/Audiobooks"
        assert calls[0][:3] == [audiobook.PROTON, "filesystem", "upload"]
        assert book in calls[0]

    def test_a_conflict_strategy_is_always_given(self, monkeypatch, tmp_path):
        """Without one the CLI asks what to do about a file already there, and a script waiting
        on an answer nobody will give looks exactly like a hung upload."""
        book, calls = self.ran(monkeypatch, tmp_path)
        audiobook.upload([book])
        assert "-f" in calls[0] and calls[0][calls[0].index("-f") + 1] == "replace"

    def test_somewhere_else_if_asked(self, monkeypatch, tmp_path):
        book, calls = self.ran(monkeypatch, tmp_path)
        audiobook.upload([book], "/my-files/Elsewhere")
        assert calls[0][-1] == "/my-files/Elsewhere"

    def test_a_failure_stops_rather_than_claiming_success(self, monkeypatch, tmp_path):
        book, _calls = self.ran(monkeypatch, tmp_path, returncode=1)
        with pytest.raises(SystemExit):
            audiobook.upload([book])

    def test_without_the_cli_it_says_so(self, monkeypatch, tmp_path):
        monkeypatch.setattr(audiobook.os.path, "exists", lambda p: False)
        with pytest.raises(SystemExit) as e:
            audiobook.upload(["/books/A Book.m4b"])
        assert "proton-drive" in str(e.value)


class TestTrackNumber:
    def test_it_reads_the_first_half(self):
        assert audiobook.track_number({"track": "3/12"}) == 3

    def test_a_bare_number(self):
        assert audiobook.track_number({"track": 5}) == 5

    def test_missing_or_odd(self):
        assert audiobook.track_number({}) == 10_000
        assert audiobook.track_number({"track": "side B"}) == 10_000
