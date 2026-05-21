"""Self-query: answer questions about capabilities by reading the graph,
not by re-prompting an LLM with the docs.

This is keyword-overlap retrieval over the capability graph — not
semantic understanding. Every answer cites the node ids it came from;
if no node matches, the agent says so rather than inventing one. For
richer matching, swap ``_explain_implementation`` for an LLM-backed
retriever — the graph is the same either way.
"""

from __future__ import annotations

from typing import Optional

from activegraph import Graph


def summarize_capabilities(graph: Graph) -> str:
    """One-line summary per Capability, grounded in the graph."""
    caps = graph.objects(type="Capability")
    if not caps:
        return "(no capabilities ingested yet — run ingest + extract first)"
    lines = ["I can do the following (each grounded in the capability graph):"]
    for c in sorted(caps, key=lambda o: o.data.get("name", "")):
        name = c.data.get("name", "?")
        desc = c.data.get("description", "")
        # APIs this capability uses
        rels = graph.relations(source=c.id)
        api_count = sum(1 for r in rels if r.type.startswith("API_"))
        lines.append(f"  - {name}: {desc}  (apis_wired={api_count})")
    return "\n".join(lines)


def answer_question(graph: Graph, question: str) -> str:
    """Route the question to the right graph reader."""
    q = question.strip().lower()
    if not q:
        return "(empty question)"
    if q.startswith("what can you do") or "capabilities" in q:
        return summarize_capabilities(graph)
    if q.startswith("how would you implement") or q.startswith("how would you "):
        topic = question.split(maxsplit=4)[-1] if len(question.split()) > 4 else question
        return _explain_implementation(graph, topic)
    if q.startswith("can you configure yourself") or "configure yourself" in q:
        return (
            "Yes — call `propose_patch_for(graph, your_goal)` to generate a "
            "PatchProposal grounded in extracted capabilities, then "
            "`sandbox_apply` to test it in a fork before promoting."
        )
    if q.startswith("list ") or q.startswith("show "):
        return _list_by_type(graph, question)
    return _grep_graph(graph, question)


