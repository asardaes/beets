# This file is part of beets.
# Copyright 2016, Thomas Scholtes.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""This module includes various helpers that provide fixtures, capture
information or mock the environment.

- The `control_stdin` and `capture_stdout` context managers allow one to
  interact with the user interface.

- `has_program` checks the presence of a command on the system.

- The `ImportSessionFixture` allows one to run importer code while
  controlling the interactions through code.

- The `TestHelper` class encapsulates various fixtures that can be set up.
"""

from __future__ import annotations

import os
import os.path
import shutil
import subprocess
import sys
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from io import StringIO
from pathlib import Path
from tempfile import gettempdir, mkdtemp, mkstemp
from typing import Any, ClassVar
from unittest.mock import patch

import responses
from mediafile import Image, MediaFile

import beets
import beets.plugins
from beets import importer, logging, util
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.importer import ImportSession
from beets.library import Item, Library
from beets.test import _common
from beets.ui.commands import TerminalImportSession
from beets.util import (
    MoveOperation,
    bytestring_path,
    cached_classproperty,
    clean_module_tempdir,
    syspath,
)


class LogCapture(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.messages = []

    def emit(self, record):
        self.messages.append(str(record.msg))


@contextmanager
def capture_log(logger="beets"):
    capture = LogCapture()
    log = logging.getLogger(logger)
    log.addHandler(capture)
    try:
        yield capture.messages
    finally:
        log.removeHandler(capture)


@contextmanager
def control_stdin(input=None):
    """Sends ``input`` to stdin.

    >>> with control_stdin('yes'):
    ...     input()
    'yes'
    """
    org = sys.stdin
    sys.stdin = StringIO(input)
    try:
        yield sys.stdin
    finally:
        sys.stdin = org


@contextmanager
def capture_stdout():
    """Save stdout in a StringIO.

    >>> with capture_stdout() as output:
    ...     print('spam')
    ...
    >>> output.getvalue()
    'spam'
    """
    org = sys.stdout
    sys.stdout = capture = StringIO()
    try:
        yield sys.stdout
    finally:
        sys.stdout = org
        print(capture.getvalue())


def has_program(cmd, args=["--version"]):
    """Returns `True` if `cmd` can be executed."""
    full_cmd = [cmd] + args
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.check_call(
                full_cmd, stderr=devnull, stdout=devnull, stdin=devnull
            )
    except OSError:
        return False
    except subprocess.CalledProcessError:
        return False
    else:
        return True


def check_reflink_support(path: str) -> bool:
    try:
        import reflink
    except ImportError:
        return False

    return reflink.supported_at(path)


class ConfigMixin:
    @cached_property
    def config(self) -> beets.IncludeLazyConfig:
        """Base beets configuration for tests."""
        config = beets.config
        config.sources = []
        config.read(user=False, defaults=True)

        config["plugins"] = []
        config["verbose"] = 1
        config["ui"]["color"] = False
        config["threaded"] = False
        return config


NEEDS_REFLINK = unittest.skipUnless(
    check_reflink_support(gettempdir()), "no reflink support for libdir"
)


class IOMixin:
    @cached_property
    def io(self) -> _common.DummyIO:
        return _common.DummyIO()

    def setUp(self):
        super().setUp()
        self.io.install()

    def tearDown(self):
        super().tearDown()
        self.io.restore()


class TestHelper(ConfigMixin):
    """Helper mixin for high-level cli and plugin tests.

    This mixin provides methods to isolate beets' global state provide
    fixtures.
    """

    resource_path = Path(os.fsdecode(_common.RSRC)) / "full.mp3"

    db_on_disk: ClassVar[bool] = False

    @cached_property
    def temp_dir_path(self) -> Path:
        return Path(self.create_temp_dir())

    @cached_property
    def temp_dir(self) -> bytes:
        return util.bytestring_path(self.temp_dir_path)

    @cached_property
    def lib_path(self) -> Path:
        lib_path = self.temp_dir_path / "libdir"
        lib_path.mkdir(exist_ok=True)
        return lib_path

    @cached_property
    def libdir(self) -> bytes:
        return bytestring_path(self.lib_path)

    # TODO automate teardown through hook registration

    def setup_beets(self):
        """Setup pristine global configuration and library for testing.

        Sets ``beets.config`` so we can safely use any functionality
        that uses the global configuration.  All paths used are
        contained in a temporary directory

        Sets the following properties on itself.

        - ``temp_dir`` Path to a temporary directory containing all
          files specific to beets

        - ``libdir`` Path to a subfolder of ``temp_dir``, containing the
          library's media files. Same as ``config['directory']``.

        - ``lib`` Library instance created with the settings from
          ``config``.

        Make sure you call ``teardown_beets()`` afterwards.
        """
        temp_dir_str = str(self.temp_dir_path)
        self.env_patcher = patch.dict(
            "os.environ",
            {
                "BEETSDIR": temp_dir_str,
                "HOME": temp_dir_str,  # used by Confuse to create directories.
            },
        )
        self.env_patcher.start()

        self.config["directory"] = str(self.lib_path)

        if self.db_on_disk:
            dbpath = util.bytestring_path(self.config["library"].as_filename())
        else:
            dbpath = ":memory:"
        self.lib = Library(dbpath, self.libdir)

    def teardown_beets(self):
        self.env_patcher.stop()
        self.lib._close()
        self.remove_temp_dir()

    # Library fixtures methods

    def create_item(self, **values):
        """Return an `Item` instance with sensible default values.

        The item receives its attributes from `**values` paratmeter. The
        `title`, `artist`, `album`, `track`, `format` and `path`
        attributes have defaults if they are not given as parameters.
        The `title` attribute is formatted with a running item count to
        prevent duplicates. The default for the `path` attribute
        respects the `format` value.

        The item is attached to the database from `self.lib`.
        """
        values_ = {
            "title": "t\u00eftle {0}",
            "artist": "the \u00e4rtist",
            "album": "the \u00e4lbum",
            "track": 1,
            "format": "MP3",
        }
        values_.update(values)
        values_["title"] = values_["title"].format(1)
        values_["db"] = self.lib
        item = Item(**values_)
        if "path" not in values:
            item["path"] = "audio." + item["format"].lower()
        # mtime needs to be set last since other assignments reset it.
        item.mtime = 12345
        return item

    def add_item(self, **values):
        """Add an item to the library and return it.

        Creates the item by passing the parameters to `create_item()`.

        If `path` is not set in `values` it is set to `item.destination()`.
        """
        # When specifying a path, store it normalized (as beets does
        # ordinarily).
        if "path" in values:
            values["path"] = util.normpath(values["path"])

        item = self.create_item(**values)
        item.add(self.lib)

        # Ensure every item has a path.
        if "path" not in values:
            item["path"] = item.destination()
            item.store()

        return item

    def add_item_fixture(self, **values):
        """Add an item with an actual audio file to the library."""
        item = self.create_item(**values)
        extension = item["format"].lower()
        item["path"] = os.path.join(
            _common.RSRC, util.bytestring_path("min." + extension)
        )
        item.add(self.lib)
        item.move(operation=MoveOperation.COPY)
        item.store()
        return item

    def add_album(self, **values):
        item = self.add_item(**values)
        return self.lib.add_album([item])

    def add_item_fixtures(self, ext="mp3", count=1):
        """Add a number of items with files to the database."""
        # TODO base this on `add_item()`
        items = []
        path = os.path.join(_common.RSRC, util.bytestring_path("full." + ext))
        for i in range(count):
            item = Item.from_path(path)
            item.album = f"\u00e4lbum {i}"  # Check unicode paths
            item.title = f"t\u00eftle {i}"
            # mtime needs to be set last since other assignments reset it.
            item.mtime = 12345
            item.add(self.lib)
            item.move(operation=MoveOperation.COPY)
            item.store()
            items.append(item)
        return items

    def add_album_fixture(
        self,
        track_count=1,
        fname="full",
        ext="mp3",
        disc_count=1,
    ):
        """Add an album with files to the database."""
        items = []
        path = os.path.join(
            _common.RSRC,
            util.bytestring_path(f"{fname}.{ext}"),
        )
        for discnumber in range(1, disc_count + 1):
            for i in range(track_count):
                item = Item.from_path(path)
                item.album = "\u00e4lbum"  # Check unicode paths
                item.title = f"t\u00eftle {i}"
                item.disc = discnumber
                # mtime needs to be set last since other assignments reset it.
                item.mtime = 12345
                item.add(self.lib)
                item.move(operation=MoveOperation.COPY)
                item.store()
                items.append(item)
        return self.lib.add_album(items)

    def create_mediafile_fixture(self, ext="mp3", images=[]):
        """Copy a fixture mediafile with the extension to `temp_dir`.

        `images` is a subset of 'png', 'jpg', and 'tiff'. For each
        specified extension a cover art image is added to the media
        file.
        """
        src = os.path.join(_common.RSRC, util.bytestring_path("full." + ext))
        handle, path = mkstemp(dir=self.temp_dir)
        path = bytestring_path(path)
        os.close(handle)
        shutil.copyfile(syspath(src), syspath(path))

        if images:
            mediafile = MediaFile(path)
            imgs = []
            for img_ext in images:
                file = util.bytestring_path(f"image-2x3.{img_ext}")
                img_path = os.path.join(_common.RSRC, file)
                with open(img_path, "rb") as f:
                    imgs.append(Image(f.read()))
            mediafile.images = imgs
            mediafile.save()

        return path

    # Running beets commands

    def run_command(self, *args, **kwargs):
        """Run a beets command with an arbitrary amount of arguments. The
        Library` defaults to `self.lib`, but can be overridden with
        the keyword argument `lib`.
        """
        sys.argv = ["beet"]  # avoid leakage from test suite args
        lib = None
        if hasattr(self, "lib"):
            lib = self.lib
        lib = kwargs.get("lib", lib)
        beets.ui._raw_main(list(args), lib)

    def run_with_output(self, *args):
        with capture_stdout() as out:
            self.run_command(*args)
        return out.getvalue()

    # Safe file operations

    def create_temp_dir(self, **kwargs) -> str:
        return mkdtemp(**kwargs)

    def remove_temp_dir(self):
        """Delete the temporary directory created by `create_temp_dir`."""
        shutil.rmtree(self.temp_dir_path)

    def touch(self, path, dir=None, content=""):
        """Create a file at `path` with given content.

        If `dir` is given, it is prepended to `path`. After that, if the
        path is relative, it is resolved with respect to
        `self.temp_dir`.
        """
        if dir:
            path = os.path.join(dir, path)

        if not os.path.isabs(path):
            path = os.path.join(self.temp_dir, path)

        parent = os.path.dirname(path)
        if not os.path.isdir(syspath(parent)):
            os.makedirs(syspath(parent))

        with open(syspath(path), "a+") as f:
            f.write(content)
        return path


# A test harness for all beets tests.
# Provides temporary, isolated configuration.
class BeetsTestCase(unittest.TestCase, TestHelper):
    """A unittest.TestCase subclass that saves and restores beets'
    global configuration. This allows tests to make temporary
    modifications that will then be automatically removed when the test
    completes. Also provides some additional assertion methods, a
    temporary directory, and a DummyIO.
    """

    def setUp(self):
        self.setup_beets()

    def tearDown(self):
        self.teardown_beets()


class ItemInDBTestCase(BeetsTestCase):
    """A test case that includes an in-memory library object (`lib`) and
    an item added to the library (`i`).
    """

    def setUp(self):
        super().setUp()
        self.i = _common.item(self.lib)


class PluginMixin(ConfigMixin):
    plugin: ClassVar[str]
    preload_plugin: ClassVar[bool] = True

    def setup_beets(self):
        super().setup_beets()
        if self.preload_plugin:
            self.load_plugins()

    def teardown_beets(self):
        super().teardown_beets()
        self.unload_plugins()

    def load_plugins(self, *plugins: str) -> None:
        """Load and initialize plugins by names.

        Similar setting a list of plugins in the configuration. Make
        sure you call ``unload_plugins()`` afterwards.
        """
        # FIXME this should eventually be handled by a plugin manager
        plugins = (self.plugin,) if hasattr(self, "plugin") else plugins
        self.config["plugins"] = plugins
        cached_classproperty.cache.clear()
        beets.plugins.load_plugins(plugins)
        beets.plugins.send("pluginload")
        beets.plugins.find_plugins()

    def unload_plugins(self) -> None:
        """Unload all plugins and remove them from the configuration."""
        # FIXME this should eventually be handled by a plugin manager
        for plugin_class in beets.plugins._instances:
            plugin_class.listeners = None
        self.config["plugins"] = []
        beets.plugins._classes = set()
        beets.plugins._instances = {}

    @contextmanager
    def configure_plugin(self, config: Any):
        self.config[self.plugin].set(config)
        self.load_plugins(self.plugin)

        yield

        self.unload_plugins()


class PluginTestCase(PluginMixin, BeetsTestCase):
    pass


class ImportHelper(TestHelper):
    """Provides tools to setup a library, a directory containing files that are
    to be imported and an import session. The class also provides stubs for the
    autotagging library and several assertions for the library.
    """

    default_import_config = {
        "autotag": True,
        "copy": True,
        "hardlink": False,
        "link": False,
        "move": False,
        "resume": False,
        "singletons": False,
        "timid": True,
    }

    lib: Library
    importer: ImportSession

    @cached_property
    def import_path(self) -> Path:
        import_path = self.temp_dir_path / "import"
        import_path.mkdir(exist_ok=True)
        return import_path

    @cached_property
    def import_dir(self) -> bytes:
        return bytestring_path(self.import_path)

    def setUp(self):
        super().setUp()
        self.import_media = []
        self.lib.path_formats = [
            ("default", os.path.join("$artist", "$album", "$title")),
            ("singleton:true", os.path.join("singletons", "$title")),
            ("comp:true", os.path.join("compilations", "$album", "$title")),
        ]

    def prepare_track_for_import(
        self,
        track_id: int,
        album_path: Path,
        album_id: int | None = None,
    ) -> Path:
        track_path = album_path / f"track_{track_id}.mp3"
        shutil.copy(self.resource_path, track_path)
        medium = MediaFile(track_path)
        medium.update(
            {
                "album": "Tag Album" + (f" {album_id}" if album_id else ""),
                "albumartist": None,
                "mb_albumid": None,
                "comp": None,
                "artist": "Tag Artist",
                "title": f"Tag Track {track_id}",
                "track": track_id,
                "mb_trackid": None,
            }
        )
        medium.save()
        self.import_media.append(medium)
        return track_path

    def prepare_album_for_import(
        self,
        item_count: int,
        album_id: int | None = None,
        album_path: Path | None = None,
    ) -> list[Path]:
        """Create an album directory with media files to import.

        The directory has following layout
          album/
            track_1.mp3
            track_2.mp3
            track_3.mp3
        """
        if not album_path:
            album_dir = f"album_{album_id}" if album_id else "album"
            album_path = self.import_path / album_dir

        album_path.mkdir(exist_ok=True)

        return [
            self.prepare_track_for_import(tid, album_path, album_id=album_id)
            for tid in range(1, item_count + 1)
        ]

    def prepare_albums_for_import(self, count: int = 1) -> None:
        album_dirs = self.import_path.glob("album_*")
        base_idx = int(str(max(album_dirs, default="0")).split("_")[-1]) + 1

        for album_id in range(base_idx, count + base_idx):
            self.prepare_album_for_import(1, album_id=album_id)

    def _get_import_session(self, import_dir: bytes) -> ImportSession:
        return ImportSessionFixture(
            self.lib,
            loghandler=None,
            query=None,
            paths=[import_dir],
        )

    def setup_importer(
        self, import_dir: bytes | None = None, **kwargs
    ) -> ImportSession:
        self.config["import"].set_args({**self.default_import_config, **kwargs})
        self.importer = self._get_import_session(import_dir or self.import_dir)
        return self.importer

    def setup_singleton_importer(self, **kwargs) -> ImportSession:
        return self.setup_importer(singletons=True, **kwargs)


class AsIsImporterMixin:
    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)

    def run_asis_importer(self, **kwargs):
        importer = self.setup_importer(autotag=False, **kwargs)
        importer.run()
        return importer


class ImportTestCase(ImportHelper, BeetsTestCase):
    pass


class ImportSessionFixture(ImportSession):
    """ImportSession that can be controlled programaticaly.

    >>> lib = Library(':memory:')
    >>> importer = ImportSessionFixture(lib, paths=['/path/to/import'])
    >>> importer.add_choice(importer.Action.SKIP)
    >>> importer.add_choice(importer.Action.ASIS)
    >>> importer.default_choice = importer.Action.APPLY
    >>> importer.run()

    This imports ``/path/to/import`` into `lib`. It skips the first
    album and imports the second one with metadata from the tags. For the
    remaining albums, the metadata from the autotagger will be applied.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._choices = []
        self._resolutions = []

    default_choice = importer.Action.APPLY

    def add_choice(self, choice):
        self._choices.append(choice)

    def clear_choices(self):
        self._choices = []

    def choose_match(self, task):
        try:
            choice = self._choices.pop(0)
        except IndexError:
            choice = self.default_choice

        if choice == importer.Action.APPLY:
            return task.candidates[0]
        elif isinstance(choice, int):
            return task.candidates[choice - 1]
        else:
            return choice

    choose_item = choose_match

    Resolution = Enum("Resolution", "REMOVE SKIP KEEPBOTH MERGE")

    default_resolution = "REMOVE"

    def resolve_duplicate(self, task, found_duplicates):
        try:
            res = self._resolutions.pop(0)
        except IndexError:
            res = self.default_resolution

        if res == self.Resolution.SKIP:
            task.set_choice(importer.Action.SKIP)
        elif res == self.Resolution.REMOVE:
            task.should_remove_duplicates = True
        elif res == self.Resolution.MERGE:
            task.should_merge_duplicates = True


class TerminalImportSessionFixture(TerminalImportSession):
    def __init__(self, *args, **kwargs):
        self.io = kwargs.pop("io")
        super().__init__(*args, **kwargs)
        self._choices = []

    default_choice = importer.Action.APPLY

    def add_choice(self, choice):
        self._choices.append(choice)

    def clear_choices(self):
        self._choices = []

    def choose_match(self, task):
        self._add_choice_input()
        return super().choose_match(task)

    def choose_item(self, task):
        self._add_choice_input()
        return super().choose_item(task)

    def _add_choice_input(self):
        try:
            choice = self._choices.pop(0)
        except IndexError:
            choice = self.default_choice

        if choice == importer.Action.APPLY:
            self.io.addinput("A")
        elif choice == importer.Action.ASIS:
            self.io.addinput("U")
        elif choice == importer.Action.ALBUMS:
            self.io.addinput("G")
        elif choice == importer.Action.TRACKS:
            self.io.addinput("T")
        elif choice == importer.Action.SKIP:
            self.io.addinput("S")
        else:
            self.io.addinput("M")
            self.io.addinput(str(choice))
            self._add_choice_input()


class TerminalImportMixin(IOMixin, ImportHelper):
    """Provides_a terminal importer for the import session."""

    io: _common.DummyIO

    def _get_import_session(self, import_dir: bytes) -> importer.ImportSession:
        self.io.install()
        return TerminalImportSessionFixture(
            self.lib,
            loghandler=None,
            query=None,
            io=self.io,
            paths=[import_dir],
        )


@dataclass
class AutotagStub:
    """Stub out MusicBrainz album and track matcher and control what the
    autotagger returns.
    """

    NONE = "NONE"
    IDENT = "IDENT"
    GOOD = "GOOD"
    BAD = "BAD"
    MISSING = "MISSING"
    matching: str

    length = 2

    def install(self):
        self.patchers = [
            patch("beets.metadata_plugins.album_for_id", lambda *_: None),
            patch("beets.metadata_plugins.track_for_id", lambda *_: None),
            patch("beets.metadata_plugins.candidates", self.candidates),
            patch(
                "beets.metadata_plugins.item_candidates", self.item_candidates
            ),
        ]
        for p in self.patchers:
            p.start()

        return self

    def restore(self):
        for p in self.patchers:
            p.stop()

    def candidates(self, items, artist, album, va_likely):
        if self.matching == self.IDENT:
            yield self._make_album_match(artist, album, len(items))

        elif self.matching == self.GOOD:
            for i in range(self.length):
                yield self._make_album_match(artist, album, len(items), i)

        elif self.matching == self.BAD:
            for i in range(self.length):
                yield self._make_album_match(artist, album, len(items), i + 1)

        elif self.matching == self.MISSING:
            yield self._make_album_match(artist, album, len(items), missing=1)

    def item_candidates(self, item, artist, title):
        yield TrackInfo(
            title=title.replace("Tag", "Applied"),
            track_id="trackid",
            artist=artist.replace("Tag", "Applied"),
            artist_id="artistid",
            length=1,
            index=0,
        )

    def _make_track_match(self, artist, album, number):
        return TrackInfo(
            title="Applied Track %d" % number,
            track_id="match %d" % number,
            artist=artist,
            length=1,
            index=0,
        )

    def _make_album_match(self, artist, album, tracks, distance=0, missing=0):
        if distance:
            id = " " + "M" * distance
        else:
            id = ""
        if artist is None:
            artist = "Various Artists"
        else:
            artist = artist.replace("Tag", "Applied") + id
        album = album.replace("Tag", "Applied") + id

        track_infos = []
        for i in range(tracks - missing):
            track_infos.append(self._make_track_match(artist, album, i + 1))

        return AlbumInfo(
            artist=artist,
            album=album,
            tracks=track_infos,
            va=False,
            album_id="albumid" + id,
            artist_id="artistid" + id,
            albumtype="soundtrack",
            data_source="match_source",
            bandcamp_album_id="bc_url",
        )


class AutotagImportTestCase(ImportTestCase):
    matching = AutotagStub.IDENT

    def setUp(self):
        super().setUp()
        self.matcher = AutotagStub(self.matching).install()
        self.addCleanup(self.matcher.restore)


class FetchImageHelper:
    """Helper mixin for mocking requests when fetching images
    with remote art sources.
    """

    @responses.activate
    def run(self, *args, **kwargs):
        super().run(*args, **kwargs)

    IMAGEHEADER: dict[str, bytes] = {
        "image/jpeg": b"\xff\xd8\xff" + b"\x00" * 3 + b"JFIF",
        "image/png": b"\211PNG\r\n\032\n",
        "image/gif": b"GIF89a",
        # dummy type that is definitely not a valid image content type
        "image/watercolour": b"watercolour",
        "text/html": (
            b"<!DOCTYPE html>\n<html>\n<head>\n</head>\n"
            b"<body>\n</body>\n</html>"
        ),
    }

    def mock_response(
        self,
        url: str,
        content_type: str = "image/jpeg",
        file_type: None | str = None,
    ) -> None:
        # Potentially return a file of a type that differs from the
        # server-advertised content type to mimic misbehaving servers.
        if file_type is None:
            file_type = content_type

        try:
            # imghdr reads 32 bytes
            header = self.IMAGEHEADER[file_type].ljust(32, b"\x00")
        except KeyError:
            # If we can't return a file that looks like real file of the requested
            # type, better fail the test than returning something else, which might
            # violate assumption made when writing a test.
            raise AssertionError(f"Mocking {file_type} responses not supported")

        responses.add(
            responses.GET,
            url,
            content_type=content_type,
            body=header,
        )


class CleanupModulesMixin:
    modules: ClassVar[tuple[str, ...]]

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove files created by the plugin."""
        for module in cls.modules:
            clean_module_tempdir(module)
