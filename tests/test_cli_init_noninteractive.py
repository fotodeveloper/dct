"""Non-terminal init refuses before prompts, config, DB or integration effects."""
import io
from argparse import Namespace

import pytest
from conftest import run_cli

from dct import cli


@pytest.mark.parametrize("entry", ("direct", "main"))
def test_non_tty_init_refuses_before_any_prompt_bootstrap_or_update_probe(monkeypatch, capsys, entry):
    def forbidden(*args, **kwargs):
        pytest.fail("non-TTY init reached an effect or prompt")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    for name in ("get_engine", "create_tables", "save_config", "_maybe_nudge"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    with pytest.raises(SystemExit) as exc:
        if entry == "direct":
            cli.cmd_init(Namespace())
        else:
            run_cli(monkeypatch, ["init"])
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "terminal" in output.err.lower()
    assert "dct init" in output.err
    assert "Traceback" not in output.err
    assert "setup ===" not in output.out
