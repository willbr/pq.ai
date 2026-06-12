"""Regression test: weapon bob matches Quake's V_CalcRefdef.

The weapon felt like it bobbed far too much. Quake adds the bob to BOTH the
view origin and the gun (view.c V_CalcRefdef):

    r_refdef.vieworg[2] += cl.viewheight + bob;     // camera
    view->origin[2]     += cl.viewheight;
    view->origin[i]     += forward[i]*bob*0.4;       // gun, all 3 axes
    view->origin[2]     += bob;

So both the camera and the gun share the vertical +bob -- the gun's only
motion *relative to the view* is forward*bob*0.4, a small nudge. pq bobbed the
gun by the full bob against a static (un-bobbed) camera, so the weapon sloshed
~4-7 units on screen instead of riding nearly still with the view.

view_origins() reproduces Quake's relationship: camera eye carries +bob, and
the gun is eye + forward*bob*0.4.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from client import view_origins, stair_smooth


def test_camera_carries_the_bob():
    eye, _gun = view_origins((0.0, 0.0, 0.0), 22.0, (1.0, 0.0, 0.0), 4.0)
    assert eye[2] == 0.0 + 22.0 + 4.0          # vieworg[2] += viewheight + bob


def test_gun_rides_with_the_view_not_against_it():
    bob = 4.0
    # looking level: forward is horizontal, so the gun has no vertical slosh
    eye, gun = view_origins((0.0, 0.0, 0.0), 22.0, (1.0, 0.0, 0.0), bob)
    assert gun[2] - eye[2] == 0.0              # NOT the full bob (the old bug)
    assert gun[0] - eye[0] == 1.0 * bob * 0.4  # only the forward*bob*0.4 nudge


def test_gun_vertical_offset_is_only_forward_times_bob():
    bob = 4.0
    # looking down: forward has a -Z component
    eye, gun = view_origins((0.0, 0.0, 0.0), 22.0, (0.0, 0.0, -1.0), bob)
    assert abs((gun[2] - eye[2]) - (-1.0 * bob * 0.4)) < 1e-9  # -1.6, not -1.6 - bob


def test_stair_smooth_lags_gun_with_eye():
    """Stepping up a stair, the eye lags by eye_z_offset (negative). The gun must
    lag by the SAME amount so it stays locked to the view; the eye<->gun delta is
    preserved (view.c:975-976). The old bug smoothed only the eye, so the gun
    kept its unsmoothed z and drifted up the screen."""
    bob = 4.0
    eye, gun = view_origins((0.0, 0.0, 0.0), 22.0, (0.0, 0.0, -1.0), bob)
    delta_before = gun[2] - eye[2]
    dz = -11.95                                   # mid stair-step (clamped to 12)
    seye, sgun = stair_smooth(eye, gun, dz)
    assert abs((seye[2] - eye[2]) - dz) < 1e-9    # eye lagged by dz
    assert abs((sgun[2] - gun[2]) - dz) < 1e-9    # gun lagged by the SAME dz
    assert abs((sgun[2] - seye[2]) - delta_before) < 1e-9  # delta preserved


def test_stair_smooth_tolerates_no_gun():
    """Dead / intermission frames have no weapon: gun_org is None and must pass
    through untouched while the eye still smooths."""
    eye, gun = stair_smooth((1.0, 2.0, 3.0), None, -5.0)
    assert eye == (1.0, 2.0, -2.0)
    assert gun is None


if __name__ == "__main__":
    test_camera_carries_the_bob()
    test_gun_rides_with_the_view_not_against_it()
    test_gun_vertical_offset_is_only_forward_times_bob()
    test_stair_smooth_lags_gun_with_eye()
    test_stair_smooth_tolerates_no_gun()
    print("OK")
