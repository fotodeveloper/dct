# Widget Pipeline v2 — Design Spec

A fictional but realistically sized design document used by the ingest
end-to-end tests. Prose only — a spec deliberately carries no checkboxes,
so plan progress must resolve to 0/0.

## 1. Problem

The v1 widget pipeline couples parsing, enrichment and storage in one pass.
A failure in enrichment loses the parse result, and re-runs are O(full corpus).

## 2. Goals

Incremental re-processing, at-least-once delivery into the store, and a
queryable audit trail of every pipeline decision.

## 3. Non-goals

Real-time streaming, multi-tenant isolation and cross-region replication are
explicitly out of scope for v2.

## 4. Glossary

Widget: the unit of user content. Enrichment: derived metadata attached to a
widget. Batch: a set of widgets processed under one manifest.

## 5. Architecture Overview

Three stages connected by durable queues: parse → enrich → store. Each stage
is idempotent and keyed by content hash plus manifest id.

## 6. Data Model

Tables: widgets, manifests, enrichments, decisions. All tables carry
created_at and deleted_at (soft deletes, ledger pattern).

## 7. Identity and Anchors

Every widget gets a ULID at first sight; downstream stages reference the ULID,
never positional indexes, so reordering a batch cannot corrupt references.

## 8. Parse Stage

A pure function from raw bytes to a structural tree. No I/O besides the input;
all warnings are collected, never raised.

## 9. Enrichment Stage

Rule-based enrichers run first (cheap, deterministic); an optional model-based
enricher handles the uncertain remainder and may be disabled by config.

## 10. Store Stage

Upserts keyed by ULID. A second run over an unchanged manifest is a no-op by
source-hash short-circuit.

## 11. Failure Handling

Each stage writes a decision row before side effects. Crash recovery replays
from the last decision, never from the beginning.

## 12. Configuration Surface

One TOML file: queue paths, enricher toggles, model name, timeouts. An
environment variable overrides the store URL for tests.

## 13. CLI Surface

`widgetd ingest <manifest>`, `widgetd rescan <path>`, `widgetd status`.
All commands print machine-readable summaries with --json.

## 14. Testing Strategy

Unit tests per stage with in-memory queues; one end-to-end test ingests a
bundled fixture manifest twice and asserts the second run is a no-op.

## 15. Migration From v1

A one-time backfill maps v1 rows to ULIDs, preserving timestamps. The backfill
is idempotent and resumable.

## 16. Open Questions

Whether decision rows need retention limits, and whether the model-based
enricher should batch requests.
