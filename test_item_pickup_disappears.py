"""Regression test: picked-up items stop rendering.

Quake's item touch functions hide the item by setting `self.model =
string_null` (an empty model string) WITHOUT calling setmodel -- so the
entity's `modelindex` is left unchanged. The engine hides it in
SV_WriteEntitiesToClient (WinQuake sv_main.c):

    if (!ent->v.modelindex || !pr_strings[ent->v.model])
        continue;        // empty model string -> not sent to the client

So the render-entity enumerators must skip entities whose `.model` string is
empty, even when `modelindex` still points at the old model. Otherwise a
health/ammo box (bsp) or weapon/key/powerup (mdl) stays visible after pickup.
"""

from quake.sv import Server


class FakeVM:
    def __init__(self, ents):
        self.ents = ents
        self.num_edicts = len(ents)
        self.free = [e is None for e in ents]

    def fget_i(self, num, slot):
        return int(self.ents[num].get(slot, 0))

    def fget_v(self, num, slot):
        return self.ents[num].get(slot, (0.0, 0.0, 0.0))

    def fget_f(self, num, slot):
        return float(self.ents[num].get(slot, 0.0))


def _server(precache, ents):
    srv = Server.__new__(Server)
    srv.vm = FakeVM(ents)
    srv.model_precache = precache
    srv.f = {n: n for n in ("modelindex", "model", "origin", "angles", "frame")}
    return srv


def test_bsp_pickup_hidden_after_model_cleared():
    # model offset 7 = a live "maps/b_bh25.bsp" string; offset 0 = string_null
    precache = ["", "maps/e1m1.bsp", "maps/b_bh25.bsp"]
    ents = [
        {},
        {"modelindex": 2, "model": 7, "origin": (1.0, 2.0, 3.0)},   # on the floor
        {"modelindex": 2, "model": 0, "origin": (9.0, 9.0, 9.0)},   # picked up
    ]
    got = _server(precache, ents).bsp_model_entities()
    assert [g[0] for g in got] == [2]          # only the un-picked-up box
    assert got[0][1] == (1.0, 2.0, 3.0)


def test_mdl_item_hidden_after_model_cleared():
    precache = ["", "maps/e1m1.bsp", "progs/g_shot.mdl"]
    ents = [
        {},
        {"modelindex": 2, "model": 7, "origin": (1.0, 2.0, 3.0), "frame": 0},
        {"modelindex": 2, "model": 0, "origin": (9.0, 9.0, 9.0), "frame": 0},
    ]
    got = _server(precache, ents).alias_entities()
    assert [g[0] for g in got] == [2]
    assert got[0][1] == (1.0, 2.0, 3.0)


if __name__ == "__main__":
    test_bsp_pickup_hidden_after_model_cleared()
    test_mdl_item_hidden_after_model_cleared()
    print("OK")
