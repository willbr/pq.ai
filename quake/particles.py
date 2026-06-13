"""The faithful r_part.c particle system, as a pure-stdlib module shared by the
server (quake/sv.py) and the demo-playback client (quake/cl_parse.py).

In live play the server owns the particle pool; in demo playback there is no
server, so the client drives the SAME code here. Both keep a plain `particles`
list whose entries are the 10-element form the renderer reads:

    [x, y, z, vx, vy, vz, color, die, type, ramp]

All functions operate on that list in place (and return it where useful). Time
(`now`), frametime (`dt`) and `gravity` (= sv_gravity, 800.0) are passed in
rather than read off a server, so the module couples to neither sv nor cl.

Ports cite id's r_part.c (the WinQuake client particle renderer)."""
import math
import random

# colour-ramp tables explosions and fire animate through as they cool (r_part.c:29)
_RAMP1 = (0x6f, 0x6d, 0x6b, 0x69, 0x67, 0x65, 0x63, 0x61)   # pt_explode
_RAMP2 = (0x6f, 0x6e, 0x6d, 0x6c, 0x6b, 0x6a, 0x68, 0x66)   # pt_explode2
_RAMP3 = (0x6d, 0x6b, 6, 5, 4, 3)                           # pt_fire (rocket smoke)

# particle types (r_part.c ptype_t): pick the per-frame gravity/decel/colour path
PT_STATIC   = 0     # no gravity (tracers, voor trail)
PT_GRAV     = 1     # falls (blood)
PT_SLOWGRAV = 2     # falls (gunshots, splashes) -- same integration as pt_grav
PT_FIRE     = 3     # rises, cools via ramp3, dies at ramp >= 6 (rocket/grenade smoke)
PT_EXPLODE  = 4     # accelerates, cools via ramp1, dies at ramp >= 8
PT_EXPLODE2 = 5     # decelerates (1x), cools via ramp2, dies at ramp >= 8
PT_BLOB     = 6     # accelerates (tarbaby/quad)
PT_BLOB2    = 7     # decelerates in x/y (tarbaby/quad)

MAX_PARTICLES = 2048    # id's pool size (r_part.c MAX_PARTICLES); drop the oldest past it
# id spawns ~1024 particles per explosion/splash; the pure-Python point rasteriser
# can't carry that, so the big bursts are subsampled to this many while keeping
# id's exact per-particle physics, colours, lifetimes and jitter. The small
# effects (spikes, gunshots, trails) keep their full id counts.
_BIG_BURST = 128


def cap(particles):
    """Bound the live list at MAX_PARTICLES, dropping the oldest. id stops
    spawning when its fixed pool is exhausted; trimming the front is the
    visually gentler equivalent for our growable list."""
    n = len(particles)
    if n > MAX_PARTICLES:
        del particles[:n - MAX_PARTICLES]


def run_particle_effect(particles, org, dirv, color, count, now):
    """R_RunParticleEffect: gunshots/spikes. Each particle is pt_slowgrav,
    seeded at dir*15 (barely moving -- the random kick is commented out in
    id), coloured (color & ~7) + rand&7, jittered +/-8 about org. count==1024
    is id's rocket-explosion shorthand and routes to R_ParticleExplosion."""
    if count == 1024:
        particle_explosion(particles, org, now)
        return
    for _ in range(count):
        particles.append([
            org[0] + (random.randint(0, 15) - 8),
            org[1] + (random.randint(0, 15) - 8),
            org[2] + (random.randint(0, 15) - 8),
            dirv[0] * 15.0, dirv[1] * 15.0, dirv[2] * 15.0,
            ((color & ~7) + random.randint(0, 7)) & 255,
            now + 0.1 * random.randint(0, 4), PT_SLOWGRAV, 0.0])
    cap(particles)


