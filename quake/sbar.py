"""Quake status bar: sbar.c port. Composites the classic two-strip HUD --
the 320x24 SBAR (armor / face / health / ammo with the big 24x24 digits)
under the 320x24 IBAR (weapon slots, small ammo counts, items, sigils) --
into the renderer's 8-bit indexed framebuffer, bottom-centred
(Sbar_DrawPic's single-player x + (vid.width-320)/2). Palette index 255 is
transparent (Draw_TransPic); BACKTILE tiles under any margins wider than
320 (the Draw_TileClear look, screen-origin aligned: x%64, y%64). Pure: no
OS, UI, or engine imports beyond the wad parser feeding it.

The caller owns the two timers cl_parse.c keeps client-side and passes them
in: item_gettime[bit] (pickup flash animations) and faceanimtime (the 0.2s
pain face after damage).

Two deliberate deviations from WinQuake's sbar.c:
- Item/sigil pickup blink: the original DOS sbar.c gated the "flash frame"
  skip on the weapon loop's leftover `flashon` variable (an id quirk);
  WinQuake neutralises it with an explicit `flashon = 0`, so items never
  actually blink there. We blink on `int((time - t) * 10) & 1` for 2s.
- Sbar_DrawCharacter has its >320 centring commented out in WinQuake's
  deathmatch path only; in single-player it centres, which is what we do
  unconditionally (including its +4 x offset), so the small ammo counts
  stay aligned with the strips at any width.
"""

SBAR_W, SBAR_H = 320, 24      # each strip (SBAR_HEIGHT in sbar.c)
SBAR_LINES = 2 * SBAR_H       # rows reserved at the bottom (screen.c sb_lines)
TRANSPARENT = 255

# item bits (defs.qc / sbar.c) -- the QC stores .weapon as one of these too
IT_SHOTGUN = 1                # IT_SHOTGUN << i, i in 0..6, are the 7 slots
IT_SUPER_SHOTGUN = 2
IT_NAILGUN = 4
IT_SUPER_NAILGUN = 8
IT_GRENADE_LAUNCHER = 16
IT_ROCKET_LAUNCHER = 32
IT_LIGHTNING = 64
IT_SHELLS = 256
IT_NAILS = 512
IT_ROCKETS = 1024
IT_CELLS = 2048
IT_ARMOR1 = 8192
IT_ARMOR2 = 16384
IT_ARMOR3 = 32768
IT_INVISIBILITY = 524288
IT_INVULNERABILITY = 1048576
IT_QUAD = 4194304

_WEAPON_LUMPS = ("shotgun", "sshotgun", "nailgun", "snailgun",
                 "rlaunch", "srlaunch", "lightng")


