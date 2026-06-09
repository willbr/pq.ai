"""Regression test: bonus items spin (client-side rotation).

Bug: world .mdl pickups (weapons, keys, powerups, armor) stood still. In Quake
these models carry the EF_ROTATE (8) flag in their .mdl header, and the client
overrides their yaw every frame with anglemod(100*cl.time) -- see
WinQuake/cl_main.c (`if (ent->model->flags & EF_ROTATE) ent->angles[1] =
bobjrotate;`). This engine never parsed the header flags nor applied the spin,
so items rendered with their static spawn angle.

Covers two seams of the fix:
  1. Mdl parses the header `flags` field; rotating items have EF_ROTATE set,
     monsters (e.g. army.mdl) do not.
  2. spin_yaw() overrides a rotating model's yaw with the time-based bonus-item
     rotation, and leaves non-rotating models untouched.
"""

from quake.pak import Pak
from quake.mdl import Mdl, EF_ROTATE
from client import spin_yaw
from quake.sv import anglemod

PAK = "quake-shareware/id1/pak0.pak"


def test_mdl_parses_rotate_flag():
    pak = Pak(PAK)

    def flags(name):
        return Mdl(pak.read(name)).flags

    # bonus items rotate client-side
    assert flags("progs/g_shot.mdl") & EF_ROTATE
    assert flags("progs/quaddama.mdl") & EF_ROTATE
    # a monster does not
    assert not (flags("progs/soldier.mdl") & EF_ROTATE)


def test_spin_yaw_overrides_only_rotating_models():
    spawn = (0.0, 17.0, 0.0)        # pitch, yaw, roll -- yaw 17 from the spawn

    # non-rotating model keeps its spawn angles exactly
    assert spin_yaw(0, spawn, 1.234) == spawn

    # rotating model: pitch/roll preserved, yaw becomes the time-based spin
    out = spin_yaw(EF_ROTATE, spawn, 1.234)
    assert out[0] == spawn[0] and out[2] == spawn[2]
    assert out[1] == anglemod(100.0 * 1.234)

    # and it actually changes over time (it spins, not just a constant offset)
    assert spin_yaw(EF_ROTATE, spawn, 1.0)[1] != spin_yaw(EF_ROTATE, spawn, 2.0)[1]


if __name__ == "__main__":
    test_mdl_parses_rotate_flag()
    test_spin_yaw_overrides_only_rotating_models()
    print("OK")
