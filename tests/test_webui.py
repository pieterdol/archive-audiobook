"""The page's own moving parts: byte ranges, what a path is allowed to be, and the argv it builds.

Nothing here opens a socket or starts a subprocess. Everything worth testing in webui.py is a
plain function on purpose — the range parser because Safari won't play audio without it and a
wrong answer is silent, the containment checks because a book id is a folder name somebody typed,
and the argv builders because a missing --title writes a second audiobook beside the first.

The last test reads index.html rather than importing anything: with no build step, a mistyped id
in the page is a button that quietly does nothing.
"""
import html.parser
import os
import re

import pytest

import audiobook
import webui


class TestByteRanges:
    """Safari opens every audio element with "bytes=0-1" and will not play a file that answers 200
    to it, so this is what makes the player work at all."""

    SIZE = 1000

    @pytest.mark.parametrize("header, want", [
        (None, None),                               # no header at all: send the lot
        ("", None),
        ("bytes=0-1", (0, 1)),                      # the probe Safari opens with
        ("bytes=0-", (0, 999)),
        ("bytes=500-", (500, 999)),                 # a seek
        ("bytes=10-20", (10, 20)),
        ("bytes=990-99999", (990, 999)),            # asking past the end clamps
        ("bytes=-100", (900, 999)),                 # the last hundred bytes
        ("bytes=999-", (999, 999)),                 # the final byte, and not off by one
        ("BYTES=0-1", (0, 1)),                      # the header name is case-insensitive
        (" bytes=0-1 ", (0, 1)),
    ])
    def test_the_forms_a_browser_sends(self, header, want):
        assert webui.byte_range(header, self.SIZE) == want

    @pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5000-6000", "bytes=20-10",
                                       "bytes=-0"])
    def test_unsatisfiable_asks_for_a_416(self, header):
        assert webui.byte_range(header, self.SIZE) == "bad"

    @pytest.mark.parametrize("header", ["bytes=abc", "bytes=", "bytes=x-y", "chars=0-1",
                                       "0-1", "bytes=0-1, 5-6"])
    def test_nonsense_is_ignored_rather_than_refused(self, header):
        """RFC 9110 lets a server ignore a Range it doesn't understand, and it should: a browser
        sending something odd is better off with a playable file than with an error. Multi-range
        is in here because answering one part of a two-part request would be a lie."""
        assert webui.byte_range(header, self.SIZE) is None

    def test_an_empty_file_can_only_be_sent_whole(self):
        assert webui.byte_range(None, 0) is None
        assert webui.byte_range("bytes=0-", 0) == "bad"


class TestWhatAPathMayBe:
    """A book id is a folder name typed into a URL, so this is the only thing between the shelf
    and the rest of the disk."""

    @pytest.mark.parametrize("name", ["some_item_librivox", "A Book - Part One",
                                      "a", "1984", "A Book (1977).m4b"])
    def test_a_flat_name_is_fine(self, name):
        assert webui.one_component(name)

    @pytest.mark.parametrize("name", ["", ".", "..", "../etc", "a/b", "a\\b", "/etc/passwd",
                                      "./x", "a\0b", ".hidden", ".webui.json"])
    def test_anything_that_could_climb_out_is_not(self, name):
        assert not webui.one_component(name)

    def test_a_leading_dot_hides_this_module_s_own_files(self):
        """The cache and the CLI's .announcement scratch live inside the shelf. Refusing dotfiles
        means neither can be served, and neither can be mistaken for a book."""
        assert not webui.one_component(".webui.json")
        assert not webui.one_component(".announcement-0.mp3")

    @pytest.fixture
    def shelf(self, tmp_path, monkeypatch):
        book = tmp_path / "A Book"
        book.mkdir()
        (book / "01 One.mp3").write_bytes(b"\0")
        (tmp_path / "outside.txt").write_text("not yours")
        monkeypatch.setattr(webui, "ROOT", str(tmp_path))
        return tmp_path

    def test_a_real_file_in_a_real_book(self, shelf):
        assert webui.book_file("A Book", "01 One.mp3") == str(shelf / "A Book" / "01 One.mp3")

    @pytest.mark.parametrize("book, name", [
        ("A Book", "../outside.txt"),
        ("A Book", "../../etc/passwd"),
        ("..", "outside.txt"),
        ("A Book", "01 One.mp3/.."),
        ("No Such Book", "01 One.mp3"),
        ("A Book", "nothing.mp3"),
    ])
    def test_and_nothing_else(self, shelf, book, name):
        assert webui.book_file(book, name) is None

    def test_a_symlink_pointing_out_of_the_book_is_refused(self, shelf):
        """Refused by realpath, not by the name — which is the case a lexical check alone would
        wave through."""
        (shelf / "A Book" / "escape.mp3").symlink_to(shelf / "outside.txt")
        assert webui.book_file("A Book", "escape.mp3") is None

    def test_but_a_symlinked_book_folder_still_works(self, shelf, tmp_path):
        """A series living on another disk is the reason book_dir doesn't resolve."""
        elsewhere = tmp_path.parent / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        (elsewhere / "02 Two.mp3").write_bytes(b"\0")
        (shelf / "Linked").symlink_to(elsewhere)
        assert webui.book_file("Linked", "02 Two.mp3")


