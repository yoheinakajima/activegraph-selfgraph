# selfgraph

A minimal ActiveGraph agent that **ingests its own runtime/docs**,
**builds a capability graph**, and **proposes safe self-configuration
patches** for goals it has never seen before.

The whole point: the agent does *not* hardcode a workflow (no OODA,
no PDCA template). It reads what ActiveGraph and its own source can
do, represents that as graph nodes, and assembles a proposal from
those primitives. If the graph doesn't have a node for something, the
agent says so instead of inventing one.

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
the persisted graph.

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

USER: Configure yourself to track project updates using whatever
       pattern makes sense.
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
├── extract.py     regex / signature pass + seed anchors → Capability /
│                  API / Behavior / ObjectType / Constraint /
│                  AuthorityRule (+ optional LLM augment pass)
├── query.py       graph-cited answers to "what can you do?" etc.
├── propose.py     compose a PatchProposal from extracted graph state
├── guardrails.py  validate proposals against allowed v1 change kinds
├── sandbox.py     fork / apply / diff / (optional) promote
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

### Guardrails

| Allowed change kinds | Rejected |
| --- | --- |
| `add_object`, `add_relation`, `add_policy`, `add_state_bucket`, `add_task`, `add_evaluation`, `bind_behavior` | shell / `subprocess` / `os.system` / `__import__` / `exec(` / `eval(` / network calls / file writes, mutations of `AuthorityRule` or `Capability` without explicit approval, `bind_behavior` for behaviors not already in the graph, policies that declare `can_approve` |

`tests/test_smoke.py` exercises both the accept and reject paths.

### Fork / test / promote

ActiveGraph's `Runtime.fork(at_event=...)` requires a SQLite-backed
runtime. The demo uses `persist_to=...` so real forks work. The CLI
falls back to a structural in-memory replay when no SQLite store is
attached — the user-visible flow (apply → diff → promote) is the same.

## Limitations (v1, on purpose)

- **No arbitrary code in patches.** `bind_behavior` references only
  behaviors already discovered by the extractor. The agent cannot
  author a new Python function from a goal.
- **No external side effects.** Patches mutate the graph and nothing
  else — no HTTP, no shell, no file writes outside the SQLite store.
- **No mutation of `AuthorityRule` nodes without explicit approval.**
- **Deterministic extractor is the contract; the LLM pass is
  additive.** Running without an API key produces the same graph
  shape on every machine.
- **No multi-step planning.** The proposer composes one PatchProposal
  per goal. It does not chain proposals or run them as a campaign.
- **No UI, no auth, no remote store.** SQLite event log only.

## Files

- `demo.py` — the scripted 3-step demo.
- `tests/test_smoke.py` — accept + reject paths.
- `selfgraph/*.py` — the agent.
- `.selfgraph/graph.db` — created on first `build`; safe to delete.
