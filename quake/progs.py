"""QuakeC progs.dat (version 6) loader. Pure stdlib.

progs.dat is the compiled game logic: a flat array of 32-bit globals operated on
by a list of bytecode statements, plus the metadata to make sense of them.

Header (dprograms_t): int version, crc, then six (ofs, count) lump pairs
  -- statements, globaldefs, fielddefs, functions, strings, globals -- and
  int entityfields (size of an edict's field block, in 32-bit slots).

  statement  <Hhhh   op a b c           (op is unsigned; a/b/c are global offsets
                                          or, for IF/GOTO, signed jumps)
  def        <HHi     type ofs s_name    (type low 15 bits = etype; bit15 = saveglobal)
  function   <7i8B    first_statement parm_start locals profile s_name s_file
                      numparms parm_size[8]   (first_statement < 0 => builtin -N)

The globals lump is the live machine state. We keep it as one bytearray with two
aliased memoryview casts -- float and int over the SAME memory -- which is exactly
the C `union eval_t`: slot o is G_FLOAT via gf[o] or G_INT via gi[o], no copying.
All little-endian.
"""

import struct

PROG_VERSION = 6

# etype_t -- what a def/field slot holds
ev_void, ev_string, ev_float, ev_vector, ev_entity, ev_field, ev_function, ev_pointer = range(8)

DEF_SAVEGLOBAL = 1 << 15

# reserved global offsets (pr_comp.h): return value + 8 param slots, 3 each for vectors
OFS_RETURN = 1
OFS_PARM0 = 4
OFS_PARM_STRIDE = 3

_S_HEADER = struct.Struct("<2i 12i i")   # version crc, 6*(ofs,count), entityfields
_S_STATEMENT = struct.Struct("<Hhhh")    # op a b c
_S_DEF = struct.Struct("<HHi")           # type ofs s_name
_S_FUNCTION = struct.Struct("<7i8B")     # first_statement parm_start locals profile
                                         #   s_name s_file numparms parm_size[8]


class Function:
    __slots__ = ("first_statement", "parm_start", "locals", "s_name", "s_file",
                 "numparms", "parm_size", "name")

    def __init__(self, fields, name):
        (self.first_statement, self.parm_start, self.locals, _profile,
         self.s_name, self.s_file, self.numparms, *psize) = fields
        self.parm_size = psize          # 8 bytes; first numparms are meaningful
        self.name = name

    @property
    def builtin(self):
        """Builtin number (>0) if this is engine-implemented, else 0."""
        return -self.first_statement if self.first_statement < 0 else 0

    def __repr__(self):
        kind = f"builtin #{self.builtin}" if self.builtin else f"@{self.first_statement}"
        return f"<Function {self.name!r} {kind} parms={self.numparms}>"


