# QuakeC compiler (qcc) — pure-Python port

**Date:** 2026-06-14
**Status:** Approved design — ready for implementation planning

## Goal

Port id Software's QuakeC compiler **qcc** to pure-Python stdlib, living in a new
`quake/qcc/` subpackage. It compiles a `progs.src` manifest + its `.qc` source
files into a version-6 `progs.dat` — the same bytecode format `quake/progs.py`
already loads and `quake/pr_exec.py` already executes.

**Success criterion: byte-identical output.** Compiling id's shipped `v101qc`
source set must produce a `progs.dat` that is bit-for-bit equal to the output of
id's genuine `qccdos.exe` run on the same source.

This closes the toolchain loop: pq.ai already reads, runs, and renders `progs.dat`;
with this it can also *produce* it from source, in pure Python.

## Reference sources (id GPL, in `quake-source/quake-tools/qcc/`)

- `pr_lex.c` — lexer, type parsing, frame macros.
- `pr_comp.c` — symbol table, expression/statement parser, codegen, opcode table.
- `qcc.c` — `progs.src` driver, `WriteData` serializer, `progdefs.h` + CRC.
- `qcc.h` / `pr_comp.h` — data structures and the on-disk format (the latter also
  vendored at `quake-source/WinQuake/pr_comp.h`, shared with the runtime).
- `v101qc/` — the Quake v1.01 game source (35 `.qc` files + `progs.src`); our
  compile input and the basis of the oracle.
- `qccdos.exe` + `cwsdpmi.exe` — id's shipped compiler, run under DOSBox-x to
  generate the oracle.

This is a **Pythonic reimplementation**, not a line-by-line transliteration: it
uses idiomatic classes/dataclasses and clean module boundaries. Byte-identity is
preserved not by mirroring C control flow but by honoring a small set of explicit
**ordering invariants** (Section 4), each cited to its qcc origin and guarded by
the oracle diff.

## Non-goals (provably no effect on `progs.dat` bytes)

- **Precache scanning.** `PR_ParseFunctionCall`'s `precache_sound/model/file`
  detection only fills asset-copy tables; it emits no defs, statements, or
  globals. Omitted.
- **Asset/packaging driver modes** — `-copy`, `-pak`, `-bspmodels`, `CopyFiles`.
  Out of scope; we produce `progs.dat` only.
- **`-asm` disassembly dump.** Debug output only.
- **Full error recovery.** id's `longjmp` + skip-to-semicolon + "stopped at 10
  errors" loop is not needed for the happy-path v101qc compile. We raise on first
  error. Error *messages* are mirrored for diagnostic parity; recovery is not.

## Architecture — `quake/qcc/` subpackage

Focused modules, each one bounded responsibility:

| Module | Responsibility |
|---|---|
| `lexer.py` | `Token`, `Lexer`. Typed token stream: names, punctuation (maximal-munch, ordered table), float/vector/string immediates, `//` + `/* */` comments. Owns `$frame` macro state and the `$cd/$origin/...` skips. |
| `types.py` | `Type` (etype + aux/parms), the interning table (`PR_FindType`), the 8 base types, `type_size`. |
| `symbols.py` | `Def`, the insertion-ordered symbol table, global-slot allocator, vector `_x/_y/_z` autogen, field entity-offset allocator (`size_fields`). |
| `codegen.py` | The opcode table, `Statement` emit, temp-result global allocation, jump back-patching helpers. |
| `compiler.py` | Recursive-descent parser: precedence-climbing expressions with type-directed opcode selection, statements (`if/else/while/do/local/return`), defs, function bodies, builtins (`#N`), `[state]`. Holds the shared compile state. |
| `writer.py` | Six-lump serialization, little-endian packing, `progdefs.h` text generation, CCITT/XMODEM CRC. |
| `__init__.py` | `compile_progs_src(path) -> bytes` public entry. |
| `__main__.py` | `python -m quake.qcc [-src DIR]` CLI writing the `.dat`. |

Inside the subpackage use **relative imports** (project convention). Run the
self-test with `python -m quake.qcc`.

## Data flow

```
progs.src ── driver reads dest path + ordered file list
   │
   └─ for each .qc (state PERSISTS across files — one accumulating unit):
        Lexer(tokens) ─► compiler ─► mutates shared CompileState:
                                       defs, globals[], statements[],
                                       functions[], strings, size_fields
   │
   └─ writer.pack(state) ─► header + 6 lumps (LE) ─► bytes
```

State is **never reset between files** — exactly id's model, which is why
`defs.qc` must be first in `progs.src`.

