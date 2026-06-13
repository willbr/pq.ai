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


def test_intermission_overlay_draws_pics():
    """Sbar_IntermissionOverlay: the 'complete' title + 'inter' label pics and
    the big 24x24 digit pics for time/secrets/kills -- NOT the conchars font."""
    from quake.conchars import load_qpic
    pak = Pak("quake-shareware/id1/pak0.pak")
    sb = Sbar(Wad(pak.read("gfx.wad")))
    complete = load_qpic(pak.read("gfx/complete.lmp"))   # 192x24 opaque title
    inter = load_qpic(pak.read("gfx/inter.lmp"))         # 160x144 label panel
    fb = bytearray(bytes((BG,)) * (W * H))
    ist = {"time": 83, "secrets": 2, "total_secrets": 4,   # 83s -> 1:23
           "monsters": 15, "total_monsters": 30}
    sb.intermission_overlay(fb, W, H, ist, complete, inter)
    sx = (W - 320) // 2                                    # 0 at 320 wide
    # title + label pics land at id's coords
    _assert_pic_at(fb, W, sx + 64, 24, complete, "complete title")
    _assert_pic_at(fb, W, sx + 0, 56, inter, "inter labels")
    # time 1:23 -- IntermissionNumber(160,64,1,3): 1 digit, x += (3-1)*24 -> 208
    _assert_pic_at(fb, W, sx + 208, 64, sb.nums[0][1], "time minutes '1'")
    _assert_pic_at(fb, W, sx + 234, 64, sb.colon, "time colon")
    _assert_pic_at(fb, W, sx + 246, 64, sb.nums[0][2], "seconds tens '2'")
    _assert_pic_at(fb, W, sx + 266, 64, sb.nums[0][3], "seconds ones '3'")
    # secrets 2 / 4 at row 104; slash at 232
    _assert_pic_at(fb, W, sx + 232, 104, sb.slash, "secrets slash")
    _assert_pic_at(fb, W, sx + 208, 104, sb.nums[0][2], "secrets found '2'")
    _assert_pic_at(fb, W, sx + 288, 104, sb.nums[0][4], "secrets total '4'")


if __name__ == "__main__":
    test_strips_on_the_right_rows()
    test_face_tiers_and_pain()
    test_red_digits_when_low()
    test_weapon_slot_states()
    test_invulnerability_face_and_666()
    test_transparency_keeps_strip_pixels()
    test_backtile_fills_margins_on_wide_buffers()
    test_intermission_overlay_draws_pics()
    print("OK")
