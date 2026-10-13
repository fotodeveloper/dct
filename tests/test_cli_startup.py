"""`dct` starts without the database layer when it does not need it (#921).

`dct --version`, help and the hooks' fast paths never touch a database, yet
importing SQLAlchemy and dct.db costs ~200 ms of every start. These tests run
the real entry point in a fresh interpreter and assert the MODULE SET it
loaded — never a wall-clock number, which would depend on the machine.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from conftest import REPO_ROOT

pytestmark = pytest.mark.slow

# The database layer: none of these may be imported by a command that does
# not open a database.
HEAVY = ("sqlalchemy", "dct.db", "dct.core.engine")

# Runs `dct <argv>` through dct.cli.main() and reports which HEAVY modules the
# process loaded by the time the command finished (it exits via SystemExit
# for --version / --help, or returns).
PROBE = r'''
import json, sys
heavy, argv = json.loads(sys.argv[1]), json.loads(sys.argv[2])
sys.argv = ["dct", *argv]
code = 0
try:
    import dct.cli
    dct.cli.main()
except SystemExit as exc:
    code = exc.code or 0
sys.stdout.flush()
print("\nPROBE " + json.dumps({"code": code, "loaded": [m for m in heavy if m in sys.modules]}))
'''


def probe(*argv: str) -> dict:
    out = subprocess.run([sys.executable, "-c", PROBE, json.dumps(HEAVY), json.dumps(argv)],
                         cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL, check=False)
    assert out.returncode == 0, out.stderr
    line = [part for part in out.stdout.splitlines() if part.startswith("PROBE ")][-1]
    return json.loads(line[len("PROBE "):])


@pytest.mark.parametrize("argv", [
    ("--version",),
    ("--help",),
    (),                              # no command: the top-level help
    ("clog", "--help"),              # a group whose parser carries core vocabularies
    ("resolve", "--help"),           # --status choices (TERMINAL_STATUSES)
    ("clog", "release-advance", "--help"),  # positional status choices (RELEASE_FLOW)
], ids=lambda argv: " ".join(argv) or "no-command")
def test_commands_without_a_database_never_import_the_database_layer(argv):
    result = probe(*argv)
    assert result == {"code": 0, "loaded": []}


def test_the_probe_sees_the_database_layer_when_a_command_needs_it():
    """The detector is not vacuous: a command that opens the database (the
    suite's throwaway SQLite file, #822) loads exactly the layer the other
    test proves absent."""
    result = probe("db", "upgrade")
    assert result["code"] == 0 and sorted(result["loaded"]) == sorted(HEAVY)


def test_the_parser_vocabularies_match_their_core_definitions():
    """dct.cli mirrors these as literals so the parser can be built without
    dct.db; the core modules stay the single source of the values."""
    from dct import cli
    from dct.core.items import TERMINAL_STATUSES
    from dct.core.releases import RELEASE_FLOW

    assert cli.TERMINAL_STATUSES == TERMINAL_STATUSES
    assert cli.RELEASE_FLOW == RELEASE_FLOW
