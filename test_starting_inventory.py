"""Regression test: the player starts with Quake's default loadout.

Quake's SetNewParms (client.qc) gives a fresh player only the Axe and Shotgun
with 25 shells -- not a full arsenal:

    parm1 = IT_SHOTGUN | IT_AXE;   // items
    parm4 = 25;                    // ammo_shells
    parm8 = 1;                     // weapon = IT_SHOTGUN
    (nails / rockets / cells = 0)

W_SetCurrentAmmo then ORs in the IT_SHELLS ammo-type bit and sets currentammo
to the shell count, so after spawn the player has shotgun + axe, 25 shells, and
no other weapons or ammo.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import (Server, IT_SHOTGUN, IT_AXE, IT_SHELLS, IT_SUPER_SHOTGUN,
                IT_NAILGUN, IT_GRENADE_LAUNCHER, IT_ROCKET_LAUNCHER,
                IT_LIGHTNING, IT_NAILS, IT_ROCKETS, IT_CELLS)

PAK = "quake-shareware/id1/pak0.pak"


def test_player_starts_with_axe_shotgun_25_shells():
    pak = Pak(PAK)
    sv = Server(Progs(pak.read("progs.dat")),
                bsp=Bsp(pak.read("maps/e1m1.bsp")),
                mapname="maps/e1m1.bsp", skill=1)
    sv.load_level()
    sv.spawn_player((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))

    g = lambda name: sv.vm.fget_f(sv.player, sv.f[name])
    items = int(g("items"))

    # owns exactly shotgun + axe (+ the IT_SHELLS active-ammo bit)
    assert items & IT_SHOTGUN
    assert items & IT_AXE
    # none of the other weapons
    for bit in (IT_SUPER_SHOTGUN, IT_NAILGUN, IT_GRENADE_LAUNCHER,
                IT_ROCKET_LAUNCHER, IT_LIGHTNING):
        assert not (items & bit), f"should not own weapon bit {bit}"
    # no non-shell ammo-type bits
    for bit in (IT_NAILS, IT_ROCKETS, IT_CELLS):
        assert not (items & bit), f"should not have ammo bit {bit}"

    assert int(g("weapon")) == IT_SHOTGUN
    assert g("ammo_shells") == 25.0
    assert g("ammo_nails") == 0.0
    assert g("ammo_rockets") == 0.0
    assert g("ammo_cells") == 0.0
    assert g("currentammo") == 25.0


if __name__ == "__main__":
    test_player_starts_with_axe_shotgun_25_shells()
    print("OK")