class TestWhichFilesAreChapters:
    """This list has to be the one `pack` would make. If the two disagree, every chapter name goes
    against the wrong track."""

    @pytest.mark.parametrize("name", ["01 One.mp3", "02 Two.M4A", "x.flac", "y.opus"])
    def test_audio_counts(self, name):
        assert webui.is_track(name)

    @pytest.mark.parametrize("name", ["The Book.m4b", "01 One.mp3.part", ".announcement-0.mp3",
                                      "cover.jpg", "names.txt", "concat.txt"])
    def test_and_the_rest_does_not(self, name):
        """Its own output, a half-finished download, and the scratch a killed run left behind —
        the last of which was once packed as track 55 of a 54-track book."""
        assert not webui.is_track(name)

    def test_the_predicate_matches_the_cli_s_own(self):
        """Belt and braces: the same names put through the expression inside pack()."""
        for name in ["01 One.mp3", "The Book.m4b", "01 One.mp3.part", ".x.mp3", "cover.jpg"]:
            low = name.lower()
            theirs = (low.endswith(audiobook.AUDIO) and not name.startswith(".")
                      and not low.endswith((".m4b", ".part")))
            assert webui.is_track(name) == theirs, name

    def test_tracks_come_back_in_playing_order(self, tmp_path):
        for name in ["10 Ten.mp3", "2 Two.mp3", "1 One.mp3", "cover.jpg"]:
            (tmp_path / name).write_bytes(b"\0" * 3)
        tracks, m4b, cover = webui.listing(str(tmp_path))
        assert [n for n, _s in tracks] == ["1 One.mp3", "2 Two.mp3", "10 Ten.mp3"]
        assert m4b is None
        assert cover and cover[0] == "cover.jpg"


class TestNamingTheBook:
    def test_the_m4b_s_filename_is_the_title(self):
        """build_m4b names the file from the title, so the stem is the title back again — which is
        how the shelf shows "The Time Machine (Version 7)" for a folder called
        thetimemachineversion_7_2105_librivox without asking ffprobe anything."""
        assert webui.title_of("thetimemachineversion_7_2105_librivox",
                              ("The Time Machine (Version 7).m4b", 1)) \
            == "The Time Machine (Version 7)"

    def test_without_one_the_folder_will_do(self):
        assert webui.title_of("A Folder Of Audio", None) == "A Folder Of Audio"
        assert webui.title_of("some_book_librivox", None) == "some book librivox"

    @pytest.mark.parametrize("name, n, want", [
        ("01 Introduction.mp3", 1, "Introduction"),
        ("12 - CH12.mp3", 12, "CH12"),
        ("03 - The Morlocks.mp3", 3, "The Morlocks"),
        ("1984.mp3", 1, "1984"),                     # a number that is the name keeps it
        ("00 - Chapter 07.mp3", 7, "00 - Chapter 07"),  # 00 isn't track seven, so it stays
        ("Just A Name.mp3", 1, "Just A Name"),
    ])
    def test_the_number_comes_off_only_when_it_is_this_track_s(self, name, n, want):
        assert webui.stem_name(name, n) == want


