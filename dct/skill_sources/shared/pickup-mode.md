What the user said decides whether a handoff is consumed:

- **An instruction to continue** — on an unambiguous "continue" / "pickup" /
  "resume" (or `{{host.skill-prefix}}pickup` itself) the call consumes the single matching
  handoff (AUTO mode). All three commands have the same accepted consume behavior.
- **A status question** — "where were we", "what's next", "what's the state"
  → `pickup_work(project="", consume="false")`, a PURE READ, no `consumed_by`
  needed: report the state and say that a handoff is waiting and that
  "continue" picks it up. A request to only look ("show the handoff but keep
  it", an audit) is the same read.
- A word the user merely quotes or mentions ("what does "continue" do?") is
  not a command; only a request addressed to you counts.

