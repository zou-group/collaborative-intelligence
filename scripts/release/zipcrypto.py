"""Write a password-protected zip (traditional PKWARE "ZipCrypto" encryption, deflate) that Python's own zipfile, unzip,
7-Zip and the system archive tools can open, with no third-party package.

ZipCrypto is the scheme of GPQA's own dataset.zip. It is not strong encryption and is not meant to be: the password is
published, and the point is only that crawlers do not pick the text up. The output is deterministic (fixed timestamps,
sorted entries, header bytes derived from the content), so packing the same files twice gives the same bytes.

    from zipcrypto import write_encrypted_zip
    write_encrypted_zip("out.zip", {"a/b.txt": b"...", ...}, password="...", comment="...")
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

_CRC_TABLE = []
for _n in range(256):
    _c = _n
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)

DOS_TIME, DOS_DATE = 0, (0 << 9) | (1 << 5) | 1  # 1980-01-01 00:00:00, the usual fixed time of reproducible zips


class _Keys:
    def __init__(self, password: bytes):
        self.k0, self.k1, self.k2 = 0x12345678, 0x23456789, 0x34567890
        for b in password:
            self.update(b)

    def update(self, b: int) -> None:
        t = _CRC_TABLE
        self.k0 = (self.k0 >> 8) ^ t[(self.k0 ^ b) & 0xFF]
        self.k1 = ((self.k1 + (self.k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        self.k2 = (self.k2 >> 8) ^ t[(self.k2 ^ (self.k1 >> 24)) & 0xFF]

    def encrypt(self, data: bytes) -> bytes:
        t = _CRC_TABLE
        k0, k1, k2 = self.k0, self.k1, self.k2
        out = bytearray(len(data))
        for i, p in enumerate(data):
            tmp = (k2 | 2) & 0xFFFF
            out[i] = p ^ (((tmp * (tmp ^ 1)) >> 8) & 0xFF)
            k0 = (k0 >> 8) ^ t[(k0 ^ p) & 0xFF]
            k1 = ((k1 + (k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
            k2 = (k2 >> 8) ^ t[(k2 ^ (k1 >> 24)) & 0xFF]
        self.k0, self.k1, self.k2 = k0, k1, k2
        return bytes(out)


def write_encrypted_zip(path: str | Path, files: dict[str, bytes], password: str, comment: str = "") -> int:
    """Write `files` ({archive name: content}) to `path`, every entry deflated and encrypted with `password`. Returns
    the number of entries. Names use '/' and must be relative."""
    pw = password.encode()
    local, central = bytearray(), bytearray()
    names = sorted(files)
    for name in names:
        if name.startswith("/") or ".." in name.split("/") or "\\" in name:
            raise ValueError(f"unsafe archive name: {name}")
        data = files[name]
        crc = zlib.crc32(data) & 0xFFFFFFFF
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        comp = co.compress(data) + co.flush()
        keys = _Keys(pw)
        head = hashlib.sha256(pw + name.encode() + data).digest()[:11] + bytes([crc >> 24])  # last byte: password check
        enc = keys.encrypt(head) + keys.encrypt(comp)
        bname = name.encode("utf-8")
        flags = 0x1 | (0x800 if not name.isascii() else 0)
        offset = len(local)
        local += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 8, DOS_TIME, DOS_DATE, crc, len(enc), len(data),
                             len(bname), 0) + bname + enc
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, (3 << 8) | 20, 20, flags, 8, DOS_TIME, DOS_DATE, crc,
                               len(enc), len(data), len(bname), 0, 0, 0, 0, (0o100644 << 16), offset) + bname
    bcomment = comment.encode("utf-8")[:65535]
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(names), len(names), len(central), len(local), len(bcomment))
    Path(path).write_bytes(bytes(local + central + end + bcomment))
    return len(names)
