"""Server-side protocol serialization: ports of WinQuake sv_main.c's
SV_CreateBaseline / SV_WriteEntitiesToClient / SV_WriteClientdataToMessage /
SV_SendClientDatagram and the serverinfo signon. Reads the server's QuakeC VM
edicts and emits a protocol-15 message via quake.msg.MsgWriter. The client
half is quake/cl_parse.py. Functions take a Server so they can stay out of the
already-large sv.py."""
import math
from dataclasses import dataclass

from . import protocol as P
from .msg import MsgWriter


@dataclass
class Baseline:
    """Spawn-time entity state the client deltas updates against
    (SV_CreateBaseline, sv_main.c:925). Mirrors entity_state_t's baseline."""
    modelindex: int = 0
    frame: int = 0
    colormap: int = 0
    skin: int = 0
    effects: int = 0
    origin: tuple = (0.0, 0.0, 0.0)
    angles: tuple = (0.0, 0.0, 0.0)


def create_baseline(sv):
    """Snapshot every live edict's render state into sv.baselines. The signon
    (write_serverinfo) emits each as a svc_spawnbaseline so the client can delta
    later updates against it. SV_CreateBaseline, sv_main.c:925-975."""
    vm, f = sv.vm, sv.f
    sv.baselines = {}
    # SV_CreateBaseline loops every edict from 0. The world (edict 0) and the
    # client edict(s) always get a baseline; any other edict with no modelindex
    # is skipped (sv_main.c:937 `entnum > maxclients && !modelindex`). This port
    # allocates the single player edict dynamically (sv.player), not at index 1,
    # so the C `0 < entnum <= maxclients` player test maps to `e == sv.player`.
    player = getattr(sv, "player", 0)
    for e in range(0, vm.num_edicts):
        if vm.free[e]:
            continue
        mi = int(vm.fget_i(e, f["modelindex"]))   # modelindex is an int field
        if e != 0 and e != player and mi == 0:
            continue
        colormap = int(vm.fget_f(e, f["colormap"]))
        if e != 0 and e == player:                # SV_CreateBaseline: player
            colormap = e
            mi = sv.model_index("progs/player.mdl")
        sv.baselines[e] = Baseline(
            modelindex=mi,
            frame=int(vm.fget_f(e, f["frame"])),
            colormap=colormap,
            skin=int(vm.fget_f(e, f["skin"])),
            effects=int(vm.fget_f(e, f["effects"])),
            origin=tuple(vm.fget_v(e, f["origin"])),
            angles=tuple(vm.fget_v(e, f["angles"])),
        )


