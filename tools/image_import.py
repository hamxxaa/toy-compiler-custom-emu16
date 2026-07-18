#!/usr/bin/env python3
"""Import LibreSprite/Aseprite indexed PNG + JSON exports into the EMU16 asset pipeline (stdlib only).

Usage:
    python tools/image_import.py <project_list> <out_dir>

project_list lines (whitespace-separated, '#' = comment):  <name> <src> <json|-> <fps_default|-> [rot=<csv>]
  - a row whose json is '-' is the MASTER PALETTE source: its colours become a 512-byte palette asset.
    <src> may be a `.gpl` (GIMP/LibreSprite palette -- the editable, git-friendly source of truth) or
    an indexed PNG (its embedded PLTE is used). Line/index order == PRAM order: APPEND, never reorder.
  - every other row is a sprite sheet PNG; each animation tag (or the whole sheet if untagged) becomes
    one concatenated sheet asset (all its frames' index bytes back to back).
  - an optional 5th field `rot=90,180,270` bakes those clockwise rotations of EVERY frame as extra
    slots after the base ones -- draw a tile once, get its rotations (the PPU can't rotate at render).
    90/270 need square frames. Rotated tile k @ the n-th listed angle is at base_slot + (n+1)*count + k.

Emits into <out_dir>:
  *.bin              one blob per asset (palette = 512 B RGB565; sheet = w*h*count index bytes)
  assets.manifest    feeds tools/pack_assets.py (asset order == EPAK ids)
  sprites.gen.txt    `const` ids the game includes:  PAL_<NAME>, ANIM_<NAME>_<TAG>_{ID,W,H,COUNT,FPS}
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import png  # noqa: E402


def rgb565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def palette_bytes(palette):
    """256 entries -> 512 bytes little-endian RGB565 (missing entries are black)."""
    out = bytearray()
    for i in range(256):
        r, g, b = palette[i] if i < len(palette) else (0, 0, 0)
        w = rgb565(r, g, b)
        out.append(w & 0xFF)
        out.append((w >> 8) & 0xFF)
    return bytes(out)


def read_gpl(path):
    """Parse a GIMP palette (.gpl) -> list of (r,g,b) in index order. LibreSprite/Aseprite, GIMP,
    and most editors export this. Line order == palette index == PRAM order, so APPEND new colors
    (never reorder) to keep existing sprites valid. Skips the header, '#' comments, and the
    Name:/Columns: metadata lines; takes the first three ints of each colour line as R G B."""
    pal = []
    with open(path, encoding="utf-8") as f:
        if "GIMP Palette" not in f.readline():
            raise SystemExit(f"{path}: not a GIMP palette (.gpl) -- missing 'GIMP Palette' header")
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.lower().startswith(("name:", "columns:")):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                pal.append((int(parts[0]), int(parts[1]), int(parts[2])))
            except ValueError:
                continue
    if len(pal) > 256:
        raise SystemExit(f"{path}: {len(pal)} colours exceeds the 256-entry PRAM")
    return pal


def frame_list(meta):
    frames = meta["frames"]
    return frames if isinstance(frames, list) else list(frames.values())   # Array or Hash export


def slice_frame(img, rect):
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    out = bytearray()
    for r in range(h):
        base = (y + r) * img.width + x
        out += img.indices[base:base + w]
    return bytes(out), w, h


def rotate_pattern(data, w, h, turns):
    """Rotate a w*h row-major index buffer `turns` * 90 degrees CLOCKWISE. Returns (bytes, nw, nh).
    Dims swap on odd turns (90/270). The PPU can't rotate at render time (a tilemap cell is just a
    pattern id), so a rotated tile is a baked pattern slot -- this bakes it."""
    out = bytes(data)
    cw, ch = w, h
    for _ in range(turns % 4):
        rot = bytearray(cw * ch)
        for y in range(ch):
            for x in range(cw):
                rot[x * ch + (ch - 1 - y)] = out[y * cw + x]   # (x,y) -> (ch-1-y, x) in the ch-wide result
        out = bytes(rot)
        cw, ch = ch, cw
    return out, cw, ch


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    list_path, out_dir = sys.argv[1], sys.argv[2]
    base_dir = os.path.dirname(os.path.abspath(list_path))
    os.makedirs(out_dir, exist_ok=True)

    manifest = []   # (asset_name, type, w, h, bin_filename) -- index == EPAK id
    consts = []
    reg_count = []  # registry rows, index-aligned with manifest (== clip id); palettes -> 0
    reg_period = []

    with open(list_path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) not in (4, 5):
                raise SystemExit(f"{list_path}:{lineno}: expected "
                                 f"'<name> <src> <json|-> <fps|-> [rot=<deg,csv>]'")
            name, src_rel, json_rel, fps_default = parts[:4]
            rot_angles = []                                       # extra rotated slots to bake (deg CW)
            if len(parts) == 5:
                if not parts[4].startswith("rot="):
                    raise SystemExit(f"{list_path}:{lineno}: 5th field must be rot=<csv>, got '{parts[4]}'")
                for a in parts[4][4:].split(","):
                    deg = int(a)
                    if deg not in (90, 180, 270):
                        raise SystemExit(f"{list_path}:{lineno}: rotation must be 90/180/270, got {deg}")
                    rot_angles.append(deg)

            if json_rel == "-":                                  # master palette source
                binname = f"{name}.bin"
                if src_rel.lower().endswith(".gpl"):
                    pal = read_gpl(os.path.join(base_dir, src_rel))      # editable source of truth
                else:
                    pal = png.read(os.path.join(base_dir, src_rel)).palette   # PNG's embedded PLTE
                with open(os.path.join(out_dir, binname), "wb") as bf:
                    bf.write(palette_bytes(pal))
                consts.append(f"const PAL_{name.upper()} = {len(manifest)};")
                manifest.append((name, "palette", 0, 0, binname))
                reg_count.append(0)
                reg_period.append(0)
                continue

            img = png.read(os.path.join(base_dir, src_rel))      # sprite sheet (indexed PNG)
            with open(os.path.join(base_dir, json_rel), encoding="utf-8") as jf:
                meta = json.load(jf)
            frames = frame_list(meta)
            tags = meta.get("meta", {}).get("frameTags", [])
            if not tags:
                tags = [{"name": name, "from": 0, "to": len(frames) - 1}]

            for tag in tags:
                fr, to = tag["from"], tag["to"]
                blob, fw, fh = bytearray(), 0, 0
                for fi in range(fr, to + 1):
                    data, w, h = slice_frame(img, frames[fi]["frame"])
                    if fw == 0:
                        fw, fh = w, h
                    elif (w, h) != (fw, fh):
                        raise SystemExit(f"{name}/{tag['name']}: frame {fi} is {w}x{h}, expected {fw}x{fh}")
                    blob += data
                count = to - fr + 1
                # Bake rotated variants as EXTRA slots after the base frames (one block per angle, in
                # order): rotated tile k @ the n-th listed angle sits at base_slot + (n+1)*count + k.
                # The game references those slots like any other tile -- rotation is invisible to it.
                for deg in rot_angles:
                    turns = deg // 90
                    if turns % 2 == 1 and fw != fh:
                        raise SystemExit(f"{name}/{tag['name']}: 90/270 rotation needs square frames "
                                         f"(got {fw}x{fh})")
                    for fi in range(fr, to + 1):
                        data, w, h = slice_frame(img, frames[fi]["frame"])
                        rdata, _rw, _rh = rotate_pattern(data, w, h, turns)
                        blob += rdata
                count *= 1 + len(rot_angles)
                dur = frames[fr].get("duration", 0)
                fps = round(1000 / dur) if dur else int(fps_default)
                # The runtime divides elapsed_ms by the per-frame PERIOD (no fps multiply -> no
                # int16 overflow). Uniform timing v1: one period for the whole clip.
                period = max(1, round(1000 / fps))
                if count * period > 0xFFFF:
                    raise SystemExit(f"{name}/{tag['name']}: clip duration {count*period} ms "
                                     f"exceeds 65535 (count {count} * period {period}); split the clip")
                # Untagged single-clip sheet (auto-tag name == asset name) -> ANIM_<NAME>_ID, not the
                # doubled ANIM_<NAME>_<NAME>_ID. Tagged sheets stay <name>_<tag>.
                aname = name if tag["name"] == name else f"{name}_{tag['name']}"
                binname = f"{aname}.bin"
                with open(os.path.join(out_dir, binname), "wb") as bf:
                    bf.write(bytes(blob))
                up = aname.upper()
                # Only the symbolic clip handle is a const now; W/H/COUNT/FPS moved into the
                # registry tables below (indexed by this id).
                consts.append(f"const ANIM_{up}_ID = {len(manifest)};")
                manifest.append((aname, "sprite", fw, fh, binname))
                reg_count.append(count)
                reg_period.append(period)

    with open(os.path.join(out_dir, "assets.manifest"), "w", encoding="utf-8") as mf:
        mf.write("# name type w h source  (generated by image_import.py)\n")
        for (n, t, w, h, src) in manifest:
            mf.write(f"{n} {t} {w} {h} {src}\n")

    # The clip registry: static metadata indexed by id (== EPAK id == manifest index), consumed by
    # lib/anim.lib. Palette rows are zero (never used as clips). clip_buf holds runtime sheet
    # addresses (0 = unloaded), filled by anim_load() at runtime.
    def arr_lit(vals):
        return "{ " + ", ".join(str(v) for v in vals) + " }"

    reg_w = [w for (_n, _t, w, _h, _s) in manifest]
    reg_h = [h for (_n, _t, _w, h, _s) in manifest]
    registry = []
    if manifest:
        registry = [
            f"const NUM_CLIPS = {len(manifest)};",
            f"var byte clip_w[NUM_CLIPS]      = {arr_lit(reg_w)};",
            f"var byte clip_h[NUM_CLIPS]      = {arr_lit(reg_h)};",
            f"var byte clip_count[NUM_CLIPS]  = {arr_lit(reg_count)};",
            f"var int  clip_period[NUM_CLIPS] = {arr_lit(reg_period)};",
            f"var int  clip_buf[NUM_CLIPS];",
        ]

    with open(os.path.join(out_dir, "sprites.gen.txt"), "w", encoding="utf-8") as cf:
        cf.write("// Generated by tools/image_import.py - asset ids + clip registry (include this,\n")
        cf.write("// before lib/anim.lib, which reads the clip_* tables).\n")
        cf.write("{\n")                              # a compilation unit must be brace-wrapped
        cf.write("\n".join("    " + c for c in consts) + "\n")
        if registry:
            cf.write("\n" + "\n".join("    " + r for r in registry) + "\n")
        cf.write("}\n")

    print(f"imported {len(manifest)} asset(s) -> {out_dir}/assets.manifest + sprites.gen.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
