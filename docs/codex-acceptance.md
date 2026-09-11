# Live acceptance evidence (2026-09-11)

Actual signed-in CLI clients ran with default model selection and isolated DCT
configuration, SQLite database and project. No source transcript was supplied to
the resumer. No production tracking state was read or modified.

Claude created a handoff via `handoff_work` after reading the current snapshot.
A fresh Codex resumed via `pickup_work(consume="false")`, identified the precise
item/checkpoint and marker from persisted checkpoint text, and consumed that
handoff. Database verification confirmed origin client/session/source=mcp and
recorded consumption. The source prompt did not supply the marker to Codex.

A second targeted Codex run read the installed pickup skill and managed AGENTS
instructions. It read first, then consumed an explicitly authorized fixture
handoff using `include="none"` and identity `codex @ <project> <UTC timestamp>`.
Item and checkpoint statuses remained unchanged.

The initial sandboxed Claude authentication attempt failed; normal keychain and
network access resolved it. Default noninteractive Codex approval policy `never`
blocked MCP reads; `--approve-for-me` permitted the authorized isolated calls.
The first harness prompt prohibited shell reads, so that attempt did not exercise
the skill file; the targeted follow-up explicitly allowed those reads and passed.
No blanket permission-bypass flags were used.

## Bounded context microbenchmark

Two fresh Codex CLI runs received the same identification task against the same
fixture. One exposed only `pickup_work` and made one read with
`include="sprint,handoff"`; the other had no DCT MCP server and read equivalent
compact JSON from one local context file. Both returned the same correct item and
pending checkpoint. No mutations occurred. This is a one-tool context packaging
microbenchmark, not the default 55-tool DCT experience.

| Variant | Input tokens | Cached input | Uncached input | Output tokens | Calls |
|---|---:|---:|---:|---:|---:|
| One DCT pickup tool | 58,271 | 40,832 | 17,439 | 137 | 1 MCP |
| Equivalent compact context file | 36,340 | 29,312 | 7,028 | 95 | 1 shell read |

These observed results do not demonstrate token savings: the DCT run used more
input tokens for this small fixture. Prompt/schema overhead and unequal cache
hits are material confounders. Input includes cached tokens; do not add the cache
column to input. No universal cost, latency, or token-saving claim follows.
The Codex JSON event stream did not return a model identifier; none is inferred
from a model catalog. Models were not overridden.

The earlier full-catalog live resume used 177,305 input tokens, of which 153,344
were cached, and 556 output tokens. The targeted installed-skill run with one
exposed tool used 84,267 input (72,576 cached) and 417 output. These are different
workloads, not an A/B comparison. Model-level token usage is distinct from the
separate deterministic wire measurement in pytest (serialized JSON bytes).