class TestTheNamesFile:
    """read_names skips blank lines and # comments and then refuses a count that doesn't match, so
    a name it would skip must be impossible to save."""

    @pytest.mark.parametrize("given, want", [
        ("  Chapter  One  ", "Chapter One"),
        ("#1", "1"),                                 # would have been read as a comment
        ("# The Prologue", "The Prologue"),
        ("a\nb", "a b"),                             # would have been read as two chapters
        ("", "Chapter 7"),
        ("   ", "Chapter 7"),
        (None, "Chapter 7"),
    ])
    def test_a_line_read_names_would_skip_is_impossible(self, given, want):
        assert webui.safe_name(given, 7) == want

    def test_saving_writes_what_reading_gets_back(self, tmp_path):
        given = ["  The Hook ", "#2 A Wish Fulfilled", "Practice"]
        saved = webui.write_names(str(tmp_path), given)
        assert saved == ["The Hook", "2 A Wish Fulfilled", "Practice"]
        # The count round-trip is the point: read it back the way the CLI will.
        assert audiobook.read_names(str(tmp_path / "names.txt"), 3) == saved

    def test_the_file_it_writes_carries_the_count_as_a_comment(self, tmp_path):
        webui.write_names(str(tmp_path), ["One", "Two"])
        first = (tmp_path / "names.txt").read_text().splitlines()[0]
        assert first.startswith("# 2 tracks")

    def test_nothing_is_left_behind_on_the_way(self, tmp_path):
        webui.write_names(str(tmp_path), ["One"])
        assert os.listdir(tmp_path) == ["names.txt"]

    def test_the_form_is_always_one_row_per_track(self, tmp_path):
        tracks = [("01 One.mp3", 1), ("02 Two.mp3", 1), ("03 Three.mp3", 1)]
        names, source = webui.names_now(str(tmp_path), tracks)
        assert len(names) == 3 and source == "tracks"
        (tmp_path / "names.txt").write_text("# a comment\nAlpha\n\nBeta\nGamma\n")
        names, source = webui.names_now(str(tmp_path), tracks)
        assert (names, source) == (["Alpha", "Beta", "Gamma"], "names.txt")

    def test_a_names_file_of_the_wrong_length_still_opens_the_page(self, tmp_path):
        """Refusing here would leave no way to fix the very file that's wrong."""
        tracks = [("01 One.mp3", 1), ("02 Two.mp3", 1), ("03 Three.mp3", 1)]
        (tmp_path / "names.txt").write_text("Alpha\n")
        names, source = webui.names_now(str(tmp_path), tracks)
        assert len(names) == 3 and source == "stale"
        assert names[0] == "Alpha"

    def test_the_built_m4b_s_chapters_beat_the_filenames(self, tmp_path):
        """They're what the book was last built with, so somebody meant them; the filenames are
        only what the download happened to produce."""
        tracks = [("01 One.mp3", 1), ("02 Two.mp3", 1)]
        names, source = webui.names_now(str(tmp_path), tracks, ["Introduction", "The Machine"])
        assert (names, source) == (["Introduction", "The Machine"], "the .m4b")


class TestTheArgvItBuilds:
    def test_getting_a_book_passes_the_shelf(self):
        argv = webui.argv_get({"identifier": "some_item_librivox"})
        assert argv[2:] == [os.path.join(webui.HERE, "audiobook.py"), "m4b",
                            "some_item_librivox", "--dir", webui.ROOT]
        assert argv[1] == "-u"          # or stdout is block-buffered and the log arrives at the end

    def test_and_the_options_when_they_are_asked_for(self):
        argv = webui.argv_get({"identifier": "x", "format": "64Kbps MP3", "announce": True,
                               "voice": "bm_george", "upload": True})
        assert "--format" in argv and "64Kbps MP3" in argv
        assert argv[argv.index("--voice") + 1] == "bm_george"
        assert "--announce" in argv and "--upload" in argv

    def test_a_rebuild_always_says_the_title_and_the_author(self, tmp_path):
        """`m4b` names its output from archive.org's metadata and `pack` names it from the folder.
        Leaving these off would write "some_folder.m4b" beside "The Time Machine.m4b" and leave the
        stale one sitting there looking finished."""
        argv = webui.argv_build({}, str(tmp_path), "The Time Machine (Version 7)", "H. G. Wells")
        assert argv[argv.index("--title") + 1] == "The Time Machine (Version 7)"
        assert argv[argv.index("--author") + 1] == "H. G. Wells"
        assert "pack" in argv

    def test_a_rebuild_uses_names_txt_when_there_is_one(self, tmp_path):
        assert "--names" not in webui.argv_build({}, str(tmp_path), "T", "A")
        (tmp_path / "names.txt").write_text("One\n")
        argv = webui.argv_build({}, str(tmp_path), "T", "A")
        assert argv[argv.index("--names") + 1] == str(tmp_path / "names.txt")

    def test_a_bitrate_that_isn_t_one_is_refused(self):
        assert webui.bitrate_of({"bitrate": "48k"}) == "48k"
        assert webui.bitrate_of({}) == ""
        with pytest.raises(ValueError):
            webui.bitrate_of({"bitrate": "48k; rm -rf /"})

    def test_a_voice_that_isn_t_one_is_refused(self):
        assert webui.voice_of({}) == "af_heart"
        assert webui.voice_of({"voice": "bm_george"}) == "bm_george"
        with pytest.raises(ValueError):
            webui.voice_of({"voice": "../../etc/passwd"})


