# Classic HUD Sprites (sbar + ibar) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the text status bar in textured/zbuf mode with the genuine Quake status bar (sbar + ibar strips from `gfx.wad`), with the 3D viewport shrunk by 48 rows exactly as WinQuake's `R_SetVrect` does.

**Architecture:** New `quake/wad.py` (WAD2/qpic parser, ports `wad.c`) and `quake/sbar.py` (pure compositor porting `sbar.c`'s `Sbar_Draw`, blitting palette indices into the 8-bit framebuffer). The renderer gains a `sbar_lines` attribute that shrinks the z-buffer view height; `client.py` appends the reserved rows, calls `sbar.draw()` over them, and tracks the two client-side timers (`faceanimtime`, `item_gettime`) that `cl_parse.c` kept. Default video resolution moves to 320×200.

**Tech Stack:** Pure Python stdlib (`struct`), shareware `pak0.pak` data, existing standalone-script test convention (`tests/_bootstrap.py`, `PQ_AUDIO=0`, prints `OK`).

**Spec:** `docs/superpowers/specs/2026-06-12-hud-sprites-design.md`. Reference sources: `quake-source/WinQuake/wad.c`, `sbar.c`, `cl_parse.c` (timers), `screen.c` (`sb_lines`). Cite them in docstrings/commits as the repo convention requires.

**Conventions reminder:** relative imports inside `quake/` (`from .wad import Wad`); absolute outside (`from quake.sbar import Sbar`). Run tests with `PQ_AUDIO=0`. Each test file is a standalone script with an `if __name__ == "__main__"` block printing `OK`.

---

### Task 1: WAD2 parser (`quake/wad.py`)

**Files:**
- Create: `quake/wad.py`
- Test: `tests/test_wad.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the WAD2 parser (gfx.wad inside pak0.pak): directory, qpic
lumps, raw lumps, case-insensitive lookup. Needs the shareware pak."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.pak import Pak
from quake.wad import Wad


def _wad():
    return Wad(Pak("quake-shareware/id1/pak0.pak").read("gfx.wad"))


def test_directory_parses():
    w = _wad()
    names = w.names()
    assert len(names) == 163
    assert "sbar" in names and "num_0" in names and "conchars" in names


def test_qpic_sizes_match_sbar_layout():
    w = _wad()
    for name, (ww, hh) in (("sbar", (320, 24)), ("ibar", (320, 24)),
                           ("num_0", (24, 24)), ("anum_minus", (24, 24)),
                           ("face1", (24, 24)), ("sb_sigil1", (8, 16)),
                           ("inv_lightng", (48, 16)), ("backtile", (64, 64))):
        pw, ph, px = w.qpic(name)
        assert (pw, ph) == (ww, hh), name
        assert len(px) == ww * hh, name


def test_lookup_is_case_insensitive():
    w = _wad()
    assert w.qpic("SBAR") == w.qpic("sbar")


def test_raw_lump_conchars():
    # CONCHARS is type 0x44 (raw 128x128 font sheet, no qpic header)
    w = _wad()
    assert len(w.lump("conchars")) == 128 * 128


def test_qpic_rejects_non_qpic_lump():
    w = _wad()
    try:
        w.qpic("conchars")
    except ValueError:
        pass
    else:
        assert False, "qpic() accepted a non-qpic lump"


if __name__ == "__main__":
    test_directory_parses()
    test_qpic_sizes_match_sbar_layout()
    test_lookup_is_case_insensitive()
    test_raw_lump_conchars()
    test_qpic_rejects_non_qpic_lump()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_wad.py`
Expected: `ModuleNotFoundError: No module named 'quake.wad'`

- [ ] **Step 3: Write the implementation**

`quake/wad.py` — follow `quake/pak.py`'s structure (module docstring with the binary layout, `struct.Struct` constants, class, `__main__` self-test):

```python
"""Quake WAD2 archive reader (gfx.wad: the 2D/HUD art). Pure stdlib.
Ports the lump directory of WinQuake's wad.c (W_LoadWadFile/W_GetLumpName).

WAD2 layout (little-endian):
  header: char id[4] = "WAD2"; int32 numlumps; int32 infotableofs
  directory at infotableofs: numlumps entries, each 32 bytes:
      int32 filepos, disksize, size; char type, compression, pad1, pad2;
      char name[16]
  qpic lump (type 0x42): int32 width, height; byte pixels[width*height]
      (palette indices; 255 is transparent by convention -- the drawer's
      business, not ours). Other types (0x44 CONCHARS) are raw bytes.

Lump names are stored upper-case but looked up case-insensitively, as
W_CleanupName does.
"""

import struct

HEADER = struct.Struct("<4sii")          # id, numlumps, infotableofs
ENTRY = struct.Struct("<iiibbbb16s")     # filepos, disksize, size, type,
                                         # compression, pad1, pad2, name
TYP_QPIC = 0x42


class Wad:
    def __init__(self, data):
        self.data = data
        magic, numlumps, ofs = HEADER.unpack_from(data, 0)
        if magic != b"WAD2":
            raise ValueError(f"not a WAD2 file (magic {magic!r})")
        self.lumps = {}                  # lowercase name -> (filepos, size, type)
        for i in range(numlumps):
            (pos, _disk, size, typ, _comp, _p1, _p2,
             name) = ENTRY.unpack_from(data, ofs + i * ENTRY.size)
            name = name.split(b"\0", 1)[0].decode("ascii", "replace").lower()
            self.lumps[name] = (pos, size, typ)

    def lump(self, name):
        """Raw lump bytes (e.g. CONCHARS, a headerless 128x128 font sheet)."""
        pos, size, _typ = self.lumps[name.lower()]
        return self.data[pos:pos + size]

    def qpic(self, name):
        """A type-0x42 picture lump as (width, height, pixels): pixels is
        width*height palette indices, row-major."""
        pos, _size, typ = self.lumps[name.lower()]
        if typ != TYP_QPIC:
            raise ValueError(f"{name}: lump type {typ:#x} is not a qpic")
        w, h = struct.unpack_from("<ii", self.data, pos)
        return w, h, self.data[pos + 8:pos + 8 + w * h]

    def names(self):
        return sorted(self.lumps)


if __name__ == "__main__":
    from .pak import Pak
    pak = Pak("quake-shareware/id1/pak0.pak")
    wad = Wad(pak.read("gfx.wad"))
    print(f"gfx.wad: {len(wad.lumps)} lumps")
    for n in wad.names():
        pos, size, typ = wad.lumps[n]
        dims = "%dx%d" % wad.qpic(n)[:2] if typ == TYP_QPIC else f"{size}B"
        print(f"  {n:16s} type={typ:#04x} {dims}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_wad.py`
Expected: `OK`
Also run: `python -m quake.wad` — expect the 163-lump listing, no traceback.

- [ ] **Step 5: Commit**

```bash
git add quake/wad.py tests/test_wad.py
git commit -m "wad: WAD2/qpic parser for gfx.wad (ports wad.c lump directory)"
```

---

### Task 2: Status-bar compositor (`quake/sbar.py`)

**Files:**
- Create: `quake/sbar.py`
- Test: `tests/test_sbar.py`

Pure module, no Client/Server involved: tests compose into a synthetic buffer.
Port `sbar.c` faithfully — `Sbar_DrawInventory`, `Sbar_DrawNormal`,
`Sbar_DrawFace`, `Sbar_DrawNum` — and verify each formula against
`quake-source/WinQuake/sbar.c` while implementing (the code below cites line
behaviour, but the C source is the authority).

- [ ] **Step 1: Write the failing test**

```python
"""Pixel-level tests for quake/sbar.py, the Sbar_Draw port: strips land on
the right rows, faces/numbers/weapon states pick the right lumps, palette
index 255 stays transparent, BACKTILE fills margins on wide buffers.
Pure -- no Client/Server; needs the shareware pak for gfx.wad."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.pak import Pak
from quake.wad import Wad
from quake.sbar import (Sbar, IT_SHOTGUN, IT_NAILGUN, IT_SHELLS, IT_ARMOR1,
                        IT_INVULNERABILITY)

W, H = 320, 200
BG = 0x07                      # arbitrary background index to detect overdraw
NO_GETTIME = [0.0] * 32


def _wad():
    return Wad(Pak("quake-shareware/id1/pak0.pak").read("gfx.wad"))


def _st(**kw):
    st = {"health": 100, "armor": 50, "ammo": 25, "shells": 25, "nails": 0,
          "rockets": 0, "cells": 0,
          "items": IT_SHOTGUN | IT_SHELLS | IT_ARMOR1,
          "weapon_bit": IT_SHOTGUN}
    st.update(kw)
    return st


def _draw(sb, st, time=10.0, w=W, h=H, gettime=NO_GETTIME, faceanim=0.0):
    fb = bytearray(bytes((BG,)) * (w * h))
    sb.draw(fb, w, h, st, time, gettime, faceanim)
    return fb


def _assert_pic_at(fb, fbw, x, y, pic, msg):
    """Every non-transparent source pixel landed; transparent ones didn't."""
    pw, ph, px = pic
    for r in range(ph):
        for c in range(pw):
            s = px[r * pw + c]
            if s != 255:
                assert fb[(y + r) * fbw + x + c] == s, f"{msg} at +({c},{r})"


def test_strips_on_the_right_rows():
    wad = _wad()
    sb = Sbar(wad)
    fb = _draw(sb, _st())
    # an untouched column of the sbar strip (x 208..224 has no icons/digits)
    sw, shh, spx = wad.qpic("sbar")
    for r in range(shh):
        assert fb[(H - 24 + r) * W + 210] == spx[r * sw + 210]
    # ibar top-right corner (no items/sigils in _st, so it stays background art)
    iw, ih, ipx = wad.qpic("ibar")
    assert fb[(H - 48) * W + 300] == ipx[300]
    # nothing above the bar was touched
    assert fb[: (H - 48) * W] == bytes((BG,)) * ((H - 48) * W)


def test_face_tiers_and_pain():
    wad = _wad()
    sb = Sbar(wad)
    # health 100 -> sb_faces[4][0] == FACE1 (sbar.c: f = 4 when >= 100)
    _assert_pic_at(_draw(sb, _st(health=100)), W, 112, H - 24,
                   wad.qpic("face1"), "face1 at full health")
    # health 10 -> f = 0 -> FACE5
    _assert_pic_at(_draw(sb, _st(health=10)), W, 112, H - 24,
                   wad.qpic("face5"), "face5 at low health")
    # pain face while time <= faceanimtime
    _assert_pic_at(_draw(sb, _st(health=100), time=1.0, faceanim=1.1), W,
                   112, H - 24, wad.qpic("face_p1"), "pain face")


def test_red_digits_when_low():
    wad = _wad()
    sb = Sbar(wad)
    # health 20: right-aligned 3 digits at x=136 -> '2' lands at x=160, red
    _assert_pic_at(_draw(sb, _st(health=20)), W, 160, H - 24,
                   wad.qpic("anum_2"), "red 2")
    # health 100 uses gold digits: '1' at x=136
    _assert_pic_at(_draw(sb, _st(health=100)), W, 136, H - 24,
                   wad.qpic("num_1"), "gold 1")


def test_weapon_slot_states():
    wad = _wad()
    sb = Sbar(wad)
    st = _st(items=IT_SHOTGUN | IT_NAILGUN | IT_SHELLS | IT_ARMOR1,
             weapon_bit=IT_NAILGUN)
    fb = _draw(sb, st)
    # selected weapon -> INV2_, owned-unselected -> INV_ (slot i at x=i*24)
    _assert_pic_at(fb, W, 0, H - 40, wad.qpic("inv_shotgun"), "owned shotgun")
    _assert_pic_at(fb, W, 48, H - 40, wad.qpic("inv2_nailgun"), "selected nailgun")
    # fresh pickup flashes: gettime now-ish -> INVA* frame, not INV_
    gt = [0.0] * 32
    gt[0] = 9.95                      # shotgun picked up 0.05s ago at time=10
    fb = _draw(sb, st, gettime=gt)
    iw, ih, ipx = wad.qpic("inv_shotgun")
    flashed = any(fb[(H - 40 + r) * W + c] != ipx[r * iw + c]
                  for r in range(ih) for c in range(iw)
                  if ipx[r * iw + c] != 255)
    assert flashed, "pickup flash frame expected"


def test_invulnerability_face_and_666():
    wad = _wad()
    sb = Sbar(wad)
    fb = _draw(sb, _st(items=_st()["items"] | IT_INVULNERABILITY))
    _assert_pic_at(fb, W, 112, H - 24, wad.qpic("face_invul2"), "pent face")
    _assert_pic_at(fb, W, 24, H - 24, wad.qpic("anum_6"), "666 armor")


def test_transparency_keeps_strip_pixels():
    wad = _wad()
    sb = Sbar(wad)
    fb = _draw(sb, _st(health=100))
    # num_1 has transparent (255) pixels; underneath them the sbar strip art
    # must survive. Find one and check.
    nw, nh, npx = wad.qpic("num_1")
    sw, shh, spx = wad.qpic("sbar")
    for r in range(nh):
        for c in range(nw):
            if npx[r * nw + c] == 255:
                assert fb[(H - 24 + r) * W + 136 + c] == spx[r * sw + 136 + c]
                return
    assert False, "num_1 has no transparent pixels?"


def test_backtile_fills_margins_on_wide_buffers():
    wad = _wad()
    sb = Sbar(wad)
    wide = 400                        # sx = 40; margins 0..40 and 360..400
    fb = _draw(sb, _st(), w=wide, h=H)
    bw, bh, bpx = wad.qpic("backtile")
    y = H - 10
    assert fb[y * wide + 5] == bpx[(y & 63) * 64 + (5 & 63)]
    assert fb[y * wide + 395] == bpx[(y & 63) * 64 + (395 & 63)]


if __name__ == "__main__":
    test_strips_on_the_right_rows()
    test_face_tiers_and_pain()
    test_red_digits_when_low()
    test_weapon_slot_states()
    test_invulnerability_face_and_666()
    test_transparency_keeps_strip_pixels()
    test_backtile_fills_margins_on_wide_buffers()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_sbar.py`
Expected: `ModuleNotFoundError: No module named 'quake.sbar'`

- [ ] **Step 3: Write the implementation**

`quake/sbar.py`:

```python
"""Quake status bar: sbar.c port. Composites the classic two-strip HUD --
the 320x24 SBAR (armor / face / health / ammo with the big 24x24 digits)
under the 320x24 IBAR (weapon slots, small ammo counts, items, sigils) --
into the renderer's 8-bit indexed framebuffer, bottom-centred
(Sbar_DrawPic's single-player x + (vid.width-320)/2). Palette index 255 is
transparent (Draw_TransPic); BACKTILE tiles under any margins wider than
320 (the R_DrawTiledPoly look). Pure: no OS, UI, or engine imports beyond
the wad parser feeding it.

The caller owns the two timers cl_parse.c keeps client-side and passes them
in: item_gettime[bit] (pickup flash animations) and faceanimtime (the 0.2s
pain face after damage).
"""

SBAR_W, SBAR_H = 320, 24      # each strip (SBAR_HEIGHT in sbar.c)
SBAR_LINES = 2 * SBAR_H       # rows reserved at the bottom (screen.c sb_lines)
TRANSPARENT = 255

# item bits (defs.qc / sbar.c) -- the QC stores .weapon as one of these too
IT_SHOTGUN = 1                # IT_SHOTGUN << i, i in 0..6, are the 7 slots
IT_SHELLS = 256
IT_NAILS = 512
IT_ROCKETS = 1024
IT_CELLS = 2048
IT_ARMOR1 = 8192
IT_ARMOR2 = 16384
IT_ARMOR3 = 32768
IT_INVISIBILITY = 524288
IT_INVULNERABILITY = 1048576
IT_QUAD = 4194304

_WEAPON_LUMPS = ("shotgun", "sshotgun", "nailgun", "snailgun",
                 "rlaunch", "srlaunch", "lightng")


class Sbar:
    """Loads the lump set Sbar_Init loads; draw() is Sbar_Draw."""

    def __init__(self, wad):
        q = wad.qpic
        # nums[color][digit]: 0 gold (NUM_), 1 red (ANUM_); [10] is minus
        self.nums = [[q(f"num_{i}") for i in range(10)] + [q("num_minus")],
                     [q(f"anum_{i}") for i in range(10)] + [q("anum_minus")]]
        # weapons[state][slot]: 0 owned, 1 selected, 2..6 pickup flash frames
        self.weapons = ([[q(f"inv_{w}") for w in _WEAPON_LUMPS],
                         [q(f"inv2_{w}") for w in _WEAPON_LUMPS]] +
                        [[q(f"inva{i}_{w}") for w in _WEAPON_LUMPS]
                         for i in range(1, 6)])
        self.ammo = [q("sb_shells"), q("sb_nails"), q("sb_rocket"),
                     q("sb_cells")]
        self.armor = [q("sb_armor1"), q("sb_armor2"), q("sb_armor3")]
        self.items = [q("sb_key1"), q("sb_key2"), q("sb_invis"),
                      q("sb_invuln"), q("sb_suit"), q("sb_quad")]
        self.sigils = [q(f"sb_sigil{i}") for i in range(1, 5)]
        # faces[f]: f=0 is near-death FACE5 .. f=4 is healthy FACE1, each
        # (normal, pain) -- mirrors the reversed sb_faces[] fill in Sbar_Init
        self.faces = [(q(f"face{5 - i}"), q(f"face_p{5 - i}"))
                      for i in range(5)]
        self.face_invis = q("face_invis")
        self.face_invuln = q("face_invul2")
        self.face_invis_invuln = q("face_inv2")
        self.face_quad = q("face_quad")
        self.sbar = q("sbar")
        self.ibar = q("ibar")
        self.disc = q("disc")
        self.backtile = q("backtile")
        self.conchars = wad.lump("conchars")   # 128x128, 16x16 grid of 8x8

    # ---- blit primitives (draw.c) ----
    def _pic(self, fb, fbw, x, y, pic):
        """Draw_TransPic: copy palette indices, 255 transparent. Opaque pics
        (the strips) take the row-slice fast path."""
        w, h, px = pic
        if TRANSPARENT not in px:
            for r in range(h):
                d = (y + r) * fbw + x
                fb[d:d + w] = px[r * w:(r + 1) * w]
            return
        for r in range(h):
            d = (y + r) * fbw + x
            s = r * w
            for i in range(w):
                c = px[s + i]
                if c != TRANSPARENT:
                    fb[d + i] = c

    def _char(self, fb, fbw, x, y, num):
        """Draw_Character from CONCHARS: 8x8 glyph, index 0 transparent."""
        src = self.conchars
        sy, sx = (num >> 4) * 8, (num & 15) * 8
        for r in range(8):
            s = (sy + r) * 128 + sx
            d = (y + r) * fbw + x
            for i in range(8):
                b = src[s + i]
                if b:
                    fb[d + i] = b

    def _num(self, fb, fbw, x, y, value, digits, red):
        """Sbar_DrawNum: right-aligned big digits, gold or red (ANUM)."""
        s = str(int(value))
        if len(s) > digits:
            s = s[len(s) - digits:]
        x += (digits - len(s)) * 24
        nums = self.nums[1 if red else 0]
        for ch in s:
            self._pic(fb, fbw, x, y, nums[10 if ch == "-" else int(ch)])
            x += 24

    # ---- Sbar_Draw ----
    def draw(self, fb, fbw, fbh, st, time, item_gettime, faceanimtime):
        """Composite both strips over the bottom SBAR_LINES rows of fb (an
        8-bit indexed framebuffer, fbw*fbh bytes). st is sv.hud_status()
        (with the raw items/weapon_bit fields); time is sv.time;
        item_gettime is the 32-entry pickup-time list and faceanimtime the
        pain-face deadline, both kept by the client (cl_parse.c)."""
        sx = (fbw - SBAR_W) >> 1
        top = fbh - SBAR_LINES
        if fbw > SBAR_W:                        # margins: tiled BACKTILE
            bt = self.backtile[2]
            for y in range(top, fbh):
                trow = (y & 63) * 64
                row = bytes(bt[trow + (x & 63)] for x in range(fbw))
                base = y * fbw
                fb[base:base + sx] = row[:sx]
                fb[base + sx + SBAR_W:base + fbw] = row[sx + SBAR_W:]
        items = st["items"]
        self._inventory(fb, fbw, sx, top, st, items, time, item_gettime)
        self._status(fb, fbw, sx, fbh - SBAR_H, st, items, time, faceanimtime)

    def _inventory(self, fb, fbw, sx, top, st, items, time, gettime):
        """Sbar_DrawInventory: the upper strip."""
        self._pic(fb, fbw, sx, top, self.ibar)
        # weapon slots: INV_ owned / INV2_ selected / INVA1-5 pickup flash
        for i in range(7):
            bit = IT_SHOTGUN << i
            if not items & bit:
                continue
            flashon = int((time - gettime[i]) * 10)
            if flashon >= 10:
                flashon = 1 if st["weapon_bit"] == bit else 0
            else:
                flashon = (flashon % 5) + 2
            self._pic(fb, fbw, sx + i * 24, top + 8, self.weapons[flashon][i])
        # ammo counts: gold console digits (chars 18+n), 3 wide per pool
        for i, key in enumerate(("shells", "nails", "rockets", "cells")):
            s = f"{st[key]:3d}"[-3:]
            for j, ch in enumerate(s):
                if ch != " ":
                    self._char(fb, fbw, sx + (6 * i + 1) * 8 - 2 + j * 8,
                               top, 18 + ord(ch) - ord("0"))
        # items (keys, powerups): bits 17..22; blink for 2s after pickup
        for i in range(6):
            if items & (1 << (17 + i)):
                t = gettime[17 + i]
                if not (t and time - t < 2 and int((time - t) * 10) & 1):
                    self._pic(fb, fbw, sx + 192 + i * 16, top + 8,
                              self.items[i])
        # sigils: serverflags folded into bits 28..31 (SV_WriteClientdata...)
        for i in range(4):
            if items & (1 << (28 + i)):
                t = gettime[28 + i]
                if not (t and time - t < 2 and int((time - t) * 10) & 1):
                    self._pic(fb, fbw, sx + SBAR_W - 32 + i * 8, top + 8,
                              self.sigils[i])

    def _status(self, fb, fbw, sx, y, st, items, time, faceanimtime):
        """Sbar_DrawNormal: the lower strip."""
        self._pic(fb, fbw, sx, y, self.sbar)
        if items & IT_INVULNERABILITY:
            self._num(fb, fbw, sx + 24, y, 666, 3, True)
            self._pic(fb, fbw, sx, y, self.disc)
        else:
            armor = st["armor"]
            self._num(fb, fbw, sx + 24, y, armor, 3, armor <= 25)
            for bit, pic in ((IT_ARMOR3, self.armor[2]),
                             (IT_ARMOR2, self.armor[1]),
                             (IT_ARMOR1, self.armor[0])):
                if items & bit:
                    self._pic(fb, fbw, sx, y, pic)
                    break
        self._face(fb, fbw, sx + 112, y, st, items, time, faceanimtime)
        health = st["health"]
        self._num(fb, fbw, sx + 136, y, health, 3, health <= 25)
        for i, bit in enumerate((IT_SHELLS, IT_NAILS, IT_ROCKETS, IT_CELLS)):
            if items & bit:
                self._pic(fb, fbw, sx + 224, y, self.ammo[i])
                break
        ammo = st["ammo"]
        self._num(fb, fbw, sx + 248, y, ammo, 3, ammo <= 10)

    def _face(self, fb, fbw, x, y, st, items, time, faceanimtime):
        """Sbar_DrawFace: powerup faces win; otherwise health tier + pain."""
        both = IT_INVISIBILITY | IT_INVULNERABILITY
        if items & both == both:
            pic = self.face_invis_invuln
        elif items & IT_QUAD:
            pic = self.face_quad
        elif items & IT_INVISIBILITY:
            pic = self.face_invis
        elif items & IT_INVULNERABILITY:
            pic = self.face_invuln
        else:
            h = st["health"]
            f = 4 if h >= 100 else max(0, h) // 20
            pic = self.faces[f][1 if time <= faceanimtime else 0]
        self._pic(fb, fbw, x, y, pic)


if __name__ == "__main__":
    from .pak import Pak
    from .wad import Wad
    sb = Sbar(Wad(Pak("quake-shareware/id1/pak0.pak").read("gfx.wad")))
    fb = bytearray(320 * 200)
    sb.draw(fb, 320, 200, {"health": 100, "armor": 50, "ammo": 25,
                           "shells": 25, "nails": 0, "rockets": 0,
                           "cells": 0, "items": IT_SHOTGUN | IT_SHELLS |
                           IT_ARMOR1, "weapon_bit": IT_SHOTGUN},
            10.0, [0.0] * 32, 0.0)
    print("composited", sum(1 for b in fb if b), "non-zero pixels")
```

While implementing, diff each method against `sbar.c` (`Sbar_DrawInventory`
~line 480, `Sbar_DrawNormal`/`Sbar_DrawFace` ~line 880-960, `Sbar_DrawNum`
~line 300). Two deliberate deviations to note in the docstring if kept:
(a) `sbar.c`'s item/sigil flash reuses the weapon loop's leftover `flashon`
variable (an id quirk) — we blink on `int((time-t)*10) & 1`; (b) WinQuake's
`Sbar_DrawCharacter` has the >320 centring commented out (ammo counts
misalign at wide resolutions) — we centre them with everything else.

- [ ] **Step 4: Run test to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_sbar.py`
Expected: `OK`. If a pixel assertion fails, check the layout constant
against `sbar.c` before touching the test — the test encodes the C layout.

- [ ] **Step 5: Commit**

```bash
git add quake/sbar.py tests/test_sbar.py
git commit -m "sbar: classic status-bar compositor (ports sbar.c Sbar_Draw)"
```

---

### Task 3: Raw HUD fields from the server (`sv.hud_status`)

**Files:**
- Modify: `quake/sv.py` (`hud_status`, ~line 2260)
- Test: `tests/test_sbar_client.py` (new — grows in Task 5)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sbar_client.py`:

```python
"""Full-stack tests for the sprite status bar: raw hud_status fields, the
viewport shrink, the framebuffer composite and the text-HUD fallback.
Boots the shareware stack like the other client tests."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import client
from quake.sbar import IT_SHOTGUN


def test_hud_status_raw_fields():
    c = client.Client("e1m1")
    st = c.sv.hud_status()
    assert st["items"] & IT_SHOTGUN          # spawn gives the shotgun
    assert st["weapon_bit"] == IT_SHOTGUN    # ...and selects it
    assert isinstance(st["items"], int)
    # e1m1 sets no serverflags yet: sigil bits 28..31 clear at spawn
    assert st["items"] >> 28 == 0
    # existing text-HUD keys are untouched
    assert st["weapon"] == "shotgun" and "health" in st


if __name__ == "__main__":
    test_hud_status_raw_fields()
    print("OK")
```

(Check the existing `_WEAPON_NAMES` table in `quake/sv.py` for the exact
string — adjust `"shotgun"` to match it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_sbar_client.py`
Expected: `KeyError: 'items'`

- [ ] **Step 3: Implement**

In `quake/sv.py` `hud_status()`, the returned dict gains two keys (existing
keys unchanged). `items` folds the episode sigils into bits 28..31 exactly
as `SV_WriteClientdataToMessage` does
(`bits | ((int)pr_global_struct->serverflags << 28)`):

```python
            "items": items | ((int(self.gget_f("serverflags")) & 15) << 28),
            "weapon_bit": g("weapon"),       # QC .weapon is the raw IT_ bit
```

(`items` is already read at the top of `hud_status`; `gget_f` is the
existing global getter used for `serverflags` elsewhere in the file.)

- [ ] **Step 4: Run tests**

Run: `PQ_AUDIO=0 python tests/test_sbar_client.py` → `OK`
Run: `PQ_AUDIO=0 python tests/test_hud_items.py` → `OK` (existing consumers unaffected)

- [ ] **Step 5: Commit**

```bash
git add quake/sv.py tests/test_sbar_client.py
git commit -m "sv: hud_status exposes raw items/weapon_bit with sigils folded in (SV_WriteClientdataToMessage)"
```

---

### Task 4: Viewport shrink in the renderer (`sbar_lines`)

**Files:**
- Modify: `quake/render.py` (`__init__` ~line 344, `_setup_zbuf` ~line 1041)
- Test: append to `tests/test_sbar_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sbar_client.py` (and to its `__main__` block):

```python
def test_renderer_sbar_lines_shrinks_view():
    from quake.pak import Pak
    from quake.bsp import Bsp
    from quake.render import Renderer
    pak = Pak("quake-shareware/id1/pak0.pak")
    pal = pak.read("gfx/palette.lmp")
    palette = [(pal[i*3], pal[i*3+1], pal[i*3+2]) for i in range(256)]
    rend = Renderer(Bsp(pak.read("maps/e1m1.bsp")), palette)
    rend.video_res = (320, 200)
    rend.sbar_lines = 48
    rend.resize(800, 600)
    assert rend.zw == 320 and rend.zh == 152      # view above the bar
    assert len(rend._zb_far) == 320 * 152
    rend.sbar_lines = 0
    rend.resize(800, 600)
    assert rend.zh == 200                          # full height again
    # auto mode shrinks the window-derived size the same way
    rend.video_res = None
    rend.zbuf_scale = 2
    rend.sbar_lines = 48
    rend.resize(800, 600)
    assert rend.zw == 400 and rend.zh == 300 - 48
```

(Match the `Renderer(...)` constructor signature used in
`tests/test_video_menu.py` — it may take the colormap too.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_sbar_client.py`
Expected: FAIL — `rend.zh == 200`, the shrink doesn't exist yet.

- [ ] **Step 3: Implement**

In `Renderer.__init__` next to `self.video_res = None` (~line 344):

```python
        self.sbar_lines = 0   # framebuffer rows reserved below the 3D view
                              # for the status bar (screen.c sb_lines); the
                              # client composites the bar into them
```

In `_setup_zbuf` (~line 1046), after `self.zw`/`self.zh` are computed from
`video_res` or the window:

```python
        if self.sbar_lines and self.zh > self.sbar_lines:
            # R_SetVrect: the 3D view renders above the status bar rows; the
            # buffers below are sized to the view, the client appends the bar
            self.zh -= self.sbar_lines
```

Nothing else changes — `_bg_frame`, `_zb_far`, `EdgeRaster` and the
projection centre (`hh = ih * 0.5` in `render_zbuffer`) all derive from
`self.zh`, so the whole rasteriser follows.

- [ ] **Step 4: Run tests**

Run: `PQ_AUDIO=0 python tests/test_sbar_client.py` → `OK`
Run: `PQ_AUDIO=0 python tests/test_video_menu.py` → `OK` (sbar_lines defaults to 0: no behaviour change)

- [ ] **Step 5: Commit**

```bash
git add quake/render.py tests/test_sbar_client.py
git commit -m "render: sbar_lines reserves status-bar rows below the 3D view (R_SetVrect)"
```

---

### Task 5: Client wiring — timers, composite, fallback, 320×200 default

**Files:**
- Modify: `client.py` (imports; `VIDEO_MODES`/`DEFAULT_VIDEO_RES` ~line 50; `__init__` ~line 152-234; `_update_view_feel` damage block ~line 371-399; `frame()` zbuf branch ~line 1048-1066 and status-bar overlay ~line 1135)
- Modify: `tests/test_video_menu.py` (default-res assertions)
- Test: append to `tests/test_sbar_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sbar_client.py` (and the `__main__` block):

```python
def _boot_zbuf(res):
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "zbuf"
    c.set_video_res(res)
    return c


def _overlay_text(rf):
    return " ".join(o[2] for o in rf.overlays)


def test_default_video_res_is_320x200():
    assert client.DEFAULT_VIDEO_RES == (320, 200)
    assert ("320x200", (320, 200)) in client.VIDEO_MODES


def test_sprite_bar_composited_at_320x200():
    c = _boot_zbuf((320, 200))
    rf = c.frame(0.016, client.InputState())
    fb, w, h = rf.framebuffer
    assert (w, h) == (320, 200)                  # full screen incl. bar rows
    assert c.rend.zh == 152                      # 3D view shrunk above it
    # the sbar strip landed: compare the untouched column (Task 2's trick)
    from quake.pak import Pak
    from quake.wad import Wad
    wad = Wad(Pak("quake-shareware/id1/pak0.pak").read("gfx.wad"))
    sw, sh, spx = wad.qpic("sbar")
    assert all(fb[(200 - 24 + r) * 320 + 210] == spx[r * sw + 210]
               for r in range(sh))
    # text status bar suppressed (diagnostics HUD line stays)
    assert "HEALTH" not in _overlay_text(rf)


def test_narrow_res_falls_back_to_text():
    c = _boot_zbuf((240, 160))
    rf = c.frame(0.016, client.InputState())
    fb, w, h = rf.framebuffer
    assert (w, h) == (240, 160)
    assert c.rend.sbar_lines == 0 and c.rend.zh == 160
    assert "HEALTH" in _overlay_text(rf)


def test_wire_mode_keeps_text_bar():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "wire"
    rf = c.frame(0.016, client.InputState())
    assert "HEALTH" in _overlay_text(rf)


def test_pain_face_timer_set_on_damage():
    c = _boot_zbuf((320, 200))
    c.frame(0.016, client.InputState())
    t0 = c.faceanimtime
    # stamp damage on the player edict the way T_Damage does, then frame
    vm, f, e = c.sv.vm, c.sv.f, c.sv.player
    vm.fset_f(e, f["dmg_take"], 10.0)
    c.frame(0.016, client.InputState())
    assert c.faceanimtime > t0
    assert c.faceanimtime <= c.sv.time + 0.2 + 1e-6
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PQ_AUDIO=0 python tests/test_sbar_client.py`
Expected: first new failure is `test_default_video_res_is_320x200`
(`DEFAULT_VIDEO_RES == (240, 160)`).

- [ ] **Step 3: Implement**

All in `client.py`:

**(a) Constants** (~line 50): add the mode and change the default —

```python
VIDEO_MODES = [("Auto", None), ("80x40", (80, 40)), ("160x80", (160, 80)),
               ("240x160", (240, 160)), ("320x200", (320, 200)),
               ("320x240", (320, 240)), ("640x480", (640, 480))]
DEFAULT_VIDEO_RES = (320, 200)        # classic: the 320-wide sbar fits exactly
```

**(b) Imports** (top of file, with the other `quake.` imports):

```python
from quake.wad import Wad
from quake.sbar import Sbar, SBAR_LINES
```

**(c) `__init__`** — after the palette/colormap reads (~line 159):

```python
        # classic sprite status bar (sbar.c), drawn in zbuf mode when the
        # framebuffer is >=320 wide; plus the two timers cl_parse.c keeps
        # client-side: per-item pickup times (flash anims) and the pain face
        self.sbar = Sbar(Wad(self.pak.read("gfx.wad")))
        self.item_gettime = [0.0] * 32
        self._prev_items = 0
        self.faceanimtime = 0.0
```

**(d) Pain-face timer** — in `_update_view_feel`'s damage branch (~line 380,
inside `if count:` where `dmg[3]` and the kick are set, alongside them):

```python
                self.faceanimtime = self.sv.time + 0.2   # V_ParseDamage
```

**(e) `frame()`** — three changes.

First, near the top of the zbuf-relevant section (right before
`PROFILER.begin("render")`, ~line 1048), sync the reserved rows and fetch
the status once (it is currently fetched later as `st = self.sv.hud_status()`
in the overlay block, ~line 1135 — move that fetch up here and reuse the
variable below; don't fetch twice):

```python
        # sprite status bar (sbar.c): zbuf mode with a >=320-wide screen.
        # Sync the renderer's reserved rows here so every path that changes
        # mode/resolution/zbuf_scale self-heals on the next frame.
        st = self.sv.hud_status()
        screen_w = (self.video_res[0] if self.video_res
                    else max(1, self._view_wh[0] // self.rend.zbuf_scale))
        sbar_lines = SBAR_LINES if (self.mode == "zbuf"
                                    and screen_w >= 320) else 0
        if self.rend.sbar_lines != sbar_lines:
            self.rend.sbar_lines = sbar_lines
            self.rend.resize(self.rend.width, self.rend.height)
        if st:
            items = st["items"]
            if items != self._prev_items:        # CL_ParseClientdata
                for j in range(32):
                    if items & (1 << j) and not self._prev_items & (1 << j):
                        self.item_gettime[j] = self.sv.time
                self._prev_items = items
```

Second, in the zbuf branch right after `framebuffer = fbdata` /
`nprim = fbdata[1] * fbdata[2]` (~line 1065), append the bar rows and
composite:

```python
            if self.rend.sbar_lines:
                fb, w, vh = fbdata
                fb.extend(bytes(w * self.rend.sbar_lines))   # the bar rows
                full_h = vh + self.rend.sbar_lines
                if st:
                    self.sbar.draw(fb, w, full_h, st, self.sv.time,
                                   self.item_gettime, self.faceanimtime)
                framebuffer = fbdata = (fb, w, full_h)
```

(`fb` is the bytearray `render_zbuffer` built from `_bg_frame`, so
`extend` is fine. With no player `st` is None and the rows stay background —
matches the no-player text-HUD behaviour.)

Third, the text status bar (~line 1135): change `st = self.sv.hud_status()`
to reuse the variable and skip when the sprite bar drew:

```python
        if st and not self.rend.sbar_lines:
```

(keep the body unchanged).

- [ ] **Step 4: Update `tests/test_video_menu.py` for the new default**

`test_client_default_video_res_is_240x160` becomes:

```python
def test_client_default_video_res_is_320x200():
    c = Client("e1m1")
    assert c.video_res == (320, 200)
    assert c.rend.video_res == (320, 200)
    assert c.rend.zw == 320 and c.rend.zh == 200   # sbar_lines syncs in frame()
```

Rename it in the `__main__` block too. Check the rest of the file for other
`(240, 160)` default assumptions (`test_video_res_persists_across_map_change`
sets its own res explicitly — fine).

- [ ] **Step 5: Run tests**

Run: `PQ_AUDIO=0 python tests/test_sbar_client.py` → `OK`
Run: `PQ_AUDIO=0 python tests/test_video_menu.py` → `OK`

- [ ] **Step 6: Run the full suite**

Run: `export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null || echo "FAIL $t"; done`
Expected: no FAIL lines. Likely candidates if something breaks: tests that
run zbuf frames and assert framebuffer sizes (`test_fb_scale.py`,
`test_particles_zbuf.py`, `test_zbuffer_raster.py` goldens — these set their
own video_res/mode; the sprite bar only activates at ≥320 wide in zbuf mode,
so they should pass untouched. Investigate, don't paper over.)

- [ ] **Step 7: Commit**

```bash
git add client.py tests/test_sbar_client.py tests/test_video_menu.py
git commit -m "client: classic sprite status bar in zbuf mode (sbar+ibar, viewport shrink, 320x200 default)"
```

---

### Task 6: Eyeball it

- [ ] **Step 1: Run the game**

Run: `python main.py e1m1` (Cocoa frontend on this Mac). Verify: classic bar
at the bottom in textured mode; shotgun slot lit (INV2), ammo counts in gold
digits; face takes damage (pain flash) and degrades with health; armor
icon/number after picking up armor; keys/sigils appear; menu → Resolution
240x160 falls back to the text bar; wire/flat modes (F/Z keys) keep the text
bar; the 3D view is not vertically squashed (the shrink changed the buffer,
not the aspect — the letterbox in the frontend handles it).

- [ ] **Step 2: Check the perf HUD**

P-key HUD: confirm the `render` bucket didn't grow materially (the bar is
~15k mostly-slice-copied pixels; the spec defers any caching until the
profiler says otherwise).

- [ ] **Step 3: Final commit (if any fixups)**

```bash
git add -A && git commit -m "sbar: fixups from manual run"
```