class Progs:
    def __init__(self, data):
        h = _S_HEADER.unpack_from(data, 0)
        version, crc = h[0], h[1]
        if version != PROG_VERSION:
            raise ValueError(f"progs version {version}, expected {PROG_VERSION}")
        self.crc = crc
        # six lumps: (ofs, count) pairs at h[2..13]
        (ofs_st, n_st, ofs_gd, n_gd, ofs_fd, n_fd,
         ofs_fn, n_fn, ofs_str, n_str, ofs_gl, n_gl) = h[2:14]
        self.entityfields = h[14]       # field slots per edict

        # --- strings: one NUL-terminated blob, indexed by byte offset. Mutable +
        # growable so new_string() can append runtime strings (entity values,
        # ftos results) and hand back a positive offset -- our stand-in for C's
        # pr_strings heap, where G_STRING is just strings + offset. ---
        self.strings = bytearray(data[ofs_str:ofs_str + n_str])

        # --- statements: (op, a, b, c) tuples ---
        self.statements = [s for s in
                           _S_STATEMENT.iter_unpack(data[ofs_st:ofs_st + n_st * _S_STATEMENT.size])]

        # --- defs: (etype, ofs, name); split global vs field, build name maps ---
        self.globaldefs, self.global_by_name = self._load_defs(data, ofs_gd, n_gd)
        self.fielddefs, self.field_by_name = self._load_defs(data, ofs_fd, n_fd)

        # --- functions ---
        self.functions = []
        self.func_by_name = {}
        for i in range(n_fn):
            f = Function(_S_FUNCTION.unpack_from(data, ofs_fn + i * _S_FUNCTION.size), "")
            f.name = self.string(f.s_name)
            self.functions.append(f)
            if f.name:
                self.func_by_name.setdefault(f.name, i)

        # --- globals: live state, two aliased views over one buffer (the eval_t union) ---
        self.globals = bytearray(data[ofs_gl:ofs_gl + n_gl * 4])
        self.gf = memoryview(self.globals).cast("f")   # float view  (G_FLOAT)
        self.gi = memoryview(self.globals).cast("i")    # int   view  (G_INT, ent/func/str refs)
        self.numglobals = n_gl

    # ---- loading helpers ----
    def _load_defs(self, data, ofs, count):
        defs = []
        by_name = {}
        for i in range(count):
            t, o, s_name = _S_DEF.unpack_from(data, ofs + i * _S_DEF.size)
            etype = t & ~DEF_SAVEGLOBAL
            name = self.string(s_name)
            # the save flag picks which globals ED_WriteGlobals persists
            defs.append((etype, o, name, bool(t & DEF_SAVEGLOBAL)))
            if name:
                by_name.setdefault(name, (etype, o))
        return defs, by_name

    # ---- runtime accessors ----
    def string(self, ofs):
        """Resolve a string offset into the strings blob -> str."""
        if ofs < 0 or ofs >= len(self.strings):
            return ""
        end = self.strings.find(b"\0", ofs)
        if end < 0:
            end = len(self.strings)
        return self.strings[ofs:end].decode("latin-1")

    def new_string(self, s):
        """Append a runtime string to the heap, return its offset (like ED_NewString).
        Handles the \\n and \\\\ escapes ED_NewString does."""
        if isinstance(s, str):
            s = s.encode("latin-1")
        s = s.replace(b"\\n", b"\n").replace(b"\\\\", b"\\")
        ofs = len(self.strings)
        self.strings += s + b"\0"
        return ofs

    def global_ofs(self, name):
        """Global-variable offset by name, or None."""
        d = self.global_by_name.get(name)
        return d[1] if d else None

    def field_ofs(self, name):
        """Edict field offset (in slots) by name, or None."""
        d = self.field_by_name.get(name)
        return d[1] if d else None

    def find_function(self, name):
        """Function index by name, or None."""
        return self.func_by_name.get(name)


if __name__ == "__main__":
    import sys
    from .pak import Pak
    pak = Pak(sys.argv[1] if len(sys.argv) > 1 else "quake-shareware/id1/pak0.pak")
    p = Progs(pak.read("progs.dat"))
    print("progs.dat:")
    print(f"  crc          {p.crc}")
    print(f"  statements   {len(p.statements)}")
    print(f"  globaldefs   {len(p.globaldefs)}")
    print(f"  fielddefs    {len(p.fielddefs)}")
    print(f"  functions    {len(p.functions)}")
    print(f"  strings      {len(p.strings)} bytes")
    print(f"  globals      {p.numglobals} slots")
    print(f"  entityfields {p.entityfields} slots/edict")

    nbi = sum(1 for f in p.functions if f.builtin)
    print(f"\n  {nbi} builtins referenced, {len(p.functions) - nbi} QC functions")
    for name in ("main", "worldspawn", "StartFrame", "PlayerPostThink", "monster_army"):
        i = p.find_function(name)
        print(f"    {name:18} {p.functions[i] if i is not None else '(absent)'}")

    print("\n  some entity fields:")
    for name in ("origin", "angles", "nextthink", "think", "touch", "classname", "health"):
        print(f"    {name:12} slot {p.field_ofs(name)}")
