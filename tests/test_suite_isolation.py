"""The test suite must never reach the developer's real dct database (#822).

Hook tests run shell hooks that call the PATH `dct`; without isolation that
binary reads the developer's config and migrates their LIVE database with
this tree's code. `conftest._suite_isolation` points every subprocess at a
throwaway SQLite file + config and puts THIS tree first on PYTHONPATH. These
tests lock that in by observing what a real child process resolves.
"""

import os
import subprocess
import sys
from pathlib import Path

import conftest
import pytest
from conftest import throwaway_refusal_reason

REPO_ROOT = Path(__file__).resolve().parent.parent


def _child(code: str, cwd: Path) -> str:
    """Run `code` in a fresh interpreter that inherits the suite's env.

    `cwd` is outside the repo on purpose: `python -c` puts the cwd on
    sys.path, which would mask a missing PYTHONPATH entry."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30, check=True, cwd=cwd,
    )
    return result.stdout.strip()


def test_subprocess_resolves_database_to_suite_tmp(tmp_path_factory, tmp_path):
    """A child's load_config() lands on the fixture's SQLite file under the
    session basetemp — never the developer's configured database."""
    url = _child("from dct.config import load_config; print(load_config().database_url)", tmp_path)
    base = Path(os.path.realpath(tmp_path_factory.getbasetemp()))
    assert url.startswith("sqlite:///"), url
    db_path = Path(os.path.realpath(url.removeprefix("sqlite:///")))
    assert base in db_path.parents, f"{db_path} is not under the suite basetemp {base}"
    assert url != conftest.LIVE_DATABASE_URL


def test_subprocess_config_path_is_suite_tmp(tmp_path_factory, tmp_path):
    """The child reads its config from the throwaway file, so no [database].url
    from the developer's dct.toml can leak in."""
    path = _child("from dct import paths; print(paths.default_config_path())", tmp_path)
    base = Path(os.path.realpath(tmp_path_factory.getbasetemp()))
    assert base in Path(os.path.realpath(path)).parents, path


def test_subprocess_imports_this_tree(tmp_path):
    """A child imports THIS checkout's `dct`, not whatever the interpreter
    has installed (an editable install of another checkout, a uv tool)."""
    dct_file = Path(_child("import dct; print(dct.__file__)", tmp_path)).resolve()
    assert dct_file == REPO_ROOT / "dct" / "__init__.py"


def test_live_identity_captured_before_isolation():
    """The K-31 guard's live identity is read at conftest import — BEFORE the
    session fixture overrides DCT_DATABASE_URL — so it is never the suite's
    own throwaway file."""
    assert conftest.LIVE_DATABASE_URL != os.environ["DCT_DATABASE_URL"]


def test_guard_refuses_captured_live_identity(monkeypatch):
    """The guard compares against the CAPTURED identity: a lane target equal
    to it is refused even though the process env now names a throwaway DB."""
    fake_live = "postgresql+psycopg://dct@db.example:5432/dct_ci"
    monkeypatch.setattr(conftest, "LIVE_DATABASE_URL", fake_live)
    reason = conftest.pg_lane_refusal_reason(fake_live, item_count=0)
    assert reason is not None and "LIVE" in reason
    # Control: a different throwaway database on the same host is allowed.
    other = "postgresql+psycopg://dct@db.example:5432/dct_ci_other"
    assert conftest.pg_lane_refusal_reason(other, item_count=0) is None
    assert throwaway_refusal_reason(fake_live) is None  # the name alone passes


@pytest.mark.parametrize("var", ["DCT_DATABASE_URL", "DCT_CONFIG_PATH", "DCT_DATA_DIR"])
def test_isolation_vars_are_set(var, tmp_path_factory):
    base = Path(os.path.realpath(tmp_path_factory.getbasetemp()))
    value = os.environ[var].removeprefix("sqlite:///")
    assert base in Path(os.path.realpath(value)).parents
