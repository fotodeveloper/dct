"""The release workflow (K-72).

Layer 1, the read-only PROOF layer:
- proof.py — pure proof rules over commit messages (trailer parser + strict
  column-0 scan, lost indented trailers, standard reverts, squash signals);
- plan.py  — build_plan (`dct release plan`) and commit_candidates
  (`dct clog candidates`): git reads through dct.core.gitinfo, database
  reads through dct.core; no side effects.

Layer 3, the EFFECTS layer:
- effects.py — attest / apply_release / resume_release / abandon /
  reconstruct (`dct release …`, `dct clog release`): stamp, changelog file,
  version sync, release commit, tag — each verified before the ledger
  (layer 2, dct.core.releases) records its progress.
"""

from dct.workflows.release.plan import (
    CANDIDATE_KEYS,
    PLAN_KEYS,
    SCHEMA_VERSION,
    build_plan,
    commit_candidates,
)

__all__ = ["CANDIDATE_KEYS", "PLAN_KEYS", "SCHEMA_VERSION", "build_plan", "commit_candidates"]
