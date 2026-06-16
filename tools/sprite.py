#!/usr/bin/env python3
"""ASCII-art sprite -> EMU16 byte array.

Draw a sprite in a text file using '#' (or X / 1 / *) for a lit pixel and '.' (or space / 0)
for a clear one, then convert it to a `var byte NAME[N] = { ... };` declaration ready for
io.lib's draw_sprite():

    python tools/sprite.py man.txt              # array name taken from the filename
    python tools/sprite.py man.txt hero         # explicit name

Bits are packed MSB-first (leftmost pixel = bit 7) and rows are padded to whole bytes — exactly
the layout draw_sprite(&name, W, H, x, y, color) expects.

Example  man.txt:
    ..####..
    .#....#.
    .######.
"""

import os
import sys

SET = set("#xX1*")   # characters that count as a lit pixel; anything else is clear


def convert(text):
    rows = [line.rstrip("\r\n") for line in text.split("\n")]
    rows = [r for r in rows if r.strip() != ""]          # drop blank separator lines
    if not rows:
        raise ValueError("no sprite rows found")
    w = max(len(r) for r in rows)
    h = len(rows)
    bpr = (w + 7) // 8
    data = []
    for r in rows:
        r = r.ljust(bpr * 8)
        for b in range(bpr):
            byte = 0
            for bit in range(8):
                if r[b * 8 + bit] in SET:
                    byte |= 0x80 >> bit
            data.append(byte)
    return w, h, bpr, data


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python tools/sprite.py <art.txt> [array_name]")
    path = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(os.path.basename(path))[0]
    with open(path) as f:
        w, h, bpr, data = convert(f.read())

    print(f"// {name}: {w}x{h} sprite -> draw_sprite(&{name}, {w}, {h}, x, y, color);")
    print(f"var byte {name}[{len(data)}] = {{")
    for row in range(h):
        cells = ", ".join(f"0x{v:02X}" for v in data[row * bpr:(row + 1) * bpr])
        comma = "," if row < h - 1 else ""
        print(f"    {cells}{comma}")
    print("};")


if __name__ == "__main__":
    main()
