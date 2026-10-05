"""Execution contracts for the changelog proof workflow (K-30 / K-45 / K-46).

The prose tests in test_skill_prose_changelog.py lock WORDS; these tests run
the documented commands on a real throwaway git repository and lock what they
DO:

- the trailer-reading commands are extracted verbatim from the authoritative
  docs/assistant-fallbacks.md and executed against commits that carry a
  trailer block, body lines, quoted / bulleted prose, a squash-preserved body
  and a local `git merge --squash` (whose indented messages must NOT count);
- the /commit sequence (stage → select from the final `git diff --cached` →
  commit without another `git add`) is replayed from the skill's own code
  blocks on a repository whose index and working tree DIFFER (deliberate
  partial staging): the trailer must describe exactly the committed hunk.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dct.assets import skills_dir

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

FALLBACKS = Path(__file__).resolve().parent.parent / "docs" / "assistant-fallbacks.md"


def _fallbacks() -> str:
    return FALLBACKS.read_text(encoding="utf-8")


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def _doc_command(pattern: str) -> str:
    """One backticked command from the authoritative doc, verbatim."""
    match = re.search(pattern, _fallbacks())
    assert match, f"assistant-fallbacks.md lost the command matching {pattern!r}"
    return match.group(1)


def _trailer_command() -> str:
    return _doc_command(r"`(git log --format='%\(trailers:key=Changelog[^`]*?) R`")


def _body_command() -> str:
    return _doc_command(r"`(git log --format=%B) R`")


def _strict_regex() -> re.Pattern:
    return re.compile(_doc_command(r"`(\^Changelog: [^`]+\$)`"))


def _env(**extra: str) -> dict:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GIT_") or k in ("GIT_EXEC_PATH",)
    }
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    })
    env.update(extra)
    return env


def _git(repo: Path, *args: str, **kw) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=_env(), check=True,
        capture_output=True, text=True, **kw,
    ).stdout


def _bash(repo: Path, script: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], cwd=repo, env=_env(**env), check=False,
        capture_output=True, text=True,
    )


def _commit(repo: Path, message: str, path: str = "f.txt") -> None:
    with open(repo / path, "a", encoding="utf-8") as fh:
        fh.write(message.splitlines()[0] + "\n")
    _git(repo, "add", "--", path)
    _git(repo, "commit", "-q", "-F", "-", input=message)


def _proven_ids(repo: Path, rng: str) -> tuple[set[int], set[int]]:
    """Run the doc's two reads for range `rng`; return (trailer ids, body ids)."""
    trailer_out = _bash(repo, f"{_trailer_command()} {rng}")
    assert trailer_out.returncode == 0, trailer_out.stderr
    trailer_ids = {
        int(part) for line in trailer_out.stdout.splitlines()
        for part in (p.strip() for p in line.split(",")) if part
    }
    body_out = _bash(repo, f"{_body_command()} {rng}")
    assert body_out.returncode == 0, body_out.stderr
    regex = _strict_regex()
    body_ids = {
        int(part) for line in body_out.stdout.splitlines() if regex.match(line)
        for part in line[len("Changelog: "):].split(", ")
    }
    return trailer_ids, body_ids


@pytest.fixture
def proof_repo(tmp_path):
    repo = tmp_path / "proj-a"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "chore: before the tag\n\nChangelog: 99\n")
    _git(repo, "tag", "v1.0")
    # 1, 2 — the trailer block /commit writes.
    _commit(repo, "feat: a\n\nWhy.\n\nChangelog: 1, 2\nCo-Authored-By: X <x@example.com>\n")
    # 3 — an exact line inside a body paragraph (what a squash keeping the
    # original messages looks like); counted by the strict body scan.
    _commit(repo, "docs: b\n\nChangelog: 3\n\nMore prose afterwards.\n")
    # 4 — the format mentioned inline in prose: never proof.
    _commit(repo, "docs: c\n\nThe format is Changelog: 4 in the trailer.\n")
    # 5, 6 — quoted and bulleted prose: never proof.
    _commit(repo, "docs: d\n\n> Changelog: 5\n\nCo-Authored-By: X <x@example.com>\n")
    _commit(repo, "docs: e\n\n- Changelog: 6\n\nCo-Authored-By: X <x@example.com>\n")
    # 7, 8, 9 — a hosted squash that kept the original commit messages.
    _commit(repo, (
        "feat: squashed (#12)\n\n* feat: part one\n\nChangelog: 7\n\n"
        "* fix: part two\n\nChangelog: 8, 9\n\nCo-Authored-By: X <x@example.com>\n"
    ))
    # 10 — a LOCAL `git merge --squash`: git indents the original messages.
    _git(repo, "checkout", "-q", "-b", "feat/x")
    _commit(repo, "feat: on branch\n\nChangelog: 10\nCo-Authored-By: X <x@example.com>\n", "g.txt")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", "feat/x")
    _git(repo, "commit", "-q", "--no-edit")
    return repo


