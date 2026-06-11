# Faults

## Fixed

- **e1m1 -45,564,20 — door teeth z-order** and **e1m1 164,1678,-200 — door
  parts z-order**: one root cause. The interlocking door halves (*1/*2 at the
  start door, *5/*6 in the slime corridor) are two brush models whose front
  faces share a plane and overlap where the teeth interlock; both inherit the
  same world-leaf key, and the span renderer's incumbent-wins epsilon gave the
  overlap to whichever half's leading edge came first, hiding the other's
  tooth border. Fixed by porting id's R_LeadingEdge same-key tie-break
  verbatim (insubmodel flag, 1% fudge, d_zistepu compare) — commit d203bd9,
  regression test in test_r_edge.py.

- **e1m1 252,858,-200 — sky brightness**: confirmed wrong, ~2x too bright.
  Sky texels were shaded through colormap row 0, the 2x overbright row;
  WinQuake draws the sky raw with no colormap pass (D_DrawSkyScans8). The
  pre-span renderer had the same bug, so this was never caught by the oracle.
  Fixed (raw-texel sky fill) — commit 4504f33, test_sky_brightness.py.

## Not reproduced

- **e1m1 400,2751,-56 — flicker from new renderer**: could not reproduce a
  renderer-specific flicker headlessly. Probed at that spot across 8 yaws ×
  3 pitches: fixed-camera time advance (only monster animation changes
  pixels), sub-pixel translation, 0.25-degree rotation steps, the trap-floor
  door (*8) filmed while moving, and a check for unfilled span gaps (zero
  background pixels leak through). In every probe the span renderer's
  frame-to-frame flip counts match the pre-span per-pixel oracle (440aa3b)
  within noise. If the flicker involved a nearby coplanar bmodel overlap it
  is fixed by d203bd9; otherwise it may be ordinary quarter-res point-sample
  texture shimmer under motion (identical in the old renderer). Needs a
  re-check in play — if it persists, note the view direction and what
  flickers (which surface, moving or still).
