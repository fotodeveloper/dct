# Measuring Codex context overhead

A DCT session can recover work state correctly without reducing token usage.
Measure those properties separately. This benchmark compares context retrieval
for one fixed task; it is not a claim about every project, model, or session.

## Controlled comparison

Use an isolated, synthetic project and SQLite database. Never run this benchmark
against production tracking data. Prepare one canonical context payload and write the exact MCP response text
bytes to the baseline file. Verify byte equality as well as parsed JSON equality.
Validate both the retrieved payload and the final answer; a cheaper wrong answer
is not an improvement.

Run fresh, persisted Codex CLI sessions with the same runtime model and reasoning
effort. The observed pilot used `gpt-6-astra` with `medium` effort. Explicitly
record both settings, CLI version, fixture hash, prompts, configuration, enabled
capabilities, and trial order. Do not substitute a different model silently.

Run all three required experimental arms:

| Arm | Context source | Purpose |
|---|---|---|
| File | One read of the exact response-text file, no DCT MCP registration | File retrieval baseline |
| MCP | One `pickup_work(consume="false")` read, only the required DCT tool enabled | DCT retrieval path |
| File with MCP registered | File read while the same DCT tool remains registered | Separate registration overhead from retrieval behavior |

Keep common instructions, corpus, model settings, and built-in tool availability
identical. In particular, do not disable shell only in the MCP arm: that changes
the advertised tool set as well as retrieval. Verify actual tool calls to enforce
each arm's assigned retrieval method. The file-with-MCP arm isolates registration
overhead while retaining the same file retrieval. It does not eliminate every
confounder. Do not call a one-tool setup the default full-catalog experience.

Isolate `HOME`, `CODEX_HOME`, and XDG configuration/data/cache/state directories.
Do not import personal `AGENTS.md`, skills, hooks, memories, or other MCP servers.
Use existing authentication only through a temporary reference; never print or
archive credentials, and remove that reference after each run. Bundled system
skills and plugin recommendations can still appear despite home isolation, so
inspect and retain the actual initial context rather than assuming it is empty.

Alternate trial order and report individual trials before summarizing paired
results. A new session is not proof of a cold provider cache. Do not label trials
"cold" or "warm" solely from their order; report their measured cached input.
Use the CLI's normal approval mechanism. A denied tool call, timeout, model
mismatch, wrong payload, or extra retrieval step invalidates the relevant trial
rather than becoming a favorable token result.

## Reproduce the benchmark

From a development checkout with the MCP dependencies installed and Codex CLI
available on `PATH`, prepare a new private directory outside the repository:

```bash
.venv/bin/python scripts/benchmark_codex.py prepare \
  --output /tmp/dct-codex-benchmark --trials 3
```

Preparation creates fixtures and performs a local MCP serialization check; it
makes no model calls. Inspect `manifest.json`, the shared payload and instructions,
and each run's configuration and prompt before executing. Three trials schedule
nine fresh model sessions, rotating the three-arm order. Use `--trials 1` for a
three-session smoke test; one observation per arm is not a robust performance
estimate. The defaults preserve `gpt-6-astra` and `medium` reasoning effort.

Live execution is explicit and consumes the signed-in account's model usage:

```bash
.venv/bin/python scripts/benchmark_codex.py run \
  --output /tmp/dct-codex-benchmark --run
```

The default authentication source is the existing Codex `auth.json`; an alternate
existing file can be specified with `--auth-path`. Its contents are never printed
or copied into the report. Do not publish the raw artifact directory: it contains
local paths and session metadata, even though the workload data is synthetic.
Existing run evidence is not overwritten; prepare a new directory for another
experiment.

Recompute analysis from retained artifacts without model calls:

```bash
.venv/bin/python scripts/benchmark_codex.py analyze \
  --output /tmp/dct-codex-benchmark
```

The root contains `manifest.json`, `versions.json`, `payload.json`, shared
instructions, and `analysis.json`. Each run retains its isolated project/home,
`prompt.txt`, `prompt-input.json`, `stdout.jsonl`, `stderr.log`, `status.json`,
`command.json`, `environment.json`, `final.txt`, and persisted Codex sessions.
Use these artifacts to distinguish a failed run from a valid expensive run.

