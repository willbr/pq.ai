"""Quake-style overlay menu: a pure, UI-agnostic state machine behind the
Escape menu. A `Menu` owns an ordered list of items and a selection cursor;
items are either a `ChoiceItem` (cycles a fixed option list, firing a callback)
or an `ActionItem` (fires a callback on Enter). It knows nothing about keycodes,
ctypes or GDI -- the frontend maps native keys onto the key_* methods and draws
what view() reports.

Pure stdlib, no OS/UI imports (same discipline as quake/console.py and
quake/perf.py). Single-thread by design: only the frame/input thread touches it."""


class ChoiceItem:
    """A menu row that cycles a fixed list of (label, value) options. Cycling
    (left/right, or Enter) advances the selection with wraparound and fires
    on_select(value) with the newly selected value."""

    def __init__(self, title, options, index, on_select):
        self.title = title
        self.options = options          # list of (label, value)
        self.index = index
        self.on_select = on_select

    @property
    def value_label(self):
        return self.options[self.index][0]

    def cycle(self, step):
        self.index = (self.index + step) % len(self.options)
        self.on_select(self.options[self.index][1])

    def activate(self):
        self.cycle(1)


class ActionItem:
    """A menu row that fires on_activate() when chosen with Enter. Left/right do
    nothing (no value to cycle)."""

    def __init__(self, title, on_activate):
        self.title = title
        self.on_activate = on_activate

    @property
    def value_label(self):
        return ""

    def cycle(self, step):
        pass

    def activate(self):
        self.on_activate()


class Menu:
    """An overlay menu: a title, an ordered item list, a selection cursor and an
    `active` flag the frontend toggles. key_* methods drive it; view() returns a
    draw-ready snapshot."""

    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected = 0
        self.active = False

    def key_up(self):
        self.selected = (self.selected - 1) % len(self.items)

    def key_down(self):
        self.selected = (self.selected + 1) % len(self.items)

    def key_left(self):
        self.items[self.selected].cycle(-1)

    def key_right(self):
        self.items[self.selected].cycle(1)

    def key_enter(self):
        self.items[self.selected].activate()

    def key_escape(self):
        self.active = False

    def view(self):
        """Draw-ready snapshot: (title, [(label, value_label, is_selected), ...])."""
        rows = [(it.title, it.value_label, i == self.selected)
                for i, it in enumerate(self.items)]
        return (self.title, rows)


if __name__ == "__main__":
    # smoke test: build a tiny menu and exercise it
    log = []
    m = Menu("T", [ChoiceItem("R", [("a", 1), ("b", 2)], 0, log.append),
                   ActionItem("Q", lambda: log.append("q"))])
    m.key_right(); m.key_down(); m.key_enter()
    assert log == [2, "q"], log
    print("OK")
