# selfgraph

A minimal ActiveGraph agent that **ingests its own repo and the
ActiveGraph runtime**, **builds a capability graph** from what it
finds, and **uses that graph to propose safe, graph-native
self-configuration patches** for new goals. Patches are validated by
guardrails and applied in a forked sandbox before they can be
promoted to the live graph.

The agent works with what it has discovered. If a primitive (a
Behavior, an EventType, an ObjectType) isn't in the graph, it doesn't
appear in the proposal — there are no hidden defaults driving the
shape of an output.

## Setup

```bash
pip install -r requirements.txt        # activegraph + (optional) anthropic
```

No API key is required. If `ANTHROPIC_API_KEY` is set, an LLM pass
augments the deterministic extraction with extra Capability /
Constraint nodes pulled from markdown — additive only; the
deterministic floor is the contract.

## Usage

```bash
python demo.py                          # the scripted three-step demo

# or via the CLI (state persists to ./.selfgraph/graph.db):
python -m selfgraph build .             # ingest repo + activegraph package
python -m selfgraph ask "what can you do?"
python -m selfgraph ask "how would you implement forking?"
python -m selfgraph propose "track inbound emails from a vendor"
python -m selfgraph promote PatchProposal#NNN
python -m selfgraph chat                # interactive REPL
```

`build` rebuilds the graph from scratch. `ask` and `propose` reuse
the persisted graph. `promote` re-runs guardrail validation against
the current persisted state before applying — a stale `validated`
status is never enough on its own.

## What the demo shows

```
USER: Read this repo and build your capability graph.
  → ingest every .py/.md file + introspect the `activegraph`
    package as a synthetic `module://...` corpus, then extract
    Capability / API / Behavior / ObjectType / Constraint /
    AuthorityRule nodes and wire them with API_CREATES,
    BEHAVIOR_SUBSCRIBES_TO, CAPABILITY_REQUIRES_APPROVAL, ...

USER: What can you do?
  → answered from `Graph.objects(type="Capability")`, not from
    prompt context. Each line cites its node id.

USER: Configure yourself to track project updates.
  → propose_patch_for inspects extracted Behaviors / EventTypes /
    ObjectTypes and composes a PatchProposal whose changes are
    grounded in those nodes (a state bucket, a Task, an atom +
    snapshot ObjectType pair, a scoped Policy, evaluation criteria).
    Guardrails validate; sandbox forks the run, applies, diffs,
    and waits for user approval before promoting.
```

## Architecture

```
selfgraph/
├── ingest.py      walk repo + introspect modules → File / Chunk objects
│                  (dedupes on path + sha256 so re-ingesting is a no-op
│                  when content is unchanged)
├── extract.py     regex / signature pass + seed anchors → Capability /
│                  API / Behavior / ObjectType / Constraint /
│                  AuthorityRule (+ optional LLM augment pass).
│                  A capability *sketch*, not authoritative introspection.
├── query.py       graph-cited answers via keyword overlap over node
│                  data. Not semantic understanding.
├── propose.py     compose a PatchProposal from extracted graph state.
├── guardrails.py  demo-grade validation against allowed v1 change kinds.
├── sandbox.py     fork / apply / diff / (optional) promote.
└── cli.py         build | ask | propose | promote | chat | demo
```

### Node types

`File`, `Chunk`, `Capability`, `API`, `Behavior`, `EventType`,
`ObjectType`, `Example`, `Constraint`, `AuthorityRule`,
`PatchProposal`, `Evaluation`, `Policy`, `BehaviorBinding`,
`Task`, plus whatever new `ObjectType`s the agent proposes.

### Relations

`FILE_HAS_CHUNK`, `API_CREATES`, `API_READS`, `API_WRITES`,
`BEHAVIOR_SUBSCRIBES_TO`, `EXAMPLE_DEMONSTRATES`,
`CAPABILITY_REQUIRES_APPROVAL`, `PATCH_PROPOSES`, `PATCH_MODIFIES`,
`ROLLS_UP_INTO`, `GROUNDED_IN`.

### Guardrails (demo-grade)

| Allowed change kinds | Rejected |
| --- | --- |
| `add_object`, `add_relation`, `add_policy`, `add_state_bucket`, `add_task`, `add_evaluation`, `bind_behavior` | shell / `subprocess` / `os.system` / `__import__` / `exec(` / `eval(` / network calls / file writes, mutations of `AuthorityRule` or `Capability` without explicit approval, `bind_behavior` for behaviors not already in the graph, policies that declare `can_approve` |

The token list is substring-based — easy to read, easy to extend,
and easy to evade. False-positives on docs that *mention* banned
tokens are possible; obfuscated payloads can slip through. Treat this
as a sketch of what the policy boundary looks like, not a hardened
sandbox. Production deployments should replace it with an AST scan
plus per-type capability checks.

The guardrail also does not yet distinguish *domain* ObjectTypes
(safe — e.g. `ProjectUpdate`) from *runtime ontology* ObjectTypes
(`Capability`, `AuthorityRule`) beyond a hand-maintained `_PROTECTED_TYPES`
list. A real version of this would carry that distinction on the
ObjectType node itself.

`tests/test_smoke.py` exercises the accept path and several reject
paths (banned-token injection, unknown-behavior binding, protected
type addition, disallowed change kind), plus the promote lifecycle.

### Fork / test / promote

ActiveGraph's `Runtime.fork(at_event=...)` requires a SQLite-backed
runtime. The demo uses `persist_to=...` so real forks work. The
in-memory path falls back to a structural replay via the documented
projector entry point — see the single comment in `sandbox.py` for
where this lives if a public equivalent ships later.

## Limitations (v1, on purpose)

- **No arbitrary code in patches.** `bind_behavior` references only
  behaviors already discovered by the extractor. The agent cannot
  author a new Python function from a goal.
- **No external side effects.** Patches mutate the graph and nothing
  else — no HTTP, no shell, no file writes outside the SQLite store.
- **No mutation of `AuthorityRule` / `Capability` nodes without
  explicit approval.**
- **Capability extraction is a sketch.** Regex/signature heuristics
  miss some `@behavior` decorators, mis-classify some API surface,
  and don't trace dynamic dispatch. The graph is good enough to
  ground proposals; treat it as a starting point, not a manifest.
- **Question answering is keyword-overlap retrieval over node data**,
  not semantic understanding.
- **Deterministic extractor is the contract; the LLM pass is
  additive.** Running without an API key produces the same graph
  shape on every machine.
- **No multi-step planning.** The proposer composes one PatchProposal
  per goal. It does not chain proposals or run them as a campaign.
- **No UI, no auth, no remote store.** SQLite event log only.

## Files

- `demo.py` — the scripted 3-step demo.
- `tests/test_smoke.py` — accept + reject paths + promote lifecycle.
- `selfgraph/*.py` — the agent.
- `.selfgraph/graph.db` — created on first `build`; safe to delete.
