"""Player jump debounce and water (WaterMove) wiring, matching Quake.

Jump: client.qc PlayerJump refuses to fire unless FL_JUMPRELEASED is set ("don't
pogo stick") and clears it on each jump; the flag is re-set whenever the jump
button is up. The engine owns the jump impulse (physics.player_move), so it
mirrors that latch -- holding jump must yield exactly one hop.

Water: in NetQuake the engine stamps the player's waterlevel/watertype and the QC
WaterMove (part of PlayerPreThink) handles the rest -- enter/leave sounds,
drowning, lava/slime damage and the FL_INWATER flag. The engine never ran
WaterMove and never fed it waterlevel, so none of that happened. Now
update_player_water stamps the edict and run_water_move drives the QC function.

Driven against the real shareware progs on e1m1.
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server, FL_INWATER
from quake.physics import Physics, CONTENTS_WATER, CONTENTS_EMPTY, JUMPSPEED, GRAVITY

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname="maps/e1m1.bsp", skill=1)
    sv.phys = Physics(b)
    sv.load_level()
    sv.spawn_player((480.0, -352.0, 88.0), (0.0, 0.0, 0.0))
    return sv


def _jump_step(ph, want_jump):
    """One player_move on (forced) flat ground in open air; return resulting
    vertical velocity. A fired jump leaves ~JUMPSPEED - GRAVITY*dt (large
    positive); a suppressed one leaves ~-GRAVITY*dt (small negative)."""
    origin = [480.0, -352.0, 88.0]
    vel = [0.0, 0.0, 0.0]
    ph.player_move(origin, vel, (0.0, 0.0, 0.0), 0.0,
                   (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), 0.0, 0.0, 0.0, 320.0,
                   True, want_jump, 0.1)
    return vel[2]


def test_jump_debounce_no_pogo():
    sv = _boot()
    ph = sv.phys
    jumped = JUMPSPEED - GRAVITY * 0.1
    threshold = jumped * 0.5            # comfortably above any gravity-only result

    # First press: armed at boot -> jumps.
    assert _jump_step(ph, want_jump=True) > threshold, "first jump didn't fire"
    # Held down across frames: must NOT re-fire (no pogo-sticking). A suppressed
    # frame leaves no jump impulse -- gravity is clipped to 0 by the ground.
    assert _jump_step(ph, want_jump=True) < threshold, "held jump re-fired (pogo)"
    assert _jump_step(ph, want_jump=True) < threshold, "held jump re-fired (pogo)"
    # Release re-arms; next press jumps again.
    assert _jump_step(ph, want_jump=False) < threshold
    assert _jump_step(ph, want_jump=True) > threshold, "re-press after release didn't jump"


def test_jump_impulse_is_additive():
    """PM_Jump does velocity[2] += 270, not = 270: jumping with existing upward
    speed (e.g. off an ascending mover) keeps it. With a riser velocity the
    resulting vertical speed must exceed a from-rest jump by about that amount."""
    sv = _boot()
    ph = sv.phys
    riser = 100.0
    origin = [480.0, -352.0, 88.0]
    vel = [0.0, 0.0, riser]
    ph.player_move(origin, vel, (0.0, 0.0, 0.0), 0.0,
                   (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), 0.0, 0.0, 0.0, 320.0,
                   True, True, 0.1)
    from_rest = JUMPSPEED - GRAVITY * 0.1
    assert vel[2] > from_rest + riser * 0.5, \
        f"jump not additive: vel_z={vel[2]:.1f} (rest jump ~{from_rest:.1f})"


class _SoundRecorder:
    def __init__(self):
        self.samples = []

    def start_sound(self, ent, chan, sample, vol, atten, origin):
        self.samples.append(sample)


def test_jump_plays_sound():
    """client.qc PlayerJump plays sound(self, CHAN_BODY, "player/plyrjmp8.wav",
    1, ATTN_NORM). The engine owns the jump impulse, so it must also own the
    sound: exactly one per hop -- held jump (debounced) and jumpless frames
    stay silent."""
    from client import Client, InputState
    c = Client("e1m1")
    rec = _SoundRecorder()
    c.sv.snd = rec

    def jumps():
        return len([s for s in rec.samples if s == "player/plyrjmp8.wav"])

    c.onground = True
    c._move(0.1, InputState(move_up=1.0))           # press: jump fires
    assert jumps() == 1, "no jump sound on jump"
    c.onground = True
    c._move(0.1, InputState(move_up=1.0))           # held: debounced, no hop
    assert jumps() == 1, "jump sound re-fired while held"
    c.onground = True
    c._move(0.1, InputState())                      # release re-arms
    c._move(0.1, InputState(move_up=1.0))           # airborne press: no hop
    assert jumps() == 1, "jump sound played while airborne"


def test_no_drowning_gasp_on_spawn():
    """A freshly spawned player is on dry land with a full lungful of air, so the
    first WaterMove must not play the drowning gasp. (PutClientInServer sets
    air_finished = time + 12; without it air_finished == 0 < time triggers gasp2
    the instant the level loads.)"""
    sv = _boot()
    rec = _SoundRecorder()
    sv.snd = rec
    sv.run_frame(0.1)
    gasps = [s for s in rec.samples if "gasp" in s]
    assert not gasps, f"drowning gasp played on spawn: {gasps}"


def test_watermove_toggles_inwater_flag():
    sv = _boot()
    vm, f, p = sv.vm, sv.f, sv.player
    assert vm.fget_f(p, f["health"]) > 0
    assert not (int(vm.fget_f(p, f["flags"])) & FL_INWATER)

    # Submerge: the QC WaterMove should set FL_INWATER this frame.
    sv.update_player_water(3, CONTENTS_WATER)
    sv.run_frame(0.1)
    assert int(vm.fget_f(p, f["waterlevel"])) == 3
    assert int(vm.fget_f(p, f["flags"])) & FL_INWATER, \
        "WaterMove did not set FL_INWATER underwater"

    # Surface: WaterMove should clear it (and play the leave-water sound).
    sv.update_player_water(0, CONTENTS_EMPTY)
    sv.run_frame(0.1)
    assert not (int(vm.fget_f(p, f["flags"])) & FL_INWATER), \
        "WaterMove did not clear FL_INWATER after leaving water"


if __name__ == "__main__":
    test_jump_debounce_no_pogo()
    test_jump_impulse_is_additive()
    test_jump_plays_sound()
    test_no_drowning_gasp_on_spawn()
    test_watermove_toggles_inwater_flag()
    print("OK")
