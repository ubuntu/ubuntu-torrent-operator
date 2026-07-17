from pathlib import Path

DOWNLOAD_DIR = Path("/var/lib/transmission-daemon/downloads")
TORRENTS_DIR = Path("/var/lib/transmission-daemon/torrents")
WATCH_DIR = Path("/var/lib/transmission-daemon/.config/transmission-daemon/watch_dir")
AQUATIC_HOME = Path("/var/lib/transmission-daemon/aquatic")
ACCESS_LIST = AQUATIC_HOME / "access-list.txt"

MAIN_USER = "debian-transmission"
HOME = Path(f"~{MAIN_USER}").expanduser()
