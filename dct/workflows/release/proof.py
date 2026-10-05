"""The changelog proof rules — pure functions over commit messages (K-18,
K-66, K-68, K-69, K-70). The ONE implementation: `/commit` writes what these
rules read, `dct release plan` and `dct clog candidates` evaluate them, and
docs/assistant-fallbacks.md describes them (it points here).

Proof of an entry id in ONE commit is the union of:
  1. git's trailer parser — the commit's `%(trailers:key=Changelog,
     valueonly,separator=%x2C)` value (TRAILER_FORMAT), split on commas;
     only all-digit parts count;
  2. a strict full-body scan — lines matching CHANGELOG_LINE_RE exactly,
     in column 0. A quoted (`> Changelog: 12`), bulleted (`- Changelog: 12`),
     indented (`    Changelog: 12`) or otherwise formatted line never counts —
     also not in an example inside a message (K-66: quote examples).

Signals that are NOT proof (reported, never stamped):
  - lost trailers: indented `Changelog:` lines (LOST_LINE_RE) — what a local
    `git merge --squash` leaves (git indents every original message by four
    spaces) — feed the squash attestation proposal (K-69);
  - squash look (squash_signals): a hosted squash subject ending `(#N)`
    (`pr_suffix`), git's local squash message (`local_squash`), indented
    trailers (`indented_trailers`). Heuristic and documented as such: a
    normal commit whose subject ends `(#12)` (an issue reference) looks the
    same — the result is a PROPOSAL only.

Reverts (K-68): a commit whose subject starts `Revert "` or `Reapply "`
(git >= 2.43 names a revert of a revert "Reapply") AND that carries git's
standard column-0 line `This reverts commit <sha>.` (or `…, reversing` for
a merge) reverts <sha>. Both conditions are required, so prose that merely
mentions a revert is not one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# The strict column-0 line — the format /commit writes (ids ascending,
# comma + space). Pinned against docs/assistant-fallbacks.md by a test.
CHANGELOG_LINE_PATTERN = r"^Changelog: [0-9]+(, [0-9]+)*$"
CHANGELOG_LINE_RE = re.compile(CHANGELOG_LINE_PATTERN, re.ASCII)
# git's trailer parser, one value per commit, several trailers joined by ','.
TRAILER_FORMAT = "%(trailers:key=Changelog,valueonly,separator=%x2C)"
# An indented Changelog line: proof lost by indentation (local merge --squash).
LOST_LINE_RE = re.compile(r"^[ \t]+Changelog: ([0-9]+(?:, [0-9]+)*)[ \t]*$", re.ASCII)

_REVERT_SUBJECT_RE = re.compile(r'^(?:Revert|Reapply) "', re.ASCII)
_REVERTS_LINE_RE = re.compile(
    r"^This reverts commit ([0-9a-fA-F]{7,64})(?:\.|, reversing\b)", re.MULTILINE | re.ASCII
)
_MERGE_PR_RE = re.compile(r"^Merge pull request #([0-9]+) ", re.ASCII)
_SUFFIX_PR_RE = re.compile(r"\(#([0-9]+)\)\s*$", re.ASCII)
LOCAL_SQUASH_MARKER = "Squashed commit of the following:"


@dataclass(frozen=True)
class Commit:
    """One commit as the proof layer sees it: full SHA, parent SHAs, the
    raw message (%B) and git's Changelog trailer value."""

    sha: str
    parents: tuple[str, ...]
    message: str
    trailer_value: str = ""

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0].strip()

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


def ids_from_trailer_value(value: str) -> set[int]:
    """Entry ids of git's `valueonly` trailer output (`12, 15,3`)."""
    parts = (part.strip() for part in (value or "").replace("\n", ",").split(","))
    return {int(part) for part in parts if part.isascii() and part.isdigit()}


def _split_ids(text: str) -> set[int]:
    return {int(part) for part in text.split(", ")}


def ids_from_body(message: str) -> set[int]:
    """Entry ids of the strict column-0 lines (CHANGELOG_LINE_RE) of `message`."""
    ids: set[int] = set()
    for line in (message or "").splitlines():
        line = line.rstrip("\r")
        if CHANGELOG_LINE_RE.match(line):
            ids |= _split_ids(line[len("Changelog: "):])
    return ids


def proven_ids(commit: Commit) -> frozenset[int]:
    """The ids `commit` proves: trailer parser ∪ strict body scan."""
    return frozenset(ids_from_trailer_value(commit.trailer_value) | ids_from_body(commit.message))


def lost_trailer_ids(message: str) -> set[int]:
    """Ids on INDENTED `Changelog:` lines — never proof (see module doc)."""
    ids: set[int] = set()
    for line in (message or "").splitlines():
        match = LOST_LINE_RE.match(line.rstrip("\r"))
        if match:
            ids |= _split_ids(match.group(1))
    return ids


def revert_target(message: str) -> str | None:
    """The (possibly abbreviated, lowercased) SHA a standard git revert
    message reverts, or None when `message` is not one."""
    subject = (message or "").split("\n", 1)[0]
    if not _REVERT_SUBJECT_RE.match(subject):
        return None
    match = _REVERTS_LINE_RE.search(message)
    return match.group(1).lower() if match else None