class Sbar:
    """Loads the lump set Sbar_Init loads; draw() is Sbar_Draw."""

    def __init__(self, wad):
        q = wad.qpic
        # nums[color][digit]: 0 gold (NUM_), 1 red (ANUM_); [10] is minus
        self.nums = [[q(f"num_{i}") for i in range(10)] + [q("num_minus")],
                     [q(f"anum_{i}") for i in range(10)] + [q("anum_minus")]]
        # weapons[state][slot]: 0 owned, 1 selected, 2..6 pickup flash frames
        self.weapons = ([[q(f"inv_{w}") for w in _WEAPON_LUMPS],
                         [q(f"inv2_{w}") for w in _WEAPON_LUMPS]] +
                        [[q(f"inva{i}_{w}") for w in _WEAPON_LUMPS]
                         for i in range(1, 6)])
        self.ammo = [q("sb_shells"), q("sb_nails"), q("sb_rocket"),
                     q("sb_cells")]
        self.armor = [q("sb_armor1"), q("sb_armor2"), q("sb_armor3")]
        self.items = [q("sb_key1"), q("sb_key2"), q("sb_invis"),
                      q("sb_invuln"), q("sb_suit"), q("sb_quad")]
        self.sigils = [q(f"sb_sigil{i}") for i in range(1, 5)]
        # faces[f]: f=0 is near-death FACE5 .. f=4 is healthy FACE1, each
        # (normal, pain) -- mirrors the reversed sb_faces[] fill in Sbar_Init
        self.faces = [(q(f"face{5 - i}"), q(f"face_p{5 - i}"))
                      for i in range(5)]
        # Sbar_IntermissionNumber's separators: ':' in the time, '/' in tallies
        self.colon = q("num_colon")
        self.slash = q("num_slash")
        self.face_invis = q("face_invis")
        self.face_invuln = q("face_invul2")
        self.face_invis_invuln = q("face_inv2")
        self.face_quad = q("face_quad")
        self.sbar = q("sbar")
        self.ibar = q("ibar")
        self.disc = q("disc")
        self.backtile = q("backtile")
        self.conchars = wad.lump("conchars")   # 128x128, 16x16 grid of 8x8

    # ---- blit primitives (draw.c) ----
    def _pic(self, fb, fbw, x, y, pic):
        """Draw_TransPic: copy palette indices, 255 transparent. Opaque pics
        (the strips) take the row-slice fast path."""
        w, h, px = pic
        if TRANSPARENT not in px:
            for r in range(h):
                d = (y + r) * fbw + x
                fb[d:d + w] = px[r * w:(r + 1) * w]
            return
        for r in range(h):
            d = (y + r) * fbw + x
            s = r * w
            for i in range(w):
                c = px[s + i]
                if c != TRANSPARENT:
                    fb[d + i] = c

    def _char(self, fb, fbw, x, y, num):
        """Draw_Character from CONCHARS: 8x8 glyph, index 0 transparent."""
        src = self.conchars
        sy, sx = (num >> 4) * 8, (num & 15) * 8
        for r in range(8):
            s = (sy + r) * 128 + sx
            d = (y + r) * fbw + x
            for i in range(8):
                b = src[s + i]
                if b:
                    fb[d + i] = b

    def _num(self, fb, fbw, x, y, value, digits, red):
        """Sbar_DrawNum: right-aligned big digits, gold or red (ANUM)."""
        s = str(int(value))
        if len(s) > digits:
            s = s[len(s) - digits:]
        x += (digits - len(s)) * 24
        nums = self.nums[1 if red else 0]
        for ch in s:
            self._pic(fb, fbw, x, y, nums[10 if ch == "-" else int(ch)])
            x += 24

    def _inter_num(self, fb, fbw, x, y, value, digits):
        """Sbar_IntermissionNumber: big gold digits, left-padded to `digits`
        (only shifts when the value is shorter; longer values are trimmed)."""
        s = str(int(value))
        if len(s) > digits:
            s = s[len(s) - digits:]
        if len(s) < digits:
            x += (digits - len(s)) * 24
        for ch in s:
            self._pic(fb, fbw, x, y, self.nums[0][10 if ch == "-" else int(ch)])
            x += 24

    def intermission_overlay(self, fb, fbw, fbh, ist, complete, inter):
        """Sbar_IntermissionOverlay (single-player): the end-of-level screen --
        the 'complete' title pic, the 'inter' Time/Secrets/Kills label panel,
        and the big 24x24 digit pics (sb_nums) with num_colon/num_slash. Centred
        horizontally like the status bar ((fbw-320)/2); id's fixed y's are kept.
        `complete`/`inter` are qpic (w,h,indices) tuples the caller loads."""
        sx = (fbw - 320) // 2
        self._pic(fb, fbw, sx + 64, 24, complete)      # Draw_Pic(64,24)
        self._pic(fb, fbw, sx + 0, 56, inter)          # Draw_TransPic(0,56)
        # time (m:ss)
        t = int(ist["time"])
        dig = t // 60
        self._inter_num(fb, fbw, sx + 160, 64, dig, 3)
        num = t - dig * 60
        self._pic(fb, fbw, sx + 234, 64, self.colon)
        self._pic(fb, fbw, sx + 246, 64, self.nums[0][num // 10])
        self._pic(fb, fbw, sx + 266, 64, self.nums[0][num % 10])
        # secrets found / total
        self._inter_num(fb, fbw, sx + 160, 104, ist["secrets"], 3)
        self._pic(fb, fbw, sx + 232, 104, self.slash)
        self._inter_num(fb, fbw, sx + 240, 104, ist["total_secrets"], 3)
        # monsters killed / total
        self._inter_num(fb, fbw, sx + 160, 144, ist["monsters"], 3)
        self._pic(fb, fbw, sx + 232, 144, self.slash)
        self._inter_num(fb, fbw, sx + 240, 144, ist["total_monsters"], 3)

    # ---- Sbar_Draw ----
    def draw(self, fb, fbw, fbh, st, time, item_gettime, faceanimtime):
        """Composite both strips over the bottom SBAR_LINES rows of fb (an
        8-bit indexed framebuffer, fbw*fbh bytes). st is sv.hud_status()
        (with the raw items/weapon_bit fields); time is sv.time;
        item_gettime is the 32-entry pickup-time list and faceanimtime the
        pain-face deadline, both kept by the client (cl_parse.c)."""
        sx = (fbw - SBAR_W) >> 1
        top = fbh - SBAR_LINES
        if fbw > SBAR_W:                        # margins: tiled BACKTILE
            bt = self.backtile[2]
            for y in range(top, fbh):
                trow = (y & 63) * 64
                row = bytes(bt[trow + (x & 63)] for x in range(fbw))
                base = y * fbw
                fb[base:base + sx] = row[:sx]
                fb[base + sx + SBAR_W:base + fbw] = row[sx + SBAR_W:]
        items = st["items"]
        self._inventory(fb, fbw, sx, top, st, items, time, item_gettime)
        self._status(fb, fbw, sx, fbh - SBAR_H, st, items, time, faceanimtime)

    def _inventory(self, fb, fbw, sx, top, st, items, time, gettime):
        """Sbar_DrawInventory: the upper strip."""
        self._pic(fb, fbw, sx, top, self.ibar)
        # weapon slots: INV_ owned / INV2_ selected / INVA1-5 pickup flash
        for i in range(7):
            bit = IT_SHOTGUN << i
            if not items & bit:
                continue
            flashon = int((time - gettime[i]) * 10)
            if flashon >= 10:
                flashon = 1 if st["weapon_bit"] == bit else 0
            else:
                flashon = (flashon % 5) + 2
            self._pic(fb, fbw, sx + i * 24, top + 8, self.weapons[flashon][i])
        # ammo counts: gold console digits (chars 18+n), 3 wide per pool
        # (Sbar_DrawCharacter adds +4 to x on top of the centring)
        for i, key in enumerate(("shells", "nails", "rockets", "cells")):
            s = f"{st[key]:3d}"[-3:]
            for j, ch in enumerate(s):
                if ch != " ":
                    self._char(fb, fbw, sx + (6 * i + 1) * 8 - 2 + 4 + j * 8,
                               top, 18 + ord(ch) - ord("0"))
        # items (keys, powerups): bits 17..22; blink for 2s after pickup
        for i in range(6):
            if items & (1 << (17 + i)):
                t = gettime[17 + i]
                if not (t and time - t < 2 and int((time - t) * 10) & 1):
                    self._pic(fb, fbw, sx + 192 + i * 16, top + 8,
                              self.items[i])
        # sigils: serverflags folded into bits 28..31 (SV_WriteClientdata...)
        for i in range(4):
            if items & (1 << (28 + i)):
                t = gettime[28 + i]
                if not (t and time - t < 2 and int((time - t) * 10) & 1):
                    self._pic(fb, fbw, sx + SBAR_W - 32 + i * 8, top + 8,
                              self.sigils[i])

    def _status(self, fb, fbw, sx, y, st, items, time, faceanimtime):
        """Sbar_DrawNormal: the lower strip."""
        self._pic(fb, fbw, sx, y, self.sbar)
        if items & IT_INVULNERABILITY:
            self._num(fb, fbw, sx + 24, y, 666, 3, True)
            self._pic(fb, fbw, sx, y, self.disc)
        else:
            armor = st["armor"]
            self._num(fb, fbw, sx + 24, y, armor, 3, armor <= 25)
            for bit, pic in ((IT_ARMOR3, self.armor[2]),
                             (IT_ARMOR2, self.armor[1]),
                             (IT_ARMOR1, self.armor[0])):
                if items & bit:
                    self._pic(fb, fbw, sx, y, pic)
                    break
        self._face(fb, fbw, sx + 112, y, st, items, time, faceanimtime)
        health = st["health"]
        self._num(fb, fbw, sx + 136, y, health, 3, health <= 25)
        for i, bit in enumerate((IT_SHELLS, IT_NAILS, IT_ROCKETS, IT_CELLS)):
            if items & bit:
                self._pic(fb, fbw, sx + 224, y, self.ammo[i])
                break
        ammo = st["ammo"]
        self._num(fb, fbw, sx + 248, y, ammo, 3, ammo <= 10)

    def _face(self, fb, fbw, x, y, st, items, time, faceanimtime):
        """Sbar_DrawFace: powerup faces win; otherwise health tier + pain."""
        both = IT_INVISIBILITY | IT_INVULNERABILITY
        if items & both == both:
            pic = self.face_invis_invuln
        elif items & IT_QUAD:
            pic = self.face_quad
        elif items & IT_INVISIBILITY:
            pic = self.face_invis
        elif items & IT_INVULNERABILITY:
            pic = self.face_invuln
        else:
            h = st["health"]
            f = 4 if h >= 100 else max(0, h) // 20
            pic = self.faces[f][1 if time <= faceanimtime else 0]
        self._pic(fb, fbw, x, y, pic)


if __name__ == "__main__":
    from .pak import Pak
    from .wad import Wad
    sb = Sbar(Wad(Pak("quake-shareware/id1/pak0.pak").read("gfx.wad")))
    fb = bytearray(320 * 200)
    sb.draw(fb, 320, 200, {"health": 100, "armor": 50, "ammo": 25,
                           "shells": 25, "nails": 0, "rockets": 0,
                           "cells": 0, "items": IT_SHOTGUN | IT_SHELLS |
                           IT_ARMOR1, "weapon_bit": IT_SHOTGUN},
            10.0, [0.0] * 32, 0.0)
    print("composited", sum(1 for b in fb if b), "non-zero pixels")
