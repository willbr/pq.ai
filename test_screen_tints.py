"""Screen colour shifts (V_UpdatePalette / V_CalcPowerupCshift, view.c).

The four cshifts -- contents (water/slime/lava), damage (red, fed by the
player's dmg_take/dmg_save like svc_damage, decaying 150/s), bonus (gold on
items.qc's stuffcmd "bf", decaying 100/s) and powerup (quad/suit/ring/pent
from .items) -- blend the base palette into Client.view_palette each frame,
which the zbuf RenderFrame carries for the frontend's LUT.
"""

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from quake.sv import CONTENTS_WATER

IT_QUAD = 4194304


def _boot():
    c = client.Client("e1m1")
    c.resize(320, 240)
    return c


def test_quad_tints_blue_and_clears():
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    inp = client.InputState()
    c.frame(0.05, inp)
    assert c.view_palette is c.palette          # no shift active

    items = int(vm.fget_f(e, f["items"]))
    vm.fset_f(e, f["items"], float(items | IT_QUAD))
    c.frame(0.05, inp)
    # palette entry 0 is black: quad (0,0,255 @ 30) blends 30*255>>8 blue in
    assert c.view_palette is not c.palette
    assert c.view_palette[0][2] > c.palette[0][2]

    vm.fset_f(e, f["items"], float(items))
    c.frame(0.05, inp)
    assert c.view_palette is c.palette


def test_damage_flash_reddens_then_decays():
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    inp = client.InputState()
    vm.fset_f(e, f["dmg_take"], 20.0)           # T_Damage stamped the edict
    c.frame(0.05, inp)
    assert vm.fget_f(e, f["dmg_take"]) == 0.0, "damage signal not consumed"
    assert c.view_palette[0][0] > c.palette[0][0], "no red flash"
    for _ in range(30):                         # 1.5 s: 60% decays at 150/s
        c.frame(0.05, inp)
    assert c.view_palette is c.palette, "damage flash never decayed"


def test_bonus_flash_on_pickup_stuffcmd():
    c = _boot()
    inp = client.InputState()
    c.sv.bonus_flash = True                     # as stuffcmd(player, "bf\n")
    c.frame(0.05, inp)
    p = c.view_palette[0]
    assert p[0] > 0 and p[1] > 0, "no gold bonus flash"
    for _ in range(15):                         # 50% at 100/s -> gone in .5s
        c.frame(0.05, inp)
    assert c.view_palette is c.palette


def test_water_contents_tint():
    c = _boot()
    c.watertype = CONTENTS_WATER
    c._update_palette(0.05)
    r, g, b = c.view_palette[0]
    assert (r, g, b) == (65, 40, 25)            # 128/256 of (130, 80, 50)


def test_zbuf_renderframe_carries_view_palette():
    c = _boot()
    c.mode = "zbuf"
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    items = int(vm.fget_f(e, f["items"]))
    vm.fset_f(e, f["items"], float(items | IT_QUAD))
    rf = c.frame(0.05, client.InputState())
    assert rf.palette is c.view_palette
    assert rf.palette_version == c.palette_version


if __name__ == "__main__":
    test_quad_tints_blue_and_clears()
    test_damage_flash_reddens_then_decays()
    test_bonus_flash_on_pickup_stuffcmd()
    test_water_contents_tint()
    test_zbuf_renderframe_carries_view_palette()
    print("OK")
