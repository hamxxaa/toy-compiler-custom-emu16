#!/usr/bin/env python3
"""Compile an MML (Music Macro Language) text file into a packed .song blob for EMU16.

Usage:
    python tools/mml.py <in.mml> <out.song>

The .song blob is loaded at runtime by lib/music.lib's music_load_song() (pack it into a .pak as a
`raw` asset via tools/pack_assets.py). It is self-contained: it carries its own instrument
definitions, so different songs can use completely different sounds.

MML QUICK REFERENCE
-------------------
Directives (one per line):
    speed N              frames per row (tempo; default 6). Bigger = slower.
    groove A B C ...     a "speed table" cycled per row -> swing/shuffle & fine tempo (overrides speed)
    inst NAME            begin an instrument definition; the lines below configure it:
        vol  L L L | L    volume macro, levels 0..15, one per row-tick. `|` marks the LOOP point;
                          no `|` = hold the last value. (the note's loudness shape)
                          the loop point must reference a real value -- a bare trailing `|`
                          (nothing after it) is a compile error; repeat the last value instead.
        duty D D | D      pulse duty macro, 0..3 (0=12.5% thin .. 3=75%). (pulse timbre)
        arp  S S | S      arpeggio: signed semitone offsets added to the note (e.g. 0 4 7 = a chord)
        vib  DEPTH SPD DELAY   vibrato (pitch waver): depth in fp units, speed, and frames of delay
        noise M          noise voice only: 0 = hiss, 1 = short/metallic
    drum LETTER INST NOTE   define a one-letter drum shortcut usable in a track (e.g. `drum k kick 30`)

Tracks (one per channel, 0..3; notes may continue on following lines):
    ch0 NAME  <notes>    channel 0 uses instrument NAME by default, then the note string
Notes:  a b c d e f g  (+ or # = sharp, - = flat) ; r = rest ; a drum LETTER plays that drum
    length:  c4 = quarter, c8 = eighth, c16 = 16th, c6 = quarter-triplet, c12 = eighth-triplet;
             a trailing dot (c8.) = 1.5x.  `l8` sets the default length for following notes.
    octave:  o4 sets octave (o4 c = middle C = MIDI 60); > up an octave, < down.
    @NAME    switch instrument mid-track.       [ ... ]N   repeat the bracketed part N times.

The row grid resolution is chosen automatically (LCM of the note lengths used), so triplets and
straight notes coexist with the fewest rows. One pattern, up to 255 rows.
"""
import argparse
import os
import re
import subprocess
import sys
from fractions import Fraction
from math import gcd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# APU DEF_INST opcodes (must match emulator/apu.cpp).
OP_VOL, OP_DUTY, OP_ARP, OP_VIB, OP_NOISE = 0x10, 0x12, 0x13, 0x14, 0x16
CHANNELS = 4
SEMI = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
DIRECTIVES = {"speed", "groove", "inst", "vol", "duty", "arp", "vib", "noise", "drum",
              "ch0", "ch1", "ch2", "ch3"}


def die(msg):
    raise SystemExit(f"mml: {msg}")


def lcm(a, b):
    return a * b // gcd(a, b)


