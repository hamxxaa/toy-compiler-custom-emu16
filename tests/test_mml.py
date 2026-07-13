#!/usr/bin/env python3
"""Tool test for tools/mml.py: compile a tiny MML song and check the emitted .song blob's header,
instrument command stream, and a couple of pattern cells. STDLIB only."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MML = """\
speed 8
inst lead
  vol 15 12 9 | 9
  duty 2
inst bass
  vol 15 10 5 0
inst kick
  noise 1
  vol 15 8 0
drum k kick 30
ch0 lead o4 c4 e4
ch2 bass o2 c4 g4
ch3 drums k4 k4
"""


def main():
    with tempfile.TemporaryDirectory() as d:
        mml_path = os.path.join(d, "s.mml")
        song_path = os.path.join(d, "s.song")
        with open(mml_path, "w") as f:
            f.write(MML)

        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "mml.py"), mml_path, song_path],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"tool failed: {r.stdout}\n{r.stderr}"
        b = open(song_path, "rb").read()

        ver, ch, rows, npat, order_len, order_loop, groove_len, il_lo, il_hi = b[:9]
        instr_len = il_lo | (il_hi << 8)
        assert ver == 1 and ch == 4 and npat == 1, f"bad header {b[:9]!r}"
        # two quarter notes per track at grid 4/whole (quarter = 1 row) -> hmm: c4 e4 = 2 quarters,
        # a quarter = 1/4 whole; only length used is 4, so grid W = 4, quarter = 1 row -> total 2 rows.
        assert rows == 2, f"expected 2 rows, got {rows}"
        assert groove_len == 1, f"expected 1 groove entry, got {groove_len}"

        groove_off = 9
        order_off = groove_off + groove_len
        instr_off = order_off + order_len
        pat_off = instr_off + instr_len
        assert list(b[groove_off:order_off]) == [8], "groove should be [8] (speed 8)"
        assert list(b[order_off:instr_off]) == [0, 255], "order should be [0, 255]"

        # Instrument stream: lead(id0) vol+duty, bass(id1) vol, kick(id2) vol+noise.
        instr = list(b[instr_off:pat_off])
        # DEF_INST_VOL=0x10, DEF_INST_DUTY=0x12, DEF_INST_NOISE=0x16.
        # lead vol "15 12 9 | 9" -> data [15,12,9,9], loop index 3 (after the '|'): [0x10, id0, 3, 4, ...].
        assert instr[:8] == [0x10, 0, 3, 4, 15, 12, 9, 9], f"lead vol macro wrong: {instr[:8]}"
        assert instr[8:13] == [0x12, 0, 255, 1, 2], f"lead duty macro wrong: {instr[8:13]}"
        assert 0x16 in instr, "kick's noise-mode command (0x16) missing from the instrument stream"

        # Pattern: row 0 = C4(60) lead / empty / C2(36) bass / kick(note30,inst2).
        def cell(row, c):
            o = pat_off + (row * 4 + c) * 2
            return (b[o], b[o + 1])
        assert cell(0, 0) == (60, 0), f"row0 ch0 should be (60,0), got {cell(0,0)}"
        assert cell(0, 2) == (36, 1), f"row0 ch2 should be (36,1) bass, got {cell(0,2)}"
        assert cell(0, 3) == (30, 2), f"row0 ch3 should be (30,2) kick, got {cell(0,3)}"
        assert cell(1, 0) == (64, 0), f"row1 ch0 should be (64,0) E4, got {cell(1,0)}"

        # An undefined note letter must be a hard error.
        bad = os.path.join(d, "bad.mml")
        with open(bad, "w") as f:
            f.write("ch0 lead o4 c q\ninst lead\n  vol 15\n")
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "mml.py"), bad, song_path],
                            capture_output=True, text=True)
        assert r2.returncode != 0, "expected failure for the undefined note 'q'"

    print("test_mml: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
