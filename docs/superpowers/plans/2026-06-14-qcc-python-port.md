# QuakeC compiler (qcc) — pure-Python port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure-Python-stdlib QuakeC compiler in a new `quake/qcc/` subpackage that compiles id's `v101qc` source to a `progs.dat` byte-identical to the output of id's genuine `qccdos.exe`.

**Architecture:** A Pythonic reimplementation (idiomatic classes, clean module boundaries) of id's `pr_lex.c` + `pr_comp.c` + `qcc.c`. Byte-identity is achieved by honoring a fixed set of ordering invariants (temp-global allocation order, immediate dedup scan, insertion-ordered def list, type interning, CRC-over-generated-progdefs.h) — documented per module and proven by a per-lump + whole-file diff against a committed oracle.

**Tech Stack:** Python 3.13 stdlib only (`struct`, `array`). DOSBox-x + `qccdos.exe` used once to generate the committed oracle. Tests are standalone `tests/test_qcc_*.py` scripts (repo convention).

**Spec:** `docs/superpowers/specs/2026-06-14-qcc-python-port-design.md`

**Reference C (read these as you port):** `quake-source/quake-tools/qcc/{pr_lex.c,pr_comp.c,qcc.c,qcc.h,pr_comp.h}`. Cite origins in docstrings/comments (project convention).

---

## File structure

| File | Responsibility |
|---|---|
| `quake/qcc/__init__.py` | `compile_progs_src(path) -> bytes`; re-export `QCCError`. |
| `quake/qcc/errors.py` | `QCCError(file, line, message)`. |
| `quake/qcc/types.py` | `etype` constants, `type_size`, `Type`, `TypeTable` (base types + interning). |
| `quake/qcc/lexer.py` | `Token`, `Lexer`: typed token stream + `$frame` macros. |
| `quake/qcc/symbols.py` | `Def`, `CompileState` (defs/globals/strings/statements/functions/counters/type table), `get_def`, `parse_immediate`, `copy_string`. |
| `quake/qcc/codegen.py` | `OP_*` constants, `OPCODES` table, `emit` (PR_Statement), address-rewrite helper. |
| `quake/qcc/compiler.py` | `Compiler`: `parse_type`, expression/term/value, function-call, statement, `[state]`, function body, defs, `compile_file`. |
| `quake/qcc/writer.py` | `progdefs_text`, `progdefs_crc`, `write_progs(state) -> bytes`. |
| `quake/qcc/__main__.py` | `python -m quake.qcc [-src DIR]` CLI. |
| `tests/gen_qcc_oracle.py` | Regenerate the oracle via DOSBox-x (kept for regen, not run in CI). |
| `tests/data/progs_v101_oracle.dat` | Committed oracle (410,616 bytes, CRC 5927). |
| `tests/test_qcc_lexer.py` | Lexer unit tests. |
| `tests/test_qcc_symbols.py` | Def/immediate/field-offset unit tests. |
| `tests/test_qcc_codegen.py` | Opcode table + emit unit tests. |
| `tests/test_qcc_compile.py` | Per-lump + byte-identity + functional-boot tests. |

**Key C conventions to preserve (from `InitData`/`PR_BeginCompilation`):**
- `numpr_globals` starts at `RESERVED_OFS = 28`. Slot 0 = `OFS_NULL`, 1 = `OFS_RETURN`, params at `4,7,10,...,25`.
- Reserved sentinel slots: statement index 0, function index 0, globaldef index 0, fielddef index 0 are **zero-filled**; string offset 0 is a NUL. So those lists start populating at index 1, and the strings blob starts at offset 1.
- Strings blob padded up to 4 bytes at the end (`strofs = (strofs+3) & ~3`).
- **Temps are NOT named defs:** `emit` allocates a global slot for a result but never adds it to the def list. So `globals` lump (all slots, incl. temps) ≫ `globaldefs` (named defs + immediates only).

---

## Task 1: Oracle generation + subpackage scaffold

**Files:**
- Create: `tests/gen_qcc_oracle.py`
- Create: `tests/data/progs_v101_oracle.dat` (generated artifact, committed)
- Create: `quake/qcc/__init__.py`, `quake/qcc/errors.py`
- Test: `tests/test_qcc_compile.py` (oracle sanity only, for now)

- [ ] **Step 1: Write the oracle generator**

Create `tests/gen_qcc_oracle.py`:

```python
"""Regenerate the qcc oracle: compile id's v101qc with id's genuine qccdos.exe
under DOSBox-x, and copy the result to tests/data/progs_v101_oracle.dat.

This is the byte-identity ground truth for quake/qcc. Not run in CI (needs
DOSBox-x: `brew install dosbox-x`); the produced .dat is committed. v101qc and
its compiled output are id's GPLv2 release, freely redistributable.

Run:  python tests/gen_qcc_oracle.py
"""
import _bootstrap  # noqa: F401
import os, shutil, subprocess, tempfile

QCC_DIR = "quake-source/quake-tools/qcc"
OUT = "tests/data/progs_v101_oracle.dat"


def main():
    if not os.path.isdir(QCC_DIR):
        raise SystemExit("run `python setup.py` first to fetch quake-tools")
    work = tempfile.mkdtemp(prefix="qccoracle")
    shutil.copy(f"{QCC_DIR}/qccdos.exe", work)
    shutil.copy(f"{QCC_DIR}/cwsdpmi.exe", work)
    shutil.copytree(f"{QCC_DIR}/v101qc", f"{work}/v101qc")
    env = dict(os.environ, SDL_VIDEODRIVER="dummy")
    subprocess.run(
        ["dosbox-x", "-silent",
         "-c", f"mount c {work}", "-c", "c:", "-c", "cd v101qc",
         "-c", r"c:\qccdos.exe", "-c", "exit"],
        env=env, timeout=180, check=True)
    src = f"{work}/progs.dat"
    if not os.path.exists(src):
        raise SystemExit("qccdos.exe produced no progs.dat")
    os.makedirs("tests/data", exist_ok=True)
    shutil.copy(src, OUT)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate and verify the oracle**

Run:
```bash
cd /Users/wjbr/src/pq.ai && python tests/gen_qcc_oracle.py
```
Expected: `wrote tests/data/progs_v101_oracle.dat (410616 bytes)`

- [ ] **Step 3: Scaffold the subpackage**

Create `quake/qcc/errors.py`:

```python
"""qcc compile errors. Mirrors id's PR_ParseError message style (pr_lex.c)."""


class QCCError(Exception):
    def __init__(self, file, line, message):
        self.file, self.line, self.message = file, line, message
        super().__init__(f"{file}:{line}:{message}")
```

Create `quake/qcc/__init__.py`:

```python
"""Pure-stdlib QuakeC compiler: compiles a progs.src manifest + its .qc files
into a version-6 progs.dat (the format quake/progs.py loads). A Pythonic
reimplementation of id's qcc (quake-source/quake-tools/qcc/). Byte-identical to
id's qccdos.exe on the same source -- see tests/test_qcc_compile.py.

Public API:
    compile_progs_src(path) -> bytes
"""
from .errors import QCCError

__all__ = ["compile_progs_src", "QCCError"]


def compile_progs_src(path):
    from .compiler import compile_progs_src as _impl
    return _impl(path)
```

- [ ] **Step 4: Write the oracle sanity test**

Create `tests/test_qcc_compile.py`:

```python
"""Byte-identity tests for quake/qcc against id's qccdos.exe oracle (v101qc).
See docs/superpowers/specs/2026-06-14-qcc-python-port-design.md."""
import _bootstrap  # noqa: F401
import struct

ORACLE = "tests/data/progs_v101_oracle.dat"


def _oracle():
    with open(ORACLE, "rb") as f:
        return f.read()


def test_oracle_present_and_valid():
    data = _oracle()
    assert len(data) == 410616, len(data)
    ver, crc = struct.unpack_from("<ii", data, 0)
    assert ver == 6 and crc == 5927, (ver, crc)


if __name__ == "__main__":
    test_oracle_present_and_valid()
    print("OK")
```

- [ ] **Step 5: Run the sanity test**

Run: `PQ_AUDIO=0 python tests/test_qcc_compile.py`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add quake/qcc tests/gen_qcc_oracle.py tests/data/progs_v101_oracle.dat tests/test_qcc_compile.py
git commit -m "feat(qcc): oracle generator + committed v101qc reference + scaffold"
```

---

## Task 2: Type system (`types.py`)

**Files:**
- Create: `quake/qcc/types.py`
- Test: `tests/test_qcc_symbols.py` (type table portion)

- [ ] **Step 1: Write the failing test**

Create `tests/test_qcc_symbols.py`:

```python
"""Unit tests for quake/qcc type table, defs, immediate dedup, field offsets."""
import _bootstrap  # noqa: F401

from quake.qcc.types import (TypeTable, ev_float, ev_vector, ev_field,
                             ev_function, ev_void, type_size)


def test_type_size():
    assert type_size == (1, 1, 1, 3, 1, 1, 1, 1)


def test_base_types_singletons():
    tt = TypeTable()
    assert tt.float is tt.float
    assert tt.float.type == ev_float
    assert tt.vector.type == ev_vector


def test_field_type_interned():
    tt = TypeTable()
    a = tt.field_of(tt.float)
    b = tt.field_of(tt.float)
    assert a is b                       # interned
    assert a.type == ev_field and a.aux_type is tt.float


def test_function_type_interned():
    tt = TypeTable()
    a = tt.function_of(tt.void, (tt.float,))
    b = tt.function_of(tt.void, (tt.float,))
    assert a is b
    assert a.type == ev_function and a.aux_type is tt.void
    assert a.parm_types == (tt.float,) and a.num_parms == 1


if __name__ == "__main__":
    for fn in (test_type_size, test_base_types_singletons,
               test_field_type_interned, test_function_type_interned):
        fn()
    print("OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_qcc_symbols.py`