def _explain_implementation(graph: Graph, topic: str) -> str:
    """Find capabilities + APIs whose names overlap the topic, plus any
    constraints that mention it. Output is graph-cited."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in topic)
    tokens = [t for t in cleaned.lower().split() if len(t) > 2]
    if not tokens:
        return "I need a more specific topic to ground the implementation in."
    # Crude stemming: drop common suffixes so "forking" matches "fork".
    stems = {t.rstrip("s").rstrip("ing").rstrip("ed") for t in tokens} | set(tokens)
    stems = {s for s in stems if len(s) > 2}

    def hits(o):
        text = " ".join(str(v) for v in o.data.values()).lower()
        return sum(1 for s in stems if s in text)

    relevant_caps = sorted(graph.objects(type="Capability"),
                           key=hits, reverse=True)[:3]
    relevant_apis = sorted(graph.objects(type="API"),
                           key=hits, reverse=True)[:5]
    relevant_behaviors = sorted(graph.objects(type="Behavior"),
                                key=hits, reverse=True)[:5]
    constraints = [c for c in graph.objects(type="Constraint") if hits(c)]

    lines = [f"Implementation sketch for: {topic}"]
    if relevant_caps and hits(relevant_caps[0]):
        lines.append("  capabilities:")
        for c in relevant_caps:
            if hits(c):
                lines.append(f"    - {c.data.get('name')}  ({c.id})")
    if relevant_apis and hits(relevant_apis[0]):
        lines.append("  apis:")
        for a in relevant_apis:
            if hits(a):
                sig = a.data.get("signature", "")
                lines.append(f"    - {a.data.get('name')}{sig}  ({a.id})")
    if relevant_behaviors and hits(relevant_behaviors[0]):
        lines.append("  behaviors:")
        for b in relevant_behaviors:
            if hits(b):
                lines.append(
                    f"    - {b.data.get('name')} on={b.data.get('on')}  ({b.id})"
                )
    if constraints:
        lines.append("  constraints that apply:")
        for c in constraints[:5]:
            lines.append(f"    - {c.data.get('text')[:140]}")
    if len(lines) == 1:
        return (
            f"I don't have graph nodes overlapping '{topic}'. Ingest more "
            f"docs or rephrase using terms that appear in extracted APIs."
        )
    return "\n".join(lines)


def _list_by_type(graph: Graph, question: str) -> str:
    q = question.lower()
    candidate_types = [
        "Capability", "API", "Behavior", "ObjectType", "RelationType",
        "Example", "Constraint", "AuthorityRule", "PatchProposal",
        "Evaluation", "File", "EventType",
    ]
    requested = [t for t in candidate_types if t.lower() in q]
    if not requested:
        return "Specify a node type, e.g. 'list constraints'."
    out: list[str] = []
    for t in requested:
        objs = graph.objects(type=t)
        out.append(f"{t} ({len(objs)}):")
        for o in objs[:25]:
            label = o.data.get("name") or o.data.get("text", "")[:80] \
                    or o.data.get("path", "") or o.id
            out.append(f"  - {label}")
        if len(objs) > 25:
            out.append(f"  ... +{len(objs) - 25} more")
    return "\n".join(out)


def trace_grounding(graph: Graph, proposal_id: str) -> str:
    """Walk PATCH_PROPOSES / PATCH_MODIFIES / GROUNDED_IN edges from a
    PatchProposal and render the citation chain back to the ingested
    File / module:// pseudo-file that produced each extracted node.

    The point of this reader is to make the agent's grounding visible:
    every change in a proposal either cites an extracted node (with a
    real source path) or carries a ``source=selfgraph-fallback-scaffold``
    tag, in which case the trace says so explicitly.
    """
    proposal = graph.get_object(proposal_id)
    if proposal is None or proposal.type != "PatchProposal":
        return f"(not a PatchProposal: {proposal_id})"
    lines: list[str] = [
        f"Grounding citations for {proposal_id}  "
        f"(used_fallback_scaffold={proposal.data.get('used_fallback_scaffold')})",
    ]

    # PATCH_PROPOSES — capabilities the proposal uses.
    proposes = [r for r in graph.relations(source=proposal_id)
                if r.type == "PATCH_PROPOSES"]
    if proposes:
        lines.append("  PATCH_PROPOSES (capabilities this proposal exercises):")
        for r in proposes:
            cap = graph.get_object(r.target)
            if cap:
                lines.append(
                    f"    → {cap.type}:{cap.data.get('name')}  ({cap.id})  "
                    f"{_source_citation(graph, cap)}"
                )

    # PATCH_MODIFIES — extracted ObjectTypes the proposal grounds itself in.
    modifies = [r for r in graph.relations(source=proposal_id)
                if r.type == "PATCH_MODIFIES"]
    if modifies:
        lines.append("  PATCH_MODIFIES (extracted ObjectTypes the proposal grounds in):")
        for r in modifies:
            ot = graph.get_object(r.target)
            if ot:
                lines.append(
                    f"    → {ot.type}:{ot.data.get('name')}  ({ot.id})  "
                    f"{_source_citation(graph, ot)}"
                )

    # Per-change citations — walk each proposed change and report whether
    # it's grounded (cites an extracted node) or scaffold (a default).
    lines.append("  per-change provenance:")
    for i, change in enumerate(proposal.data.get("changes", [])):
        lines.append(f"    [{i}] {change.get('kind'):14}  "
                     f"{_change_summary(change)}")
        for cite in _cite_change(graph, change):
            lines.append(f"          {cite}")

    return "\n".join(lines)


def _source_citation(graph: Graph, obj) -> str:
    """Cite an extracted node back to its ingest source. Seed nodes
    (Capability/AuthorityRule planted by extract._seed) carry no source
    and are labelled '(seed)'."""
    data = obj.data
    if "source_file_path" in data:
        return f"[extracted from: {data['source_file_path']}]"
    if data.get("source") == "selfgraph-fallback-scaffold":
        return "[scaffold: built-in fallback shape, not extracted]"
    if data.get("source") == "llm":
        return "[extracted by: optional LLM augmentation pass]"
    return "[seed: planted by extract._seed, not extracted from a file]"


def _change_summary(change: dict) -> str:
    if change.get("kind") in ("add_object", "add_state_bucket",
                              "add_task", "add_evaluation"):
        d = change.get("data", {})
        label = (d.get("name") or d.get("goal", "")[:40]
                 or d.get("criterion", "")[:40])
        return f"{change.get('type', '?'):14}  {label}"
    if change.get("kind") == "add_relation":
        return (f"{change.get('rel_type', '?'):20}  "
                f"{change.get('from_type')}:{change.get('from_name')} → "
                f"{change.get('to_type')}:{change.get('to_name')}")
    if change.get("kind") == "add_policy":
        return f"Policy           scope={change.get('policy', {}).get('scope')}"
    if change.get("kind") == "bind_behavior":
        return (f"{change.get('behavior'):20}  on={change.get('on_event_type')}")
    return str(change)


def _cite_change(graph: Graph, change: dict) -> list[str]:
    """Return citation lines for a single proposed change."""
    kind = change.get("kind")
    # Scaffold ObjectTypes — print the honest "not extracted" line.
    if kind == "add_object":
        data = change.get("data", {})
        if data.get("source") == "selfgraph-fallback-scaffold":
            return ["↳ source: built-in atom/snapshot scaffold "
                    "(NOT extracted from any ingested file)"]
        return ["↳ source: domain object the proposal introduces "
                "(no extraction citation — this is the new state)"]
    if kind == "add_relation":
        # GROUNDED_IN edges are the actual citation arrows — find the
        # target ObjectType and cite its extraction source.
        if change.get("rel_type") == "GROUNDED_IN":
            tgt_name = change.get("to_name")
            for ot in graph.objects(type="ObjectType"):
                if ot.data.get("name") == tgt_name:
                    return [f"↳ GROUNDED_IN target {tgt_name}  "
                            f"{_source_citation(graph, ot)}"]
            return [f"↳ GROUNDED_IN target {tgt_name}  (target not found)"]
        return ["↳ structural relation between newly proposed types"]
    if kind == "bind_behavior":
        beh_name = change.get("behavior")
        for b in graph.objects(type="Behavior"):
            if b.data.get("name") == beh_name:
                return [f"↳ binds extracted Behavior {beh_name}  "
                        f"{_source_citation(graph, b)}"]
        return [f"↳ binds {beh_name} (unknown to the graph — guardrail "
                f"will reject)"]
    if kind == "add_task":
        return ["↳ task object: encodes the user's goal in graph form"]
    if kind == "add_evaluation":
        return ["↳ evaluation criterion (selfgraph-authored)"]
    if kind == "add_policy":
        return ["↳ policy whose can_create is derived from the "
                "ObjectTypes this proposal introduces"]
    return []


def _grep_graph(graph: Graph, question: str) -> str:
    """Last-resort: substring search across object data."""
    needle = question.lower().strip("? ")
    if not needle:
        return "(no question)"
    matches: list[str] = []
    for o in graph.all_objects():
        text = " ".join(str(v) for v in o.data.values()).lower()
        if needle in text:
            label = o.data.get("name") or o.data.get("text", "")[:80] \
                    or o.data.get("path", "") or o.id
            matches.append(f"  {o.type:14}  {label}")
            if len(matches) >= 20:
                break
    if not matches:
        return f"No graph nodes match '{needle}'. (Ingest may be incomplete.)"
    return f"Graph nodes matching '{needle}':\n" + "\n".join(matches)


# ---------- tiny REPL ----------


def repl(graph: Graph) -> None:  # pragma: no cover — interactive
    print("selfgraph chat — try: 'what can you do?', 'how would you "
          "implement forking?', 'list constraints', 'quit'")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if q in ("quit", "exit", ":q"):
            return
        if not q:
            continue
        print(answer_question(graph, q))
