"""Integration tests for the ubuntu-torrent-operator charm.

They verify, in order, that:

1. the charm deploys and all of its components come online;
2. the HAProxy relations work (the ``tracker`` HTTP route and the ``bittorrent``
   TCP route);
3. a local aria2 client (installed from apt) downloads the special test torrent
   that the charm generates, end to end through HAProxy;
4. a torrent served by a bare ``ubuntu`` unit acting as a fake
   ``cdimage.ubuntu.com`` is synced by the charm and can likewise be downloaded
   by a regular torrent client.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import jubilant
import pytest
import torf
from requests import Session
from tenacity import Retrying, stop_after_attempt, wait_exponential
from utils import (
    bittorrent_handshake,
    fetch_file,
    file_sha1,
    write_file,
)

logger = logging.getLogger()

TORRENT_APP = "torrent"
HAPROXY = "haproxy"
SSC = "self-signed-certificates"
CDIMAGE = "cdimage"
HAPROXY_CHANNEL = "2.8/edge"
SSC_CHANNEL = "1/edge"

TORRENT_UNIT = f"{TORRENT_APP}/0"
HAPROXY_UNIT = f"{HAPROXY}/0"
CDIMAGE_UNIT = f"{CDIMAGE}/0"

# Hostname the charm serves. The HAProxy route is configured for it, and it is
# resolved to the HAProxy address through /etc/hosts on both the unit and the
# test runner.
TRACKER_HOSTNAME = "torrent.internal"
TRACKER_URL = f"http://{TRACKER_HOSTNAME}/announce"

PEER_PORT = 51413
TEST_TORRENT_NAME = "ubuntu-torrent-operator-test.bin"
TEST_TORRENT_SIZE = 10 * 1024 * 1024

AQUATIC_DIR = "/var/lib/transmission-daemon/aquatic"
TRANSMISSION_DIR = "/var/lib/transmission-daemon"
UNIT_DOWNLOADS_DIR = f"{TRANSMISSION_DIR}/downloads"
UNIT_TORRENTS_DIR = f"{TRANSMISSION_DIR}/torrents"
UNIT_WATCH_DIR = f"{TRANSMISSION_DIR}/.config/transmission-daemon/watch_dir"
UNIT_TEST_TORRENT_PATH = f"{UNIT_WATCH_DIR}/{TEST_TORRENT_NAME}.torrent.added"
UNIT_TEST_TORRENT_FILE_PATH = f"{UNIT_DOWNLOADS_DIR}/{TEST_TORRENT_NAME}"
UNIT_AQUATIC_ACCESS_LIST = f"{AQUATIC_DIR}/access-list.txt"
UNIT_AQUATIC_CONFIG = f"{AQUATIC_DIR}/aquatic.toml"
UNIT_TRANSMISSION_CONFIG = "/etc/transmission-daemon/settings.json"

# A bare `ubuntu` unit plays the role of cdimage.ubuntu.com: it serves a small
# image and its torrent over an rsync daemon, which the charm's synchronizer
# syncs. The torrent name must match the synchronizer's rsync include filters
# (see `rsync_torrents` in src/torrent-sync.py), hence the `xubuntu` prefix.
MOCK_CDIMAGE_TORRENT_NAME = "xubuntu-fake-test.iso"
MOCK_CDIMAGE_TORRENT_SIZE = 64 * 1024
MOCK_CDIMAGE_DIR = "/srv/fake-cdimage"
MOCK_CDIMAGE_MODULE = f"{MOCK_CDIMAGE_DIR}/cdimage"
MOCK_CDIMAGE_RSYNC_PORT = 8730

UNIT_FAKE_TORRENT_PATH = (
    f"{UNIT_TORRENTS_DIR}/cdimage/{MOCK_CDIMAGE_TORRENT_NAME}.torrent"
)
UNIT_FAKE_IMAGE = f"{UNIT_DOWNLOADS_DIR}/{MOCK_CDIMAGE_TORRENT_NAME}"
UNIT_FAKE_WATCH = f"{UNIT_WATCH_DIR}/{MOCK_CDIMAGE_TORRENT_NAME}.torrent"


@dataclass
class Deployment:
    juju: jubilant.Juju
    haproxy_ip: str
    operator_ip: str


@dataclass
class TorrentFixture:
    path: Path
    torrent: torf.Torrent

    @property
    def infohash(self) -> str:
        return self.torrent.infohash


@dataclass
class FakeCDImage:
    """The torrent and image served by the fake cdimage.ubuntu.com unit."""

    ip: str
    torrent: TorrentFixture
    image_sha1: str

    @property
    def rsync_base(self) -> str:
        return f"rsync://{self.ip}:{MOCK_CDIMAGE_RSYNC_PORT}"

    @property
    def infohash(self) -> str:
        return self.torrent.infohash


def _unit_add_host(juju: jubilant.Juju, ip: str, hostname: str) -> None:
    """Point ``hostname`` at ``ip`` inside the charm unit."""
    juju.exec(
        "sh",
        "-c",
        f"grep -q ' {hostname}$' /etc/hosts || echo '{ip} {hostname}' >> /etc/hosts",
        unit=TORRENT_UNIT,
    )


def _runner_add_host(ip: str, hostname: str) -> None:
    """Point ``hostname`` at ``ip`` on the test runner."""
    line = f"{ip} {hostname}\n"
    tee_cmd = ["tee", "-a", "/etc/hosts"]
    if line not in Path("/etc/hosts").read_text():
        if os.geteuid() != 0:
            tee_cmd = ["sudo", *tee_cmd]
        subprocess.run(
            tee_cmd,
            input=line,
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
        )


def _runner_remove_host(hostname: str) -> None:
    """Remove the test runner entry for ``hostname``."""
    command = ["sed", "-i", rf"/[[:space:]]{hostname}$/d", "/etc/hosts"]
    if os.geteuid() != 0:
        command = ["sudo", *command]
    subprocess.run(command, check=False)


def _public_address(status: jubilant.Status, app: str, unit: str) -> str:
    """Return the public address of a unit, or an empty string if not known yet."""
    app_status = status.apps.get(app)
    unit_status = app_status.units.get(unit) if app_status else None
    return unit_status.public_address if unit_status else ""


def _setup_fake_cdimage(juju: jubilant.Juju) -> None:
    """Turn the bare unit into an rsync server mimicking cdimage.ubuntu.com."""
    releases_module = f"{MOCK_CDIMAGE_DIR}/releases"
    juju.exec("apt-get", "update", "-y", unit=CDIMAGE_UNIT, wait=600)
    juju.exec("apt-get", "install", "-y", "rsync", unit=CDIMAGE_UNIT, wait=600)
    juju.exec("mkdir", "-p", MOCK_CDIMAGE_MODULE, releases_module, unit=CDIMAGE_UNIT)
    # Generate the image on the unit, so only the (small) torrent has to be
    # copied over afterwards.
    juju.exec(
        "dd",
        "if=/dev/urandom",
        f"of={MOCK_CDIMAGE_MODULE}/{MOCK_CDIMAGE_TORRENT_NAME}",
        "bs=1024",
        f"count={MOCK_CDIMAGE_TORRENT_SIZE // 1024}",
        unit=CDIMAGE_UNIT,
    )

    rsyncd_config = dedent(f"""\
        uid = nobody
        gid = nogroup
        use chroot = yes

        [cdimage]
            path = {MOCK_CDIMAGE_MODULE}
            read only = yes

        [releases]
            path = {releases_module}
            read only = yes
    """)
    juju.exec(
        "sh",
        "-c",
        f"cat > /etc/rsyncd.conf <<'EOF'\n{rsyncd_config}EOF",
        unit=CDIMAGE_UNIT,
    )
    # Run the daemon on a non-standard port to avoid clashing with any stock
    # rsync service, and as a transient unit so it is cleaned up with the model.
    juju.exec(
        "systemd-run",
        "--unit=fake-cdimage-rsync",
        "--collect",
        "rsync",
        "--daemon",
        "--no-detach",
        f"--port={MOCK_CDIMAGE_RSYNC_PORT}",
        "--config=/etc/rsyncd.conf",
        unit=CDIMAGE_UNIT,
    )
    for attempt in Retrying(
        stop=stop_after_attempt(30), wait=wait_exponential(min=1, max=5), reraise=True
    ):
        with attempt:
            juju.exec(
                "rsync",
                f"rsync://127.0.0.1:{MOCK_CDIMAGE_RSYNC_PORT}/",
                unit=CDIMAGE_UNIT,
            )


@pytest.fixture(scope="module")
def scratch_dir() -> Generator[Path, None, None]:
    """Scratch space for the torrents, downloads and logs the tests generate."""
    scratch = Path(tempfile.mkdtemp(prefix="ubuntu-torrent-operator-", dir=Path.home()))
    yield scratch
    shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture(scope="module")
def fake_cdimage(juju: jubilant.Juju, scratch_dir: Path) -> FakeCDImage:
    """Deploy a bare `ubuntu` unit that acts as a fake cdimage.ubuntu.com.

    It serves, over an rsync daemon, a small image together with the matching
    torrent, so the charm's synchronizer can sync both.
    """
    juju.deploy("ubuntu", app=CDIMAGE)
    juju.wait(lambda status: jubilant.all_active(status, CDIMAGE), timeout=1800)

    ip = _public_address(juju.status(), CDIMAGE, CDIMAGE_UNIT)
    _setup_fake_cdimage(juju)

    local_dir = scratch_dir / "fake-cdimage"
    local_dir.mkdir()
    image_path = local_dir / MOCK_CDIMAGE_TORRENT_NAME
    image_path.write_bytes(
        fetch_file(
            juju, f"{MOCK_CDIMAGE_MODULE}/{MOCK_CDIMAGE_TORRENT_NAME}", CDIMAGE_UNIT
        )
    )

    torrent = torf.Torrent(path=image_path, trackers=[TRACKER_URL])
    torrent.generate()
    torrent_path = local_dir / f"{MOCK_CDIMAGE_TORRENT_NAME}.torrent"
    torrent.write(str(torrent_path), overwrite=True)
    write_file(
        juju,
        torrent_path.read_bytes(),
        f"{MOCK_CDIMAGE_MODULE}/{MOCK_CDIMAGE_TORRENT_NAME}.torrent",
        CDIMAGE_UNIT,
    )

    return FakeCDImage(
        ip=ip,
        torrent=TorrentFixture(path=torrent_path, torrent=torrent),
        image_sha1=hashlib.sha1(image_path.read_bytes()).hexdigest(),
    )


@pytest.fixture(scope="module")
def deployment(
    juju: jubilant.Juju, charm_path: str, fake_cdimage: FakeCDImage
) -> Deployment:
    """Deploy the charm with HAProxy and self-signed-certificates, and relate them."""
    juju.deploy(
        HAPROXY,
        channel=HAPROXY_CHANNEL,
        config={"external-hostname": "haproxy.internal"},
    )
    juju.deploy(SSC, channel=SSC_CHANNEL)

    # Wait for HAProxy to have an address before deploying the charm, so the
    # seeder can advertise it from its very first announce. Otherwise it would
    # first register with its own address, leaving a stale, directly reachable
    # peer in the tracker.
    juju.wait(
        lambda status: bool(_public_address(status, HAPROXY, HAPROXY_UNIT)), timeout=900
    )
    haproxy_ip = _public_address(juju.status(), HAPROXY, HAPROXY_UNIT)
    _runner_add_host(haproxy_ip, TRACKER_HOSTNAME)

    juju.deploy(
        charm=charm_path,
        app=TORRENT_APP,
        config={
            "hostname": TRACKER_HOSTNAME,
            "tracker_url": TRACKER_URL,
            "announce_ip": haproxy_ip,
            "allow_http": True,
            "rsync_base": fake_cdimage.rsync_base,
        },
    )

    # The charm should come online on its own before anything is related.
    juju.wait(lambda status: jubilant.all_active(status, TORRENT_APP), timeout=1800)

    operator_ip = _public_address(juju.status(), TORRENT_APP, TORRENT_UNIT)

    # The charm must resolve the served hostname to HAProxy for the tracker to
    # be exercised over the `tracker` relation.
    _unit_add_host(juju, haproxy_ip, TRACKER_HOSTNAME)

    juju.integrate(f"{HAPROXY}:certificates", f"{SSC}:certificates")
    juju.integrate(f"{TORRENT_APP}:tracker", HAPROXY)
    juju.integrate(f"{TORRENT_APP}:bittorrent", HAPROXY)

    juju.wait(
        lambda status: jubilant.all_active(status, TORRENT_APP, HAPROXY, SSC),
        timeout=1800,
    )

    yield Deployment(juju=juju, haproxy_ip=haproxy_ip, operator_ip=operator_ip)

    _runner_remove_host(TRACKER_HOSTNAME)


@pytest.fixture(scope="module")
def test_torrent(deployment: Deployment, scratch_dir: Path) -> TorrentFixture:
    """Fetch the test torrent the charm generated and left in the watch directory."""
    torrent_dir = scratch_dir / "torrent"
    torrent_dir.mkdir()
    path = torrent_dir / f"{TEST_TORRENT_NAME}.torrent"
    path.write_bytes(fetch_file(deployment.juju, UNIT_TEST_TORRENT_PATH, TORRENT_UNIT))
    return TorrentFixture(path=path, torrent=torf.Torrent.read(str(path)))


def test_deploy(deployment: Deployment, test_torrent: TorrentFixture):
    """The charm deploys, comes online, and prepares the test torrent."""
    juju = deployment.juju

    assert jubilant.all_active(juju.status(), TORRENT_APP)

    # All the workloads the charm manages are running.
    for service in (
        "aquatic.service",
        "transmission-daemon.service",
        "torrent-sync.timer",
    ):
        task = juju.exec("systemctl", "is-active", service, unit=TORRENT_UNIT)
        assert task.stdout.strip() == "active", f"{service} is not active"

    # Aquatic runs behind the reverse proxy and enforces its access list.
    aquatic_config = juju.exec("cat", UNIT_AQUATIC_CONFIG, unit=TORRENT_UNIT).stdout
    assert "runs_behind_reverse_proxy = true" in aquatic_config
    assert 'mode = "allow"' in aquatic_config

    # Transmission announces to the configured tracker and advertises HAProxy.
    transmission_config = json.loads(
        juju.exec("cat", UNIT_TRANSMISSION_CONFIG, unit=TORRENT_UNIT).stdout
    )
    assert transmission_config["default-trackers"] == TRACKER_URL
    assert transmission_config["announce-ip"] == deployment.haproxy_ip
    assert transmission_config["announce-ip-enabled"] is True

    # The test torrent is generated, allowed by the tracker, and handed to Transmission.
    juju.exec("ls", UNIT_TEST_TORRENT_PATH, unit=TORRENT_UNIT)
    assert (
        test_torrent.infohash
        in juju.exec("cat", UNIT_AQUATIC_ACCESS_LIST, unit=TORRENT_UNIT).stdout
    )

    for attempt in Retrying(
        stop=stop_after_attempt(20),
        wait=wait_exponential(min=3, max=15),
        reraise=True,
    ):
        with attempt:
            listing = juju.exec(
                "transmission-remote", "127.0.0.1:9091", "--list", unit=TORRENT_UNIT
            ).stdout
            assert TEST_TORRENT_NAME in listing


def test_tracker_relation(deployment: Deployment, test_torrent: TorrentFixture):
    """The `tracker` (haproxy-route) relation serves the aquatic tracker over HTTP."""
    session = Session()
    url = f"http://{TRACKER_HOSTNAME}/"

    # The seeder announces to the tracker through HAProxy, so its torrent shows
    # up on the aquatic index page (which is only served once peers are known).
    for attempt in Retrying(
        stop=stop_after_attempt(30),
        wait=wait_exponential(min=5, max=30),
        reraise=True,
    ):
        with attempt:
            response = session.get(url, timeout=30)
            assert response.status_code == 200
            assert "BitTorrent tracker index" in response.text
            assert test_torrent.infohash in response.text


def test_bittorrent_relation(deployment: Deployment, test_torrent: TorrentFixture):
    """The `bittorrent` (haproxy-route-tcp) relation forwards peers to the seeder."""
    info_hash = bytes.fromhex(test_torrent.infohash)

    for attempt in Retrying(
        stop=stop_after_attempt(30),
        wait=wait_exponential(min=3, max=15),
        reraise=True,
    ):
        with attempt:
            bittorrent_handshake(deployment.haproxy_ip, PEER_PORT, info_hash)


def test_download(
    deployment: Deployment, test_torrent: TorrentFixture, scratch_dir: Path
):
    """A local aria2 client downloads the test torrent through HAProxy."""
    _ensure_aria2()

    workdir = scratch_dir / "download-test"
    workdir.mkdir()
    download_dir = workdir / "downloads"
    download_dir.mkdir()
    expected_sha1 = file_sha1(
        deployment.juju, UNIT_TEST_TORRENT_FILE_PATH, TORRENT_UNIT
    )

    downloaded = _aria2_download(
        test_torrent.path,
        download_dir,
        TEST_TORRENT_NAME,
        expected_sha1,
        workdir / "aria2.log",
    )
    assert downloaded.stat().st_size == TEST_TORRENT_SIZE


def test_synced_torrent(
    deployment: Deployment, fake_cdimage: FakeCDImage, scratch_dir: Path
):
    """A torrent synced from the fake cdimage.ubuntu.com is seeded and downloadable."""
    juju = deployment.juju

    # Drive the synchronizer until it has rsynced the torrent and its image from
    # the fake cdimage.ubuntu.com, and allowed the torrent with the tracker.
    for attempt in Retrying(
        stop=stop_after_attempt(30),
        wait=wait_exponential(min=3, max=15),
        reraise=True,
    ):
        with attempt:
            juju.exec("systemctl", "start", "torrent-sync.service", unit=TORRENT_UNIT)
            juju.exec("ls", UNIT_FAKE_TORRENT_PATH, unit=TORRENT_UNIT)
            juju.exec("ls", UNIT_FAKE_IMAGE, unit=TORRENT_UNIT)
            juju.exec("ls", UNIT_FAKE_WATCH, unit=TORRENT_UNIT)
            assert (
                fake_cdimage.infohash
                in juju.exec("cat", UNIT_AQUATIC_ACCESS_LIST, unit=TORRENT_UNIT).stdout
            )

    # Transmission picks up the newly watched torrent and starts seeding it.
    for attempt in Retrying(
        stop=stop_after_attempt(20),
        wait=wait_exponential(min=3, max=15),
        reraise=True,
    ):
        with attempt:
            listing = juju.exec(
                "transmission-remote", "127.0.0.1:9091", "--list", unit=TORRENT_UNIT
            ).stdout
            assert MOCK_CDIMAGE_TORRENT_NAME in listing

    # A regular torrent client can download the synced file through HAProxy.
    _ensure_aria2()

    workdir = scratch_dir / "download-synced"
    workdir.mkdir()
    download_dir = workdir / "downloads"
    download_dir.mkdir()
    downloaded = _aria2_download(
        fake_cdimage.torrent.path,
        download_dir,
        MOCK_CDIMAGE_TORRENT_NAME,
        fake_cdimage.image_sha1,
        workdir / "aria2.log",
    )
    assert downloaded.stat().st_size == MOCK_CDIMAGE_TORRENT_SIZE


def _ensure_aria2() -> None:
    """Install aria2 from apt, as the end-to-end client."""
    if not shutil.which("aria2c"):
        apt = ["apt-get"] if os.geteuid() == 0 else ["sudo", "apt-get"]
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        subprocess.run([*apt, "update", "-y"], check=True, timeout=300, env=env)
        subprocess.run(
            [*apt, "install", "-y", "aria2"],
            check=True,
            timeout=600,
            env=env,
        )


def _aria2_download(
    torrent_path: Path,
    download_dir: Path,
    name: str,
    expected_sha1: str,
    log_path: Path,
) -> Path:
    """Download a torrent with aria2c and wait for it to complete.

    ``--seed-time=0`` makes aria2c stop as soon as the download completes
    instead of seeding the file back. Unlike the transmission-cli client this
    replaces, aria2 runs under no AppArmor profile, so no override is needed for
    it to read the torrent or write into ``download_dir``.
    """
    with log_path.open("w") as log_file:
        client = subprocess.Popen(
            [
                "aria2c",
                "--seed-time=0",
                f"--dir={download_dir}",
                str(torrent_path),
            ],
            cwd=str(download_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            return _wait_for_download(download_dir, name, expected_sha1, client)
        finally:
            client.terminate()
            try:
                client.wait(timeout=30)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=30)
            logger.info("aria2c output:\n%s", log_path.read_text())


def _wait_for_download(
    download_dir: Path, name: str, expected_sha1: str, client: subprocess.Popen
) -> Path:
    """Wait until the downloaded file matches the expected checksum, and return it."""
    target = download_dir / name
    deadline = time.monotonic() + 600

    while time.monotonic() < deadline:
        if target.is_file():
            try:
                if hashlib.sha1(target.read_bytes()).hexdigest() == expected_sha1:
                    return target
            except OSError:
                pass
        # aria2c stops as soon as the download completes (``--seed-time=0``), so
        # check the file above before treating a finished process as a failure.
        if client.poll() is not None:
            raise AssertionError(
                f"aria2c exited before the download was verified "
                f"(code {client.returncode})"
            )
        time.sleep(2)

    raise AssertionError(
        f"{name} was not fully downloaded into {download_dir} "
        f"(expected sha1 {expected_sha1})"
    )