def particle_explosion(particles, org, now):
    """R_ParticleExplosion: rocket/grenade blast. Alternating pt_explode /
    pt_explode2 (one accelerates and cools through ramp1, the other
    decelerates through ramp2), seeded +/-256 u/s, +/-16 about org, ramp
    rand&3. id spawns 1024; we subsample to _BIG_BURST."""
    for i in range(_BIG_BURST):
        ptype = PT_EXPLODE if (i & 1) else PT_EXPLODE2
        particles.append([
            org[0] + (random.randint(0, 31) - 16),
            org[1] + (random.randint(0, 31) - 16),
            org[2] + (random.randint(0, 31) - 16),
            float(random.randint(0, 511) - 256),
            float(random.randint(0, 511) - 256),
            float(random.randint(0, 511) - 256),
            _RAMP1[0], now + 5.0, ptype, float(random.randint(0, 3))])
    cap(particles)


def blob_explosion(particles, org, now):
    """R_BlobExplosion: tarbaby/Quad blast. Alternating pt_blob (colour
    66+rand%6) / pt_blob2 (150+rand%6), +/-256 u/s, +/-16 about org. id
    spawns 1024; we subsample to _BIG_BURST."""
    for i in range(_BIG_BURST):
        die = now + 1.0 + random.randint(0, 1) * 8 * 0.05   # (rand&8)*0.05: 0 or .4
        if i & 1:
            ptype, col = PT_BLOB, 66 + random.randint(0, 5)
        else:
            ptype, col = PT_BLOB2, 150 + random.randint(0, 5)
        particles.append([
            org[0] + (random.randint(0, 31) - 16),
            org[1] + (random.randint(0, 31) - 16),
            org[2] + (random.randint(0, 31) - 16),
            float(random.randint(0, 511) - 256),
            float(random.randint(0, 511) - 256),
            float(random.randint(0, 511) - 256),
            col & 255, die, ptype, 0.0])
    cap(particles)


def lava_splash(particles, org, now):
    """R_LavaSplash: a ring of pt_slowgrav jets shooting up (dir.z = 256),
    colour 224+rand&7. id walks a 32x32 grid (1024); we step it coarser."""
    for i in range(-16, 16, 3):
        for j in range(-16, 16, 3):
            dx = j * 8 + random.randint(0, 7)
            dy = i * 8 + random.randint(0, 7)
            dz = 256.0
            n = math.sqrt(dx * dx + dy * dy + dz * dz)
            vel = 50 + random.randint(0, 63)
            s = vel / n
            particles.append([
                org[0] + dx, org[1] + dy, org[2] + random.randint(0, 63),
                dx * s, dy * s, dz * s,
                (224 + random.randint(0, 7)) & 255,
                now + 2.0 + random.randint(0, 31) * 0.02, PT_SLOWGRAV, 0.0])
    cap(particles)


def teleport_splash(particles, org, now):
    """R_TeleportSplash: a box of pt_slowgrav sparks, colour 7+rand&7. id
    walks a 8x8x14 grid (~896); we step it coarser."""
    for i in range(-16, 16, 8):
        for j in range(-16, 16, 8):
            for k in range(-24, 32, 8):
                dx, dy, dz = j * 8.0, i * 8.0, k * 8.0
                n = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
                vel = 50 + random.randint(0, 63)
                s = vel / n
                particles.append([
                    org[0] + i + random.randint(0, 3),
                    org[1] + j + random.randint(0, 3),
                    org[2] + k + random.randint(0, 3),
                    dx * s, dy * s, dz * s,
                    (7 + random.randint(0, 7)) & 255,
                    now + 0.2 + random.randint(0, 7) * 0.02, PT_SLOWGRAV, 0.0])
    cap(particles)


