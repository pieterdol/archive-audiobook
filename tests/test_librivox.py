"""Choosing the tracks of an archive.org item, naming them, and waiting out a refusal.

No test goes near the network: which files it picks and what it calls them are decided from a
file listing, and the retrying is tested against a urlopen that answers however the test says.
"""
import email.message
import urllib.error

import pytest

import librivox

VBR = [{"name": "book_03_wells.mp3", "format": "VBR MP3", "track": "3/3", "title": "Chapter 03",
        "size": "300"},
       {"name": "book_01_wells.mp3", "format": "VBR MP3", "track": "1/3", "title": "Chapter 01",
        "size": "100"},
       {"name": "book_02_wells.mp3", "format": "VBR MP3", "track": "2/3", "title": "Chapter 02",
        "size": "200"}]
SMALL = [dict(f, format="64Kbps MP3") for f in VBR]


class TestChoosingTheTracks:
    def test_playing_order_not_listing_order(self):
        _fmt, got = librivox.tracks(VBR)
        assert [f["title"] for f in got] == ["Chapter 01", "Chapter 02", "Chapter 03"]

    def test_the_best_format_it_has(self):
        assert librivox.tracks(VBR + SMALL)[0] == "VBR MP3"
        assert librivox.tracks(SMALL)[0] == "64Kbps MP3"

    def test_one_format_only(self):
        """An item carries the same reading three times over; taking two would download the
        book twice and play every chapter twice."""
        _fmt, got = librivox.tracks(VBR + SMALL)
        assert len(got) == 3

    def test_a_format_can_be_asked_for(self):
        fmt, got = librivox.tracks(VBR + SMALL, want="64Kbps MP3")
        assert fmt == "64Kbps MP3" and len(got) == 3

    def test_a_format_it_hasnt_got(self):
        assert librivox.tracks(VBR, want="128Kbps MP3") == ("", [])

    def test_an_item_with_no_audio(self):
        assert librivox.tracks([{"name": "cover.jpg", "format": "JPEG"}]) == ("", [])

    def test_a_track_with_no_number_goes_last(self):
        odd = VBR + [{"name": "outro.mp3", "format": "VBR MP3", "title": "Outro"}]
        _fmt, got = librivox.tracks(odd)
        assert got[-1]["title"] == "Outro"


class TestNaming:
    def test_numbered_from_the_position_and_padded(self):
        """A player sorting names alphabetically puts 10 before 2."""
        assert librivox.filename(2, VBR[2]) == "02 Chapter 02.mp3"
        assert librivox.filename(10, VBR[2]).startswith("10 ")

    def test_a_title_that_would_make_a_bad_filename(self):
        f = {"name": "x.mp3", "title": "Part 1/2: \"A Notice\"\tand more"}
        assert librivox.filename(1, f) == "01 Part 1 2 A Notice and more.mp3"

    @pytest.mark.parametrize("index,title,want", [
        (1, "01 - Introduction", "01 Introduction.mp3"),
        (7, "07 The Morlocks", "07 The Morlocks.mp3"),
        (3, "3. In the Dark", "03 In the Dark.mp3"),
    ])
    def test_a_title_that_numbers_itself_is_not_numbered_twice(self, index, title, want):
        """Plenty of readers number their own titles, which gave "01 01 - Introduction"."""
        assert librivox.filename(index, {"name": "x.mp3", "title": title}) == want

    @pytest.mark.parametrize("index,title", [
        (3, "1984"),                       # a title that is a number, and not this track's
        (2, "20,000 Leagues"),
        (5, "12 Angry Men"),
    ])
    def test_a_number_that_is_the_title_stays(self, index, title):
        out = librivox.filename(index, {"name": "x.mp3", "title": title})
        assert out == f"{index:02d} {title}.mp3"

    def test_no_title_falls_back_to_the_file(self):
        assert librivox.filename(7, {"name": "book_07_wells.mp3"}) == "07 book_07_wells.mp3"

    def test_the_extension_survives_a_long_title(self):
        f = {"name": "x.mp3", "title": "word " * 60}
        assert librivox.filename(1, f).endswith(".mp3") and len(librivox.filename(1, f)) <= 124


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

        monkeypatch.setattr(librivox.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(librivox.time, "sleep", lambda s: None)
        return seen

    def test_it_waits_and_tries_again(self, monkeypatch):
        seen = self.answers(monkeypatch, 460, 460, "the file")
        assert librivox.open_url("https://archive.org/x") == "the file"
        assert len(seen) == 3

    def test_a_dropped_connection_counts_as_busy(self, monkeypatch):
        self.answers(monkeypatch, ConnectionResetError(), "the file")
        assert librivox.open_url("https://archive.org/x") == "the file"

    def test_it_gives_up_in_the_end(self, monkeypatch):
        self.answers(monkeypatch, *([503] * librivox.TRIES))
        with pytest.raises(urllib.error.HTTPError):
            librivox.open_url("https://archive.org/x")

    def test_a_missing_file_is_not_waited_for(self, monkeypatch):
        """Waiting won't make a 404 appear, and four doubling pauses to find that out is four
        pauses too many."""
        seen = self.answers(monkeypatch, 404, "never reached")
        with pytest.raises(urllib.error.HTTPError):
            librivox.open_url("https://archive.org/x")
        assert len(seen) == 1

    def test_retry_after_is_honoured(self, monkeypatch):
        waits = []
        monkeypatch.setattr(librivox.time, "sleep", waits.append)
        headers = email.message.Message()
        headers["Retry-After"] = "30"

        def urlopen(request, timeout=None):
            if not waits:
                raise urllib.error.HTTPError(request.full_url, 429, "slow down", headers, None)
            return "the file"

        monkeypatch.setattr(librivox.urllib.request, "urlopen", urlopen)
        assert librivox.open_url("https://archive.org/x") == "the file"
        assert waits == [30]                    # not the backoff it would have chosen


class TestTrackNumber:
    def test_it_reads_the_first_half(self):
        assert librivox.track_number({"track": "3/12"}) == 3

    def test_a_bare_number(self):
        assert librivox.track_number({"track": 5}) == 5

    def test_missing_or_odd(self):
        assert librivox.track_number({}) == 10_000
        assert librivox.track_number({"track": "side B"}) == 10_000
