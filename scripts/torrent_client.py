#!/usr/bin/env python3
import hashlib
import os
import socket
import struct
import sys
import urllib.parse
import urllib.request

INFO_HASH = bytes.fromhex("d5bf5e82c67129b90b3ca233805882cc2bd49689")
HOST = "torrent.ubuntu.com"
PORT = 51413
TRACKER = "https://ps7.torrent.ubuntu.com/announce"
PEER_ID = b"-PC0001-" + b"0123456789ab"
BLOCK = 16384


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


def handshake(sock):
    reserved = b"\x00\x00\x00\x00\x00\x10\x00\x00"
    sock.sendall(bytes([19]) + b"BitTorrent protocol" + reserved + INFO_HASH + PEER_ID)
    resp = recv_exact(sock, 68)
    if resp[0] != 19 or resp[1:20] != b"BitTorrent protocol":
        raise RuntimeError("bad handshake pstr")
    if resp[28:48] != INFO_HASH:
        raise RuntimeError("info hash mismatch")
    return resp[48:68]


def fetch_metadata(sock):
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
                raise RuntimeError("metadata piece rejected")
    meta = b"".join(buf)[:size]
    if hashlib.sha1(meta).digest() != INFO_HASH:
        raise RuntimeError("metadata hash mismatch")
    info, _ = bdecode(meta)
    return info


def get_piece(sock, index, size):
    blocks = {}
    n = (size + BLOCK - 1) // BLOCK
    for b in range(n):
        begin = b * BLOCK
        send_msg(sock, 6, struct.pack(">III", index, begin, min(BLOCK, size - begin)))
    while len(blocks) < n:
        mid, p = read_msg(sock)
        if mid == 7:
            pi, begin = struct.unpack(">II", p[:8])
            if pi == index:
                blocks[begin] = p[8:]
        elif mid == 0:
            raise RuntimeError("peer choked us")
    return b"".join(blocks[b * BLOCK] for b in range(n))


def announce():
    qs = (
        "info_hash="
        + urllib.parse.quote_from_bytes(INFO_HASH)
        + "&peer_id="
        + urllib.parse.quote_from_bytes(PEER_ID)
        + "&port=6881"
        + "&uploaded=0"
        + "&downloaded=0"
        + "&left=0"
        + "&compact=1"
    )
    data = urllib.request.urlopen(TRACKER + "?" + qs, timeout=15).read()
    d, _ = bdecode(data)
    peers = d.get(b"peers")
    out = []
    if isinstance(peers, bytes):
        for i in range(0, len(peers), 6):
            ip = ".".join(str(x) for x in peers[i : i + 4])
            out.append((ip, struct.unpack(">H", peers[i + 4 : i + 6])[0]))
    elif isinstance(peers, list):
        for pr in peers:
            out.append((pr[b"ip"].decode(), pr[b"port"]))
    return out


def download(info, sock):
    piece_len = info[b"piece length"]
    hashes = info[b"pieces"]
    if b"files" in info:
        entries = [
            ([info[b"name"]] + list(f[b"path"]), f[b"length"]) for f in info[b"files"]
        ]
    else:
        entries = [([info[b"name"]], info[b"length"])]
    total = sum(length for _, length in entries)
    npieces = len(hashes) // 20

    bounds = []
    off = 0
    for parts, length in entries:
        path = os.path.join(*[p.decode() for p in parts])
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        bounds.append((open(path, "wb"), off, off + length))
        off += length

    def write_at(pos, data):
        for fh, s, e in bounds:
            if pos < e and pos + len(data) > s:
                a, b = max(pos, s), min(pos + len(data), e)
                fh.seek(a - s)
                fh.write(data[a - pos : b - pos])

    send_msg(sock, 2)
    while True:
        mid, _ = read_msg(sock)
        if mid == 1:
            break
        if mid == 0:
            raise RuntimeError("choked before start")

    for p in range(npieces):
        size = min(piece_len, total - p * piece_len)
        data = get_piece(sock, p, size)
        if hashlib.sha1(data).digest() != hashes[p * 20 : (p + 1) * 20]:
            raise RuntimeError("piece %d failed hash" % p)
        write_at(p * piece_len, data)
        print("piece %d/%d ok (%d bytes)" % (p + 1, npieces, size), flush=True)
    for fh, _, _ in bounds:
        fh.close()


def connect(host, port):
    s = socket.create_connection((host, port), timeout=15)
    s.settimeout(30)
    peer = handshake(s)
    print(f"handshake ok with peer {peer!r}")
    return s


def main():
    candidates = [(HOST, PORT)]
    try:
        candidates += announce()
    except Exception as e:
        print(f"tracker failed: {e}")
    last = None
    print(f"list of candidates: {candidates}")
    for host, port in candidates:
        try:
            print(f"connecting to {host}:{port}")
            s = connect(host, port)
            info = fetch_metadata(s)
            print(
                "metadata: name={!r} length={!r} piece length={!r}".format(
                    info.get(b"name"), info.get(b"length"), info.get(b"piece length")
                )
            )
            download(info, s)
            s.close()
            print("done")
            # return
        except Exception as e:
            last = e
            print("failed %s:%d: %s" % (host, port, e))
    print(f"all peers failed: {last}")
    sys.exit(1)


if __name__ == "__main__":
    main()