def merge_pr(subject: str) -> int | None:
    """N of a hosted merge subject `Merge pull request #N from …`."""
    match = _MERGE_PR_RE.match(subject or "")
    return int(match.group(1)) if match else None


def suffix_pr(subject: str) -> int | None:
    """N of a hosted squash / rebase subject ending `(#N)`."""
    match = _SUFFIX_PR_RE.search(subject or "")
    return int(match.group(1)) if match else None


def squash_signals(commit: Commit) -> list[str]:
    """Why a NON-merge commit looks like a squash ([] when it does not):
    'pr_suffix', 'local_squash', 'indented_trailers' (see module doc)."""
    if commit.is_merge:
        return []
    signals = []
    if suffix_pr(commit.subject) is not None:
        signals.append("pr_suffix")
    if commit.message.startswith(LOCAL_SQUASH_MARKER):
        signals.append("local_squash")
    if lost_trailer_ids(commit.message):
        signals.append("indented_trailers")
    return signals


# ── reverts (K-68) ───────────────────────────────────────────────────────────


@dataclass
class RevertAnalysis:
    """Which commits of a range are neutralized by an effective revert.

    `reverts`: one report per revert commit — {sha, target (as written),
    target_sha (resolved full SHA or None), status: 'in_range' |
    'outside_range' | 'ambiguous', effective (False when itself reverted),
    affected (commits it neutralizes, merge reverts include the commits the
    merge introduced)}.
    `neutralized`: commit SHA → the effective revert SHAs neutralizing it.
    """

    reverts: list[dict] = field(default_factory=list)
    neutralized: dict[str, list[str]] = field(default_factory=dict)


def analyze_reverts(
    commits: Iterable[Commit], introduced: Mapping[str, Iterable[str]] | None = None
) -> RevertAnalysis:
    """Resolve every standard revert in `commits` (the range) — deterministic.

    - The target must be a commit OF THE RANGE: a full SHA, or an
      abbreviation matching exactly one range commit. Otherwise the revert
      is reported ('outside_range' / 'ambiguous') and changes nothing.
    - A revert of a merge commit M also neutralizes the commits M brought in
      (`introduced[M]`, computed by the caller from git).
    - Chains: a commit is neutralized when at least one EFFECTIVE revert
      targets it, and a revert is effective when it is not neutralized
      itself — so a revert of a revert restores the original proof, a third
      revert removes it again, and so on.
    """
    commits = list(commits)
    shas = [c.sha for c in commits]
    by_sha = {c.sha: c for c in commits}
    introduced = introduced or {}
    reports: list[dict] = []
    reverters: dict[str, list[str]] = {}
    for commit in commits:
        target = revert_target(commit.message)
        if target is None:
            continue
        matches = [sha for sha in shas if sha.startswith(target)]
        report = {"sha": commit.sha, "target": target, "target_sha": None,
                  "status": "outside_range", "effective": True, "affected": []}
        if len(matches) > 1:
            report["status"] = "ambiguous"
        elif matches:
            resolved = matches[0]
            affected = [resolved]
            if by_sha[resolved].is_merge:
                affected += [s for s in introduced.get(resolved, ()) if s in by_sha and s != resolved]
            report.update(target_sha=resolved, status="in_range", affected=sorted(set(affected)))
            for sha in report["affected"]:
                reverters.setdefault(sha, []).append(commit.sha)
        reports.append(report)

    memo: dict[str, bool] = {}

    def neutralized(sha: str, visiting: frozenset[str] = frozenset()) -> bool:
        if sha in memo:
            return memo[sha]
        if sha in visiting:  # impossible in a DAG; never loop
            return False
        result = any(not neutralized(r, visiting | {sha}) for r in reverters.get(sha, ()))
        memo[sha] = result
        return result

    analysis = RevertAnalysis(reports)
    for report in reports:
        report["effective"] = not neutralized(report["sha"])
    for sha, revs in reverters.items():
        if neutralized(sha):
            analysis.neutralized[sha] = sorted(r for r in revs if not neutralized(r))
    return analysis


# ── proof of a range ─────────────────────────────────────────────────────────


@dataclass
class RangeProof:
    """Per entry id: `proving` (commits that still prove it, oldest first),
    `reverted` (proving commits neutralized → the effective revert SHAs)."""

    proving: dict[int, list[str]] = field(default_factory=dict)
    reverted: dict[int, dict[str, list[str]]] = field(default_factory=dict)
    analysis: RevertAnalysis = field(default_factory=RevertAnalysis)

    def proven(self) -> set[int]:
        return {i for i, shas in self.proving.items() if shas}


def range_proof(
    commits: Iterable[Commit], introduced: Mapping[str, Iterable[str]] | None = None
) -> RangeProof:
    """Evaluate the proof of every id cited in `commits` (oldest first)."""
    commits = list(commits)
    analysis = analyze_reverts(commits, introduced)
    out = RangeProof(analysis=analysis)
    for commit in commits:
        for entry_id in sorted(proven_ids(commit)):
            out.proving.setdefault(entry_id, [])
            if commit.sha in analysis.neutralized:
                out.reverted.setdefault(entry_id, {})[commit.sha] = analysis.neutralized[commit.sha]
            else:
                out.proving[entry_id].append(commit.sha)
    return out
