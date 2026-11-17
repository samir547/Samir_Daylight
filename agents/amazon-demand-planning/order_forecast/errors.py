"""Errors that mean "this run cannot work as configured": retrying will not help.

ConfigError is a SystemExit (so existing callers and tests that expect SystemExit still work) whose exit code
is 2, the code scripts/run_monthly.sh treats as "could not run, do not retry". The CLI prints the message.
"""
from __future__ import annotations

EXIT_CANNOT_RUN = 2


class ConfigError(SystemExit):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = EXIT_CANNOT_RUN

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""
