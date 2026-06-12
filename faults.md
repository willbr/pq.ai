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

- **e1m1 400,2751,-56 — flicker from new renderer**: reproduced from the
  user's screenshot (pos 472,2766,-56, yaw -274, pitch ~41, after pressing
  the bridge button) as a dark band / black floor flooding scanlines. Root
  cause: the extended bridge (*8) puts a face fragment near the view plane
  that projects to a degenerate screen sliver; its leading/trailing edges
  carry identical u, the sort's tie order can fire the trailing edge first,
  and the port inserted/removed surfaces unconditionally — the surface stuck
  on the stack and flooded the rest of the row. Fixed by porting id's
  inverted-span spanstate guards (R_LeadingEdge/R_TrailingEdge) — commit
  c718f38, regression test (the two offending surfaces replayed verbatim)
  in test_r_edge.py.
