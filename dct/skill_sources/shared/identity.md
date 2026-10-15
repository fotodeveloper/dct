Use truthful actor identity in `created_by` and `consumed_by`. Only the true client is mandatory. Model, session identifier and session link are included only when supplied by the host; never invent them.

A project suffix is included only from a prior successful DCT response or an explicit user-supplied slug, never a directory, worktree or task-title guess. UTC is included only from an actual provided clock, known host time context or a time tool; no approximate time. Otherwise omit unknown components entirely, including project and time: never fill them with `unknown`, `?`, `0000` or guessed values. Server-created `created_at` / `consumed_at` timestamps remain separate ledger facts.

Use `<known_actor_identity>` in calls. A client-only identity such as `Codex` or `Claude Code` is sufficient when nothing else is known. Full-format example, ONLY when all components are known: `<true client>[-<host-supplied model>] @ <verified project slug> <actual UTC time>`. The same known-component rule applies to creation and consumption.

No mandatory extra lookup or preflight is added to AUTO pickup. Preserve its one-call path; do not obtain a slug by a guessed directory mapping, call a writer to bypass a read-only schema refusal, or require a clock lookup merely to fill an optional component. A status question remains a pure read with no `consumed_by` required.

{{host.identity}}