def rocket_trail(particles, start, end, ttype, now, tracer_state):
    """R_RocketTrail: lay particles from start to end, one per unit step
    (id advances `start` by the unit direction each iteration while burning
    `dec` units of remaining length -- so the trail is dense and covers ~1/3
    of the move, by design). Per-type: pt_fire smoke (rocket/grenade), pt_grav
    blood (gibs), pt_static tracers with a perpendicular kick, voor sparkle.

    `tracer_state` is a one-element mutable holder ([count]) for id's static
    `tracercount`, which alternates the tracer's perpendicular kick across
    calls; thread the same holder through to preserve that state."""
    sx, sy, sz = start
    dx, dy, dz = end[0] - sx, end[1] - sy, end[2] - sz
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length < 1e-6:
        return
    vx, vy, vz = dx / length, dy / length, dz / length   # unit direction
    dec = 3.0
    t = now
    guard = 0
    while length > 0.0 and guard < 1024:         # guard a pathological teleport
        guard += 1
        length -= dec
        if ttype in (0, 1):                      # rocket / grenade smoke (fire)
            ramp = float(random.randint(0, 3) + (2 if ttype == 1 else 0))
            particles.append([
                sx + (random.randint(0, 5) - 3),
                sy + (random.randint(0, 5) - 3),
                sz + (random.randint(0, 5) - 3),
                0.0, 0.0, 0.0, _RAMP3[int(ramp)], t + 2.0, PT_FIRE, ramp])
        elif ttype in (2, 4):                    # blood / slight blood (grav)
            particles.append([
                sx + (random.randint(0, 5) - 3),
                sy + (random.randint(0, 5) - 3),
                sz + (random.randint(0, 5) - 3),
                0.0, 0.0, 0.0,
                67 + random.randint(0, 3), t + 2.0, PT_GRAV, 0.0])
            if ttype == 4:
                length -= 3.0                    # slight blood: sparser
        elif ttype in (3, 5):                    # tracer / tracer2 (static)
            tracer_state[0] += 1
            tc = tracer_state[0]
            col = (52 if ttype == 3 else 230) + ((tc & 4) << 1)
            if tc & 1:
                pvx, pvy = 30.0 * vy, 30.0 * -vx
            else:
                pvx, pvy = 30.0 * -vy, 30.0 * vx
            particles.append([
                sx, sy, sz, pvx, pvy, 0.0,
                col & 255, t + 0.5, PT_STATIC, 0.0])
        else:                                    # ttype 6: voor trail (static)
            particles.append([
                sx + (random.randint(0, 15) - 8),
                sy + (random.randint(0, 15) - 8),
                sz + (random.randint(0, 15) - 8),
                0.0, 0.0, 0.0,
                9 * 16 + 8 + random.randint(0, 3), t + 0.3, PT_STATIC, 0.0])
        sx += vx; sy += vy; sz += vz
    cap(particles)


def advance(particles, now, dt, gravity):
    """R_DrawParticles: integrate and age each live particle, branching on its
    type for gravity, velocity ramp and colour fade (r_part.c:697). Returns the
    surviving list. `gravity` is sv_gravity (800.0); the per-frame grav term is
    gravity * 0.05 * dt."""
    if not particles:
        return particles
    t = now
    grav = gravity * 0.05 * dt                          # frametime*sv_gravity*0.05
    dvel = 4.0 * dt
    time1, time2, time3 = 5.0 * dt, 10.0 * dt, 15.0 * dt
    live = []
    for p in particles:
        if p[7] <= t:
            continue
        # move by current velocity, then apply this type's per-frame physics
        p[0] += p[3] * dt
        p[1] += p[4] * dt
        p[2] += p[5] * dt
        ptype = p[8] if len(p) > 8 else PT_STATIC
        if ptype == PT_STATIC:
            pass
        elif ptype == PT_FIRE:
            p[9] += time1
            if p[9] >= 6.0:
                p[7] = -1.0
            else:
                p[6] = _RAMP3[int(p[9])]
            p[5] += grav                           # fire rises
        elif ptype == PT_EXPLODE:
            p[9] += time2
            if p[9] >= 8.0:
                p[7] = -1.0
            else:
                p[6] = _RAMP1[int(p[9])]
            p[3] += p[3] * dvel; p[4] += p[4] * dvel; p[5] += p[5] * dvel
            p[5] -= grav
        elif ptype == PT_EXPLODE2:
            p[9] += time3
            if p[9] >= 8.0:
                p[7] = -1.0
            else:
                p[6] = _RAMP2[int(p[9])]
            p[3] -= p[3] * dt; p[4] -= p[4] * dt; p[5] -= p[5] * dt   # 1x decel
            p[5] -= grav
        elif ptype == PT_BLOB:
            p[3] += p[3] * dvel; p[4] += p[4] * dvel; p[5] += p[5] * dvel
            p[5] -= grav
        elif ptype == PT_BLOB2:
            p[3] -= p[3] * dvel; p[4] -= p[4] * dvel                  # x/y only
            p[5] -= grav
        else:                                       # PT_GRAV / PT_SLOWGRAV
            p[5] -= grav
        live.append(p)
    return live
