#!/usr/bin/env python3
"""Minimal PNG reader for the EMU16 asset pipeline -- Python stdlib only (zlib, struct).

Reads exactly what LibreSprite/Aseprite export for indexed art: 8-bit, color-type-3 (indexed),
non-interlaced PNG. Anything else raises a clear error (keeps this small). Returns the raw palette
indices (one byte per pixel, top-to-bottom / left-to-right), the PLTE palette, and any tRNS alpha.
The only hard part (DEFLATE) comes free from the stdlib `zlib`.
"""
import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"


class Png:
    def __init__(self, width, height, indices, palette, trns):
        self.width = width
        self.height = height
        self.indices = indices      # bytes, length width*height (palette indices)
        self.palette = palette      # list of (r, g, b), each 0..255
        self.trns = trns            # list of per-index alpha (may be shorter than palette)

    def index_at(self, x, y):
        return self.indices[y * self.width + x]


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height):
    """De-filter scanlines for 8-bit indexed (bpp = 1). raw = (filter byte + width bytes) per row."""
    stride = width
    out = bytearray()
    prev = bytearray(stride)             # the row above; zeros for row 0
    pos = 0
    for _ in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 0:                      # None
            pass
        elif ft == 1:                    # Sub  (left)
            for x in range(1, stride):
                line[x] = (line[x] + line[x - 1]) & 0xFF
        elif ft == 2:                    # Up   (above)
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ft == 3:                    # Average
            for x in range(stride):
                left = line[x - 1] if x > 0 else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ft == 4:                    # Paeth
            for x in range(stride):
                left = line[x - 1] if x > 0 else 0
                upleft = prev[x - 1] if x > 0 else 0
                line[x] = (line[x] + _paeth(left, prev[x], upleft)) & 0xFF
        else:
            raise ValueError(f"PNG: unknown scanline filter type {ft}")
        out += line
        prev = line
    return bytes(out)


def read(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != PNG_SIG:
        raise ValueError(f"{path}: not a PNG (bad signature)")
    pos = 8
    width = height = None
    palette = []
    trns = []
    idat = bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length               # 4 length + 4 type + length + 4 CRC (CRC skipped)
        if ctype == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color != 3:
                raise ValueError(
                    f"{path}: need 8-bit indexed (bit-depth 8, color-type 3); got depth {depth}, "
                    f"type {color}. In LibreSprite use Sprite > Color Mode > Indexed and export PNG."
                )
            if interlace != 0:
                raise ValueError(f"{path}: interlaced PNG not supported; turn interlacing off on export.")
        elif ctype == b"PLTE":
            palette = [(body[i], body[i + 1], body[i + 2]) for i in range(0, len(body), 3)]
        elif ctype == b"tRNS":
            trns = list(body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if width is None:
        raise ValueError(f"{path}: no IHDR chunk")
    indices = _unfilter(zlib.decompress(bytes(idat)), width, height)
    return Png(width, height, indices, palette, trns)


if __name__ == "__main__":
    import sys
    img = read(sys.argv[1])
    print(f"{sys.argv[1]}: {img.width}x{img.height}, {len(img.palette)} palette entries")