# ── Parse the .mml file into: instruments (name->macros), drums (letter->(inst,note)),
#    tracks (channel->(default_inst, note_string)), and the groove table. ─────────────────────────
def parse_mml(text):
    instruments = {}   # name -> dict(vol=(loop,data)|None, duty=..., arp=..., vib=(d,s,dl)|None, noise=int)
    inst_order = []    # names in definition order (-> ids)
    drums = {}         # letter -> (inst_name, note)
    tracks = {}        # channel -> [default_inst, note_string]
    groove = [6]

    cur_inst = None
    cur_track = None

    def parse_macro(vals, lineno, ctx):
        # vals: list of tokens, possibly containing '|' loop marker. Returns (loop, [ints]).
        loop = 255
        data = []
        for v in vals:
            if v == "|":
                loop = len(data)
            else:
                data.append(int(v) & 0xFF)
        if loop == len(data):
            # A bare trailing '|' (or a lone '|' with nothing at all) points the loop index
            # one past the last value -- out of bounds in the C++ Macro.data[] array, which
            # would read whatever stale byte happens to sit there at runtime. The safe idiom
            # is to repeat the last value after the pipe: '... 9 |' -> '... 9 | 9'.
            die(f"line {lineno}: {ctx} has a '|' loop point with nothing after it -- "
                f"repeat the last value after the pipe (e.g. '... 9 |' -> '... 9 | 9'), "
                f"or drop the '|' entirely to just hold the last value")
        return loop, data

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        head = parts[0].lower()

        if head == "speed":
            groove = [int(parts[1])]
            cur_inst = cur_track = None
        elif head == "groove":
            groove = [int(x) for x in parts[1:]]
            cur_inst = cur_track = None
        elif head == "inst":
            cur_inst = parts[1]
            cur_track = None
            if cur_inst not in instruments:
                instruments[cur_inst] = {"vol": None, "duty": None, "arp": None, "vib": None, "noise": 0}
                inst_order.append(cur_inst)
        elif head in ("vol", "duty", "arp"):
            if cur_inst is None:
                die(f"line {lineno}: '{head}' outside an 'inst' block")
            instruments[cur_inst][head] = parse_macro(parts[1:], lineno, f"'{head}' macro in inst '{cur_inst}'")
        elif head == "vib":
            if cur_inst is None:
                die(f"line {lineno}: 'vib' outside an 'inst' block")
            d, s, dl = (int(x) for x in parts[1:4])
            instruments[cur_inst]["vib"] = (d, s, dl)
        elif head == "noise":
            if cur_inst is None:
                die(f"line {lineno}: 'noise' outside an 'inst' block")
            instruments[cur_inst]["noise"] = int(parts[1]) & 1
        elif head == "drum":
            # drum LETTER INST NOTE
            letter, iname, note = parts[1], parts[2], int(parts[3])
            drums[letter] = (iname, note)
            cur_inst = cur_track = None
        elif head in ("ch0", "ch1", "ch2", "ch3"):
            ch = int(head[2])
            default_inst = parts[1] if len(parts) > 1 else None
            rest = line.split(None, 2)[2] if len(parts) > 2 else ""
            tracks[ch] = [default_inst, rest]
            cur_track = ch
            cur_inst = None
        else:
            # continuation of the current track's note string
            if cur_track is None:
                die(f"line {lineno}: don't understand '{line}'")
            tracks[cur_track][1] += " " + line

    return instruments, inst_order, drums, tracks, groove


# ── Expand [ ... ]N repeats textually (innermost first, so nesting works). ───────────────────────
def expand_repeats(s):
    pat = re.compile(r"\[([^\[\]]*)\](\d+)")
    while True:
        m = pat.search(s)
        if not m:
            return s
        s = s[:m.start()] + (" " + m.group(1) + " ") * int(m.group(2)) + s[m.end():]


TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<octset>o(?P<oct>\d))
  | (?P<octup>>)
  | (?P<octdn><)
  | (?P<deflen>l(?P<dl>\d+)(?P<dldot>\.?))
  | (?P<inst>@(?P<iname>\w+))
  | (?P<rest>r(?P<rlen>\d*)(?P<rdot>\.?))
  | (?P<note>(?P<nl>[a-g])(?P<nacc>[+#-]?)(?P<nlen>\d*)(?P<ndot>\.?))
  | (?P<other>(?P<ol>[a-zA-Z])(?P<olen>\d*)(?P<odot>\.?))
""", re.VERBOSE)


# ── Turn one track's note string into a list of (position:Fraction, note, inst_name). ────────────
# note: MIDI number for a note/drum, or 1 for a rest (note-off). Also returns the track's total
# length (Fraction, in whole notes) so channels can be aligned.
def parse_track(ch, default_inst, notes, drums, valid_insts):
    s = expand_repeats(notes)
    octave = 4
    default_dur = Fraction(1, 4)
    cur_inst = default_inst
    events = []
    pos = Fraction(0)

    def dur(lenstr, dot):
        base = Fraction(1, int(lenstr)) if lenstr else default_dur
        if dot:
            base *= Fraction(3, 2)
        return base

    i = 0
    while i < len(s):
        m = TOKEN_RE.match(s, i)
        if not m or m.start() == m.end():
            die(f"ch{ch}: cannot parse near '{s[i:i+8]}'")
        i = m.end()
        kind = m.lastgroup  # note: lastgroup is the last matched *named* group; use groupdict instead
        g = m.groupdict()
        if g["ws"] is not None:
            continue
        if g["octset"] is not None:
            octave = int(g["oct"])
        elif g["octup"] is not None:
            octave += 1
        elif g["octdn"] is not None:
            octave -= 1
        elif g["deflen"] is not None:
            default_dur = Fraction(1, int(g["dl"])) * (Fraction(3, 2) if g["dldot"] else 1)
        elif g["inst"] is not None:
            cur_inst = g["iname"]
            if cur_inst not in valid_insts:
                die(f"ch{ch}: unknown instrument @{cur_inst}")
        elif g["rest"] is not None:
            d = dur(g["rlen"], g["rdot"])
            events.append((pos, 1, cur_inst))   # 1 = note-off (silence)
            pos += d
        elif g["note"] is not None:
            acc = {"+": 1, "#": 1, "-": -1, "": 0}[g["nacc"]]
            midi = (octave + 1) * 12 + SEMI[g["nl"]] + acc
            if not 0 <= midi <= 127:
                die(f"ch{ch}: note out of range (o{octave} {g['nl']})")
            d = dur(g["nlen"], g["ndot"])
            events.append((pos, midi, cur_inst))
            pos += d
        elif g["other"] is not None:
            letter = g["ol"]
            if letter not in drums:
                die(f"ch{ch}: '{letter}' is not a note (a-g), a rest (r), or a defined drum")
            iname, note = drums[letter]
            d = dur(g["olen"], g["odot"])
            events.append((pos, note, iname))
            pos += d
    return events, pos


def build_instrument_stream(instruments, inst_order, name_to_id):
    out = bytearray()
    for name in inst_order:
        ins = instruments[name]
        iid = name_to_id[name]
        if ins["vol"]:
            loop, data = ins["vol"]
            out += bytes([OP_VOL, iid, loop, len(data)]) + bytes(data)
        if ins["duty"]:
            loop, data = ins["duty"]
            out += bytes([OP_DUTY, iid, loop, len(data)]) + bytes(data)
        if ins["arp"]:
            loop, data = ins["arp"]
            out += bytes([OP_ARP, iid, loop, len(data)]) + bytes(data)
        if ins["vib"]:
            d, s, dl = ins["vib"]
            out += bytes([OP_VIB, iid, d & 0xFF, s & 0xFF, dl & 0xFF])
        if ins["noise"]:
            out += bytes([OP_NOISE, iid, ins["noise"] & 1])
    return bytes(out)


def compile_mml(text):
    instruments, inst_order, drums, tracks, groove = parse_mml(text)
    if not tracks:
        die("no tracks (need at least one 'chN ...' line)")
    name_to_id = {name: i for i, name in enumerate(inst_order)}
    if len(inst_order) > 16:
        die(f"too many instruments ({len(inst_order)}); the APU has 16 slots")

    # Parse every track, collect duration denominators to pick the row grid.
    parsed = {}
    denoms = set()
    for ch in range(CHANNELS):
        if ch not in tracks:
            parsed[ch] = ([], Fraction(0))
            continue
        default_inst, notes = tracks[ch]
        ev, total = parse_track(ch, default_inst, notes, drums, set(inst_order))
        parsed[ch] = (ev, total)
        for pos, _n, _i in ev:
            denoms.add(pos.denominator)
        denoms.add(total.denominator)

    W = 1
    for d in denoms:
        W = lcm(W, d)
    total_rows = max(int(total * W) for _ch, (_ev, total) in parsed.items()) if parsed else 0
    if total_rows == 0:
        die("song is empty")
    if total_rows > 255:
        die(f"song is {total_rows} rows; the v1 format allows <=255 in one pattern "
            f"(use fewer/longer notes or split into a shorter loop)")

    # Build the pattern grid: rows x channels, each cell [note, inst_id].
    grid = [[(0, 0) for _ in range(CHANNELS)] for _ in range(total_rows)]
    for ch in range(CHANNELS):
        ev, _total = parsed[ch]
        for pos, note, iname in ev:
            row = int(pos * W)
            if row >= total_rows:
                continue
            iid = name_to_id[iname] if iname in name_to_id else 0
            grid[row][ch] = (note, iid)

    pat_bytes = bytearray()
    for row in range(total_rows):
        for ch in range(CHANNELS):
            note, iid = grid[row][ch]
            pat_bytes += bytes([note & 0xFF, iid & 0xFF])

    instr_stream = build_instrument_stream(instruments, inst_order, name_to_id)
    order_bytes = bytes([0, 255])   # one pattern, loop to 0

    # Header (9 bytes) + sections: groove, order, instruments, patterns.
    header = bytes([
        1,                       # version
        CHANNELS,
        total_rows & 0xFF,
        1,                       # num_patterns
        len(order_bytes) & 0xFF,
        0,                       # order_loop
        len(groove) & 0xFF,
        len(instr_stream) & 0xFF,
        (len(instr_stream) >> 8) & 0xFF,
    ])
    blob = header + bytes(groove) + order_bytes + instr_stream + bytes(pat_bytes)

    info = {
        "rows": total_rows, "grid_per_whole": W, "instruments": len(inst_order),
        "groove": groove, "instr_bytes": len(instr_stream), "size": len(blob),
    }
    return blob, info


def render_wav(song_path, wav_path, seconds, loops):
    """Render a .song to a .wav using the song_render tool (bit-identical to the game's audio)."""
    exe = os.path.join(ROOT, "song_render.exe")
    if not os.path.exists(exe):
        exe = os.path.join(ROOT, "song_render")
    if not os.path.exists(exe):
        die("song_render isn't built. Run:  mingw32-make song_render   "
            "(or: g++ -O2 emulator/apu.cpp emulator/song_render.cpp -o song_render.exe)")
    cmd = [exe, song_path, wav_path]
    if seconds is not None:
        cmd += ["--seconds", str(seconds)]
    elif loops is not None:
        cmd += ["--loops", str(loops)]
    subprocess.run(cmd, check=True)


def play_wav(path):
    """Play a WAV via the OS. On Windows uses SoundPlayer (no window, plays synchronously)."""
    if os.name == "nt":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(New-Object Media.SoundPlayer '{path}').PlaySync()"])
    else:
        for player in (["afplay", path], ["aplay", path], ["xdg-open", path]):
            try:
                subprocess.run(player)
                return
            except FileNotFoundError:
                continue


def main():
    ap = argparse.ArgumentParser(
        description="Compile MML to a .song blob, and optionally preview it as audio.")
    ap.add_argument("input", help="the .mml source file")
    ap.add_argument("output", nargs="?", help="the .song output path (optional with --preview)")
    ap.add_argument("--wav", metavar="PATH", help="also render a preview .wav (needs song_render)")
    ap.add_argument("--play", action="store_true", help="play the rendered .wav")
    ap.add_argument("--preview", action="store_true",
                    help="fast loop: compile to a temp .song, render a .wav, and PLAY it")
    ap.add_argument("--seconds", type=float, help="preview length in seconds (default: 2 loops)")
    ap.add_argument("--loops", type=int, help="preview length in order-list loops (default 2)")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        text = f.read()
    blob, info = compile_mml(text)

    song_path = args.output
    if song_path is None:
        if not (args.preview or args.wav):
            die("give an output .song path, or use --preview")
        os.makedirs(os.path.join(ROOT, "build", "music"), exist_ok=True)
        song_path = os.path.join(ROOT, "build", "music", "_preview.song")
    os.makedirs(os.path.dirname(os.path.abspath(song_path)), exist_ok=True)
    with open(song_path, "wb") as f:
        f.write(blob)

    # A rough tempo estimate assuming 60 fps and a single groove value.
    spd = info["groove"][0]
    rows_per_quarter = info["grid_per_whole"] / 4
    bpm = round(60.0 * 60.0 / (rows_per_quarter * spd)) if rows_per_quarter and spd else 0
    print(f"compiled {args.input} -> {song_path}")
    print(f"  {info['rows']} rows, grid {info['grid_per_whole']}/whole-note, "
          f"{info['instruments']} instruments, groove {info['groove']} (~{bpm} BPM), {info['size']} bytes")

    wav_path = args.wav
    if args.preview and wav_path is None:
        wav_path = os.path.splitext(song_path)[0] + ".wav"
    if wav_path:
        render_wav(song_path, wav_path, args.seconds, args.loops)
    # if (args.play or args.preview) and wav_path:
        # play_wav(wav_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
