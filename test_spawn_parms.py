"""Inventory carry-over between levels (spawn parms).

Quake persists a player's loadout across changelevel through 16 float "spawn
parms": SV_SaveSpawnparms (sv_main.c) runs QC SetChangeParms with self = the
player, which strips keys/powerups, caps health and writes parm1..parm9; the
host stashes the parm globals. When the next level spawns the player, the
engine writes the stash back into parm1..16 and QC DecodeLevelParms restores
items/health/armor/ammo/weapon from them. A fresh game instead runs
SetNewParms (axe + shotgun, 25 shells).
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import (Server, IT_SHOTGUN, IT_AXE, IT_NAILGUN, IT_NAILS,
                      IT_SHELLS)

PAK = "quake-shareware/id1/pak0.pak"

# defs.qc item bits SetChangeParms must strip (keys + powerups)
IT_KEY1 = 131072
IT_QUAD = 4194304


def _boot(mapname, **kw):
    pak = Pak(PAK)
    sv = Server(Progs(pak.read("progs.dat")),
                bsp=Bsp(pak.read(f"maps/{mapname}.bsp")),
                mapname=f"maps/{mapname}.bsp", skill=1, **kw)
    sv.load_level()
    return sv


def _give_nailgun(sv):
    """Mid-game loadout: nailgun selected, nails/shells stocked, green armor."""
    f, e = sv.f, sv.player
    items = int(sv.vm.fget_f(e, f["items"]))
    sv.vm.fset_f(e, f["items"], float(items | IT_NAILGUN))
    sv.vm.fset_f(e, f["weapon"], float(IT_NAILGUN))
    sv.vm.fset_f(e, f["ammo_nails"], 80.0)
    sv.vm.fset_f(e, f["ammo_shells"], 40.0)
    sv.vm.fset_f(e, f["armorvalue"], 100.0)
    sv.vm.fset_f(e, f["armortype"], 0.3)
    sv.vm.fset_f(e, f["health"], 77.0)


def test_save_spawn_parms_captures_inventory():
    sv = _boot("e1m1")
    sv.spawn_player((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    _give_nailgun(sv)
    # keys and powerups must not survive a level change (SetChangeParms)
    f, e = sv.f, sv.player
    items = int(sv.vm.fget_f(e, f["items"]))
    sv.vm.fset_f(e, f["items"], float(items | IT_KEY1 | IT_QUAD))
    sv.vm.fset_f(e, f["health"], 200.0)         # megahealth: capped to 100

    parms = sv.save_spawn_parms()

    assert parms is not None and len(parms) == 16
    items = int(parms[0])
    assert items & IT_NAILGUN
    assert not (items & IT_KEY1), "keys must be stripped at level change"
    assert not (items & IT_QUAD), "powerups must be stripped at level change"
    assert parms[1] == 100.0                    # health capped to 100
    assert parms[2] == 100.0                    # armorvalue
    assert parms[3] == 40.0                     # ammo_shells
    assert parms[4] == 80.0                     # ammo_nails
    assert int(parms[7]) == IT_NAILGUN          # active weapon
    assert abs(parms[8] - 30.0) < 0.5           # armortype * 100


def test_spawn_player_with_parms_restores_inventory():
    sv = _boot("e1m1")
    sv.spawn_player((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    _give_nailgun(sv)
    parms = sv.save_spawn_parms()

    sv2 = _boot("e1m2")
    sv2.spawn_player((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), parms=parms)

    g = lambda name: sv2.vm.fget_f(sv2.player, sv2.f[name])
    items = int(g("items"))
    assert items & IT_NAILGUN, "nailgun must carry into the next level"
    assert int(g("weapon")) == IT_NAILGUN
    assert g("ammo_nails") == 80.0
    assert g("ammo_shells") == 40.0
    assert g("armorvalue") == 100.0
    assert abs(g("armortype") - 0.3) < 0.01
    assert g("health") == 77.0
    # W_SetCurrentAmmo ran for the carried weapon, not the default shotgun
    assert items & IT_NAILS
    assert g("currentammo") == 80.0


def test_dead_player_carries_fresh_default_parms():
    sv = _boot("e1m1")
    sv.spawn_player((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    _give_nailgun(sv)
    sv.vm.fset_f(sv.player, sv.f["health"], 0.0)

    parms = sv.save_spawn_parms()               # SetChangeParms -> SetNewParms

    assert int(parms[0]) == IT_SHOTGUN | IT_AXE
    assert parms[1] == 100.0
    assert parms[3] == 25.0                     # shells
    assert parms[4] == 0.0                      # nails


def test_client_changelevel_carries_inventory():
    import client
    c = client.Client("e1m1")
    c.resize(320, 240)
    _give_nailgun(c.sv)
    c.sv.changelevel = "e1m2"                   # as the slipgate builtin would
    c.frame(0.016, client.InputState())         # host consumes the changelevel

    assert c.sv.mapname == "maps/e1m2.bsp"
    g = lambda name: c.sv.vm.fget_f(c.sv.player, c.sv.f[name])
    assert int(g("items")) & IT_NAILGUN
    assert g("ammo_nails") == 80.0
    assert g("health") == 77.0


if __name__ == "__main__":
    test_save_spawn_parms_captures_inventory()
    test_spawn_player_with_parms_restores_inventory()
    test_dead_player_carries_fresh_default_parms()
    test_client_changelevel_carries_inventory()
    print("OK")
