# VGA pixel-aspect (CRT) toggle — design

2026-06-12. Approved approach: separate vertical focal in the zbuf renderer
(`R_ViewChanged`'s `yscale = xscale * pixelAspect`) plus aspect-aware
letterboxing in the frontends (the CRT stretch). Toggle via a `pixel_aspect`
cvar and a video-menu item; default square (current look).

## Goal

Optionally reproduce how Quake actually looked on a VGA monitor: 320×200
rendered with `pixelAspect = 5/6` (more vertical world per row) and displayed
stretched to 4:3 (each row taller). Both halves together keep proportions
correct — circles stay circles. `R_ViewChanged`'s comment is the authority:
"proper 320*200 pixelAspect = 0.8333333".

## Non-goals

Wire/flat modes (square pixels stay); non-square particle/sprite *extents*
(their 1–2px quads stay square; only their projected y-centres follow
yfocal — invisible at this scale, documented deviation); changing the
default look; per-resolution automatic aspect guessing.

## Components

### Renderer (`quake/render.py`, zbuf path only)

- `self.pixel_aspect = 1.0` in `__init__` (next to `video_res`/`sbar_lines`).
- `render_zbuffer` computes `yfocal = focal * self.pixel_aspect` (where
  `focal` is the framebuffer-scaled local) and uses it at every vertical
  projection site: world/brush surfaces, alias/view-model vertices, sprites,
  beams, and particle y-centres (the `hh - cy * focal * iz` family inside
  `render_zbuffer`). Horizontal projection unchanged. No cull-plane work:
  the zbuf path clips at the near plane plus screen space, so the projection
  change propagates everywhere on its own (`plane_gradients` derives the
  depth/texture gradients from the projected coords). The `tany` frustum
  plane at ~line 1444 belongs to the painter (wire/flat) paths — untouched.
- `render_shaded`/wireframe paths untouched.

### Client (`client.py`)

- `self._pixel_aspect = 1.0`, persisted across maps exactly like
  `_zbuf_scale` (applied to each fresh `Renderer` in `_load_map`).
- Console cvar `pixel_aspect` (float, clamped 0.5–1.0), registered with the
  other render cvars; setting it updates the live renderer immediately.
- Video menu gains `ChoiceItem("Aspect", [("Square", 1.0), ("CRT", 5/6)])`
  next to Resolution, driving the same setter.
- `RenderFrame` gains `pixel_aspect: float = 1.0`; set from the cvar when
  `mode == "zbuf"`, else left at 1.0 (wire/flat never stretch).

### Frontends (the CRT stretch)

Letterbox the framebuffer at the *stretched* aspect — display height
`round(h / pixel_aspect)`:

- macOS: `mac_cocoa` passes the stretched height into
  `mac_ui.letterbox_rect` (call-site change; the helper already takes
  arbitrary src dims).
- Windows: same one-liner at `win_gdi`'s `win_ui.letterbox_rect` call site;
  `StretchDIBits` does the scaling.
- tkinter: `PhotoImage.zoom` is integer-only, so the stretch happens during
  the fb→PPM expansion: a precomputed source-row map of length
  `round(h / pixel_aspect)` duplicates every 5th row (cheap at 320×200);
  letterbox math then uses the expanded height.

The status bar stretches with the frame — exactly what a CRT did to it.

### Interactions

- Gun fudge (`V_CalcRefdef` +2 at viewsize 100) stays keyed on `sbar_lines`
  only, as WinQuake applies it regardless of aspect.
- `sbar_lines` and `pixel_aspect` compose: at 320×200+bar+CRT the view is
  320×152 rendered with yfocal, displayed inside a 320×240-proportioned
  letterbox.

## Testing

- Renderer: project a fixed world point at `pixel_aspect` 1.0 vs 5/6 —
  projected y moves toward the view centre by exactly 5/6 of its offset,
  x unchanged; cull plane admits proportionally more vertical world.
- Client: cvar set → `RenderFrame.pixel_aspect` reflects it in zbuf mode;
  persists across the `map` command; wire mode always reports 1.0; menu
  item drives the same state.
- `mac_ui.letterbox_rect` with stretched height: 320×200 at 5/6 in an
  800×600 window letterboxes as 4:3 (fills it edge to edge).
- tk row map: length and content for 200 rows at 5/6 (240 entries, each
  source row appearing once or twice, monotonically).
- Full suite stays green (default 1.0 = no behaviour change anywhere).
