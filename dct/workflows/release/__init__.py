"""The release workflow (K-72) — layer 1, the read-only PROOF layer.

- proof.py — pure proof rules over commit messages (trailer parser + strict
  column-0 scan, lost indented trailers, standard reverts, squash signals);
- plan.py  — build_plan (`dct release plan`) and commit_candidates
  (`dct clog candidates`): git reads through dct.core.gitinfo, database
  reads through dct.core; no side effects.

The effects layer (stamp, files, commit, tag, attest, resume, abandon,
reconstruct) builds on this plan; the ledger is dct.core.releases.
"""

from dct.workflows.release.plan import (
    CANDIDATE_KEYS,
    PLAN_KEYS,
    SCHEMA_VERSION,
    build_plan,
    commit_candidates,
)

__all__ = ["CANDIDATE_KEYS", "PLAN_KEYS", "SCHEMA_VERSION", "build_plan", "commit_candidates"]
