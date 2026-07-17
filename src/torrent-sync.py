#!/usr/bin/env python3
import logging
from pathlib import Path
from subprocess import check_call, CalledProcessError

import torf

logger = logging.getLogger(__name__)

TORRENT_DIR = Path("data/torrents")
IMAGE_DIR = Path("data/images")
RSYNC_BASE = "rsync://cdimage.ubuntu.com"


def parse_torrent_name(path: Path) -> str | None:
    try:
        t = torf.Torrent.read(path)
        return t.name
    except Exception:
        logger.debug("Failed to parse %s", path, exc_info=True)
        return None


def rsync_torrents():
    TORRENT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        for subfolder in ["releases", "cdimage"]:
            source = f"{RSYNC_BASE}/{subfolder}"
            logger.info("Syncing torrents from %s", source)
            check_call(
                [
                    "rsync",
                    "--archive",
                    "--prune-empty-dirs",
                    "--include=*/",
                    # "--include=*.torrent",  # XXX restore me
                    "--include=*mini-iso*.torrent",  # XXX remove me
                    "--exclude=*",
                    source,
                    f"{TORRENT_DIR}/",
                ]
            )
    except CalledProcessError as e:
        logger.error("Torrent rsync failed with return code %d", e.returncode)


def rsync_images(torrent_paths: list[Path]):
    # TODO: investigate whether the following might do, at least for `/cdimage/`:
    # check_call(
    #     [
    #         "rsync",
    #         "--archive",
    #         "--prune-empty-dirs",
    #         "--include=*/releases/*",
    #         "--include=*.iso",
    #         "--exclude=*",
    #         source,
    #         f"{TORRENT_DIR}/",
    #     ]
    # )
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    iso_relpaths: set[Path] = set()
    for tp in torrent_paths:
        name = parse_torrent_name(tp)
        if name is None:
            continue
        rel = tp.relative_to(TORRENT_DIR)
        iso_rel = rel.parent / name
        iso_relpaths.add(iso_rel)

    for iso_rel in sorted(iso_relpaths):
        remote_path = RSYNC_BASE + str(iso_rel)
        dest = IMAGE_DIR / iso_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-av", remote_path, f"{dest}/"]
        try:
            check_call(cmd)
        except CalledProcessError as e:
            logger.error(
                "Failed to rsync %s (return code %d)", remote_path, e.returncode
            )


def torrent_files() -> list[Path]:
    return sorted(TORRENT_DIR.rglob("*.torrent"))


def run():
    rsync_torrents()
    return
    paths = torrent_files()
    paths = None
    if paths:
        rsync_images(paths)
    else:
        logger.info("No torrent files found, skipping image sync")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main():
    setup_logging()
    run()


if __name__ == "__main__":
    main()
