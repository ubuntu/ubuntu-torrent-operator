#!/usr/bin/env python3

import argparse
import logging
from pathlib import Path
from subprocess import CalledProcessError, check_call

import torf

logger = logging.getLogger(__name__)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--torrents-dir",
        default="torrents",
        type=Path,
        help="Directory to store synced torrent files (default: %(default)s)",
    )
    parser.add_argument(
        "--images-dir",
        default="images",
        type=Path,
        help="Directory to store synced image files (default: %(default)s)",
    )
    parser.add_argument(
        "--watch-dir",
        default="watch",
        type=Path,
        help="Directory to copy torrent files once their images have synced (default: %(default)s)",
    )
    parser.add_argument(
        "--access-list",
        default="access-list.txt",
        type=Path,
        help="File to store the list of available torrent hashes (default: %(default)s)",
    )
    return parser.parse_args()


def parse_torrent(path: Path) -> torf.Torrent:
    try:
        t = torf.Torrent.read(path)
        return t
    except Exception:
        logger.debug("Failed to parse %s", path, exc_info=True)
        return None


def rsync_torrents(source: str, torrent_dir: Path):
    try:
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
                f"{torrent_dir}/",
            ]
        )
    except CalledProcessError as e:
        logger.error("Torrent rsync failed with return code %d", e.returncode)


def sync_single_torrent_image(
    source: str, torrents_dir: Path, images_dir: Path, torrent: Path
):

    image_name = parse_torrent(torrent).name
    image_uri = torrent.relative_to(torrents_dir).parent / image_name

    destination_path = images_dir / image_uri.name
    remote_path = f"{source}/{image_uri}"

    logger.info("Syncing image %s", remote_path)
    cmd = ["rsync", "-av", remote_path, f"{destination_path}"]
    try:
        check_call(cmd)
    except CalledProcessError as e:
        logger.error("Failed to rsync %s (return code %d)", remote_path, e.returncode)


def add_torrent_to_watch_dir(watch_dir: Path, torrent: Path):
    torrent.copy_into(watch_dir)


def add_torrent_to_access_list(access_list: Path, torrent_path: Path):
    hash = parse_torrent(torrent_path).infohash
    current_list = set()
    if access_list.exists():
        current_list = set(access_list.read_text().splitlines())
    current_list.add(hash)
    access_list.write_text("\n".join(current_list))


def main():
    args = parse_args()
    setup_logging()

    rsync_base = "rsync://cdimage.ubuntu.com"
    args.torrents_dir.mkdir(parents=True, exist_ok=True)
    args.images_dir.mkdir(parents=True, exist_ok=True)
    args.watch_dir.mkdir(parents=True, exist_ok=True)

    for subfolder in ["releases", "cdimage"]:
        source = f"{rsync_base}/{subfolder}"
        dest_torrent_dir = args.torrents_dir / subfolder
        rsync_torrents(source, dest_torrent_dir)
        for torrent in dest_torrent_dir.rglob("*.torrent"):
            try:
                sync_single_torrent_image(
                    source, dest_torrent_dir, args.images_dir, torrent
                )
                add_torrent_to_watch_dir(args.watch_dir, torrent)
                add_torrent_to_access_list(args.access_list, torrent)
            except Exception as e:
                logger.error("Failed to sync image for %s (%s)", torrent, e)


if __name__ == "__main__":
    main()
