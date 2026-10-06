import logging
import os
from subprocess import check_call

import torf

from constants import ACCESS_LIST, DOWNLOAD_DIR, MAIN_USER, WATCH_DIR

logger = logging.getLogger(__name__)


class TorrentTest:
    def __init__(self):
        self._user = MAIN_USER
        self._download_dir = DOWNLOAD_DIR
        self._watch_dir = WATCH_DIR
        self._access_list = ACCESS_LIST

    def install(self):
        pass

    def configure(self):
        self._create_test_torrent()

    def _create_test_torrent(self):
        logger.info("creating test torrent")
        test_name = "ubuntu-torrent-operator-test.bin"
        test_file = self._download_dir / test_name
        torrent_file = self._watch_dir / f"{test_name}.torrent"

        self._download_dir.mkdir(parents=True, exist_ok=True)
        if not test_file.exists():
            header = b"Ubuntu Torrent Operator test file\n"
            with test_file.open("wb") as f:
                f.write(header)
                f.write(os.urandom(10 * 1024 * 1024 - len(header)))
            check_call(["chown", f"{self._user}:{self._user}", test_file])

        # Build the torrent in memory so its hash can be added to the access
        # list before Transmission consumes it from the watch-dir.
        torrent = torf.Torrent(path=test_file)
        torrent.generate()
        self._add_to_access_list(torrent.infohash)

        self._watch_dir.mkdir(parents=True, exist_ok=True)
        torrent.write(torrent_file, overwrite=True)
        check_call(["chown", f"{self._user}:{self._user}", torrent_file])

    def _add_to_access_list(self, infohash):
        current_list = set()
        if self._access_list.exists():
            current_list = set(self._access_list.read_text().splitlines())
        current_list.add(infohash)
        self._access_list.parent.mkdir(parents=True, exist_ok=True)
        self._access_list.write_text("\n".join(current_list))
        check_call(["chown", f"{self._user}:{self._user}", self._access_list])
