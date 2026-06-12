"""View feel (view.c / sv_user.c): strafe roll, weapon punch, damage kick,
stair-step eye smoothing.

V_CalcRoll leans the camera cl_rollangle (2 deg) into strafe velocity;
.punchangle (set by QC on weapon fire) is added to the view angles and decays
10 deg/s server-side (DropPunchAngle, sv_user.c); taking damage kicks
pitch/roll by 0.6*count toward the inflictor for v_kicktime (0.5s); and the
eye's z lags stair steps by rising 80 u/s (max 12 behind). The blended angles
land in Client.view_angles (pitch, yaw, roll) each frame and the roll reaches
the renderers via angle_vectors(yaw, pitch, roll).
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from quake.render import angle_vectors


def _boot():
    c = client.Client("e1m1")
    c.resize(320, 240)
    return c


def test_strafe_roll_leans_into_velocity():
    c = _boot()
    inp = client.InputState()
    c.frame(0.05, inp)
    assert abs(c.view_angles[2]) < 1e-6         # standing still: no roll

    fwd, right, up = angle_vectors(c.yaw, 0.0)
    c.vel = [right[0] * 400.0, right[1] * 400.0, 0.0]   # full-speed strafe
    c.frame(0.05, inp)
    assert abs(c.view_angles[2] - 2.0) < 1e-6, \
        f"expected full 2.0 deg lean, got {c.view_angles[2]}"


def test_punchangle_kicks_and_decays():
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    inp = client.InputState()
    vm.fset_v(e, f["punchangle"], (-2.0, 0.0, 0.0))     # as W_FireShotgun
    c.frame(0.016, inp)
    assert c.view_angles[0] < c.pitch, "punch did not kick the view up"
    for _ in range(30):                                 # DropPunchAngle 10/s
        c.frame(0.05, inp)
    assert vm.fget_v(e, f["punchangle"])[0] == 0.0, "punch never decayed"


def test_damage_kick_rolls_toward_inflictor_then_decays():
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    inp = client.InputState()
    # an inflictor square to the player's right: full roll kick
    fwd, right, up = angle_vectors(c.yaw, 0.0)
    ox, oy, oz = vm.fget_v(e, f["origin"])
    src = c.sv.vm.alloc_edict()
    vm.fset_v(src, f["origin"], (ox + right[0] * 100.0,
                                 oy + right[1] * 100.0, oz))
    vm.fset_f(e, f["dmg_take"], 20.0)
    vm.fset_i(e, f["dmg_inflictor"], src)
    c.frame(0.016, inp)
    assert abs(c.view_angles[2]) > 1.0, "damage did not roll the view"
    for _ in range(15):                                 # v_kicktime = 0.5 s
        c.frame(0.05, inp)
    assert abs(c.view_angles[2]) < 1e-6, "damage kick never decayed"


def test_stair_step_smooths_eye_z():
    # unit-level: frame()'s _move re-derives onground/pos, so drive the
    # V_CalcRefdef oldz port directly
    c = _boot()
    c._update_view_feel(0.05, False)                    # seed oldz at rest
    c.onground = True
    c.pos[2] += 16.0                                    # popped up a stair
    c._update_view_feel(0.05, False)
    # the smoothed eye rises 80 u/s (4 u this frame), so it lags the pop,
    # clamped to at most 12 units behind
    assert -12.0 <= c.eye_z_offset <= -11.9, f"got {c.eye_z_offset}"
    for _ in range(10):
        c._update_view_feel(0.05, False)
    assert abs(c.eye_z_offset) < 1e-6, "eye never caught up"


def test_dead_view_rolls_to_80():
    """Death cam: V_CalcViewRoll forces ROLL = 80 once health <= 0 (view.c:824),
    so the view tilts onto its side. punchangle is still added on top
    (view.c:958, applied regardless of death), and the eye drops to the corpse's
    view_ofs z = -8 set by PlayerDie. Pins the behaviour that already matched the
    reference, so it can't silently regress."""
    c = _boot()
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    inp = client.InputState()
    vm.fset_f(e, f["health"], -10.0)
    c.sv.gset_f("time", c.sv.time); c.sv.gset_i("self", e); c.sv.gset_i("other", e)
    vm.execute(vm.fget_i(e, f["th_die"]))
    for _ in range(3):
        c.frame(0.05, inp)
    assert c.sv.player_health() <= 0, "player did not die"
    assert abs(c.view_angles[2] - 80.0) < 1e-6, \
        f"dead view roll should be 80, got {c.view_angles[2]}"
    # punchangle still rides on top of the death roll (it decays ~0.16 deg over
    # the frame via DropPunchAngle, so ~84.84 rather than a clean 85)
    vm.fset_v(e, f["punchangle"], (0.0, 0.0, 5.0))
    c.frame(0.016, inp)
    assert 84.0 < c.view_angles[2] < 85.0, \
        f"punchangle should add to the 80 deg death roll, got {c.view_angles[2]}"


def test_angle_vectors_roll_tilts_the_basis():
    f0, r0, u0 = angle_vectors(0.0, 0.0)
    f1, r1, u1 = angle_vectors(0.0, 0.0, 90.0)
    assert all(abs(a - b) < 1e-9 for a, b in zip(f0, f1))   # forward unchanged
    # rolled 90 deg: right becomes -up, up becomes right
    assert all(abs(r1[i] - (-u0[i])) < 1e-9 for i in range(3))
    assert all(abs(u1[i] - r0[i]) < 1e-9 for i in range(3))


if __name__ == "__main__":
    test_strafe_roll_leans_into_velocity()
    test_punchangle_kicks_and_decays()
    test_damage_kick_rolls_toward_inflictor_then_decays()
    test_stair_step_smooths_eye_z()
    test_dead_view_rolls_to_80()
    test_angle_vectors_roll_tilts_the_basis()
    print("OK")
