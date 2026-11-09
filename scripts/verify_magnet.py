#!/usr/bin/env python3
"""verify_magnet.py -- scrape magnet links from a webpage and print each
torrent's metadata (fetched live from the swarm).

Usage:
    python3 verify_magnet.py URL [URL ...]

URL may be an http(s) page, a local file, or a magnet: URI directly.

For every distinct magnet found we:
  1. parse it for the info hash, display name and trackers,
  2. announce to those trackers to discover peers,
  3. connect to a peer (a torrent client serving the swarm) and pull the
     metadata via the BEP 9 "ut_metadata" extension,
  4. print name, total size, piece length and file list next to the magnet.
"""

import base64
import hashlib
import html
import os
import re
import socket
import struct
import sys
import urllib.parse
import urllib.request

BLOCK = 16384
PEER_ID = b"-VM0001-" + os.urandom(12)
USER_AGENT = "verify_magnet.py/1.0"

MAGNET_RE = re.compile(r"magnet:\?[^\"'<>\s\\]+")

DEFAULT_TIMEOUT = 15
MAX_PEERS = 20


# --------------------------------------------------------------------------
# bencode
# --------------------------------------------------------------------------
def bencode(o):
    if isinstance(o, int):
        return b"i%de" % o
    if isinstance(o, bytes):
        return b"%d:%s" % (len(o), o)
    if isinstance(o, str):
        return bencode(o.encode())
    if isinstance(o, (list, tuple)):
        return b"l" + b"".join(bencode(x) for x in o) + b"e"
    if isinstance(o, dict):
        items = sorted(o.items(), key=lambda kv: kv[0])
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(o))


def bdecode(data, i=0):
    c = data[i : i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1 : j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i : i + 1] != b"e":
            v, i = bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i : i + 1] != b"e":
            k, i = bdecode(data, i)
            v, i = bdecode(data, i)
            out[k] = v
        return out, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1 : j + 1 + n], j + 1 + n


# --------------------------------------------------------------------------
# BitTorrent wire helpers
# --------------------------------------------------------------------------
def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def read_msg(sock):
    n = struct.unpack(">I", recv_exact(sock, 4))[0]
    if n == 0:
        return None, b""
    body = recv_exact(sock, n)
    return body[0], body[1:]


def send_msg(sock, mid, payload=b""):
    body = bytes([mid]) + payload
    sock.sendall(struct.pack(">I", len(body)) + body)


def handshake(sock, info_hash):
    reserved = b"\x00\x00\x00\x00\x00\x10\x00\x00"  # extension protocol bit
    sock.sendall(bytes([19]) + b"BitTorrent protocol" + reserved + info_hash + PEER_ID)
    resp = recv_exact(sock, 68)
    if resp[0] != 19 or resp[1:20] != b"BitTorrent protocol":
        raise RuntimeError("bad handshake pstr")
    if resp[28:48] != info_hash:
        raise RuntimeError("info hash mismatch")
    return resp[48:68]


def fetch_metadata(sock, info_hash):
    """BEP 9 / ut_metadata exchange, verified against the requested hash."""
    our_ut = 1
    send_msg(sock, 20, bytes([0]) + bencode({b"m": {b"ut_metadata": our_ut}}))
    ut = None
    size = None
    while ut is None:
        mid, p = read_msg(sock)
        if mid == 20 and p[:1] == b"\x00":
            d, _ = bdecode(p[1:])
            ut = d.get(b"m", {}).get(b"ut_metadata")
            size = d.get(b"metadata_size")
    if not ut or not size:
        raise RuntimeError("peer does not support metadata exchange")
    npieces = (size + BLOCK - 1) // BLOCK
    buf = [None] * npieces
    for i in range(npieces):
        send_msg(sock, 20, bytes([ut]) + bencode({b"msg_type": 0, b"piece": i}))
    got = 0
    while got < npieces:
        mid, p = read_msg(sock)
        if mid == 20 and p[:1] == bytes([our_ut]):
            d, idx = bdecode(p[1:])
            if d.get(b"msg_type") == 1:
                buf[d[b"piece"]] = p[1 + idx :]
                got += 1
            elif d.get(b"msg_type") == 2:
                raise RuntimeError(f"metadata piece {d.get(b'piece')} rejected")
    meta = b"".join(buf)[:size]
    if hashlib.sha1(meta).digest() != info_hash:
        raise RuntimeError("metadata hash mismatch")
    info, _ = bdecode(meta)
    return info