## What to capture

Run without `--ephemeral`. Keep the CLI JSON event stream and its matching
persisted rollout. Match `thread.started.thread_id` to rollout session identity;
other files may belong to prompt diagnostics or approval-review sessions.
Preserve stderr and exit status separately.

The rollout provides these useful records:

- `session_meta`: CLI version, provider, and base instructions.
- `turn_context`: runtime model, reasoning effort, and active execution settings.
- `response_item`: recorded initial messages, tool calls/results, and answers.
- `event_msg` with `payload.type="token_count"`: cumulative `total_token_usage`
  and latest `last_token_usage`, under `payload.info`.

Usage fields include `input_tokens`, `cached_input_tokens`,
`cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`, and
`total_tokens`. Report input, cached input, uncached input (`input - cached`),
and output separately. Cached input is already included in input. Do not add
reasoning output to output unless the reporting API explicitly defines it as
additional; preserve the raw fields for inspection.

Count distinct cumulative usage observations. Repeated identical cumulative
snapshots must not be counted twice, but identical *last-usage* values can belong
to different model steps. Verify cumulative monotonicity and consistency between
step deltas and latest usage. Report missing records or counter resets explicitly.
These are model-step usage observations, not a count of raw HTTP requests:
transport retries and internal review traffic are not established by them.

`turn_context.model` identifies the runtime-selected model. It is not necessarily
a backend-resolved snapshot ID. If the latter is not returned, record it as
unavailable instead of inferring it from a model catalog.

### Prompt reconstruction is incomplete

`codex debug prompt-input PROMPT` produces a JSON list of model-input messages.
In the verified CLI 0.153.4 pilot it did **not** include the base instructions or
a tool-schema array. The actual rollout retained base instructions separately.
Even with the same environment and prompt, diagnostic and live messages differed:
the skills text was 7,774 versus 4,892 UTF-8 bytes, respectively.

Archive diagnostic output as a reconstruction, not the exact HTTP request.
Base instructions plus recorded initial messages improve observability, but do
not prove that the complete request, every schema, or every server-side addition
has been captured. Byte counts explain text size; they are not token counts.

## Verified observability pilot

A single isolated file-read task, not an A/B comparison, used CLI 0.153.4 with
runtime model `gpt-6-astra`, effort `medium`. One tool call generated two persisted
usage observations:

| Observation | Input | Cached input | Output |
|---|---:|---:|---:|
| First latest usage | 15,725 | 12,160 | 53 |
| Second latest usage | 15,843 | 15,488 | 8 |
| Final cumulative usage | 31,568 | 27,648 | 61 |

The latest-usage values sum exactly to the final cumulative values. This proves
why one user turn with one retrieval operation can report much more input than
the retrieved text: context is counted again at later model steps, even when it
hits the provider's prompt cache. It does not establish the precise overhead of
DCT, since this pilot did not compare retrieval arms.

## Earlier exploratory results

The initial [acceptance experiment](codex-acceptance.md) retained only ephemeral
CLI event summaries. Its 58,271-versus-36,340 input-token result is a valid report
of those turn totals, but cannot identify the exact cause of the difference.
Both responses parsed to identical JSON; MCP text was 1,854 UTF-8 bytes versus
1,076 for the compact file, and user prompts were 339 versus 312 bytes. Those
small text differences do not account for the entire token gap by themselves.

That experiment did not retain per-step usage, a runtime model identifier, or
the full actual prompt. It also isolated `CODEX_HOME` but not the entire home,
allowing personal skill metadata to remain a confounder. Only one DCT tool was
enabled; attributing the result to all 55 tool schemas would be unsupported.
The controlled comparison above is intended to replace those limitations, not
retroactively supply missing evidence.

## Controlled results

A controlled run on 2026-09-11 completed all nine scheduled sessions: three
repeats of each arm, with the order rotated by repeat. Every session returned the
correct answer, used exactly its assigned retrieval operation, selected
`gpt-6-astra` with `medium` effort, preserved its fixture database, and produced
two distinct main-model usage observations. Input includes cached input; uncached
input is the derived difference.

