"""Helpers shared by the charm integration tests."""

import base64
import logging
import socket

import jubilant

logger = logging.getLogger()

BITTORRENT_PROTOCOL = b"BitTorrent protocol"
# BitTorrent handshake: <pstrlen><pstr><reserved><info_hash><peer_id>
BITTORRENT_RESERVED = b"\x00\x00\x00\x00\x00\x10\x00\x00"
HANDSHAKE_LENGTH = 68


def recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Read exactly ``num_bytes`` from ``sock`` or raise."""
    buf = b""
    while len(buf) < num_bytes:
        chunk = sock.recv(num_bytes - len(buf))
        if not chunk:
            raise EOFError("connection closed while reading")
        buf += chunk
    return buf


def bittorrent_handshake(host, port, info_hash, timeout=15):
    """Perform a BitTorrent handshake against a peer.

    Returns the peer id of the remote peer. Raises if the endpoint does not
    speak BitTorrent or serves a different torrent (i.e. the info hash does not
    match).
    """
    peer_id = b"-UT0001-" + b"0123456789ab"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(
            bytes([len(BITTORRENT_PROTOCOL)])
            + BITTORRENT_PROTOCOL
            + BITTORRENT_RESERVED
            + info_hash
            + peer_id
        )
        response = recv_exact(sock, HANDSHAKE_LENGTH)

    if response[0] != len(BITTORRENT_PROTOCOL):
        raise ValueError("bad handshake length")
    if response[1:20] != BITTORRENT_PROTOCOL:
        raise ValueError("bad handshake protocol")
    if response[28:48] != info_hash:
        raise ValueError("info hash mismatch")
    return response[48:68]


def fetch_file(juju: jubilant.Juju, remote_path: str, unit: str) -> bytes:
    """Read a (possibly binary) file from a unit and return its bytes."""
    task = juju.exec("base64", "-w", "0", remote_path, unit=unit)
    return base64.b64decode("".join(task.stdout.split()))


def write_file(juju: jubilant.Juju, data: bytes, remote_path: str, unit: str) -> None:
    """Write binary data to a file on a unit.

    The data is base64-encoded to survive the shell and ``juju exec``.
    """
    encoded = base64.b64encode(data).decode()
    juju.exec("sh", "-c", f"printf %s {encoded} | base64 -d > {remote_path}", unit=unit)


def file_sha1(juju: jubilant.Juju, remote_path: str, unit: str) -> str:
    """Return the SHA1 checksum of a file on a unit."""
    task = juju.exec("sha1sum", remote_path, unit=unit)
    return task.stdout.split()[0]
