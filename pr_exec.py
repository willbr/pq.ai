"""QuakeC bytecode interpreter -- the VM that runs progs.dat. Pure stdlib.

Ported from pr_exec.c. The machine is dead simple: a flat array of 32-bit globals
(progs.gf / progs.gi -- float and int views of the same memory) operated on by a
list of (op, a, b, c) statements, where a/b/c are global slot offsets.

Memory model (kept flat and integer-indexed, like Quake itself):
  * globals      -- progs.gf[o] / progs.gi[o], the eval_t union per slot.
  * edict fields -- one bytearray for ALL edicts; edict N's field block starts at
                    N * edict_size slots. self.ef / self.ei are float/int views.
  * entity ref   -- stored in a slot as an int: the edict number N (0 = world).
  * field pointer-- (OP_ADDRESS result / OP_STOREP arg) a slot index into the edict
                    array == N * edict_size + field_slot. ADDRESS makes them and
                    STOREP consumes them, so the convention is self-consistent.

Calls: PR_EnterFunction saves the callee's locals region onto a stack, copies the
caller's OFS_PARM slots into the callee's parm_start, and runs. PR_LeaveFunction
restores. All copies go through the int view so any bit pattern (float/ent/func/
string ref) survives intact.
"""

from progs import OFS_RETURN, OFS_PARM0, OFS_PARM_STRIDE

# opcodes, in pr_comp.h enum order
(OP_DONE, OP_MUL_F, OP_MUL_V, OP_MUL_FV, OP_MUL_VF, OP_DIV_F, OP_ADD_F, OP_ADD_V,
 OP_SUB_F, OP_SUB_V, OP_EQ_F, OP_EQ_V, OP_EQ_S, OP_EQ_E, OP_EQ_FNC, OP_NE_F, OP_NE_V,
 OP_NE_S, OP_NE_E, OP_NE_FNC, OP_LE, OP_GE, OP_LT, OP_GT, OP_LOAD_F, OP_LOAD_V,
 OP_LOAD_S, OP_LOAD_ENT, OP_LOAD_FLD, OP_LOAD_FNC, OP_ADDRESS, OP_STORE_F, OP_STORE_V,
 OP_STORE_S, OP_STORE_ENT, OP_STORE_FLD, OP_STORE_FNC, OP_STOREP_F, OP_STOREP_V,
 OP_STOREP_S, OP_STOREP_ENT, OP_STOREP_FLD, OP_STOREP_FNC, OP_RETURN, OP_NOT_F,
 OP_NOT_V, OP_NOT_S, OP_NOT_ENT, OP_NOT_FNC, OP_IF, OP_IFNOT, OP_CALL0, OP_CALL1,
 OP_CALL2, OP_CALL3, OP_CALL4, OP_CALL5, OP_CALL6, OP_CALL7, OP_CALL8, OP_STATE,
 OP_GOTO, OP_AND, OP_OR, OP_BITAND, OP_BITOR) = range(66)

MAX_STACK_DEPTH = 64        # vanilla is 32; give headroom
RUNAWAY = 1_000_000         # statements before we assume an infinite loop


class PR_RunError(Exception):
    pass