def test_merge_squash_fixture_really_indents_the_original_trailer(proof_repo):
    body = _git(proof_repo, "log", "-1", "--format=%B")
    assert "    Changelog: 10" in body  # the premise of the doc's squash rule


def test_documented_range_commands_select_the_tag_range(proof_repo):
    sha = _git(proof_repo, "rev-parse", "HEAD").strip()
    verify = _doc_command(r'`(git rev-parse -q --verify "refs/tags/v\$CUR")`')
    ancestor = _doc_command(r'`(git merge-base --is-ancestor "v\$CUR" "\$SHA")`')
    assert _bash(proof_repo, verify, CUR="1.0").returncode == 0
    assert _bash(proof_repo, ancestor, CUR="1.0", SHA=sha).returncode == 0
    assert _bash(proof_repo, verify, CUR="2.0").returncode != 0


def test_documented_reads_prove_exactly_the_trailer_and_body_lines(proof_repo):
    sha = _git(proof_repo, "rev-parse", "HEAD").strip()
    trailer_ids, body_ids = _proven_ids(proof_repo, f"v1.0..{sha}")
    assert trailer_ids == {1, 2}
    assert body_ids == {1, 2, 3, 7, 8, 9}
    proven = trailer_ids | body_ids
    assert proven == {1, 2, 3, 7, 8, 9}
    # Inline prose, quoted / bulleted lines, the indented merge --squash
    # trailer and the trailer below the previous tag are never proof.
    assert proven.isdisjoint({4, 5, 6, 10, 99})


def _code_block_after(text: str, heading: str) -> str:
    start = text.index(heading)
    match = re.search(r"```bash\n(.*?)\n```", text[start:], re.DOTALL)
    assert match, f"no bash block after {heading!r}"
    return match.group(1)


def test_commit_sequence_attests_exactly_the_partially_staged_hunk(tmp_path):
    body = _skill("commit")
    stage_block = _code_block_after(body, "### Step 4: Stage")
    commit_block = _code_block_after(body, "### Step 6: Commit")
    assert stage_block.startswith("git add -- ")
    assert "git add" not in commit_block and "commit -a" not in commit_block

    repo = tmp_path / "proj-a"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    filler = [f"line {i}\n" for i in range(20)]
    (repo / "app.py").write_text("".join(filler), encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    # Deliberate partial staging: the index holds hunk A only, the working
    # tree holds A + B.
    (repo / "app.py").write_text("alpha_feature()\n" + "".join(filler), encoding="utf-8")
    _git(repo, "add", "app.py")
    (repo / "app.py").write_text(
        "alpha_feature()\n" + "".join(filler) + "beta_feature()\n", encoding="utf-8"
    )
    (repo / "notes.py").write_text("notes_helper()\n", encoding="utf-8")

    # Step 4 — stage the plan, but keep a partially staged path as it is.
    plan = ["app.py", "notes.py"]
    partial = (
        set(_git(repo, "diff", "--cached", "--name-only").split())
        & set(_git(repo, "diff", "--name-only").split())
    )
    assert partial == {"app.py"}
    to_add = " ".join(p for p in plan if p not in partial)
    script = re.sub(r"^git add -- .*$", f"git add -- {to_add}", stage_block)
    assert _bash(repo, script).returncode == 0

    # Step 5 — select from the complete, now-final staged diff.
    entries = {1: "alpha_feature", 2: "beta_feature", 3: "notes_helper"}
    staged = _git(repo, "diff", "--cached")
    selected = sorted(i for i, needle in entries.items() if needle in staged)
    assert selected == [1, 3]

    # Step 6 — the skill's commit command, verbatim except the placeholders.
    script = (
        commit_block
        .replace("<type>(<scope>): <description> (v<current_version>)", "feat(app): add alpha (v1.0)")
        .replace("Changelog: 12, 15", "Changelog: " + ", ".join(map(str, selected)))
    )
    result = _bash(repo, script)
    assert result.returncode == 0, result.stderr

    committed = _git(repo, "show", "HEAD:app.py")
    assert "alpha_feature()" in committed and "beta_feature()" not in committed
    assert _git(repo, "show", "HEAD:notes.py") == "notes_helper()\n"
    assert "beta_feature()" in _git(repo, "diff")  # B stays unstaged
    trailer_ids, _ = _proven_ids(repo, "HEAD~1..HEAD")
    assert trailer_ids == {1, 3}