def write_entities_to_client(sv, w, view_origin):
    """SV_WriteEntitiesToClient (sv_main.c:427): per live edict, diff render
    state against its baseline, write the changed-field bitmask (command byte
    carries U_SIGNAL) then only the changed fields. Phase 1 sends every entity
    (no PVS cull -- # TODO(perf): cull like sv_main.c:451)."""
    vm, f = sv.vm, sv.f
    player = sv.player
    for e in range(1, vm.num_edicts):
        if vm.free[e]:
            continue
        base = sv.baselines.get(e)
        if base is None:                     # spawned after baseline: full send
            base = Baseline()
        mi = int(vm.fget_i(e, f["modelindex"]))   # modelindex is an int field
        if e == player:
            # WinQuake always sends the client its own entity (the viewentity)
            # so the client can position the view -- even though this port's
            # hand-built player edict has an empty model string. Its baseline
            # forces progs/player.mdl (SV_CreateBaseline), so delta against that.
            # SceneFromClient skips the viewentity, so the own body is not drawn.
            mi = base.modelindex
        elif mi == 0 or vm.fget_i(e, f["model"]) == 0:  # no model index, or
            continue                                    # model string cleared
            # (item picked up) -- mirror sv_main.c:451 !modelindex || !model
        frame = int(vm.fget_f(e, f["frame"]))
        colormap = int(vm.fget_f(e, f["colormap"]))
        skin = int(vm.fget_f(e, f["skin"]))
        effects = int(vm.fget_f(e, f["effects"]))
        origin = vm.fget_v(e, f["origin"])
        angles = vm.fget_v(e, f["angles"])
        movetype = int(vm.fget_f(e, f["movetype"]))

        bits = 0
        if abs(origin[0] - base.origin[0]) > 0.1:
            bits |= P.U_ORIGIN1
        if abs(origin[1] - base.origin[1]) > 0.1:
            bits |= P.U_ORIGIN2
        if abs(origin[2] - base.origin[2]) > 0.1:
            bits |= P.U_ORIGIN3
        if angles[0] != base.angles[0]:
            bits |= P.U_ANGLE1
        if angles[1] != base.angles[1]:
            bits |= P.U_ANGLE2
        if angles[2] != base.angles[2]:
            bits |= P.U_ANGLE3
        if movetype == 4:                    # MOVETYPE_STEP -> no client lerp
            bits |= P.U_NOLERP
        if frame != base.frame:
            bits |= P.U_FRAME
        if colormap != base.colormap:
            bits |= P.U_COLORMAP
        if skin != base.skin:
            bits |= P.U_SKIN
        if effects != base.effects:
            bits |= P.U_EFFECTS
        if mi != base.modelindex:
            bits |= P.U_MODEL

        if e >= 256:
            bits |= P.U_LONGENTITY
        if bits >= 256:
            bits |= P.U_MOREBITS

        w.byte((bits & 0xff) | P.U_SIGNAL)   # sv_main.c:517
        if bits & P.U_MOREBITS:
            w.byte((bits >> 8) & 0xff)
        if bits & P.U_LONGENTITY:
            w.short(e)
        else:
            w.byte(e)
        if bits & P.U_MODEL:
            w.byte(mi)
        if bits & P.U_FRAME:
            w.byte(frame)
        if bits & P.U_COLORMAP:
            w.byte(colormap)
        if bits & P.U_SKIN:
            w.byte(skin)
        if bits & P.U_EFFECTS:
            w.byte(effects)
        if bits & P.U_ORIGIN1:
            w.coord(origin[0])
        if bits & P.U_ANGLE1:
            w.angle(angles[0])
        if bits & P.U_ORIGIN2:
            w.coord(origin[1])
        if bits & P.U_ANGLE2:
            w.angle(angles[1])
        if bits & P.U_ORIGIN3:
            w.coord(origin[2])
        if bits & P.U_ANGLE3:
            w.angle(angles[2])


def write_clientdata_to_message(sv, w):
    """SV_WriteClientdataToMessage (sv_main.c:576): svc_clientdata + SU_* bits
    then the changed view fields, with items(long)/health(short)/ammo/weapon
    always sent. Reads the player edict."""
    vm, f = sv.vm, sv.f
    e = sv.player
    items = int(vm.fget_f(e, f["items"]))
    items |= (int(getattr(sv, "serverflags", 0)) & 0x0f) << 28   # episode sigils, sv_main.c
    view_ofs = vm.fget_v(e, f["view_ofs"])
    punch = vm.fget_v(e, f["punchangle"])
    vel = vm.fget_v(e, f["velocity"])
    weaponframe = int(vm.fget_f(e, f["weaponframe"]))
    armor = int(vm.fget_f(e, f["armorvalue"]))
    weapon = int(vm.fget_f(e, f["weapon"]))   # IT_ bit -> STAT_ACTIVEWEAPON
    onground = int(vm.fget_f(e, f["flags"])) & 512   # FL_ONGROUND
    waterlevel = int(vm.fget_f(e, f["waterlevel"]))

    bits = 0
    if view_ofs[2] != P.DEFAULT_VIEWHEIGHT:
        bits |= P.SU_VIEWHEIGHT
    for i in range(3):
        if punch[i]:
            bits |= (P.SU_PUNCH1 << i)
        if vel[i]:
            bits |= (P.SU_VELOCITY1 << i)
    bits |= P.SU_ITEMS                         # always carry items in SP
    if onground:
        bits |= P.SU_ONGROUND
    if waterlevel >= 2:
        bits |= P.SU_INWATER
    if weaponframe:
        bits |= P.SU_WEAPONFRAME
    if armor:
        bits |= P.SU_ARMOR
    bits |= P.SU_WEAPON                          # always set (the C guard is commented out) -- sv_main.c

    w.byte(P.svc_clientdata)
    w.short(bits)
    if bits & P.SU_VIEWHEIGHT:
        w.char(int(view_ofs[2]))
    if bits & P.SU_IDEALPITCH:
        w.char(0)
    for i in range(3):
        if bits & (P.SU_PUNCH1 << i):
            w.char(int(punch[i]))
        if bits & (P.SU_VELOCITY1 << i):
            w.char(math.trunc(vel[i] / 16))   # packed /16, truncate toward zero -- sv_main.c
    w.long(items)
    if bits & P.SU_WEAPONFRAME:
        w.byte(weaponframe)
    if bits & P.SU_ARMOR:
        w.byte(armor)
    if bits & P.SU_WEAPON:
        # .weaponmodel is a string field; resolve it the way view_weapon()/
        # hud_status() do (pr.string of the int field), then map to its model
        # index. (The plan's vm.fget_s does not exist in this VM.)
        wmodel = sv.pr.string(vm.fget_i(e, f["weaponmodel"]))
        w.byte(sv.model_index(wmodel))
    w.short(int(vm.fget_f(e, f["health"])))
    w.byte(int(vm.fget_f(e, f["currentammo"])))
    w.byte(int(vm.fget_f(e, f["ammo_shells"])))
    w.byte(int(vm.fget_f(e, f["ammo_nails"])))
    w.byte(int(vm.fget_f(e, f["ammo_rockets"])))
    w.byte(int(vm.fget_f(e, f["ammo_cells"])))
    w.byte(weapon)                            # STAT_ACTIVEWEAPON (IT_ bit)