class VM:
    def __init__(self, progs, max_edicts=600):
        self.pr = progs
        self.gf = progs.gf
        self.gi = progs.gi
        self.strings = progs.strings

        # --- edict field storage: one flat buffer, float/int union views ---
        self.edict_size = progs.entityfields        # slots per edict
        self.max_edicts = max_edicts
        self._ent_buf = bytearray(max_edicts * self.edict_size * 4)
        self.ef = memoryview(self._ent_buf).cast("f")
        self.ei = memoryview(self._ent_buf).cast("i")
        # engine-side bookkeeping (not QC-visible), indexed by edict number
        self.free = [True] * max_edicts
        self.free[0] = False                          # world is always live
        self.num_edicts = 1                           # just the world to start

        # --- call state ---
        self.stack = []          # list of (return_statement, function)
        self.localstack = []     # saved locals, flat ints
        self.xfunction = None
        self.xstatement = 0
        self.argc = 0
        self.trace = False

        # --- builtins: installed later by pr_builtins; index 0 unused ---
        self.builtins = [self._no_builtin]

        # --- well-known offsets (None-tolerant for synthetic tests) ---
        self.ofs_self = progs.global_ofs("self")
        self.ofs_time = progs.global_ofs("time")
        self.fld_nextthink = progs.field_ofs("nextthink")
        self.fld_frame = progs.field_ofs("frame")
        self.fld_think = progs.field_ofs("think")

    # ----- builtin not installed -----
    def _no_builtin(self):
        raise PR_RunError("call to unimplemented builtin")

    # ----- edict field access (used by OP_STATE and by builtins) -----
    def ent_base(self, num):
        return num * self.edict_size

    # field get/set: num = edict number, slot = field offset (in 32-bit slots)
    def fget_f(self, num, slot):
        return self.ef[num * self.edict_size + slot]

    def fset_f(self, num, slot, v):
        self.ef[num * self.edict_size + slot] = v

    def fget_i(self, num, slot):
        return self.ei[num * self.edict_size + slot]

    def fset_i(self, num, slot, v):
        self.ei[num * self.edict_size + slot] = v

    def fget_v(self, num, slot):
        b = num * self.edict_size + slot
        return (self.ef[b], self.ef[b + 1], self.ef[b + 2])

    def fset_v(self, num, slot, v):
        b = num * self.edict_size + slot
        self.ef[b], self.ef[b + 1], self.ef[b + 2] = v[0], v[1], v[2]

    # ----- builtin parameter / return access (OFS_PARM* and OFS_RETURN) -----
    def parm_f(self, i):
        return self.gf[OFS_PARM0 + i * OFS_PARM_STRIDE]

    def parm_i(self, i):
        return self.gi[OFS_PARM0 + i * OFS_PARM_STRIDE]

    def parm_v(self, i):
        o = OFS_PARM0 + i * OFS_PARM_STRIDE
        return (self.gf[o], self.gf[o + 1], self.gf[o + 2])

    def parm_strofs(self, i):
        return self.gi[OFS_PARM0 + i * OFS_PARM_STRIDE]

    def parm_str(self, i):
        return self.pr.string(self.gi[OFS_PARM0 + i * OFS_PARM_STRIDE])

    def ret_f(self, v):
        self.gf[OFS_RETURN] = v

    def ret_i(self, v):
        self.gi[OFS_RETURN] = v

    def ret_v(self, x, y, z):
        self.gf[OFS_RETURN] = x
        self.gf[OFS_RETURN + 1] = y
        self.gf[OFS_RETURN + 2] = z

    # ----- edict allocation (ED_Alloc / ED_Free / ED_ClearEdict) -----
    def clear_edict(self, num):
        b = num * self.edict_size * 4
        self._ent_buf[b:b + self.edict_size * 4] = bytes(self.edict_size * 4)
        self.free[num] = False

    def alloc_edict(self):
        # no clients in our single-player walker, so reuse starts at edict 1
        for i in range(1, self.num_edicts):
            if self.free[i]:
                self.clear_edict(i)
                return i
        i = self.num_edicts
        if i >= self.max_edicts:
            raise PR_RunError("ED_Alloc: no free edicts")
        self.num_edicts += 1
        self.clear_edict(i)
        return i

    def free_edict(self, num):
        self.clear_edict(num)
        if self.fld_nextthink is not None:
            self.fset_f(num, self.fld_nextthink, -1.0)
        self.free[num] = True

    # ======================================================================
    # call frame management
    # ======================================================================
    def enter_function(self, f):
        self.stack.append((self.xstatement, self.xfunction))
        if len(self.stack) >= MAX_STACK_DEPTH:
            raise PR_RunError("stack overflow")

        gi = self.gi
        ps = f.parm_start
        # save the locals region the callee is about to clobber
        for i in range(f.locals):
            self.localstack.append(gi[ps + i])
        # copy the caller's parameters into the callee's locals
        o = ps
        for i in range(f.numparms):
            src = OFS_PARM0 + i * OFS_PARM_STRIDE
            for j in range(f.parm_size[i]):
                gi[o] = gi[src + j]
                o += 1
        self.xfunction = f
        return f.first_statement - 1     # the loop's s++ lands on first_statement

    def leave_function(self):
        if not self.stack:
            raise PR_RunError("prog stack underflow")
        gi = self.gi
        f = self.xfunction
        c = f.locals
        base = len(self.localstack) - c
        ps = f.parm_start
        for i in range(c):               # restore caller's locals
            gi[ps + i] = self.localstack[base + i]
        del self.localstack[base:]
        ret_s, self.xfunction = self.stack.pop()
        return ret_s

    # ======================================================================
    # the interpreter loop
    # ======================================================================
    def execute(self, fnum):
        pr = self.pr
        if not fnum or fnum >= len(pr.functions):
            raise PR_RunError("NULL function")

        gf, gi = self.gf, self.gi
        statements = pr.statements
        functions = pr.functions
        esize = self.edict_size

        exitdepth = len(self.stack)
        s = self.enter_function(functions[fnum])
        runaway = RUNAWAY

        while True:
            s += 1
            op, a, b, c = statements[s]
            runaway -= 1
            if runaway == 0:
                raise PR_RunError("runaway loop error")
            self.xstatement = s

            # --- ordered roughly by dynamic frequency ---
            if op == OP_STORE_F or op == OP_STORE_S or op == OP_STORE_ENT \
                    or op == OP_STORE_FLD or op == OP_STORE_FNC:
                gi[b] = gi[a]
            elif op == OP_STORE_V:
                gi[b] = gi[a]; gi[b + 1] = gi[a + 1]; gi[b + 2] = gi[a + 2]

            elif op == OP_IFNOT:
                if not gi[a]:
                    s += b - 1
            elif op == OP_IF:
                if gi[a]:
                    s += b - 1
            elif op == OP_GOTO:
                s += a - 1

            elif op == OP_ADD_F:
                gf[c] = gf[a] + gf[b]
            elif op == OP_SUB_F:
                gf[c] = gf[a] - gf[b]
            elif op == OP_MUL_F:
                gf[c] = gf[a] * gf[b]
            elif op == OP_DIV_F:
                gf[c] = gf[a] / gf[b] if gf[b] else 0.0

            elif op == OP_LOAD_F or op == OP_LOAD_S or op == OP_LOAD_ENT \
                    or op == OP_LOAD_FLD or op == OP_LOAD_FNC:
                gi[c] = self.ei[gi[a] * esize + gi[b]]
            elif op == OP_LOAD_V:
                p = gi[a] * esize + gi[b]
                gi[c] = self.ei[p]; gi[c + 1] = self.ei[p + 1]; gi[c + 2] = self.ei[p + 2]

            elif OP_CALL0 <= op <= OP_CALL8:
                self.argc = op - OP_CALL0
                fnum2 = gi[a]
                if not fnum2:
                    raise PR_RunError("NULL function call")
                newf = functions[fnum2]
                if newf.first_statement < 0:        # builtin
                    self.builtins[-newf.first_statement]()
                else:
                    s = self.enter_function(newf)

            elif op == OP_RETURN or op == OP_DONE:
                gi[OFS_RETURN] = gi[a]
                gi[OFS_RETURN + 1] = gi[a + 1]
                gi[OFS_RETURN + 2] = gi[a + 2]
                s = self.leave_function()
                if len(self.stack) == exitdepth:
                    return

            elif op == OP_ADDRESS:
                gi[c] = gi[a] * esize + gi[b]
            elif op == OP_STOREP_F or op == OP_STOREP_S or op == OP_STOREP_ENT \
                    or op == OP_STOREP_FLD or op == OP_STOREP_FNC:
                self.ei[gi[b]] = gi[a]
            elif op == OP_STOREP_V:
                p = gi[b]
                self.ei[p] = gi[a]; self.ei[p + 1] = gi[a + 1]; self.ei[p + 2] = gi[a + 2]

            elif op == OP_EQ_F:
                gf[c] = float(gf[a] == gf[b])
            elif op == OP_NE_F:
                gf[c] = float(gf[a] != gf[b])
            elif op == OP_LE:
                gf[c] = float(gf[a] <= gf[b])
            elif op == OP_GE:
                gf[c] = float(gf[a] >= gf[b])
            elif op == OP_LT:
                gf[c] = float(gf[a] < gf[b])
            elif op == OP_GT:
                gf[c] = float(gf[a] > gf[b])

            elif op == OP_NOT_F or op == OP_NOT_FNC or op == OP_NOT_ENT:
                gf[c] = float(not gi[a])
            elif op == OP_NOT_S:
                sref = gi[a]
                gf[c] = float(not sref or sref >= len(self.strings) or self.strings[sref] == 0)
            elif op == OP_NOT_V:
                gf[c] = float(gf[a] == 0.0 and gf[a + 1] == 0.0 and gf[a + 2] == 0.0)

            elif op == OP_AND:
                gf[c] = float(bool(gf[a]) and bool(gf[b]))
            elif op == OP_OR:
                gf[c] = float(bool(gf[a]) or bool(gf[b]))
            elif op == OP_BITAND:
                gf[c] = float(int(gf[a]) & int(gf[b]))
            elif op == OP_BITOR:
                gf[c] = float(int(gf[a]) | int(gf[b]))

            elif op == OP_ADD_V:
                gf[c] = gf[a] + gf[b]; gf[c + 1] = gf[a + 1] + gf[b + 1]; gf[c + 2] = gf[a + 2] + gf[b + 2]
            elif op == OP_SUB_V:
                gf[c] = gf[a] - gf[b]; gf[c + 1] = gf[a + 1] - gf[b + 1]; gf[c + 2] = gf[a + 2] - gf[b + 2]
            elif op == OP_MUL_V:                     # dot product -> float
                gf[c] = gf[a] * gf[b] + gf[a + 1] * gf[b + 1] + gf[a + 2] * gf[b + 2]
            elif op == OP_MUL_FV:
                f = gf[a]
                gf[c] = f * gf[b]; gf[c + 1] = f * gf[b + 1]; gf[c + 2] = f * gf[b + 2]
            elif op == OP_MUL_VF:
                f = gf[b]
                gf[c] = f * gf[a]; gf[c + 1] = f * gf[a + 1]; gf[c + 2] = f * gf[a + 2]

            elif op == OP_EQ_V:
                gf[c] = float(gf[a] == gf[b] and gf[a + 1] == gf[b + 1] and gf[a + 2] == gf[b + 2])
            elif op == OP_NE_V:
                gf[c] = float(gf[a] != gf[b] or gf[a + 1] != gf[b + 1] or gf[a + 2] != gf[b + 2])
            elif op == OP_EQ_S:
                gf[c] = float(pr.string(gi[a]) == pr.string(gi[b]))
            elif op == OP_NE_S:
                gf[c] = float(pr.string(gi[a]) != pr.string(gi[b]))
            elif op == OP_EQ_E or op == OP_EQ_FNC:
                gf[c] = float(gi[a] == gi[b])
            elif op == OP_NE_E or op == OP_NE_FNC:
                gf[c] = float(gi[a] != gi[b])

            elif op == OP_STATE:
                ed = self.ent_base(gi[self.ofs_self])
                self.ef[ed + self.fld_nextthink] = gf[self.ofs_time] + 0.1
                self.ef[ed + self.fld_frame] = gf[a]
                self.ei[ed + self.fld_think] = gi[b]

            else:
                raise PR_RunError(f"bad opcode {op} at statement {s}")


