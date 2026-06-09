"""Quake-style console: the pure, UI-agnostic mechanism behind the drop-down
console. A `Console` owns a command table, a cvar table, command aliases, a
scrollback buffer, a single-line text editor, command history and
tab-completion. It knows nothing about keycodes, ctypes or GDI -- the frontend
maps native key/char events onto the key_* methods and draws what view_lines()
and the input line report.

Pure stdlib, no OS/UI imports (same discipline as quake/perf.py), so it loads
cleanly from inside the package and from the root frontends. Single-thread by
design: only the frame/input thread touches it."""

import collections


def tokenize(line):
    """Split a console line into tokens. Whitespace separates; a double-quoted
    run is a single token (quotes stripped). Mirrors Quake's Cmd_TokenizeString
    enough for our commands."""
    out = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        if line[i] == '"':
            i += 1
            start = i
            while i < n and line[i] != '"':
                i += 1
            out.append(line[start:i])
            if i < n:
                i += 1                          # skip closing quote
        else:
            start = i
            while i < n and not line[i].isspace():
                i += 1
            out.append(line[start:i])
    return out


def _common_prefix(strings):
    """Longest common leading substring of a non-empty list of strings."""
    s1, s2 = min(strings), max(strings)
    for i, ch in enumerate(s1):
        if ch != s2[i]:
            return s1[:i]
    return s1


def _wrap(text, width):
    """Word-wrap one (newline-free) line to `width` columns, hard-breaking any
    single token longer than the width. Returns a list of >=1 chunks."""
    if width <= 0 or len(text) <= width:
        return [text]
    out, line = [], ""
    for word in text.split(" "):
        while len(word) > width:                # hard-break an over-long token
            if line:
                out.append(line)
                line = ""
            out.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            out.append(line)
            line = word
    out.append(line)
    return out


class Cvar:
    """A named console variable. The value is stored as a string (like Quake);
    as_float/as_int/as_bool give tolerant numeric views (junk -> 0)."""

    def __init__(self, name, value, default=None, on_change=None, help=""):
        self.name = name
        self.value = str(value)
        self.default = str(default if default is not None else value)
        self.on_change = on_change
        self.help = help

    def as_float(self):
        try:
            return float(self.value)
        except ValueError:
            return 0.0

    def as_int(self):
        return int(self.as_float())

    def as_bool(self):
        return self.as_float() != 0.0


class _Command:
    __slots__ = ("fn", "help")

    def __init__(self, fn, help=""):
        self.fn = fn
        self.help = help


class TeeStdout:
    """Wrap a text stream, mirroring each complete line into a sink callback
    (the console's print). Partial writes buffer until a newline. Used by the
    frontend to make engine print() output appear in the console."""

    def __init__(self, stream, sink):
        self._stream = stream
        self._sink = sink
        self._buf = ""

    def write(self, s):
        self._stream.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sink(line)
        return len(s)

    def flush(self):
        self._stream.flush()