def build_signon(sv):
    """The three connect-handshake message blocks a real Quake server sends,
    mirroring SV_SendServerinfo / the prespawn sv.signon flush / Host_Spawn_f
    (sv_main.c:189, host_cmd.c:1254, host_cmd.c:1279). Returned as three byte
    blocks; the recorder writes them as the demo's first three frames and the
    live loopback concatenates+parses them. Ends each phase with svc_signonnum
    1/2/3 -- the sequence a WinQuake client needs to reach SIGNONS and render."""
    # --- phase 0: serverinfo ---
    w0 = MsgWriter()
    w0.byte(P.svc_print)
    w0.string(f"\x02\nPQ.AI demo, protocol {P.PROTOCOL_VERSION}\n")
    w0.byte(P.svc_serverinfo)
    w0.long(P.PROTOCOL_VERSION)
    w0.byte(1)                                   # maxclients
    w0.byte(0)                                   # gametype: GAME_COOP
    w0.string(sv.level_name())
    for name in sv.model_precache[1:]:
        w0.string(name)
    w0.string("")
    for name in sv.sound_precache[1:]:
        w0.string(name)
    w0.string("")
    w0.byte(P.svc_cdtrack)
    cd = sv.cdtrack()                            # worldspawn .sounds (Task 3 adds it; 0 ok)
    w0.byte(cd); w0.byte(cd)
    w0.byte(P.svc_setview)
    w0.short(sv.player)
    w0.byte(P.svc_signonnum); w0.byte(1)

    # --- phase 1: prespawn buffer (statics, baselines, static sounds) ---
    w1 = MsgWriter()
    write_static_entities(sv, w1)                # Task 4 (no-op until then)
    for e, base in sv.baselines.items():
        w1.byte(P.svc_spawnbaseline)
        w1.short(e)
        w1.byte(base.modelindex); w1.byte(base.frame)
        w1.byte(base.colormap); w1.byte(base.skin)
        for i in range(3):
            w1.coord(base.origin[i]); w1.angle(base.angles[i])
    write_static_sounds(sv, w1)                  # Task 3 (no-op until then)
    w1.byte(P.svc_signonnum); w1.byte(2)

    # --- phase 2: spawn block ---
    w2 = MsgWriter()
    w2.byte(P.svc_time); w2.float(sv.time)
    write_all_lightstyles(sv, w2)                # all 64 styles (host_cmd.c:1352)
    write_total_stats(sv, w2)                    # secret/monster totals (host_cmd.c:1362)
    ang = sv.player_angles() or (0.0, 0.0, 0.0)
    w2.byte(P.svc_setangle)
    w2.angle(ang[0]); w2.angle(ang[1]); w2.angle(0.0)
    write_clientdata_to_message(sv, w2)
    w2.byte(P.svc_signonnum); w2.byte(3)
    return [bytes(w0.data), bytes(w1.data), bytes(w2.data)]


