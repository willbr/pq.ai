# quake/protocol.py
"""Quake protocol-15 numeric constants, verbatim from WinQuake protocol.h.
Shared by the server serializer (sv_send.py) and the client parser
(cl_parse.py). No logic -- just the message catalogue and bit layouts."""

PROTOCOL_VERSION = 15
DEFAULT_VIEWHEIGHT = 22

# server -> client messages (protocol.h)
svc_bad = 0
svc_nop = 1
svc_disconnect = 2
svc_updatestat = 3
svc_version = 4
svc_setview = 5
svc_sound = 6
svc_time = 7
svc_print = 8
svc_stufftext = 9
svc_setangle = 10
svc_serverinfo = 11
svc_lightstyle = 12
svc_updatename = 13
svc_updatefrags = 14
svc_clientdata = 15
svc_stopsound = 16
svc_updatecolors = 17
svc_particle = 18
svc_damage = 19
svc_spawnstatic = 20
svc_spawnbaseline = 22
svc_temp_entity = 23
svc_setpause = 24
svc_signonnum = 25
svc_centerprint = 26
svc_killedmonster = 27
svc_foundsecret = 28
svc_spawnstaticsound = 29
svc_intermission = 30
svc_finale = 31
svc_cdtrack = 32
svc_sellscreen = 33
svc_cutscene = 34

# entity update bit flags (protocol.h); high bit of the command byte = U_SIGNAL
U_MOREBITS = 1 << 0
U_ORIGIN1 = 1 << 1
U_ORIGIN2 = 1 << 2
U_ORIGIN3 = 1 << 3
U_ANGLE2 = 1 << 4
U_NOLERP = 1 << 5
U_FRAME = 1 << 6
U_SIGNAL = 1 << 7
U_ANGLE1 = 1 << 8
U_ANGLE3 = 1 << 9
U_MODEL = 1 << 10
U_COLORMAP = 1 << 11
U_SKIN = 1 << 12
U_EFFECTS = 1 << 13
U_LONGENTITY = 1 << 14

# clientdata (svc_clientdata) bit flags (protocol.h)
SU_VIEWHEIGHT = 1 << 0
SU_IDEALPITCH = 1 << 1
SU_PUNCH1 = 1 << 2
SU_PUNCH2 = 1 << 3
SU_PUNCH3 = 1 << 4
SU_VELOCITY1 = 1 << 5
SU_VELOCITY2 = 1 << 6
SU_VELOCITY3 = 1 << 7
SU_ITEMS = 1 << 9
SU_ONGROUND = 1 << 10
SU_INWATER = 1 << 11
SU_WEAPONFRAME = 1 << 12
SU_ARMOR = 1 << 13
SU_WEAPON = 1 << 14

# svc_sound field-mask bits (protocol.h)
SND_VOLUME = 1 << 0
SND_ATTENUATION = 1 << 1
SND_LOOPING = 1 << 2

# stat indices (protocol.h): svc_updatestat / cl.stats
STAT_HEALTH = 0
STAT_FRAGS = 1
STAT_WEAPON = 2
STAT_AMMO = 3
STAT_ARMOR = 4
STAT_WEAPONFRAME = 5
STAT_SHELLS = 6
STAT_NAILS = 7
STAT_ROCKETS = 8
STAT_CELLS = 9
STAT_ACTIVEWEAPON = 10
STAT_TOTALSECRETS = 11
STAT_TOTALMONSTERS = 12
STAT_SECRETS = 13
STAT_MONSTERS = 14

# temp-entity subtypes (protocol.h): payload of svc_temp_entity
TE_SPIKE = 0
TE_SUPERSPIKE = 1
TE_GUNSHOT = 2
TE_EXPLOSION = 3
TE_TAREXPLOSION = 4
TE_LIGHTNING1 = 5
TE_LIGHTNING2 = 6
TE_WIZSPIKE = 7
TE_KNIGHTSPIKE = 8
TE_LIGHTNING3 = 9
TE_LAVASPLASH = 10
TE_TELEPORT = 11