Expected: FAIL — `ModuleNotFoundError: quake.qcc.types`

- [ ] **Step 3: Implement `types.py`**

```python
"""QuakeC type system. Ports pr_lex.c type defs + PR_FindType interning.

etype_t (pr_comp.h): the 8 value kinds. type_size is slots-per-value (only
vector is 3). Complex types (field/function) are interned by structure so
identity comparison works in codegen, mirroring PR_FindType (pr_lex.c)."""

ev_void, ev_string, ev_float, ev_vector, ev_entity, ev_field, ev_function, ev_pointer = range(8)

type_size = (1, 1, 1, 3, 1, 1, 1, 1)


class Type:
    __slots__ = ("type", "aux_type", "parm_types", "num_parms")

    def __init__(self, type, aux_type=None, parm_types=(), num_parms=0):
        self.type = type
        self.aux_type = aux_type        # field value type / function return type
        self.parm_types = tuple(parm_types)
        self.num_parms = num_parms      # -1 = varargs

    def __repr__(self):
        return f"<Type {self.type} aux={self.aux_type and self.aux_type.type}>"


class TypeTable:
    """Owns the base type singletons and interns complex types."""

    def __init__(self):
        self.void = Type(ev_void)
        self.string = Type(ev_string)
        self.float = Type(ev_float)
        self.vector = Type(ev_vector)
        self.entity = Type(ev_entity)
        self.field = Type(ev_field)
        # type_function is a void() used for state forward-decls (pr_lex.c:49)
        self.function = Type(ev_function, aux_type=self.void)
        self.pointer = Type(ev_pointer)
        self.floatfield = Type(ev_field, aux_type=self.float)  # pr_lex.c:53
        self._complex = [self.function]   # PR_BeginCompilation links type_function

    def _find(self, proto):
        for t in self._complex:
            if (t.type == proto.type and t.aux_type is proto.aux_type
                    and t.num_parms == proto.num_parms
                    and t.parm_types == proto.parm_types):
                return t
        self._complex.append(proto)
        return proto

    def field_of(self, aux):
        return self._find(Type(ev_field, aux_type=aux))

    def function_of(self, ret, parm_types, num_parms=None):
        n = len(parm_types) if num_parms is None else num_parms
        return self._find(Type(ev_function, aux_type=ret,
                               parm_types=parm_types, num_parms=n))
```

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_qcc_symbols.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add quake/qcc/types.py tests/test_qcc_symbols.py
git commit -m "feat(qcc): type system with field/function interning"
```

---

## Task 3: Lexer (`lexer.py`)

**Files:**
- Create: `quake/qcc/lexer.py`
- Test: `tests/test_qcc_lexer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_qcc_lexer.py`:

```python
"""Unit tests for quake/qcc lexer (ports pr_lex.c PR_Lex)."""
import _bootstrap  # noqa: F401

from quake.qcc.lexer import Lexer, TT_NAME, TT_PUNCT, TT_IMMEDIATE, TT_EOF
from quake.qcc.types import TypeTable, ev_float, ev_string, ev_vector


def toks(src):
    lx = Lexer(src, "t.qc", TypeTable())
    out = []
    while True:
        lx.next()
        if lx.token_type == TT_EOF:
            break
        out.append((lx.token_type, lx.token))
    return out


def test_names_and_punct():
    assert toks("void foo;") == [
        (TT_NAME, "void"), (TT_NAME, "foo"), (TT_PUNCT, ";")]


def test_maximal_munch():
    # "<=" must win over "<"
    assert toks("a <= b") == [
        (TT_NAME, "a"), (TT_PUNCT, "<="), (TT_NAME, "b")]


def test_comments_skipped():
    assert toks("a // gone\n b /* also gone */ c") == [
        (TT_NAME, "a"), (TT_NAME, "b"), (TT_NAME, "c")]


def test_float_immediate():
    lx = Lexer("3.5", "t.qc", TypeTable())
    lx.next()
    assert lx.token_type == TT_IMMEDIATE
    assert lx.immediate_type.type == ev_float
    assert lx.immediate == 3.5


def test_negative_immediate_gotcha():
    # "a-5" lexes name then NEGATIVE immediate (pr_lex.c:462) -> two tokens
    out = toks("a-5")
    assert out[0] == (TT_NAME, "a")
    assert out[1][0] == TT_IMMEDIATE


def test_string_escapes():
    lx = Lexer(r'"hi\nthere"', "t.qc", TypeTable())
    lx.next()
    assert lx.immediate_type.type == ev_string
    assert lx.immediate_string == "hi\nthere"


def test_vector_immediate():
    lx = Lexer("'1 -2 3.5'", "t.qc", TypeTable())
    lx.next()
    assert lx.immediate_type.type == ev_vector
    assert lx.immediate == (1.0, -2.0, 3.5)


def test_frame_macros():
    # $frame defines indices; later $name -> float immediate of its index
    out = toks("$frame walk1 walk2\n $walk2")
    assert out[-1][0] == TT_IMMEDIATE
    lx = Lexer("$frame walk1 walk2\n $walk2", "t.qc", TypeTable())
    seq = []
    while True:
        lx.next()
        if lx.token_type == TT_EOF:
            break
        seq.append(lx.immediate if lx.token_type == TT_IMMEDIATE else lx.token)
    assert seq == [1.0]                  # walk2 -> index 1