def write_static_entities(sv, w):    # Task 4
    return


def write_static_sounds(sv, w):
    """svc_spawnstaticsound for each looping ambient the QC spawned (PF_ambient
    sound, pr_cmds.c:506): 3 coords, sound index, vol*255, atten*64. Sourced from
    sv.ambients (list of (name, pos, vol, atten)), which load_level built from the
    ambientsound builtin."""
    for name, pos, vol, atten in sv.ambients:
        idx = sv.sound_index(name)
        if idx <= 0:
            continue
        w.byte(P.svc_spawnstaticsound)
        for c in pos:
            w.coord(c)
        w.byte(idx)
        w.byte(min(255, int(vol * 255)))
        w.byte(min(255, int(atten * 64)))


def write_all_lightstyles(sv, w):
    """svc_lightstyle for ALL 64 styles at spawn (host_cmd.c:1352). Styles the
    QC never set are sent as empty strings, like WinQuake. Seeds
    sv._prev_lightstyles with what we send so the first per-frame write_reliable
    doesn't redundantly re-emit every style as "changed" (it still emits on
    genuine changes -- torches keep flickering)."""
    for i in range(64):
        patt = sv.lightstyles.get(i, "")
        w.byte(P.svc_lightstyle)
        w.byte(i)
        w.string(patt)
        sv._prev_lightstyles[i] = patt


def write_total_stats(sv, w):
    """svc_updatestat for the secret/monster totals at spawn (host_cmd.c:1362).
    Same QC globals the intermission tally reads."""
    for stat, value in ((P.STAT_TOTALSECRETS, sv.total_secrets()),
                        (P.STAT_TOTALMONSTERS, sv.total_monsters()),
                        (P.STAT_SECRETS, sv.found_secrets()),
                        (P.STAT_MONSTERS, sv.killed_monsters())):
        w.byte(P.svc_updatestat)
        w.byte(stat)
        w.long(int(value))


def write_serverinfo(sv, w):
    """Thin back-compat wrapper: concatenate the three build_signon phases into
    one buffer (the old single-block signon behaviour). The live loopback and
    existing tests parse this all at once."""
    for block in build_signon(sv):
        w.data += block


def write_reliable(sv, w):
    """Per-frame reliable messages: lightstyle changes (svc_lightstyle),
    centerprint, intermission, and a teleport svc_setangle. (Stat updates ride
    in clientdata in single-player, so svc_updatestat is reserved for the
    intermission secret/monster totals.)"""
    for idx, patt in sv.lightstyles.items():
        if sv._prev_lightstyles.get(idx) != patt:
            sv._prev_lightstyles[idx] = patt
            w.byte(P.svc_lightstyle)
            w.byte(idx)
            w.string(patt)
    cm = sv.center_msg
    if cm and cm is not sv._sent_center:
        sv._sent_center = cm
        w.byte(P.svc_centerprint)
        w.string(cm[0])
    if sv._setangle is not None:
        w.byte(P.svc_setangle)
        for a in sv._setangle:
            w.angle(a)
        sv._setangle = None
    if sv.intermission_active():
        if not sv._sent_intermission:
            sv._sent_intermission = True
            w.byte(P.svc_intermission)


def build_datagram(sv, w):
    """SV_SendClientDatagram (sv_main.c:720): one frame's message --
    svc_time, clientdata, entity deltas, then reliable updates and the
    accumulated unreliable events. View origin for (future) PVS culling is the
    player eye."""
    w.byte(P.svc_time)
    w.float(sv.time)
    write_clientdata_to_message(sv, w)
    eye = sv.player_origin() or (0.0, 0.0, 0.0)
    write_entities_to_client(sv, w, eye)
    write_reliable(sv, w)
    for fn in sv.unreliable:                    # svc_sound / temp ents / particle
        fn(w)