# --------------------------------------------------------------------------
# connection (direct, or tunnelled through an HTTP proxy via CONNECT)
# --------------------------------------------------------------------------
def _provider_proxy():
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("http")


def _proxy_connect(proxy_url, host, port, timeout):
    u = urllib.parse.urlsplit(
        proxy_url if "://" in proxy_url else "http://" + proxy_url
    )
    s = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(
        (f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n").encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(512)
        if not chunk:
            break
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b" 200 " not in status:
        s.close()
        raise OSError(f"proxy refused: {status.decode('latin1')}")
    return s


def open_peer(host, port, timeout):
    """Open a TCP connection to a peer, falling back to an HTTP proxy tunnel."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        return s, "direct"
    except OSError as direct_err:
        proxy = _provider_proxy()
        if not proxy:
            raise direct_err
        s = _proxy_connect(proxy, host, port, timeout)
        return s, "proxy"


# --------------------------------------------------------------------------
# magnet parsing, tracker announce
# --------------------------------------------------------------------------
def parse_magnet(uri):
    q = urllib.parse.urlsplit(uri).query
    params = urllib.parse.parse_qs(q, keep_blank_values=True)
    info_hash = None
    for xt in params.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            h = xt[len("urn:btih:") :]
            if len(h) == 40:
                info_hash = bytes.fromhex(h)
            elif len(h) == 32:
                info_hash = base64.b32decode(h.upper())
            break
    return {
        "uri": uri,
        "info_hash": info_hash,
        "name": (params.get("dn") or [None])[0],
        "trackers": params.get("tr", []),
        "peers": params.get("x.pe", []),
        "web_seeds": params.get("ws", []),
    }


def decode_peers(d):
    out = []
    peers = d.get(b"peers")
    if isinstance(peers, bytes):
        for i in range(0, len(peers) - 5, 6):
            ip = ".".join(str(x) for x in peers[i : i + 4])
            out.append((ip, struct.unpack(">H", peers[i + 4 : i + 6])[0]))
    elif isinstance(peers, list):
        for pr in peers:
            try:
                out.append((pr[b"ip"].decode(), int(pr[b"port"])))
            except (KeyError, UnicodeDecodeError):
                pass
    peers6 = d.get(b"peers6")
    if isinstance(peers6, bytes):
        for i in range(0, len(peers6) - 17, 18):
            ip = socket.inet_ntop(socket.AF_INET6, peers6[i : i + 16])
            out.append((ip, struct.unpack(">H", peers6[i + 16 : i + 18])[0]))
    return out


def announce(tracker, info_hash, timeout):
    scheme = urllib.parse.urlsplit(tracker).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError("unsupported tracker scheme %r" % (scheme or "?"))
    qs = (
        "info_hash="
        + urllib.parse.quote_from_bytes(info_hash)
        + "&peer_id="
        + urllib.parse.quote_from_bytes(PEER_ID)
        + "&port=6881&uploaded=0&downloaded=0&left=0"
        + f"&numwant={MAX_PEERS}&key=F033E7D2&compact=1&supportcrypto=1"
    )
    sep = "&" if "?" in tracker else "?"
    req = urllib.request.Request(tracker + sep + qs, headers={"User-Agent": USER_AGENT})
    data = urllib.request.urlopen(req, timeout=timeout).read()
    d, _ = bdecode(data)
    if b"failure reason" in d:
        raise RuntimeError(d[b"failure reason"].decode("latin1", "replace"))
    known = {
        b"peers",
        b"peers6",
        b"interval",
        b"complete",
        b"incomplete",
        b"tracker id",
        b"min interval",
        b"warning message",
    }
    d = {k: v for k, v in d.items() if k in known}
    return decode_peers(d)


# --------------------------------------------------------------------------
# page scraping / reporting
# --------------------------------------------------------------------------
def read_source(source, timeout):
    if source.lower().startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    if source.lower().startswith("magnet:"):
        return source.encode()
    with open(source, "rb") as fh:
        return fh.read()


def find_magnets(text):
    seen = {}
    for raw in MAGNET_RE.findall(text):
        uri = html.unescape(raw)
        info = parse_magnet(uri)
        key = info["info_hash"].hex() if info["info_hash"] else uri
        seen.setdefault(key, info)
    return list(seen.values())


def human_size(n):
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{n} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


def info_files(info):
    if b"files" in info:
        return [
            ("/".join(p.decode("utf-8", "replace") for p in f[b"path"]), f[b"length"])
            for f in info[b"files"]
        ]
    return [(info[b"name"].decode("utf-8", "replace"), info[b"length"])]


def print_metadata(info, source_peer):
    files = info_files(info)
    total = sum(length for _, length in files)
    npieces = len(info.get(b"pieces", b"")) // 20
    print(f"    name         : {info[b'name'].decode('utf-8', 'replace')}")
    print(f"    total size   : {human_size(total)} ({total} bytes)")
    print(f"    piece length : {info.get(b'piece length', 0)} bytes")
    print(f"    pieces       : {npieces}")
    if len(files) == 1:
        print("    files        : 1 (single file torrent)")
    else:
        print(f"    files        : {len(files)}")
        for path, length in files:
            print(f"      - {path} ({human_size(length)})")
    print(f"    source peer  : {source_peer}")


def resolve_metadata(magnet, timeout):
    """Try every known peer until one hands us verified metadata."""
    info_hash = magnet["info_hash"]
    candidates = []

    def add(host, port):
        if host and 0 < int(port) < 65536 and (host, int(port)) not in candidates:
            candidates.append((host, int(port)))

    for hint in magnet["peers"]:
        if ":" in hint:
            host, _, port = hint.rpartition(":")
            add(host.strip("[]"), port)

    for tracker in magnet["trackers"]:
        print(f"    tracker {tracker}")
        try:
            peers = announce(tracker, info_hash, timeout)
            print(f"      -> {len(peers)} peer(s)")
            for host, port in peers:
                add(host, port)
        except Exception as e:
            print(f"      !! {e}")

    if not candidates:
        print("    !! no peers available")
        return None

    for host, port in candidates[:MAX_PEERS]:
        label = f"{host}:{port}"
        try:
            sock, via = open_peer(host, port, timeout)
            try:
                handshake(sock, info_hash)
                info = fetch_metadata(sock, info_hash)
            finally:
                sock.close()
            print(f"      peer {label} ({via}) -> metadata OK")
            return info, label
        except Exception as e:
            print(f"      peer {label} -> {e}")
    return None


def process(source, timeout):
    print(f"\n=== {source} ===")
    try:
        text = read_source(source, timeout)
    except Exception as e:
        print(f"  !! could not read source: {e}")
        return
    magnets = find_magnets(text.decode("utf-8", "replace"))
    if not magnets:
        print("  no magnet links found")
        return
    print(f"  found {len(magnets)} magnet link(s)")

    for i, magnet in enumerate(magnets, 1):
        ih = magnet["info_hash"]
        label = magnet["name"] or (ih.hex() if ih else "(no info hash)")
        print(f"\n  [{i}/{len(magnets)}] {label}")
        print(f"    magnet: {magnet['uri']}")
        if not magnet["info_hash"]:
            print("    !! no btih info hash in magnet; skipping")
            continue
        print(f"    info hash: {magnet['info_hash'].hex()}")
        result = resolve_metadata(magnet, timeout)
        if result:
            info, peer = result
            print_metadata(info, peer)
        else:
            print("    !! could not retrieve metadata")


def main(argv):
    timeout = DEFAULT_TIMEOUT
    sources = []
    it = iter(argv[1:])
    for arg in it:
        if arg == "--timeout":
            timeout = int(next(it))
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            sources.append(arg)
    if not sources:
        print(__doc__)
        return 2
    for source in sources:
        process(source, timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