if __name__ == "__main__":
    for fn in (test_names_and_punct, test_maximal_munch, test_comments_skipped,
               test_float_immediate, test_negative_immediate_gotcha,
               test_string_escapes, test_vector_immediate, test_frame_macros):
        fn()
    print("OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_qcc_lexer.py`
Expected: FAIL — `ModuleNotFoundError: quake.qcc.lexer`

- [ ] **Step 3: Implement `lexer.py`**

```python
"""QuakeC lexer. Ports pr_lex.c (PR_Lex and friends): one token of lookahead,
maximal-munch punctuation, float/vector/string immediates, // and /* */
comments, and $frame macro substitution.

Frame macros (pr_lex.c PR_LexGrab): `$frame a b c` records names with auto-
incrementing indices; a later `$a` is replaced inline by its float index. Other
$commands ($cd/$origin/$base/$flags/$scale/$skin) are skipped to end of line."""

from .errors import QCCError

TT_EOF, TT_NAME, TT_PUNCT, TT_IMMEDIATE = range(4)

# longer symbols MUST precede shorter partial matches (pr_lex.c:38)
PUNCTUATION = ["&&", "||", "<=", ">=", "==", "!=", ";", ",", "!", "*", "/",
               "(", ")", "-", "+", "=", "[", "]", "{", "}", "...", ".",
               "<", ">", "#", "&", "|"]

_SKIP_GRABS = {"cd", "origin", "base", "flags", "scale", "skin"}


class Lexer:
    def __init__(self, src, filename, types):
        self.s = src
        self.p = 0
        self.n = len(src)
        self.file = filename
        self.line = 1
        self.types = types
        self.token = ""
        self.token_type = TT_EOF
        self.immediate_type = None
        self.immediate = None            # float or (x,y,z)
        self.immediate_string = ""
        self.frames = {}                 # name -> index

    def error(self, msg):
        raise QCCError(self.file, self.line, msg)

    # --- character helpers ---
    def _peek(self, k=0):
        i = self.p + k
        return self.s[i] if i < self.n else "\0"

    def _skip_ws(self):
        while self.p < self.n:
            c = self.s[self.p]
            if c == "\n":
                self.line += 1
                self.p += 1
            elif c <= " ":
                self.p += 1
            elif c == "/" and self._peek(1) == "/":
                while self.p < self.n and self.s[self.p] != "\n":
                    self.p += 1
            elif c == "/" and self._peek(1) == "*":
                self.p += 2
                while self.p < self.n and not (self.s[self.p - 1] == "*"
                                               and self.s[self.p] == "/"):
                    if self.s[self.p] == "\n":
                        self.line += 1
                    self.p += 1
                self.p += 1
            else:
                break

    # --- token producers ---
    def _lex_string(self):
        self.p += 1
        out = []
        while True:
            if self.p >= self.n:
                self.error("EOF inside quote")
            c = self.s[self.p]; self.p += 1
            if c == "\n":
                self.error("newline inside quote")
            if c == "\\":
                e = self.s[self.p]; self.p += 1
                if e == "n":
                    out.append("\n")
                elif e == '"':
                    out.append('"')
                else:
                    self.error("Unknown escape char")
            elif c == '"':
                break
            else:
                out.append(c)
        self.immediate_string = "".join(out)
        self.token = self.immediate_string
        self.token_type = TT_IMMEDIATE
        self.immediate_type = self.types.string

    def _lex_number(self):
        start = self.p
        while self.p < self.n and (self.s[self.p].isdigit() or self.s[self.p] == "."):
            self.p += 1
        self.token = self.s[start:self.p]
        return float(self.token)

    def _lex_vector(self):
        self.p += 1
        v = []
        for _ in range(3):
            # leading sign handled like PR_LexNumber via atof
            if self._peek() == "-":
                self.p += 1
                v.append(-self._lex_number())
            else:
                v.append(self._lex_number())
            while self.p < self.n and self.s[self.p] <= " " and self.s[self.p] != "'":
                if self.s[self.p] == "\n":
                    self.line += 1
                self.p += 1
        if self._peek() != "'":
            self.error("Bad vector")
        self.p += 1
        self.immediate = (v[0], v[1], v[2])
        self.token_type = TT_IMMEDIATE
        self.immediate_type = self.types.vector

    def _lex_name(self):
        start = self.p
        while self.p < self.n:
            c = self.s[self.p]
            if c.isalnum() or c == "_":
                self.p += 1
            else:
                break
        self.token = self.s[start:self.p]
        self.token_type = TT_NAME

    def _lex_punct(self):
        for sym in PUNCTUATION:
            if self.s.startswith(sym, self.p):
                self.token = sym
                self.p += len(sym)
                self.token_type = TT_PUNCT
                return
        self.error("Unknown punctuation")

    # --- $ grab handling (pr_lex.c PR_LexGrab) ---
    def _simple_token(self):
        """Parse a whitespace/comma/semicolon-delimited word on the current
        line; return None at end of line/file (pr_lex.c PR_SimpleGetToken)."""
        while self.p < self.n and self.s[self.p] <= " ":
            if self.s[self.p] in ("\n", "\0"):
                return None
            self.p += 1
        start = self.p
        while self.p < self.n and self.s[self.p] > " " and self.s[self.p] not in ",;":
            self.p += 1
        return self.s[start:self.p] if self.p > start else None

    def _lex_grab(self):
        self.p += 1  # skip $
        word = self._simple_token()
        if word is None:
            self.error("hanging $")
        if word == "frame":
            while True:
                w = self._simple_token()
                if w is None:
                    break
                self.frames[w] = len(self.frames)
            self.next()
        elif word in _SKIP_GRABS:
            while self._simple_token() is not None:
                pass
            self.next()
        else:
            if word not in self.frames:
                self.error(f"Unknown frame macro ${word}")
            self.token = str(self.frames[word])
            self.token_type = TT_IMMEDIATE
            self.immediate_type = self.types.float
            self.immediate = float(self.frames[word])

    # --- main entry (pr_lex.c PR_Lex) ---
    def next(self):
        self.token = ""
        self._skip_ws()
        if self.p >= self.n:
            self.token_type = TT_EOF
            return
        c = self.s[self.p]
        if c == '"':
            self._lex_string()
        elif c == "'":
            self._lex_vector()
        elif c.isdigit() or (c == "-" and self._peek(1).isdigit()):
            self.token_type = TT_IMMEDIATE
            self.immediate_type = self.types.float
            if c == "-":
                self.p += 1
                self.immediate = -self._lex_number()
            else:
                self.immediate = self._lex_number()
        elif c.isalpha() or c == "_":
            self._lex_name()
        elif c == "$":
            self._lex_grab()
        else:
            self._lex_punct()
```

> **Note on `\0`:** `self.s` is real source text; the sentinel `"\0"` from `_peek` only signals end-of-buffer and never matches `isdigit()`/`isalpha()`.

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_qcc_lexer.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add quake/qcc/lexer.py tests/test_qcc_lexer.py
git commit -m "feat(qcc): lexer with frame macros and immediates"
```

---

## Task 4: Symbols, defs, immediates (`symbols.py`)

**Files:**
- Create: `quake/qcc/symbols.py`
- Modify: `tests/test_qcc_symbols.py` (add Def/immediate/field tests)

This module owns `CompileState` (the shared, persists-across-files compile data) plus `get_def` (PR_GetDef), `parse_immediate` (PR_ParseImmediate), `copy_string` (CopyString).

- [ ] **Step 1: Write the failing tests (append to `tests/test_qcc_symbols.py`)**

Add these imports at the top and functions to the file (and to the `__main__` runner list):

```python
from quake.qcc.symbols import CompileState, RESERVED_OFS


def test_reserved_offset():
    st = CompileState()
    assert RESERVED_OFS == 28
    assert st.numpr_globals == 28        # PR_BeginCompilation


def test_vector_autogen_members():
    st = CompileState()
    org = st.get_def(st.types.vector, "org", None, allocate=True)
    x = st.get_def(None, "org_x", None, allocate=False)
    y = st.get_def(None, "org_y", None, allocate=False)
    z = st.get_def(None, "org_z", None, allocate=False)
    assert x and y and z
    assert org.ofs == x.ofs              # parent shares first element's slot
    assert (y.ofs, z.ofs) == (x.ofs + 1, x.ofs + 2)
    assert st.numpr_globals == 28 + 3    # vector consumed 3 slots


def test_field_offset_allocation():
    st = CompileState()
    h = st.get_def(st.types.field_of(st.types.float), "health", None, True)
    # the field's global slot holds its entity offset; first field offset is 0
    assert st.gi(h.ofs) == 0
    assert st.size_fields == 1
    o = st.get_def(st.types.field_of(st.types.float), "origin2", None, True)
    assert st.gi(o.ofs) == 1
    assert st.size_fields == 2


def test_immediate_dedup():
    st = CompileState()
    a = st.parse_immediate(st.types.float, 5.0)
    b = st.parse_immediate(st.types.float, 5.0)   # same value -> same def/slot
    c = st.parse_immediate(st.types.float, 6.0)
    assert a is b and a.ofs == b.ofs
    assert c.ofs != a.ofs
    assert st.gf(a.ofs) == 5.0 and st.gf(c.ofs) == 6.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_qcc_symbols.py`
Expected: FAIL — `ImportError: cannot import name 'CompileState'`

- [ ] **Step 3: Implement `symbols.py`**

```python
"""Compile state, defs, immediates. Ports pr_comp.c PR_GetDef / PR_ParseImmediate
and qcc.c CopyString / InitData.

CompileState is the single accumulating unit id keeps across all source files
(no reset between files). It holds:
  defs       insertion-ordered named defs + immediates (-> globaldefs lump)
  globals    float view + int view over one growable buffer (eval_t union)
  numpr_globals  next free global slot (incl. temps, which are NOT in `defs`)
  strings    NUL-separated blob, byte-indexed (string 0 is NUL)
  statements emitted dstatements (index 0 reserved/zero)
  functions  dfunctions (index 0 reserved/zero)
  size_fields running entity field-offset counter (-> entityfields)

Byte-identity invariants live here: temp slots are never reused; immediates are
deduped by a linear scan of `defs` in insertion order (first appearance fixes
the offset); vector defs auto-create _x/_y/_z."""

import array
from .types import (TypeTable, type_size, ev_void, ev_vector, ev_field,
                    ev_function, ev_string, ev_float)

RESERVED_OFS = 28           # OFS_NULL + OFS_RETURN + 8*3 param slots
OFS_RETURN = 1
OFS_PARM0 = 4

_MAX_GLOBALS = 1 << 16      # grown on demand


class Def:
    __slots__ = ("type", "name", "ofs", "scope", "initialized")

    def __init__(self, type, name, ofs, scope=None, initialized=False):
        self.type = type
        self.name = name
        self.ofs = ofs
        self.scope = scope          # the function Def it's local to, or None
        self.initialized = initialized


class CompileState:
    def __init__(self):
        self.types = TypeTable()
        self.defs = []                          # insertion order
        self._by_name = {}                      # name -> [Def, ...] (scoping)
        self._buf = bytearray(_MAX_GLOBALS * 4)
        self._gf = memoryview(self._buf).cast("f")
        self._gi = memoryview(self._buf).cast("i")
        self.numpr_globals = RESERVED_OFS       # PR_BeginCompilation
        self.strings = bytearray(b"\x00")       # string 0 = NUL (InitData strofs=1)
        self.statements = [(0, 0, 0, 0)]        # index 0 reserved (InitData)
        self.statement_lines = [0]
        self.functions = [None]                 # index 0 reserved (InitData)
        self.size_fields = 0
        self.cur_file = ""                      # s_file string offset filename
        # PR_ParseImmediate / value plumbing
        self.def_ret = Def(None, "temp", OFS_RETURN)
        self.def_parms = [Def(None, "temp", OFS_PARM0 + 3 * i) for i in range(8)]

    # --- global accessors (eval_t union) ---
    def gf(self, o):
        return self._gf[o]

    def gi(self, o):
        return self._gi[o]

    def set_gf(self, o, v):
        self._gf[o] = v

    def set_gi(self, o, v):
        self._gi[o] = v

    def _alloc_globals(self, n):
        ofs = self.numpr_globals
        self.numpr_globals += n
        return ofs

    # --- strings (qcc.c CopyString) ---
    def copy_string(self, s):
        old = len(self.strings)
        self.strings += s.encode("latin-1") + b"\x00"
        return old

    # --- PR_GetDef (pr_comp.c) ---
    def get_def(self, type, name, scope, allocate):
        for d in self._by_name.get(name, ()):
            if d.scope is not None and d.scope is not scope:
                continue                        # different function's local
            if type is not None and d.type is not type:
                raise _mismatch(name)
            return d
        if not allocate:
            return None

        d = Def(type, name, self.numpr_globals, scope=scope)
        self.defs.append(d)
        self._by_name.setdefault(name, []).append(d)

        if type.type == ev_vector:
            for suf in ("_x", "_y", "_z"):
                self.get_def(self.types.float, name + suf, scope, True)
        else:
            self._alloc_globals(type_size[type.type])

        if type.type == ev_field:
            self.set_gi(d.ofs, self.size_fields)
            if type.aux_type.type == ev_vector:
                for suf in ("_x", "_y", "_z"):
                    self.get_def(self.types.floatfield, name + suf, scope, True)
            else:
                self.size_fields += type_size[type.aux_type.type]
        return d

    # --- PR_ParseImmediate (pr_comp.c) ---
    def parse_immediate(self, imm_type, value):
        """value: float, str, or (x,y,z). Dedup against existing constants."""
        for d in self.defs:
            if not d.initialized or d.type is not imm_type:
                continue
            if imm_type is self.types.string:
                if _str_at(self.strings, self.gi(d.ofs)) == value:
                    return d
            elif imm_type is self.types.float:
                if self.gf(d.ofs) == value:
                    return d
            elif imm_type is self.types.vector:
                if (self.gf(d.ofs), self.gf(d.ofs + 1), self.gf(d.ofs + 2)) == value:
                    return d
        # allocate a new constant
        d = Def(imm_type, "IMMEDIATE", self.numpr_globals,
                scope=None, initialized=True)
        self.defs.append(d)
        self._by_name.setdefault("IMMEDIATE", []).append(d)
        self._alloc_globals(type_size[imm_type.type])
        if imm_type is self.types.string:
            self.set_gi(d.ofs, self.copy_string(value))
        elif imm_type is self.types.float:
            self.set_gf(d.ofs, value)
        else:                                    # vector
            self.set_gf(d.ofs, value[0])
            self.set_gf(d.ofs + 1, value[1])
            self.set_gf(d.ofs + 2, value[2])
        return d


def _mismatch(name):
    from .errors import QCCError
    return QCCError("?", 0, f"Type mismatch on redeclaration of {name}")


def _str_at(blob, ofs):
    end = blob.index(0, ofs)
    return blob[ofs:end].decode("latin-1")
```

> **Note:** `_mismatch` lacks file/line context here; `compiler.py` catches and re-raises symbol errors with the lexer's current position (Task 6). Acceptable — the message text matches id.

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_qcc_symbols.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add quake/qcc/symbols.py tests/test_qcc_symbols.py
git commit -m "feat(qcc): compile state, defs, immediate dedup, field offsets"
```

---

## Task 5: Codegen — opcodes + emit (`codegen.py`)

**Files:**
- Create: `quake/qcc/codegen.py`
- Test: `tests/test_qcc_codegen.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_qcc_codegen.py`:

```python
"""Unit tests for quake/qcc codegen: opcode table integrity + emit."""
import _bootstrap  # noqa: F401

from quake.qcc.codegen import (OPCODES, OP_DONE, OP_MUL_F, OP_STORE_F,
                               OP_LOAD_F, OP_ADDRESS, OP_RETURN, OP_CALL0,
                               OP_BITOR, emit)
from quake.qcc.symbols import CompileState


def test_opcode_indices_match_names():
    # the array index IS the bytecode op (statement->op = op - pr_opcodes)
    assert OPCODES[OP_DONE].opname == "DONE"
    assert OPCODES[OP_MUL_F].opname == "MUL_F"
    assert OPCODES[OP_STORE_F].opname == "STORE_F"
    assert OPCODES[OP_LOAD_F].opname == "INDIRECT"   # the . load family
    assert OPCODES[OP_ADDRESS].opname == "ADDRESS"
    assert OPCODES[OP_RETURN].opname == "RETURN"
    assert OPCODES[OP_CALL0].opname == "CALL0"
    assert OPCODES[OP_BITOR].opname == "BITOR"


def test_emit_allocates_result_global():
    st = CompileState()
    a = st.parse_immediate(st.types.float, 2.0)
    b = st.parse_immediate(st.types.float, 3.0)
    before = st.numpr_globals
    c = emit(st, OPCODES[OP_MUL_F], a, b)
    assert c.ofs == before                  # result is a fresh temp global
    assert st.numpr_globals == before + 1   # never reused
    op, sa, sb, sc = st.statements[-1]
    assert op == OP_MUL_F and sa == a.ofs and sb == b.ofs and sc == c.ofs


def test_emit_store_no_result():
    st = CompileState()
    src = st.parse_immediate(st.types.float, 1.0)
    dst = st.get_def(st.types.float, "x", None, True)
    before = st.numpr_globals
    ret = emit(st, OPCODES[OP_STORE_F], src, dst)   # right-associative
    assert st.numpr_globals == before               # no temp allocated
    assert ret is src                                # store returns its rhs
    op, sa, sb, sc = st.statements[-1]
    assert op == OP_STORE_F and sa == src.ofs and sb == dst.ofs and sc == 0


if __name__ == "__main__":
    for fn in (test_opcode_indices_match_names, test_emit_allocates_result_global,
               test_emit_store_no_result):
        fn()
    print("OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_qcc_codegen.py`
Expected: FAIL — `ModuleNotFoundError: quake.qcc.codegen`

- [ ] **Step 3: Implement `codegen.py`**

```python
"""Codegen: the opcode table (pr_comp.c pr_opcodes) and PR_Statement (emit).

The OPCODES list is ordered identically to the OP_* enum in pr_comp.h, so the
list index IS the bytecode opcode (id computes `statement->op = op - pr_opcodes`).
Each entry's a/b/c types are etype ints, used by the parser for type-directed
opcode selection (pr_comp.c PR_Expression).

emit() mirrors PR_Statement: it appends a (op,a,b,c) statement; for ops with a
real result type (not void, not right-associative) it allocates ONE fresh global
for the result and never reuses it (this is the core byte-identity invariant)."""

from collections import namedtuple
from .types import (type_size, ev_void, ev_string, ev_float, ev_vector,
                    ev_entity, ev_field, ev_function, ev_pointer)

Op = namedtuple("Op", "name opname priority right_assoc a b c")

V, S, F, VEC, E, FLD, FN, P = (ev_void, ev_string, ev_float, ev_vector,
                               ev_entity, ev_field, ev_function, ev_pointer)

OPCODES = [
    Op("<DONE>",   "DONE",     -1, False, E,  FLD, V),
    Op("*",        "MUL_F",     2, False, F,  F,  F),
    Op("*",        "MUL_V",     2, False, VEC, VEC, F),
    Op("*",        "MUL_FV",    2, False, F,  VEC, VEC),
    Op("*",        "MUL_VF",    2, False, VEC, F,  VEC),
    Op("/",        "DIV",       2, False, F,  F,  F),
    Op("+",        "ADD_F",     3, False, F,  F,  F),
    Op("+",        "ADD_V",     3, False, VEC, VEC, VEC),
    Op("-",        "SUB_F",     3, False, F,  F,  F),
    Op("-",        "SUB_V",     3, False, VEC, VEC, VEC),
    Op("==",       "EQ_F",      4, False, F,  F,  F),
    Op("==",       "EQ_V",      4, False, VEC, VEC, F),
    Op("==",       "EQ_S",      4, False, S,  S,  F),
    Op("==",       "EQ_E",      4, False, E,  E,  F),
    Op("==",       "EQ_FNC",    4, False, FN, FN, F),
    Op("!=",       "NE_F",      4, False, F,  F,  F),
    Op("!=",       "NE_V",      4, False, VEC, VEC, F),
    Op("!=",       "NE_S",      4, False, S,  S,  F),
    Op("!=",       "NE_E",      4, False, E,  E,  F),
    Op("!=",       "NE_FNC",    4, False, FN, FN, F),
    Op("<=",       "LE",        4, False, F,  F,  F),
    Op(">=",       "GE",        4, False, F,  F,  F),
    Op("<",        "LT",        4, False, F,  F,  F),
    Op(">",        "GT",        4, False, F,  F,  F),
    Op(".",        "INDIRECT",  1, False, E,  FLD, F),
    Op(".",        "INDIRECT",  1, False, E,  FLD, VEC),
    Op(".",        "INDIRECT",  1, False, E,  FLD, S),
    Op(".",        "INDIRECT",  1, False, E,  FLD, E),
    Op(".",        "INDIRECT",  1, False, E,  FLD, FLD),
    Op(".",        "INDIRECT",  1, False, E,  FLD, FN),
    Op(".",        "ADDRESS",   1, False, E,  FLD, P),
    Op("=",        "STORE_F",   5, True,  F,  F,  F),
    Op("=",        "STORE_V",   5, True,  VEC, VEC, VEC),
    Op("=",        "STORE_S",   5, True,  S,  S,  S),
    Op("=",        "STORE_ENT", 5, True,  E,  E,  E),
    Op("=",        "STORE_FLD", 5, True,  FLD, FLD, FLD),
    Op("=",        "STORE_FNC", 5, True,  FN, FN, FN),
    Op("=",        "STOREP_F",  5, True,  P,  F,  F),
    Op("=",        "STOREP_V",  5, True,  P,  VEC, VEC),
    Op("=",        "STOREP_S",  5, True,  P,  S,  S),
    Op("=",        "STOREP_ENT", 5, True, P,  E,  E),
    Op("=",        "STOREP_FLD", 5, True, P,  FLD, FLD),
    Op("=",        "STOREP_FNC", 5, True, P,  FN, FN),
    Op("<RETURN>", "RETURN",   -1, False, V,  V,  V),
    Op("!",        "NOT_F",    -1, False, F,  V,  F),
    Op("!",        "NOT_V",    -1, False, VEC, V,  F),
    Op("!",        "NOT_S",    -1, False, VEC, V,  F),
    Op("!",        "NOT_ENT",  -1, False, E,  V,  F),
    Op("!",        "NOT_FNC",  -1, False, FN, V,  F),
    Op("<IF>",     "IF",       -1, False, F,  F,  V),
    Op("<IFNOT>",  "IFNOT",    -1, False, F,  F,  V),
    Op("<CALL0>",  "CALL0",    -1, False, FN, V,  V),
    Op("<CALL1>",  "CALL1",    -1, False, FN, V,  V),
    Op("<CALL2>",  "CALL2",    -1, False, FN, V,  V),
    Op("<CALL3>",  "CALL3",    -1, False, FN, V,  V),
    Op("<CALL4>",  "CALL4",    -1, False, FN, V,  V),
    Op("<CALL5>",  "CALL5",    -1, False, FN, V,  V),
    Op("<CALL6>",  "CALL6",    -1, False, FN, V,  V),
    Op("<CALL7>",  "CALL7",    -1, False, FN, V,  V),
    Op("<CALL8>",  "CALL8",    -1, False, FN, V,  V),
    Op("<STATE>",  "STATE",    -1, False, F,  F,  V),
    Op("<GOTO>",   "GOTO",     -1, False, F,  V,  V),
    Op("&&",       "AND",       6, False, F,  F,  F),
    Op("||",       "OR",        6, False, F,  F,  F),
    Op("&",        "BITAND",    2, False, F,  F,  F),
    Op("|",        "BITOR",     2, False, F,  F,  F),
]

# named indices (must match OPCODES order == pr_comp.h enum)
_idx = {}
for _i, _op in enumerate(OPCODES):
    # disambiguate the duplicate-named rows by opname for the constants we need
    _idx.setdefault(_op.opname, _i)
(OP_DONE, OP_MUL_F, OP_LOAD_F, OP_ADDRESS, OP_STORE_F, OP_STORE_V, OP_RETURN,
 OP_IF, OP_IFNOT, OP_CALL0, OP_STATE, OP_GOTO, OP_NOT_F, OP_BITOR) = (
    _idx["DONE"], _idx["MUL_F"], _idx["INDIRECT"], _idx["ADDRESS"],
    _idx["STORE_F"], _idx["STORE_V"], _idx["RETURN"], _idx["IF"], _idx["IFNOT"],
    _idx["CALL0"], _idx["STATE"], _idx["GOTO"], _idx["NOT_F"], _idx["BITOR"])

TOP_PRIORITY = 6
NOT_PRIORITY = 4

OP_INDEX = {op: i for i, op in enumerate(OPCODES)}   # Op -> bytecode index


def emit(state, op, var_a, var_b):
    """PR_Statement: append (op,a,b,c); allocate a result global unless the op
    is void-result or right-associative. Returns the result Def (or var_a for
    right-associative ops, so chained '=' works)."""
    from .symbols import Def
    idx = OP_INDEX[op]
    a = var_a.ofs if var_a is not None else 0
    b = var_b.ofs if var_b is not None else 0
    if op.c == ev_void or op.right_assoc:
        c = 0
        var_c = None
    else:
        var_c = Def(state.types_for(op.c), None, state.numpr_globals)
        c = state.numpr_globals
        state.numpr_globals += type_size[op.c]
    state.statements.append((idx, a, b, c))
    state.statement_lines.append(state.cur_line)
    return var_a if op.right_assoc else var_c
```

> **`state.types_for(etype)` and `state.cur_line`:** add a small helper + field to `CompileState` (Task 4 module) now:
> - In `symbols.py`, add `self.cur_line = 0` in `__init__`.
> - In `symbols.py`, add method:
>   ```python
>   def types_for(self, etype):
>       return (self.types.void, self.types.string, self.types.float,
>               self.types.vector, self.types.entity, self.types.field,
>               self.types.function, self.types.pointer)[etype]
>   ```
> Make these two edits as part of this task, re-run `tests/test_qcc_symbols.py` (still `OK`), then proceed.

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_qcc_codegen.py && PQ_AUDIO=0 python tests/test_qcc_symbols.py`
Expected: `OK` then `OK`

- [ ] **Step 5: Commit**

```bash
git add quake/qcc/codegen.py quake/qcc/symbols.py tests/test_qcc_codegen.py
git commit -m "feat(qcc): opcode table and PR_Statement emit"
```

---

## Task 6: The parser (`compiler.py`)

**Files:**
- Create: `quake/qcc/compiler.py`
- Test: `tests/test_qcc_compile.py` (add a small end-to-end inline-compile test)

This is the largest module: `parse_type`, precedence-climbing `expression`, `term`, `value`, function-call marshalling, `statement` with jump back-patching, `[state]`, function bodies, top-level defs, and the `compile_file` / `compile_progs_src` driver. Port closely from `pr_comp.c` (PR_Expression, PR_Statement-callers, PR_ParseStatement, PR_ParseDefs, PR_ParseImmediateStatements, PR_ParseState) and `pr_lex.c` PR_ParseType.

- [ ] **Step 1: Write the failing test (append to `tests/test_qcc_compile.py`)**

```python
import os, tempfile
from quake.qcc import compile_progs_src
from quake.progs import Progs


def test_inline_minimal_compile():
    # a tiny self-contained progs: one field, one builtin, one function
    src = """
.float health;
void(string s) dprint = #1;
float() main =
{
    local float x;
    x = 3 + 4;
    dprint("hi");
    return;
};
"""
    d = tempfile.mkdtemp(prefix="qccmini")
    with open(f"{d}/test.qc", "w") as f:
        f.write(src)
    with open(f"{d}/progs.src", "w") as f:
        f.write("progs.dat\ntest.qc\n")
    data = compile_progs_src(f"{d}/progs.src")
    p = Progs(data)                              # our loader accepts it
    names = {fn.name for fn in p.functions if fn}
    assert "main" in names and "dprint" in names
    assert p.functions and any(fn and fn.builtin == 1 for fn in p.functions)


if __name__ == "__main__":
    test_oracle_present_and_valid()
    test_inline_minimal_compile()
    print("OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_qcc_compile.py`
Expected: FAIL — `ImportError`/`AttributeError` (compiler not implemented)

- [ ] **Step 3: Implement `compiler.py`**

```python
"""The QuakeC parser + driver. Ports pr_comp.c (PR_Expression, PR_Term,
PR_ParseValue, PR_ParseFunctionCall, PR_ParseStatement, PR_ParseState,
PR_ParseImmediateStatements, PR_GetDef callers, PR_ParseDefs, PR_CompileFile)
and pr_lex.c PR_ParseType, plus qcc.c's progs.src driver loop.

Single accumulating unit: one CompileState is shared across every source file;
state is never reset, so defs.qc (listed first in progs.src) is visible to all
later files."""

import os
from .errors import QCCError
from .lexer import Lexer, TT_EOF, TT_NAME, TT_PUNCT, TT_IMMEDIATE
from .symbols import CompileState, OFS_RETURN
from .types import (type_size, ev_void, ev_string, ev_float, ev_vector,
                    ev_entity, ev_field, ev_function, ev_pointer)
from . import codegen
from .codegen import (OPCODES, OP_INDEX, OP_DONE, OP_LOAD_F, OP_ADDRESS,
                      OP_STORE_V, OP_RETURN, OP_IF, OP_IFNOT, OP_CALL0,
                      OP_STATE, OP_GOTO, OP_NOT_F, TOP_PRIORITY, NOT_PRIORITY,
                      emit)

_TYPE_NAMES = {"float": "float", "vector": "vector", "entity": "entity",
               "string": "string", "void": "void"}


class Compiler:
    def __init__(self, state, lexer):
        self.st = state
        self.lx = lexer
        self.scope = None           # current function Def, or None
        self.locals_end = 0

    # --- lexer glue ---
    def err(self, msg):
        raise QCCError(self.lx.file, self.lx.line, msg)

    def lex(self):
        self.lx.next()
        self.st.cur_line = self.lx.line

    def check(self, s):
        if self.lx.token == s and self.lx.token_type in (TT_PUNCT, TT_NAME):
            self.lex()
            return True
        return False

    def expect(self, s):
        if self.lx.token != s:
            self.err(f"expected {s}, found {self.lx.token}")
        self.lex()

    def parse_name(self):
        if self.lx.token_type != TT_NAME:
            self.err("not a name")
        name = self.lx.token
        self.lex()
        return name

    def get_def(self, type, name, scope, allocate):
        try:
            return self.st.get_def(type, name, scope, allocate)
        except QCCError as e:
            raise QCCError(self.lx.file, self.lx.line, e.message) from None

    # --- PR_ParseType (pr_lex.c) ---
    def parse_type(self):
        t = self.st.types
        if self.check("."):
            return t.field_of(self.parse_type())
        name = self.lx.token
        base = {"float": t.float, "vector": t.vector, "entity": t.entity,
                "string": t.string, "void": t.void}.get(name)
        if base is None:
            self.err(f'"{name}" is not a type')
        self.lex()
        if not self.check("("):
            return base
        # function type
        parms = []
        num = 0
        if not self.check(")"):
            if self.check("..."):
                num = -1
            else:
                while True:
                    pt = self.parse_type()
                    pname = self.parse_name()
                    parms.append((pt, pname))
                    num += 1
                    if not self.check(","):
                        break
            self.expect(")")
        ftype = t.function_of(base, tuple(p[0] for p in parms), num)
        # stash parm names for PR_ParseImmediateStatements
        self._last_parm_names = [p[1] for p in parms]
        return ftype

    # --- immediates / values ---
    def parse_immediate(self):
        d = self.st.parse_immediate(self.lx.immediate_type,
                                    self._imm_value())
        self.lex()
        return d

    def _imm_value(self):
        it = self.lx.immediate_type
        if it is self.st.types.string:
            return self.lx.immediate_string
        return self.lx.immediate

    def parse_value(self):
        if self.lx.token_type == TT_IMMEDIATE:
            return self.parse_immediate()
        name = self.parse_name()
        d = self.get_def(None, name, self.scope, False)
        if d is None:
            self.err(f'Unknown value "{name}"')
        return d

    # --- PR_Term ---
    def term(self):
        st = self.st
        if self.check("!"):
            e = self.expression(NOT_PRIORITY)
            t = e.type.type
            op = {ev_float: "NOT_F", ev_string: "NOT_S", ev_entity: "NOT_ENT",
                  ev_vector: "NOT_V", ev_function: "NOT_FNC"}.get(t)
            if op is None:
                self.err("type mismatch for !")
            return emit(st, OPCODES[_op_by_name(op)], e, None)
        if self.check("("):
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            return e
        return self.parse_value()

    # --- PR_Expression (precedence climbing + type-directed opcode select) ---
    def expression(self, priority):
        if priority == 0:
            return self.term()
        e = self.expression(priority - 1)
        st = self.st
        while True:
            if priority == 1 and self.check("("):
                e = self.function_call(e)
                continue
            matched = None
            for i, op in enumerate(OPCODES):
                if op.priority != priority:
                    continue
                if self.lx.token != op.name or self.lx.token_type not in (TT_PUNCT,):
                    continue
                self.lex()
                if op.right_assoc:
                    # rewrite a trailing LOAD into ADDRESS (lvalue) -- pr_comp.c:479
                    last = st.statements[-1][0]
                    if OP_LOAD_F <= last < OP_LOAD_F + 6:
                        st.statements[-1] = (OP_ADDRESS,) + st.statements[-1][1:]
                        e = self._as_pointer(e)
                    e2 = self.expression(priority)
                else:
                    e2 = self.expression(priority - 1)
                op = self._select(op, e, e2)
                if op.right_assoc:
                    res = emit(st, op, e2, e)
                else:
                    res = emit(st, op, e, e2)
                # field access result type comes from the field's aux_type
                if op.opname == "INDIRECT":
                    res.type = e2.type.aux_type
                e = res
                matched = op
                break
            if matched is None:
                break
        return e

    def _select(self, op, e, e2):
        """Walk the same-named opcode rows until operand types match
        (pr_comp.c type-check loop)."""
        ta, tb = e.type.type, e2.type.type
        if op.name == ".":
            tc = e2.type.aux_type.type if e2.type.aux_type else -1
        else:
            tc = ev_void
        i = OP_INDEX[op]
        while True:
            cand = OPCODES[i]
            if cand.name != op.name:
                self.err(f"type mismatch for {op.name}")
            if (ta == cand.a and tb == cand.b
                    and (tc == ev_void or tc == cand.c)):
                return cand
            i += 1
            if i >= len(OPCODES):
                self.err(f"type mismatch for {op.name}")

    def _as_pointer(self, e):
        # give e a pointer type whose aux is its current type
        from .types import Type
        e.type = Type(ev_pointer, aux_type=e.type)
        return e

    # --- PR_ParseFunctionCall ---
    def function_call(self, func):
        st = self.st
        t = func.type
        if t.type != ev_function:
            self.err("not a function")
        arg = 0
        if not self.check(")"):
            while True:
                if t.num_parms != -1 and arg >= t.num_parms:
                    self.err("too many parameters")
                e = self.expression(TOP_PRIORITY)
                if t.num_parms != -1 and e.type is not t.parm_types[arg]:
                    self.err(f"type mismatch on parm {arg}")
                parm = st.def_parms[arg]
                parm.type = t.parm_types[arg] if t.num_parms != -1 else e.type
                emit(st, OPCODES[OP_STORE_V], e, parm)   # vector copy = everything
                arg += 1
                if not self.check(","):
                    break
            if t.num_parms != -1 and arg != t.num_parms:
                self.err("too few parameters")
            self.expect(")")
        if arg > 8:
            self.err("More than eight parameters")
        emit(st, OPCODES[OP_CALL0 + arg], func, None)
        st.def_ret.type = t.aux_type
        return st.def_ret

    # --- PR_ParseStatement (control flow with back-patching) ---
    def statement(self):
        st = self.st
        if self.check("{"):
            while not self.check("}"):
                self.statement()
            return
        if self.check("return"):
            if self.check(";"):
                emit(st, OPCODES[OP_RETURN], None, None)
                return
            e = self.expression(TOP_PRIORITY)
            self.expect(";")
            emit(st, OPCODES[OP_RETURN], e, None)
            return
        if self.check("while"):
            self.expect("(")
            top = len(st.statements)
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            patch = len(st.statements)
            emit(st, OPCODES[OP_IFNOT], e, None)
            self.statement()
            self._emit_goto(top - len(st.statements))
            self._patch_b(patch, len(st.statements) - patch)
            return
        if self.check("do"):
            top = len(st.statements)
            self.statement()
            self.expect("while")
            self.expect("(")
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            self.expect(";")
            self._emit_if(e, top - len(st.statements))
            return
        if self.check("local"):
            self.parse_defs()
            self.locals_end = st.numpr_globals
            return
        if self.check("if"):
            self.expect("(")
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            patch1 = len(st.statements)
            emit(st, OPCODES[OP_IFNOT], e, None)
            self.statement()
            if self.check("else"):
                patch2 = len(st.statements)
                emit(st, OPCODES[OP_GOTO], None, None)
                self._patch_b(patch1, len(st.statements) - patch1)
                self.statement()
                self._patch_a(patch2, len(st.statements) - patch2)
            else:
                self._patch_b(patch1, len(st.statements) - patch1)
            return
        self.expression(TOP_PRIORITY)
        self.expect(";")

    # statement back-patch helpers (statements are (op,a,b,c) tuples)
    def _patch_a(self, i, val):
        op, a, b, c = self.st.statements[i]
        self.st.statements[i] = (op, val, b, c)

    def _patch_b(self, i, val):
        op, a, b, c = self.st.statements[i]
        self.st.statements[i] = (op, a, val, c)

    def _emit_goto(self, rel):
        self.st.statements.append((OP_GOTO, rel, 0, 0))
        self.st.statement_lines.append(self.st.cur_line)

    def _emit_if(self, e, rel):
        self.st.statements.append((OP_IF, e.ofs, rel, 0))
        self.st.statement_lines.append(self.st.cur_line)

    # --- PR_ParseState ---
    def parse_state(self):
        if (self.lx.token_type != TT_IMMEDIATE
                or self.lx.immediate_type is not self.st.types.float):
            self.err("state frame must be a number")
        s1 = self.parse_immediate()
        self.expect(",")
        name = self.parse_name()
        d = self.get_def(self.st.types.function, name, None, True)
        self.expect("]")
        emit(self.st, OPCODES[OP_STATE], s1, d)

    # --- PR_ParseImmediateStatements (function body) ---
    def parse_function_body(self, ftype, parm_names):
        st = self.st
        if self.check("#"):
            if (self.lx.token_type != TT_IMMEDIATE
                    or self.lx.immediate_type is not st.types.float
                    or self.lx.immediate != int(self.lx.immediate)):
                self.err("Bad builtin immediate")
            builtin = int(self.lx.immediate)
            self.lex()
            return {"builtin": builtin, "code": 0, "parm_ofs": []}
        parm_ofs = []
        for i in range(ftype.num_parms):
            d = self.get_def(ftype.parm_types[i], parm_names[i], self.scope, True)
            parm_ofs.append(d.ofs)
        code = len(st.statements)
        if self.check("["):
            self.parse_state()
        self.expect("{")
        while not self.check("}"):
            self.statement()
        emit(st, OPCODES[OP_DONE], None, None)
        return {"builtin": 0, "code": code, "parm_ofs": parm_ofs}

    # --- PR_ParseDefs ---
    def parse_defs(self):
        st = self.st
        self._last_parm_names = []
        type = self.parse_type()
        parm_names = list(getattr(self, "_last_parm_names", []))
        if self.scope and type.type in (ev_field, ev_function):
            self.err("Fields and functions must be global")
        while True:
            name = self.parse_name()
            d = self.get_def(type, name, self.scope, True)
            if self.check("="):
                if d.initialized:
                    self.err(f"{name} redeclared")
                if type.type == ev_function:
                    locals_start = self.locals_end = st.numpr_globals
                    self.scope = d
                    f = self.parse_function_body(type, parm_names)
                    self.scope = None
                    d.initialized = True
                    st.set_gi(d.ofs, len(st.functions))      # G_FUNCTION = index
                    self._emit_function(d, f, type, locals_start)
                    if not self.check(","):
                        break
                    continue
                elif self.lx.immediate_type is not type:
                    self.err(f"wrong immediate type for {name}")
                d.initialized = True
                self._store_immediate(d, type)
                self.lex()
            if not self.check(","):
                break
        self.expect(";")

    def _store_immediate(self, d, type):
        it = self.lx.immediate_type
        if type.type == ev_string:
            st = self.st
            st.set_gi(d.ofs, st.copy_string(self.lx.immediate_string))
        elif type.type == ev_float:
            self.st.set_gf(d.ofs, self.lx.immediate)
        elif type.type == ev_vector:
            v = self.lx.immediate
            self.st.set_gf(d.ofs, v[0]); self.st.set_gf(d.ofs + 1, v[1])
            self.st.set_gf(d.ofs + 2, v[2])
        else:
            self.st.set_gi(d.ofs, int(self.lx.immediate))

    def _emit_function(self, d, f, type, locals_start):
        st = self.st
        first = -f["builtin"] if f["builtin"] else f["code"]
        parm_size = [type_size[type.parm_types[i].type]
                     for i in range(type.num_parms if type.num_parms != -1 else 0)]
        st.functions.append({
            "first_statement": first,
            "parm_start": locals_start,
            "locals": self.locals_end - locals_start,
            "s_name": st.copy_string(d.name),
            "s_file": st.cur_file,
            "numparms": type.num_parms,
            "parm_size": parm_size,
        })

    # --- PR_CompileFile ---
    def compile_file(self):
        self.lex()
        while self.lx.token_type != TT_EOF:
            self.scope = None
            self.parse_defs()


def _op_by_name(opname):
    for i, op in enumerate(OPCODES):
        if op.opname == opname:
            return i
    raise KeyError(opname)


# --- driver (qcc.c main) ---
def compile_progs_src(path):
    base = os.path.dirname(path)
    with open(path) as f:
        tokens = f.read().split()
    dest = tokens[0]                                  # output filename (ignored)
    files = tokens[1:]
    state = CompileState()
    for fname in files:
        with open(os.path.join(base, fname)) as fh:
            src = fh.read()
        state.cur_file = state.copy_string(fname)
        lx = Lexer(src, fname, state.types)
        Compiler(state, lx).compile_file()
    _finish(state)
    from .writer import write_progs
    return write_progs(state)


def _finish(state):
    # PR_FinishCompilation: every prototyped global function must be defined
    for d in state.defs:
        if (d.type is not None and d.type.type == ev_function
                and d.scope is None and not d.initialized):
            raise QCCError("?", 0, f"function {d.name} was not defined")
```

> **Note on `num_parms == -1` (varargs):** `_emit_function` writes `numparms = -1` for builtins like `dprint`; this matches id (`df->numparms = f->def->type->num_parms`). The writer packs it as a signed int.

- [ ] **Step 4: Run to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_qcc_compile.py`
Expected: FAIL at `test_inline_minimal_compile` only if `writer.py` is missing — implement Task 7 first if so. (The driver imports `write_progs`.) If you are executing strictly task-by-task, **swap the order: do Task 7 before running this step**, then return here. Expected after Task 7: `OK`.

- [ ] **Step 5: Commit**

```bash
git add quake/qcc/compiler.py tests/test_qcc_compile.py
git commit -m "feat(qcc): recursive-descent parser, codegen driver"
```

---

## Task 7: Writer + CRC (`writer.py`)

**Files:**
- Create: `quake/qcc/writer.py`
- Test: covered by `tests/test_qcc_compile.py` (CRC + per-lump in Task 8)

- [ ] **Step 1: Write the failing test (append to `tests/test_qcc_compile.py`)**

```python
def test_crc_matches_oracle():
    # compiling the real v101qc must reproduce id's progdefs CRC (5927)
    import struct
    data = compile_progs_src(
        "quake-source/quake-tools/qcc/v101qc/progs.src")
    _, crc = struct.unpack_from("<ii", data, 0)
    assert crc == 5927, crc
```

Add `test_crc_matches_oracle()` to the `__main__` runner.

- [ ] **Step 2: Run to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_qcc_compile.py`
Expected: FAIL — `ModuleNotFoundError: quake.qcc.writer`

- [ ] **Step 3: Implement `writer.py`**

```python
"""Serialize CompileState to a v6 progs.dat, and compute the progdefs CRC.

Ports qcc.c WriteData / PR_WriteProgdefs / CRC_*. Lump order on disk: header,
strings, statements, functions, globaldefs, fielddefs, globals. All fields are
little-endian. The CRC is a CCITT/XMODEM 16-bit CRC over the generated
progdefs.h TEXT (must reproduce 5927 for standard Quake)."""

import struct
from .types import (ev_void, ev_string, ev_float, ev_vector, ev_entity,
                    ev_field, ev_function, ev_pointer, type_size)

PROG_VERSION = 6
DEF_SAVEGLOBAL = 1 << 15
RESERVED_OFS = 28

_S_HEADER = struct.Struct("<2i 12i i")
_S_STATEMENT = struct.Struct("<Hhhh")
_S_DEF = struct.Struct("<HHi")
_S_FUNCTION = struct.Struct("<7i8B")

# --- CRC (qcc.c CRC_*: CCITT poly 0x1021, init 0xffff, no reflect, xor 0) ---
def _crc_table():
    tab = []
    for i in range(256):
        v = i << 8
        for _ in range(8):
            v = ((v << 1) ^ 0x1021) if (v & 0x8000) else (v << 1)
            v &= 0xffff
        tab.append(v)
    return tab


_CRCTAB = _crc_table()


def _crc_bytes(data):
    crc = 0xffff
    for byte in data:
        crc = ((crc << 8) & 0xffff) ^ _CRCTAB[((crc >> 8) ^ byte) & 0xff]
    return crc


_CTYPE = {ev_float: "float", ev_vector: "vec3_t", ev_string: "string_t",
          ev_function: "func_t", ev_entity: "int"}


def progdefs_text(state):
    """Reproduce PR_WriteProgdefs' generated file text, char-for-char."""
    out = []
    out.append("\n/* file generated by qcc, do not modify */\n\ntypedef struct\n"
               "{\tint\tpad[%i];\n" % RESERVED_OFS)
    defs = state.defs
    i = 0
    # globals until end_sys_globals
    while i < len(defs):
        d = defs[i]
        if d.name == "end_sys_globals":
            break
        et = d.type.type
        if et == ev_vector:
            out.append("\tvec3_t\t%s;\n" % d.name)
            i += 3                         # skip _x/_y/_z element defs
        elif et == ev_float:
            out.append("\tfloat\t%s;\n" % d.name)
        elif et == ev_string:
            out.append("\tstring_t\t%s;\n" % d.name)
        elif et == ev_function:
            out.append("\tfunc_t\t%s;\n" % d.name)
        else:
            out.append("\tint\t%s;\n" % d.name)
        i += 1
    out.append("} globalvars_t;\n\n")
    # fields until end_sys_fields
    out.append("typedef struct\n{\n")
    i = 0
    while i < len(defs):
        d = defs[i]
        if d.name == "end_sys_fields":
            break
        if d.type.type != ev_field:
            i += 1
            continue
        at = d.type.aux_type.type
        if at == ev_vector:
            out.append("\tvec3_t\t%s;\n" % d.name)
            i += 3
        elif at == ev_float:
            out.append("\tfloat\t%s;\n" % d.name)
        elif at == ev_string:
            out.append("\tstring_t\t%s;\n" % d.name)
        elif at == ev_function:
            out.append("\tfunc_t\t%s;\n" % d.name)
        else:
            out.append("\tint\t%s;\n" % d.name)
        i += 1
    out.append("} entvars_t;\n\n")
    return "".join(out)


def progdefs_crc(state):
    return _crc_bytes(progdefs_text(state).encode("latin-1"))


def _build_defs(state):
    """globaldefs + fielddefs (qcc.c WriteData def walk). Index 0 is reserved."""
    globaldefs = [(0, 0, 0)]            # zero sentinel
    fielddefs = [(0, 0, 0)]
    for d in state.defs:
        et = d.type.type
        if et == ev_field:
            fielddefs.append((d.type.aux_type.type, state.gi(d.ofs),
                              state.copy_string(d.name)))
        dtype = et
        if (not d.initialized and et != ev_function and et != ev_field
                and d.scope is None):
            dtype |= DEF_SAVEGLOBAL
        globaldefs.append((dtype, d.ofs, state.copy_string(d.name)))
    return globaldefs, fielddefs


def write_progs(state):
    crc = progdefs_crc(state)
    # NOTE: _build_defs appends def names to the string blob AFTER all source
    # strings, exactly as WriteData does its CopyString calls during the walk.
    globaldefs, fielddefs = _build_defs(state)

    # pad strings to 4 bytes (qcc.c: strofs = (strofs+3)&~3)
    strings = bytearray(state.strings)
    while len(strings) % 4:
        strings.append(0)

    parts = []
    header = bytearray(_S_HEADER.size)
    parts.append(header)
    ofs = len(header)

    def emit(blob):
        nonlocal ofs
        start = ofs
        parts.append(blob)
        ofs += len(blob)
        return start

    ofs_strings = emit(bytes(strings))
    stmt = bytearray()
    for (op, a, b, c) in state.statements:
        stmt += _S_STATEMENT.pack(op & 0xffff, a, b, c)
    ofs_statements = emit(stmt)

    fns = bytearray()
    for fn in state.functions:
        if fn is None:                          # index 0 sentinel
            fns += _S_FUNCTION.pack(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            continue
        ps = list(fn["parm_size"]) + [0] * (8 - len(fn["parm_size"]))
        fns += _S_FUNCTION.pack(fn["first_statement"], fn["parm_start"],
                                fn["locals"], 0, fn["s_name"], fn["s_file"],
                                fn["numparms"], *ps[:8])
    ofs_functions = emit(fns)

    gdef = bytearray()
    for (t, o, s) in globaldefs:
        gdef += _S_DEF.pack(t & 0xffff, o & 0xffff, s)
    ofs_globaldefs = emit(gdef)

    fdef = bytearray()
    for (t, o, s) in fielddefs:
        fdef += _S_DEF.pack(t & 0xffff, o & 0xffff, s)
    ofs_fielddefs = emit(fdef)

    globs = bytearray()
    for i in range(state.numpr_globals):
        globs += struct.pack("<i", state.gi(i))
    ofs_globals = emit(globs)

    _S_HEADER.pack_into(
        header, 0,
        PROG_VERSION, crc,
        ofs_statements, len(state.statements),
        ofs_globaldefs, len(globaldefs),
        ofs_fielddefs, len(fielddefs),
        ofs_functions, len(state.functions),
        ofs_strings, len(strings),
        ofs_globals, state.numpr_globals,
        state.size_fields)
    return b"".join(bytes(p) for p in parts)
```

> **Critical ordering note (cite in code):** in `WriteData`, the def-name strings are `CopyString`'d *during* the def walk, which runs **after** all source compilation but **before** the strings lump is written — so def names land at the **end** of the strings blob. `_build_defs` must therefore run before `strings` is snapshotted for padding. The code above calls `_build_defs(state)` first; it mutates `state.strings`, then we copy+pad. Keep this order. The CRC, however, is computed on the *progdefs text*, independent of the blob.

- [ ] **Step 4: Run to verify CRC passes**

Run: `PQ_AUDIO=0 python tests/test_qcc_compile.py`
Expected: `test_crc_matches_oracle` PASSES (`crc == 5927`); `test_inline_minimal_compile` PASSES. (Whole-file byte-identity is asserted in Task 8.)

- [ ] **Step 5: Commit**

```bash
git add quake/qcc/writer.py tests/test_qcc_compile.py
git commit -m "feat(qcc): progs.dat writer + progdefs CRC (5927)"
```

---

## Task 8: Per-lump diff + byte-identity + functional boot

This is where byte-identity is actually achieved. The per-lump diff localizes any residual ordering bug; fix until each lump matches, then the whole-file assert is automatic.

**Files:**
- Modify: `tests/test_qcc_compile.py`

- [ ] **Step 1: Write the per-lump + byte-identity + boot tests (append)**

```python
V101 = "quake-source/quake-tools/qcc/v101qc/progs.src"


def _lumps(data):
    import struct
    h = struct.unpack_from("<2i 12i i", data, 0)
    names = ["statements", "globaldefs", "fielddefs", "functions",
             "strings", "globals"]
    # element sizes in bytes
    esz = {"statements": 8, "globaldefs": 8, "fielddefs": 8,
           "functions": 36, "strings": 1, "globals": 4}
    out = {}
    for i, n in enumerate(names):
        ofs, cnt = h[2 + i * 2], h[3 + i * 2]
        out[n] = data[ofs:ofs + cnt * esz[n]]
    out["entityfields"] = h[14]
    out["crc"] = h[1]
    return out


def test_per_lump_matches_oracle():
    mine = _lumps(compile_progs_src(V101))
    ref = _lumps(_oracle())
    assert mine["crc"] == ref["crc"], "crc"
    assert mine["entityfields"] == ref["entityfields"], "entityfields"
    for lump in ("strings", "functions", "statements", "globaldefs",
                 "fielddefs", "globals"):
        assert mine[lump] == ref[lump], (
            f"lump {lump} differs: mine={len(mine[lump])} ref={len(ref[lump])}")


def test_byte_identical_to_oracle():
    assert compile_progs_src(V101) == _oracle()


def test_self_compiled_boots():
    # functional sanity: our compiled retail progs boots e1m1 in the VM
    import io
    from quake.pak import Pak
    from quake.bsp import Bsp
    from quake.progs import Progs
    from quake.sv import Server
    from quake.physics import Physics
    data = compile_progs_src(V101)
    pak = Pak("quake-shareware/id1/pak0.pak")
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(data), bsp=b, mapname="maps/e1m1.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    for _ in range(3):
        sv.run_frame(0.1)
```

Add all three to the `__main__` runner.

- [ ] **Step 2: Run and read the first failing lump**

Run: `PQ_AUDIO=0 python tests/test_qcc_compile.py`
Expected (first pass likely FAILS): the assertion names the first mismatching lump. Debug in this order — `strings` → `functions` → `statements` → `globaldefs` → `fielddefs` → `globals`. Common causes and where to look:
- **strings length off by N:** def-name append order (writer `_build_defs` ordering note) or a missing `CopyString` for an immediate string.
- **statements differ early:** opcode selection (`_select`), the `LOAD→ADDRESS` rewrite, or arg marshalling order in `function_call`.
- **globaldefs differ:** `DEF_SAVEGLOBAL` predicate, or a temp accidentally added to `state.defs`.
- **globals differ:** immediate dedup scan, vector `_x/_y/_z` allocation, or field-offset writes.

- [ ] **Step 3: Fix the localized bug, re-run, repeat**

Iterate Step 2/3 until `test_per_lump_matches_oracle` and `test_byte_identical_to_oracle` PASS. Make a focused commit per fix:

```bash
git add quake/qcc tests/test_qcc_compile.py
git commit -m "fix(qcc): <lump> byte-identity — <root cause>"
```

- [ ] **Step 4: Verify the full suite passes**

Run: `PQ_AUDIO=0 python tests/test_qcc_compile.py`
Expected: `OK` (oracle sanity, inline, crc, per-lump, byte-identical, boot all pass)

- [ ] **Step 5: Commit**

```bash
git add tests/test_qcc_compile.py
git commit -m "test(qcc): per-lump + byte-identity + functional boot vs oracle"
```

---

## Task 9: CLI + docs

**Files:**
- Create: `quake/qcc/__main__.py`
- Modify: `README.md` (architecture map pointer), `CLAUDE.md` (data-flow line)

- [ ] **Step 1: Implement the CLI**

Create `quake/qcc/__main__.py`:

```python
"""CLI: python -m quake.qcc [-src DIR]   (mirrors qcc.c main).

Looks for progs.src in DIR (default cwd), compiles, writes the dest file named
on progs.src's first line (relative to DIR)."""

import os
import sys
from . import compile_progs_src


def main(argv):
    src_dir = "."
    if "-src" in argv:
        src_dir = argv[argv.index("-src") + 1]
    src_path = os.path.join(src_dir, "progs.src")
    with open(src_path) as f:
        dest = f.read().split()[0]
    data = compile_progs_src(src_path)
    out = os.path.join(src_dir, dest)
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 2: Verify the CLI reproduces the oracle**

Run:
```bash
cd /Users/wjbr/src/pq.ai && python -m quake.qcc -src quake-source/quake-tools/qcc/v101qc \
  && cmp quake-source/quake-tools/qcc/v101qc/progs.dat tests/data/progs_v101_oracle.dat \
  && echo IDENTICAL
```
Expected: `wrote .../progs.dat (410616 bytes)` then `IDENTICAL`. Then clean up the generated file:
```bash
rm quake-source/quake-tools/qcc/v101qc/progs.dat
```

- [ ] **Step 3: Add README + CLAUDE.md pointers**

In `README.md`, add `quake/qcc/` to the architecture/data-flow section near the `progs.py` entry, e.g.: "`quake/qcc/` — pure-Python QuakeC compiler (the inverse of `progs.py`): compiles `progs.src` + `.qc` → byte-identical `progs.dat`. `python -m quake.qcc -src DIR`."

In `CLAUDE.md`, under the `quake/progs.py` line in the data-flow block, add: "`quake/qcc/` QuakeC compiler (.qc → progs.dat, byte-identical to id's qcc; oracle in tests/)."

- [ ] **Step 4: Run the whole qcc test set muted**

Run:
```bash
cd /Users/wjbr/src/pq.ai && PQ_AUDIO=0 sh -c 'for t in tests/test_qcc_*.py; do python "$t" || exit 1; done'
```
Expected: four `OK` lines.

- [ ] **Step 5: Commit**

```bash
git add quake/qcc/__main__.py README.md CLAUDE.md
git commit -m "feat(qcc): CLI entry point + docs"
```

---

## Self-review notes (addressed)

- **Spec coverage:** scope (Task 1 boundary + omissions), architecture/modules (Tasks 2–9 one per module), the 8 byte-identity invariants (RESERVED_OFS/temps Task 4–5; dedup Task 4; def order + SAVEGLOBAL Task 7; vector/field Task 4; CRC Task 7; strings padding Task 7; function records Task 6–7), data flow (driver Task 6), error handling (`QCCError` Task 1, raised throughout), testing incl. per-lump + byte-identity + boot (Task 8) and oracle generation (Task 1), licensing (Task 1 generator docstring).
- **Type/name consistency:** `compile_progs_src`, `CompileState`, `get_def`, `parse_immediate`, `copy_string`, `emit`, `OPCODES`/`OP_INDEX`, `write_progs`, `progdefs_crc` are used identically across tasks. `state.cur_line`/`state.cur_file`/`state.types_for` are introduced in Task 5's note before first use.
- **Known ordering hazard called out:** def-name strings append after source strings (Task 7 note) — the single most likely source of a `strings`/whole-file diff; Task 8 Step 2 lists it first.
- **Task-order caveat:** Task 6 Step 4 depends on Task 7's `write_progs`; noted inline to do Task 7 first if executing strictly sequentially.
