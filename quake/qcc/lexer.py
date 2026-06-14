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