class TestReadingWhatTheCliSays:
    """Nothing invents a progress protocol: this reads the lines audiobook.py already prints."""

    GETTING = """The Time Machine (version 2) — H. G. Wells
http://creativecommons.org/publicdomain/zero/1.0/
12 tracks, VBR MP3, 210 MB → /shelf/time_machine_ms_librivox

  ✓ 01 Chapter 01.mp3
  · 02 Chapter 02.mp3
    … carrying on from 12.4 MB
  ✓ 03 Chapter 03.mp3
"""

    def job(self, text):
        job = webui.new_job("get", "a_book", ["x"], "A Book")
        for line in text.splitlines(keepends=True):
            webui.note(job, line)
        return job

    def test_it_counts_the_tracks_and_the_ticks(self):
        job = self.job(self.GETTING)
        assert job["total"] == 12
        assert job["done"] == 3           # a ✓ is downloaded, a · was already here
        assert job["stage"] == "downloading"

    def test_the_last_line_is_the_thing_to_show(self):
        """"… carrying on from 12.4 MB" and "… archive.org said 503, waiting 8s" are already the
        right sentence — there's nothing to add to them."""
        job = self.job("12 tracks, VBR MP3, 210 MB → /x\n    … archive.org said 503, waiting 8s\n")
        assert job["now"] == "… archive.org said 503, waiting 8s"

    def test_it_follows_the_stages(self):
        job = self.job(self.GETTING + "Measuring 12 tracks…\n")
        assert job["stage"] == "measuring"
        webui.note(job, 'Saying: "The Time Machine" then 0.7s\n')
        assert job["stage"] == "announcing"
        webui.note(job, "Encoding 3.4 h to AAC 64k mono — a few minutes…\n")
        assert job["stage"] == "encoding"
        # Kept so the size of the .part can be turned into a percentage — build_m4b hands ffmpeg
        # capture_output=True, so there is no other sign of progress for minutes at a time.
        assert (job["hours"], job["bitrate"]) == (3.4, 64)
        webui.note(job, "Uploading 1 file, 104 MB, to /my-files/Audiobooks …\n")
        assert job["stage"] == "uploading"

    def test_a_pack_counts_its_tracks_too(self):
        """`pack` says "3 tracks from /folder" — no comma, unlike the download's "3 tracks, VBR"."""
        job = self.job("A Test Book — A Reader\n3 tracks from /shelf/A Test Book\n")
        assert job["total"] == 3

    def test_a_blank_line_is_not_the_news(self):
        job = self.job("Measuring 3 tracks…\n\n\n")
        assert job["now"] == "Measuring 3 tracks…"


