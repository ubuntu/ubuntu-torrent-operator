import logging
from pathlib import Path
from subprocess import CalledProcessError, check_call
from textwrap import dedent

from constants import ACCESS_LIST, DOWNLOAD_DIR, MAIN_USER, TORRENTS_DIR, WATCH_DIR

logger = logging.getLogger(__name__)


class Synchronizer:
    def __init__(self, shipped_script_location, shipped_venv):
        self._shipped_script = shipped_script_location
        self._shipped_venv = shipped_venv
        self._user = MAIN_USER
        self._service_path = Path("/etc/systemd/system/torrent-sync.service")
        self._timer_path = Path("/etc/systemd/system/torrent-sync.timer")
        self._torrents_dir = TORRENTS_DIR
        self._images_dir = DOWNLOAD_DIR
        self._watch_dir = WATCH_DIR
        self._access_list = ACCESS_LIST

    def install(self):
        self._install_deps()

    def _install_deps(self):
        try:
            check_call(["apt-get", "update", "-y"])
            check_call(["apt-get", "install", "-y", "rsync"])
        except CalledProcessError as e:
            logger.debug("Package install failed with return code %d", e.returncode)

    def configure(self):
        self._write_systemd_service()
        self._write_systemd_timer()

    def start(self):
        check_call(["systemctl", "enable", "--now", "torrent-sync.timer"])

    def stop(self):
        check_call(["systemctl", "disable", "--now", "torrent-sync.timer"])

    def _write_systemd_service(self):
        logger.info("writing torrent-sync systemd service file")
        content = dedent(f"""\
            [Unit]
            Description=Sync torrent files from cdimage.ubuntu.com
            After=network-online.target
            Wants=network-online.target

            [Service]
            Type=oneshot
            User={self._user}
            Group={self._user}
            ExecStart={self._shipped_venv}/bin/python {self._shipped_script} \
                --torrents-dir {self._torrents_dir} \
                --images-dir {self._images_dir} \
                --watch-dir {self._watch_dir} \
                --access-list {self._access_list}
        """)

        self._service_path.write_text(content)
        check_call(["systemctl", "daemon-reload"])

    def _write_systemd_timer(self):
        logger.info("writing torrent-sync systemd timer file")
        content = dedent("""
            [Unit]
            Description=Run torrent-sync every 5 minutes

            [Timer]
            OnCalendar=*:0/5
            Persistent=true

            [Install]
            WantedBy=timers.target
            """)

        self._timer_path.write_text(content)
        check_call(["systemctl", "daemon-reload"])
        check_call(["systemctl", "enable", "--now", self._timer_path.name])
