"""Self-query: answer questions about capabilities by reading the graph,
not by re-prompting an LLM with the docs.

The contract here is simple: every answer cites the graph nodes it came
from. If a fact isn't in the graph, the agent says so — and the fix is
to ingest more, not to make something up.
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
