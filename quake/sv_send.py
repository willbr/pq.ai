"""Server-side protocol serialization: ports of WinQuake sv_main.c's
SV_CreateBaseline / SV_WriteEntitiesToClient / SV_WriteClientdataToMessage /
SV_SendClientDatagram and the serverinfo signon. Reads the server's QuakeC VM
edicts and emits a protocol-15 message via quake.msg.MsgWriter. The client
half is quake/cl_parse.py. Functions take a Server so they can stay out of the
already-large sv.py."""
from dataclasses import dataclass

from . import protocol as P


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
    for e in range(1, vm.num_edicts):
        if vm.free[e]:
            continue
        mi = int(vm.fget_i(e, f["modelindex"]))   # modelindex is an int field
        sv.baselines[e] = Baseline(
            modelindex=mi,
            frame=int(vm.fget_f(e, f["frame"])),
            colormap=int(vm.fget_f(e, f["colormap"])),
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
    for e in range(1, vm.num_edicts):
        if vm.free[e]:
            continue
        base = sv.baselines.get(e)
        if base is None:                     # spawned after baseline: full send
            base = Baseline()
        mi = int(vm.fget_i(e, f["modelindex"]))   # modelindex is an int field
        if mi == 0:                          # invisible (no model) -- skip
            continue
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
    if weapon:
        bits |= P.SU_WEAPON

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
            w.char(int(vel[i]) // 16)         # packed /16, sv_main.c
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


def write_serverinfo(sv, w):
    """The signon (SV_SendClientMessages signon phase): svc_serverinfo with the
    precache lists, then a svc_spawnbaseline per entity, then svc_signonnum.
    Sent once at connect / after a changelevel so the client builds its model
    and sound indices before any entity update arrives."""
    w.byte(P.svc_serverinfo)
    w.long(P.PROTOCOL_VERSION)
    w.byte(1)                                  # maxclients (single-player)
    w.byte(0)                                  # gametype: GAME_COOP/standard
    w.string(sv.level_name())                  # printable level title
    for name in sv.model_precache[1:]:         # index 0 is "" (skip)
        w.string(name)
    w.string("")                               # precache list terminator
    for name in sv.sound_precache[1:]:
        w.string(name)
    w.string("")
    for e, base in sv.baselines.items():
        w.byte(P.svc_spawnbaseline)
        w.short(e)
        w.byte(base.modelindex)
        w.byte(base.frame)
        w.byte(base.colormap)
        w.byte(base.skin)
        for i in range(3):
            w.coord(base.origin[i])
            w.angle(base.angles[i])
    w.byte(P.svc_setview)
    w.short(sv.player)                          # the view entity = player edict
    w.byte(P.svc_signonnum)
    w.byte(1)


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
    if cm and cm is not getattr(sv, "_sent_center", None):
        sv._sent_center = cm
        w.byte(P.svc_centerprint)
        w.string(cm[0])
    if sv._setangle is not None:
        w.byte(P.svc_setangle)
        for a in sv._setangle:
            w.angle(a)
        sv._setangle = None
    if sv.intermission_active():
        if not getattr(sv, "_sent_intermission", False):
            sv._sent_intermission = True
            w.byte(P.svc_intermission)
            w.string("")


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
