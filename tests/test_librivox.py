"""Choosing and naming the tracks of an archive.org item.

The fetching itself isn't tested — it's two GETs against somebody else's service — but which
files it picks and what it calls them decide whether a player plays the book in order, and
those are decided from a file listing rather than from the network.
"""
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

    def test_no_title_falls_back_to_the_file(self):
        assert librivox.filename(7, {"name": "book_07_wells.mp3"}) == "07 book_07_wells.mp3"

    def test_the_extension_survives_a_long_title(self):
        f = {"name": "x.mp3", "title": "word " * 60}
        assert librivox.filename(1, f).endswith(".mp3") and len(librivox.filename(1, f)) <= 124


class TestTrackNumber:
    def test_it_reads_the_first_half(self):
        assert librivox.track_number({"track": "3/12"}) == 3

    def test_a_bare_number(self):
        assert librivox.track_number({"track": 5}) == 5

    def test_missing_or_odd(self):
        assert librivox.track_number({}) == 10_000
        assert librivox.track_number({"track": "side B"}) == 10_000
