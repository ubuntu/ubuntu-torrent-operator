from pathlib import Path

MAIN_USER = "debian-transmission"
HOME = Path("/var/lib/transmission-daemon")

DOWNLOAD_DIR = HOME / "downloads"
TORRENTS_DIR = HOME / "torrents"
WATCH_DIR = HOME / ".config/transmission-daemon/watch_dir"
AQUATIC_HOME = HOME / "aquatic"
ACCESS_LIST = AQUATIC_HOME / "access-list.txt"

AQUATIC_PORT = 3000
TRANSMISSION_PEER_PORT = 51413

HAPROXY_ROUTE_RELATION = "haproxy-route"
HAPROXY_ROUTE_TCP_RELATION = "haproxy-route-tcp"