class Console:
    MAX_LINES = 1024
    PAGE = 8                                     # lines per PgUp/PgDn

    def __init__(self, width=80):
        self.commands = {}                       # name -> _Command
        self.cvars = {}                          # name -> Cvar
        self.aliases = {}                        # name -> console line
        self.lines = collections.deque(maxlen=self.MAX_LINES)
        self.input = ""
        self.cursor = 0
        self.history = []
        self.hist_pos = 0                        # == len(history) means "live edit"
        self.scroll = 0                          # lines scrolled up from the bottom
        self.active = False
        self.width = width

    # ---- registration ----
    def register_command(self, name, fn, help=""):
        self.commands[name] = _Command(fn, help)

    def register_cvar(self, name, default, on_change=None, help=""):
        cv = Cvar(name, default, default=default, on_change=on_change, help=help)
        self.cvars[name] = cv
        return cv

    def register_alias(self, name, text):
        self.aliases[name] = text

    # ---- output ----
    def print(self, text):
        for raw in str(text).split("\n"):
            if raw == "":
                self.lines.append("")
            else:
                for chunk in _wrap(raw, self.width):
                    self.lines.append(chunk)
        self.scroll = 0                          # newest output jumps into view

    # ---- execution ----
    def execute(self, line, _depth=0):
        args = tokenize(line)
        if not args:
            return
        name, rest = args[0], args[1:]
        if name in self.aliases:
            if _depth > 16:
                self.print("alias recursion too deep")
                return
            self.execute(self.aliases[name], _depth + 1)
            return
        if name in self.commands:
            try:
                self.commands[name].fn(rest)
            except Exception as e:
                self.print(f"error: {e}")
            return
        if name in self.cvars:
            cv = self.cvars[name]
            if rest:
                cv.value = rest[0]
                if cv.on_change:
                    cv.on_change(cv)
            else:
                self.print(f'"{name}" is "{cv.value}"')
            return
        self.print(f'Unknown command "{name}"')

    # ---- line editor ----
    def key_char(self, ch):
        if len(ch) != 1 or ch < " " or ch == "\x7f":
            return
        self.input = self.input[:self.cursor] + ch + self.input[self.cursor:]
        self.cursor += 1

    def key_backspace(self):
        if self.cursor > 0:
            self.input = self.input[:self.cursor - 1] + self.input[self.cursor:]
            self.cursor -= 1

    def key_delete(self):
        if self.cursor < len(self.input):
            self.input = self.input[:self.cursor] + self.input[self.cursor + 1:]

    def key_left(self):
        self.cursor = max(0, self.cursor - 1)

    def key_right(self):
        self.cursor = min(len(self.input), self.cursor + 1)

    def key_home(self):
        self.cursor = 0

    def key_end(self):
        self.cursor = len(self.input)

    def key_enter(self):
        line = self.input
        self.print("]" + line)
        self.input = ""
        self.cursor = 0
        if line.strip():
            self.history.append(line)
            self.execute(line)
        self.hist_pos = len(self.history)

    # ---- history ----
    def key_up(self):
        if self.hist_pos > 0:
            self.hist_pos -= 1
            self.input = self.history[self.hist_pos]
            self.cursor = len(self.input)

    def key_down(self):
        if self.hist_pos < len(self.history):
            self.hist_pos += 1
            self.input = (self.history[self.hist_pos]
                          if self.hist_pos < len(self.history) else "")
            self.cursor = len(self.input)

    # ---- tab-completion ----
    def key_tab(self):
        # completes against the whole input string (command-name only), like
        # stock Quake -- not the current token of a multi-word line
        prefix = self.input
        if not prefix:
            return
        names = sorted(set(self.commands) | set(self.cvars) | set(self.aliases))
        matches = [n for n in names if n.startswith(prefix)]
        if not matches:
            return
        if len(matches) == 1:
            self.input = matches[0] + " "
            self.cursor = len(self.input)
            return
        common = _common_prefix(matches)
        if len(common) > len(prefix):
            self.input = common
            self.cursor = len(common)
        self.print("  ".join(matches))

    # ---- scrollback paging / view ----
    def key_pageup(self):
        top = max(0, len(self.lines) - 1)
        self.scroll = min(top, self.scroll + self.PAGE)

    def key_pagedown(self):
        self.scroll = max(0, self.scroll - self.PAGE)

    def view_lines(self, n):
        """Return the up-to-`n` scrollback lines visible for the current scroll
        offset, oldest-to-newest. `n` is the viewport height in lines; this is
        the only place that knows it, so it also clamps `self.scroll` to
        [0, total - n] (an authoritative re-clamp each render, since key_pageup
        bounds scroll only coarsely)."""
        total = len(self.lines)
        self.scroll = max(0, min(self.scroll, max(0, total - n)))
        end = total - self.scroll
        start = max(0, end - n)
        return list(self.lines)[start:end]