class TestTheCoverInsideTheTracks:
    """A publisher's MP3s carry the artwork in an ID3 frame, where neither a player looking at the
    folder nor build_m4b can reach it. Getting it out means choosing between the several pictures a
    file can hold, and the file's own labelling of them can't be trusted."""

    def art(self, *sizes):
        return [{"index": i, "codec": "mjpeg", "width": w, "height": h, "what": what}
                for i, (w, h, what) in enumerate(sizes, start=1)]

    def test_nothing_attached_means_nothing_to_take(self):
        assert webui.best_art([]) is None

    def test_one_picture_is_the_one(self):
        assert webui.best_art(self.art((938, 1425, "Other")))["width"] == 938

    def test_the_biggest_wins(self):
        """A small square beside a large portrait is a series emblem, not the book's cover — and it
        is the one the file labels "Cover (front)"."""
        chosen = webui.best_art(self.art((800, 1200, "Other"), (400, 400, "Cover (front)")))
        assert (chosen["width"], chosen["height"]) == (800, 1200)

    def test_landscape_loses_however_big_it_is(self):
        """A wide picture of the CD case with the disc beside it is not a cover, and on one book it
        was the *only* other candidate."""
        chosen = webui.best_art(self.art((500, 386, "Other"), (450, 684, "Other")))
        assert (chosen["width"], chosen["height"]) == (450, 684)
        # even when the landscape one has far more pixels
        chosen = webui.best_art(self.art((4000, 1000, "Other"), (300, 450, "Other")))
        assert (chosen["width"], chosen["height"]) == (300, 450)

    def test_the_label_only_breaks_a_tie(self):
        """Between two pictures of the same shape and size there is nothing else left to go on."""
        chosen = webui.best_art(self.art((600, 900, "Other"), (600, 900, "Cover (front)")))
        assert chosen["what"] == "Cover (front)"

    def test_a_book_with_a_cover_is_left_alone(self, tmp_path, monkeypatch):
        """Not an error worth hiding: the batch button walks the whole shelf, so "it already has
        one" is the ordinary answer for most of it."""
        monkeypatch.setattr(webui, "ROOT", str(tmp_path))
        book = tmp_path / "A Book"
        book.mkdir()
        (book / "01 One.mp3").write_bytes(b"\0")
        (book / "cover.jpg").write_bytes(b"\xff\xd8\xff")
        version, why = webui.take_artwork("A Book", str(book))
        assert version is None and "already" in why

    def test_a_book_with_no_audio_has_nothing_to_take_from(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webui, "ROOT", str(tmp_path))
        book = tmp_path / "Empty"
        book.mkdir()
        version, why = webui.take_artwork("Empty", str(book))
        assert version is None and "no audio" in why

    def test_a_square_counts_as_portrait(self):
        """Square artwork is what archive.org serves, and it is a cover; landscape is what a
        photograph of a box is."""
        chosen = webui.best_art(self.art((600, 400, "Other"), (500, 500, "Other")))
        assert (chosen["width"], chosen["height"]) == (500, 500)


class TestPickingOutAnImage:
    """A magic-byte allowlist, because a content type and a filename are both whatever the client
    felt like saying — and ffmpeg will happily pull frame one out of a video."""

    @pytest.mark.parametrize("raw", [b"\xff\xd8\xff\xe0 jpeg", b"\x89PNG\r\n\x1a\n",
                                     b"GIF89a...", b"BM haha", b"RIFF____WEBPVP8 ",
                                     b"\0\0\0 ftypavif"])
    def test_pictures(self, raw):
        assert webui.looks_like_image(raw)

    @pytest.mark.parametrize("raw", [b"", b"not an image at all", b"\0\0\0 ftypisom",
                                     b"ID3\x04\0\0 an mp3", b"<svg xmlns=", b"%PDF-1.4"])
    def test_and_not_pictures(self, raw):
        """ftypisom is an MP4 and ID3 is an MP3 with cover art inside it. Both would convert, and
        both would give you a picture you didn't choose."""
        assert not webui.looks_like_image(raw)


class TestTheSmallThings:
    @pytest.mark.parametrize("given, want", [("1234.5", 1234.5), ("12:34", 754.0),
                                            ("1:02:03", 3723.0), ("", 0.0), (None, 0.0),
                                            ("nonsense", 0.0)])
    def test_archive_writes_a_length_either_way(self, given, want):
        assert webui.parse_length(given) == want

    @pytest.mark.parametrize("seconds, want", [(0, ""), (None, ""), (240, "4 m"),
                                              (12420, "3 h 27 m"), (3600, "1 h 0 m")])
    def test_a_runtime_a_person_would_say(self, seconds, want):
        assert webui.runtime(seconds) == want


