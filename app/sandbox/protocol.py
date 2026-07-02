"""Tiny length-prefixed JSON framing for the app↔sandbox-worker Unix-domain socket.

Wire format: a 4-byte big-endian unsigned length, then that many bytes of UTF-8 JSON.
Used by both sides (app client in executor._run_code_remote, worker in server.py). No
app imports here so it stays dependency-free and importable from either process.
"""

import json
import struct

# Hard cap on a single frame so a malformed/hostile length can't make the peer allocate
# unboundedly. Tool results (stdout + a few SVG figures) are well under this.
MAX_FRAME = 32 * 1024 * 1024


def send_frame(sock, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError(f"frame too large to send ({len(data)} bytes)")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_frame(sock) -> dict:
    (n,) = struct.unpack("!I", _recv_exactly(sock, 4))
    if n > MAX_FRAME:
        raise ValueError(f"frame too large ({n} bytes)")
    return json.loads(_recv_exactly(sock, n).decode("utf-8"))


def _recv_exactly(sock, n: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < n:
        b = sock.recv(n - got)
        if not b:
            raise ConnectionError("socket closed mid-frame")
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)
