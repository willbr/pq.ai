"""E1M8 (Ziggurat Vertigo) low gravity.

id's worldspawn (qw-qc/world.qc) runs `cvar_set("sv_gravity","100")` when the
world model is maps/e1m8.bsp, else "800". Every gravity site in the original
reads the runtime cvar `sv_gravity.value` (sv_phys.c SV_AddGravity, the player
MOVETYPE_WALK path via SV_Physics_Client, the toss/step movers, the corpse-fall
hitsound threshold, and r_part.c particle droop). The port must do the same:
the cvar our cvar_set already stores has to actually drive the physics.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)
from client import Client


def test_world_gravity_cvar_reaches_physics():
    # e1m1 is normal gravity; e1m8 is the low-gravity secret level.
    assert Client("e1m1").phys.gravity == 800.0
    assert Client("e1m8").phys.gravity == 100.0


def _freefall_dz(client, gravity, dt=0.01):
    """One frame of pure player freefall (no input, off the ground) high in the
    air; returns the change in vertical velocity. SV_AddGravity applies exactly
    -sv_gravity.value * frametime before SV_WalkMove, and a 0.08-unit step
    can't reach the floor, so the delta is the gravity term alone."""
    client.sv.cvars["sv_gravity"] = gravity     # the cvar drives physics live
    pos = list(client.pos)
    pos[2] += 24.0                              # lift clear of the spawn floor
    vel = [0.0, 0.0, 0.0]
    client.phys.player_move(
        pos, vel, (0.0, 0.0, 0.0), 0.0,
        (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), 0.0, 0.0, 0.0, 320.0,
        False, False, dt)
    return vel[2]


def test_player_freefall_uses_runtime_gravity():
    c = Client("e1m1")
    assert abs(_freefall_dz(c, 800.0) - (-8.0)) < 1e-4
    assert abs(_freefall_dz(c, 100.0) - (-1.0)) < 1e-4   # 1/8 g floats the player


def test_runtime_cvar_set_is_live():
    # A mid-game change to sv_gravity (console `set`, QC cvar_set) is read live;
    # physics shares the host's cvar dict, so no propagation step can go stale.
    c = Client("e1m1")
    assert c.phys.gravity == 800.0
    c.sv.cvars["sv_gravity"] = 200.0
    assert c.phys.gravity == 200.0


if __name__ == "__main__":
    test_world_gravity_cvar_reaches_physics()
    test_player_freefall_uses_runtime_gravity()
    test_runtime_cvar_set_is_live()
    print("OK")
