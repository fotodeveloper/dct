"""User-facing CLI text carries no internal triage card numbers (#877).

Card numbers such as "K-76" are a developer cross-reference (docstrings,
comments, docs/); a user reading `--help` or a refusal cannot know them.
"""

import argparse
import ast
import pathlib
import re

from dct.cli import build_parser

CARD = re.compile(r"\bK-\d+")
PKG = pathlib.Path(__file__).resolve().parent.parent / "dct"


def _walk(parser, path=("dct",)):
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _walk(sub, (*path, name))


def test_no_card_number_in_any_help():
    leaks = [
        " ".join(path) + ": " + m.group()
        for path, parser in _walk(build_parser())
        for m in CARD.finditer(parser.format_help())
    ]
    assert leaks == []


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ids.add(id(node.value))
    return ids


def test_no_card_number_in_runtime_string_literals():
    """Every non-docstring string literal (help, print, refusal text) of the
    CLI and the release workflow is free of card numbers."""
    files = [PKG / "cli.py", *sorted((PKG / "workflows" / "release").glob("*.py")),
             PKG / "core" / "changelog_file.py"]
    leaks = []
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and CARD.search(node.value)):
                leaks.append(f"{f.name}:{node.lineno}")
    assert leaks == []
