"""qcc compile errors. Mirrors id's PR_ParseError message style (pr_lex.c)."""


class QCCError(Exception):
    def __init__(self, file, line, message):
        self.file, self.line, self.message = file, line, message
        super().__init__(f"{file}:{line}:{message}")