## Byte-identity invariants (the heart of the port)

Documented, asserted contracts; the oracle diff is the backstop that proves we
honored them.

1. **Globals start at `RESERVED_OFS = 28`** (slots 0–27: `OFS_NULL`,
   `OFS_RETURN`, 8 param slots × 3). Every def/temp takes the *next* slot in
   source-encounter order. (`PR_BeginCompilation`)
2. **Temp result globals are never reused** — one fresh global per non-store,
   non-void-result op. (`PR_Statement`)
3. **Immediate dedup** — linear scan of the def list in insertion order, first
   match wins; the first appearance of a constant fixes its offset for all later
   uses. Strings/floats/vectors compared by value. (`PR_ParseImmediate`)
4. **Def list is insertion-ordered**; `globaldefs` (and `fielddefs` for field
   defs) are emitted in that exact order; the `DEF_SAVEGLOBAL` bit is set for
   uninitialized non-function/non-field global-scope defs. (`WriteData`)
5. **Vector defs** auto-create `_x/_y/_z` float defs immediately after the parent;
   field defs allocate entity offsets via the running `size_fields` (vector fields
   recurse with `_x/_y/_z`). (`PR_GetDef`)
6. **CRC** is the CCITT/XMODEM CRC computed over the generated `progdefs.h`
   *text*, byte-for-byte, and must reproduce **5927**. (`PR_WriteProgdefs`,
   `CRC_*`)
7. **String blob** is NUL-terminated, padded to 4 bytes (`strofs = (strofs+3)&~3`);
   string 0 is null; immediates `CopyString`'d at first appearance. (`WriteData`)
8. **Function records** — `first_statement = -builtin` for `#N`, else the code
   address; `parm_start`/`locals`/`parm_size[]` from the local slab.
   (`PR_ParseDefs`)

## Error handling

QuakeC compile errors raise `QCCError(file, line, message)` with id's message text
mirrored where practical. No multi-error recovery (see non-goals). Lexer EOF inside
quotes/vectors, unknown punctuation, type mismatches, redeclarations, and "function
not defined" (the `PR_FinishCompilation` check) all map to `QCCError`.

## Testing (TDD against the oracle)

**Oracle generation (one-shot, committed):** a helper compiles `v101qc` via
`SDL_VIDEODRIVER=dummy dosbox-x` running `qccdos.exe`, and the resulting
`progs.dat` is committed as `tests/data/progs_v101_oracle.dat` (410,616 bytes,
CRC 5927) so the test suite needs no DOSBox. The generator script is kept at
`tests/gen_qcc_oracle.py` and documented for regen.

**Licensing note:** unlike the gitignored shareware *data* (id copyright), both
`v101qc` and its compiled `progs.dat` derive from id's **GPLv2** Quake / Quake-Tools
release and are freely redistributable, so committing the reference `.dat` is fine.

Tests, written before the code they cover:

- **Lexer** — token streams for representative `.qc` snippets (strings with
  escapes, vectors, negative-immediate `a - 5` gotcha, frame macros, comments).
- **Immediate dedup** — repeated constants share offsets; first-appearance order
  fixes offsets.
- **Codegen unit** — a single expression (e.g. `self.health = self.health - x;`)
  emits the expected `Statement` sequence incl. the `LOAD→ADDRESS` rewrite and
  `STOREP`.
- **CRC** — generated `progdefs.h` text yields CRC 5927.
- **Per-lump equality** — compile `v101qc`, compare each lump (strings,
  statements, functions, globaldefs, fielddefs, globals) to the oracle
  separately, so a failure localizes before the whole-file assert.
- **Byte-identity (primary)** — `compile_progs_src(v101qc) == oracle_bytes`.
- **Functional sanity** — boot the self-compiled `progs.dat` through `pr_exec.py`,
  run a few e1m1 frames, confirm no VM faults.

Tests follow the repo convention: standalone `tests/test_qcc_*.py` scripts using
the existing `_bootstrap.py`, `test_*` functions, print `OK`. Run muted
(`PQ_AUDIO=0`).

## Implementation order (rough)

1. Oracle generation + commit the reference `.dat`.
2. `types.py`, `symbols.py` (foundations, unit-tested).
3. `lexer.py` (unit-tested against snippets).
4. `codegen.py` + opcode table.
5. `compiler.py` (expressions → statements → defs → functions/state).
6. `writer.py` + CRC; reach first full compile.
7. Per-lump diff → close gaps → byte-identity green.
8. Functional boot test; `__main__.py` CLI; docstrings + README pointer.