| Repeat | Arm | Input | Cached input | Uncached input | Output | Model steps |
|---:|---|---:|---:|---:|---:|---:|
| 1 | File | 23,006 | 10,752 | 12,254 | 121 | 2 |
| 1 | File with MCP registered | 24,544 | 20,992 | 3,552 | 59 | 2 |
| 1 | MCP | 24,926 | 20,992 | 3,934 | 105 | 2 |
| 2 | File | 22,944 | 19,456 | 3,488 | 59 | 2 |
| 2 | File with MCP registered | 24,544 | 11,520 | 13,024 | 59 | 2 |
| 2 | MCP | 24,989 | 20,992 | 3,997 | 168 | 2 |
| 3 | File | 23,238 | 19,456 | 3,782 | 53 | 2 |
| 3 | File with MCP registered | 24,600 | 20,992 | 3,608 | 115 | 2 |
| 3 | MCP | 24,926 | 20,992 | 3,934 | 105 | 2 |

| Arm | Median input | Median cached | Median uncached | Median output |
|---|---:|---:|---:|---:|
| File | 23,006 | 19,456 | 3,782 | 59 |
| File with MCP registered | 24,544 | 20,992 | 3,608 | 59 |
| MCP | 24,926 | 20,992 | 3,934 | 105 |

Each column is summarized independently. Consequently, median input minus
median cached input need not equal median uncached input; the per-run rows retain
the arithmetic relationship for each observation.

The main-model input median was 24,926 for MCP versus 23,006 for File: a
1,920-token increase, approximately 8.35% for this small synthetic one-tool
workload. Within repeats, the registration contrast
(`File with MCP registered - File`) had a median input difference of 1,538
tokens and a range of 1,362 to 1,600. The invocation contrast
(`MCP - File with MCP registered`) had a median of 382 and a range of 326 to 445.
These are medians of the three paired contrasts, rather than differences
calculated from the arm medians. Their sum happens to equal the 1,920-token
difference here, but that equality is not guaranteed in general.

| Repeat | Registration input difference | Invocation input difference |
|---:|---:|---:|
| 1 | +1,538 | +382 |
| 2 | +1,600 | +445 |
| 3 | +1,362 | +326 |

The CLI approval mechanism also created a separate guardian rollout for each
MCP trial. Raw rollouts retain that work, while the primary analysis selects the
task rollout by its recorded thread identity and excludes guardian usage. The
guardian observations must therefore be reported separately; the main-model
table is not a statement of complete account billing.

| Repeat | Source | Model / effort | Input | Cached | Uncached | Output | Reasoning output |
|---:|---|---|---:|---:|---:|---:|---:|
| 1 | Guardian | `codex-auto-review` / `low` | 7,191 | 4,864 | 2,327 | 149 | 93 |
| 2 | Guardian | `codex-auto-review` / `low` | 7,186 | 4,864 | 2,322 | 213 | 152 |
| 3 | Guardian | `codex-auto-review` / `low` | 7,190 | 4,864 | 2,326 | 102 | 46 |

No price conversion is made because the evidence records model-level usage, not
a complete billable-request ledger or provider pricing decision.

The run used Codex CLI 0.153.4, Python 3.14.0, dctracker 1.5.0, MCP 2.2.0,
and SQLAlchemy 2.0.52. Its exact synthetic MCP payload was 3,840 UTF-8 bytes
with SHA-256
`635a41bfbf2d731a0a4cab0e39d47a3393c3e1e3fd5067b065fbc4b0cab75ca0`.
Published evidence intentionally omits private local paths and session UUIDs.
Raw artifacts remain local and must not be committed.

Cache behavior varied substantially between repeats, so trial order does not
support cold/warm labels. Prompt reconstruction still does not expose the raw
HTTP requests. This controlled result cannot retroactively explain the earlier
21,931-input-token gap, and its one-tool registration arm says nothing about the
overhead of a default full 55-tool catalog.
