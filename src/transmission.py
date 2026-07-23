import json
import logging
from pathlib import Path
from subprocess import CalledProcessError, check_call

from constants import DOWNLOAD_DIR, MAIN_USER, WATCH_DIR

logger = logging.getLogger(__name__)


class Transmission:
    def __init__(self):
        self._user = MAIN_USER
        self._download_dir = DOWNLOAD_DIR
        self._watch_dir = WATCH_DIR
        self._config_path = Path("/etc/transmission-daemon/settings.json")

    def install(self):
        self._install_deps()

    def _install_deps(self):
        try:
            check_call(["apt-get", "update", "-y"])
            check_call(
                [
                    "apt-get",
                    "install",
                    "-y",
                    "transmission-cli",
                    "transmission-daemon",
                ]
            )
        except CalledProcessError as e:
            logger.debug("Package install failed with return code %d", e.returncode)
            return

    def configure(self):
        self._write_configuration()

    def start(self):
        check_call(["systemctl", "enable", "--now", "transmission-daemon.service"])

    def stop(self):
        check_call(["systemctl", "disable", "--now", "transmission-daemon.service"])

    def _write_configuration(self):
        logger.info("configuring transmission")
        check_call(["systemctl", "stop", "transmission-daemon.service"])
        self._watch_dir.mkdir(parents=True, exist_ok=True)
        check_call(["chown", "-R", f"{self._user}:{self._user}", self._watch_dir])
        current_config = json.loads(self._config_path.read_text())
        current_config["watch-dir"] = str(self._watch_dir)
        current_config["watch-dir-enabled"] = True
        # XXX Let's have easy control for now, we'll see to restrict this later
        current_config["rpc-authentication-required"] = False
        current_config["default-trackers"] = "https://torrent.ubuntu.com"
        self._config_path.write_text(json.dumps(current_config))
        check_call(["systemctl", "start", "transmission-daemon.service"])