class TestThePage:
    """index.html has no build step, so a mistyped id is a button that silently does nothing and a
    forgotten closing tag is a layout that collapses on the phone only."""

    @pytest.fixture
    def page(self):
        with open(os.path.join(webui.HERE, "index.html")) as f:
            return f.read()

    def test_every_id_the_script_reaches_for_exists(self, page):
        # Both the markup and the ids the script writes into innerHTML, since several controls —
        # the Get button, the format picker — only exist once something has been rendered.
        have = set(re.findall(r'id="([\w-]+)"', page))
        wanted = set(re.findall(r'\$\$?\("#([\w-]+)', page))
        assert not (wanted - have), f"the script looks for ids that don't exist: {wanted - have}"

    def test_the_tags_balance(self, page):
        class Check(html.parser.HTMLParser):
            VOID = {"meta", "link", "br", "hr", "img", "input", "source", "track", "area",
                    "base", "col", "embed", "param", "wbr"}

            def __init__(self):
                super().__init__()
                self.stack, self.trouble = [], []

            def handle_starttag(self, tag, attrs):
                if tag not in self.VOID:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in self.VOID:
                    return
                if not self.stack:
                    self.trouble.append(f"</{tag}> with nothing open")
                elif self.stack[-1] != tag:
                    self.trouble.append(f"</{tag}> closes <{self.stack[-1]}>")
                else:
                    self.stack.pop()

        check = Check()
        check.feed(page)
        assert not check.trouble, check.trouble
        assert not check.stack, f"never closed: {check.stack}"

    def test_the_ios_head_is_there(self, page):
        """viewport-fit=cover plus black-translucent is what puts the page under the status bar,
        which is what makes every env(safe-area-inset-*) in the stylesheet necessary."""
        assert "viewport-fit=cover" in page
        assert "apple-mobile-web-app-status-bar-style" in page
        assert "env(safe-area-inset-top" in page

    def test_no_input_is_small_enough_to_make_ios_zoom(self, page):
        """Anything under 16px makes Safari zoom the page on focus, and a zoom halfway down a
        54-row list throws away where you were."""
        style = page.split("<style>")[1].split("</style>")[0]
        rule = re.search(r"textarea, input\[type=text\], select \{([^}]*)\}", style)
        assert rule and "font-size:16px" in rule.group(1).replace(" ", "")
        # And the rows, which get their density from padding instead.
        assert re.search(r"\.nm \{[^}]*padding:", style)
        assert not re.search(r"\.nm \{[^}]*font-size:1[0-5]", style)

    def test_the_grid_reflows_without_a_media_query(self, page):
        """auto-fill with a minimum goes from two columns on a phone to seven on a desktop by
        itself. The only media query in here is the one shrinking the player's skip buttons."""
        assert "repeat(auto-fill,minmax(104px,1fr))" in page.replace(" ", "")

    def test_the_script_parses(self, page):
        """The one thing a browser would tell you and nothing else will. esprima is an ES2017
        parser, so ?? and ?. are swapped for something it knows — this is here for unbalanced
        braces, unterminated template literals and stray quotes, not for the operators."""
        esprima = pytest.importorskip("esprima")
        script = page.split("<script>")[1].split("</script>")[0]
        esprima.parseScript(script.replace("??", "||").replace("?.", "."))

    def test_every_url_it_fetches_is_one_the_server_answers(self, page):
        """The page and the routing table live in different files with nothing linking them, so a
        renamed endpoint is a button that 404s and a spinner that never stops.

        Checked as prefixes and trailing segments rather than whole paths, because the ids in the
        middle are spliced in and reassembling them from the source is more fragile than the thing
        it would be testing."""
        script = page.split("<script>")[1].split("</script>")[0]
        routes = ["/api/books", "/api/books/*", "/api/books/*/names", "/api/books/*/cover",
                  "/api/books/*/build", "/api/books/*/artwork", "/api/search", "/api/item",
                  "/api/get", "/api/artwork", "/api/jobs/*", "/api/jobs/*/cancel",
                  "/file/*/*", "/get/*/*"]
        # Every literal that starts a path, up to wherever an id gets spliced in.
        starts = {m.group(1) for m in re.finditer(r'["`](/(?:api|file|get)[\w/-]*)', script)}
        assert starts, "the page doesn't fetch anything at all?"
        for start in starts:
            assert any(r.startswith(start.rstrip("/")) for r in routes), \
                f"the page fetches {start}, which nothing serves"
        # And what comes after an id: /names, /cover, /build, /cancel.
        tails = {m.group(1) for m in re.finditer(r'\)\}?\s*\+?\s*["`](/[\w-]+)["`]', script)}
        for tail in tails:
            assert any(r.endswith(tail) for r in routes), \
                f"the page posts to {tail}, which nothing serves"

    def test_covers_are_letterboxed_rather_than_cropped(self, page):
        """Archive.org artwork is usually a square tile with the title set as text inside the
        picture, and cover in a 2/3 box takes a third off the top and bottom — the title band."""
        style = page.split("<style>")[1].split("</style>")[0]
        art = re.search(r"\.bookcard \.art img \{([^}]*)\}", style)
        assert art and "object-fit:contain" in art.group(1)