# ==========================================================================
# self-test: a hand-assembled micro-program exercises the control-flow core
# (enter/leave, param copy, CALL/RETURN, ADD, and an IF/GOTO loop) with no
# builtins or edicts -- the parts that are easy to get subtly wrong.
# ==========================================================================
if __name__ == "__main__":
    from array import array
    from progs import Function

    class FakeProgs:
        """Minimal duck-typed stand-in: just what VM.execute touches."""
        def __init__(self, statements, functions, nglobals=128):
            self.statements = statements
            self.functions = functions
            self.entityfields = 4
            self.strings = b"\0"
            self._buf = bytearray(nglobals * 4)
            self.gf = memoryview(self._buf).cast("f")
            self.gi = memoryview(self._buf).cast("i")

        def string(self, o):
            return ""

        def global_ofs(self, name):
            return None

        def field_ofs(self, name):
            return None

    def func(first, parm_start=40, locals=0, numparms=0, psize=()):
        ps = list(psize) + [0] * (8 - len(psize))
        return Function((first, parm_start, locals, 0, 0, 0, numparms, *ps), "")

    # globals layout: 1-3 return, 4.. parms; constants + scratch up high
    C3, C4, RESULT, ADDIDX, C0, C1, TMP, SUMIDX, SUMRES, C5 = range(30, 40)

    st = [(OP_DONE, 0, 0, 0)]            # functions[0] is the reserved empty one
    funcs = [func(0)]

    # --- add(x,y){ return x+y; }  at parm_start 40, locals x=40 y=41 ---
    add_first = len(st)
    st += [(OP_ADD_F, 40, 41, OFS_RETURN),
           (OP_RETURN, OFS_RETURN, 0, 0)]
    add_idx = len(funcs)
    funcs.append(func(add_first, parm_start=40, locals=2, numparms=2, psize=(1, 1)))

    # --- sum1ton(n){ acc=0; i=1; while(i<=n){acc+=i; i++;} return acc; } ---
    # parm_start 50: n=50, acc=51, i=52
    sum_first = len(st)
    L_loop = sum_first + 2
    st += [
        (OP_STORE_F, C0, 51, 0),         # acc = 0
        (OP_STORE_F, C1, 52, 0),         # i   = 1
        (OP_LE, 52, 50, TMP),            # L_loop: TMP = (i <= n)
        (OP_IFNOT, TMP, 0, 0),           # if !TMP goto L_exit  (patched below)
        (OP_ADD_F, 51, 52, 51),          # acc += i
        (OP_ADD_F, 52, C1, 52),          # i += 1
        (OP_GOTO, 0, 0, 0),              # goto L_loop          (patched below)
        (OP_STORE_F, 51, OFS_RETURN, 0), # L_exit: return acc
        (OP_RETURN, OFS_RETURN, 0, 0),
    ]
    L_exit = sum_first + 7
    st[sum_first + 3] = (OP_IFNOT, TMP, L_exit - (sum_first + 3), 0)
    st[sum_first + 6] = (OP_GOTO, L_loop - (sum_first + 6), 0, 0)
    sum_idx = len(funcs)
    funcs.append(func(sum_first, parm_start=50, locals=3, numparms=1, psize=(1,)))

    # --- main(){ result = add(3,4); } and separately sum1ton(5) ---
    main_first = len(st)
    st += [
        (OP_STORE_F, C3, OFS_PARM0, 0),      # parm0 = 3
        (OP_STORE_F, C4, OFS_PARM0 + 3, 0),  # parm1 = 4
        (OP_CALL2, ADDIDX, 0, 0),            # add(3,4) -> OFS_RETURN
        (OP_STORE_F, OFS_RETURN, RESULT, 0),
        (OP_STORE_F, C5, OFS_PARM0, 0),      # parm0 = 5
        (OP_CALL1, SUMIDX, 0, 0),            # sum1ton(5) -> OFS_RETURN
        (OP_STORE_F, OFS_RETURN, SUMRES, 0),
        (OP_DONE, 0, 0, 0),
    ]
    main_idx = len(funcs)
    funcs.append(func(main_first, parm_start=60, locals=0, numparms=0))

    pr = FakeProgs(st, funcs)
    pr.gf[C3] = 3.0; pr.gf[C4] = 4.0; pr.gf[C5] = 5.0
    pr.gf[C0] = 0.0; pr.gf[C1] = 1.0
    pr.gi[ADDIDX] = add_idx
    pr.gi[SUMIDX] = sum_idx

    vm = VM(pr, max_edicts=2)
    vm.execute(main_idx)

    got_add = pr.gf[RESULT]
    got_sum = pr.gf[SUMRES]
    print(f"add(3,4)    = {got_add}   (expect 7.0)")
    print(f"sum1ton(5)  = {got_sum}   (expect 15.0)")
    assert got_add == 7.0, got_add
    assert got_sum == 15.0, got_sum
    print("\ninterpreter core OK: enter/leave, param copy, CALL/RETURN, ADD, IF/GOTO loop")
