"""Capability extraction.

Reads Chunk objects from the graph and emits capability nodes.

Two extraction paths:

* deterministic: regex + heuristics over Python signatures, markdown
  headings, and ``@behavior`` / ``@llm_behavior`` / ``@tool`` decorators.
* LLM (optional): when ``ANTHROPIC_API_KEY`` is set, an LLM pass over
  the same chunks proposes additional Capability / Constraint nodes.
  This is *additive* — the deterministic pass always runs first so the
  graph has a reproducible floor.

Node types emitted:
    Capability, API, Behavior, ObjectType, RelationType,
    Example, Constraint, AuthorityRule

Relations emitted:
    API_CREATES, API_READS, API_WRITES,
    BEHAVIOR_USES, BEHAVIOR_SUBSCRIBES_TO,
    EXAMPLE_DEMONSTRATES, CONSTRAINT_LIMITS,
    CAPABILITY_REQUIRES_APPROVAL
"""

from __future__ import annotations

import os
import re
from typing import Optional

from activegraph import Graph


# ---------- deterministic patterns ----------

_RE_BEHAVIOR_DECO = re.compile(
    r"@(behavior|llm_behavior|relation_behavior)\s*\(([^)]*)\)\s*\n\s*def\s+(\w+)",
    re.MULTILINE,
)
_RE_TOOL_DECO = re.compile(r"@tool\s*\([^)]*\)\s*\n\s*def\s+(\w+)", re.MULTILINE)
_RE_PY_DEF = re.compile(r"^\s*(?:def|class)\s+(\w+)", re.MULTILINE)
_RE_API_SIG = re.compile(r"^## (?:def|class)\s+(\w+)(\([^)]*\))?", re.MULTILINE)
_RE_ON_LIST = re.compile(r"on\s*=\s*\[([^\]]*)\]")
_RE_CREATES_LIST = re.compile(r"creates\s*=\s*\[([^\]]*)\]")
_RE_MUST = re.compile(
    r"(?:^|[.\s])(must (?:not )?[^.\n]{3,140}\.)",
    re.IGNORECASE,
)
_RE_OBJTYPE_HINT = re.compile(
    r"(?:object[_\s]?type|ObjectType\(name=|add_object\(\s*[\"'])([A-Z][A-Za-z0-9_]+)"
)
# Relaxed ObjectType match: ``ObjectType(name="<name>")`` /
# ``ObjectType(name='<name>')`` constructor calls with whitespace
# tolerance and ANY case for ``<name>``. This is the convention the
# activegraph runtime uses for its pack ObjectTypes (lowercase names
# like ``"company"`` / ``"document"``); the original
# ``_RE_OBJTYPE_HINT`` only catches capitalized identifiers and so
# misses those. Both regexes run against every chunk and their
# captures are unioned in ``_scan_chunk``. Nothing downstream
# (``propose.py``, ``classify_change``) is changed — only what gets
# extracted.
_RE_OBJTYPE_CONSTRUCTOR = re.compile(
    r"ObjectType\(\s*name\s*=\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
)

# Heuristic seed capabilities — the agent will still ground them in
# extracted API/Behavior nodes, but we name them up front so the graph
# has stable anchors users can ask about.
_SEED_CAPABILITIES = [
    ("ingest-repo",        "Read and chunk local files into File/Chunk objects."),
    ("extract-capability", "Mine signatures and docs for graph-native capabilities."),
    ("answer-question",    "Answer questions by querying the capability graph."),
    ("propose-patch",      "Generate a structured PatchProposal for a user goal."),
    ("validate-patch",     "Reject unsafe or out-of-scope patch proposals."),
    ("sandbox-apply",      "Apply a proposal in a fork, run test events, diff."),
]

# Authority rules — encoded as Object so the validator can check them.
_AUTHORITY_RULES = [
    {
        "name": "no-arbitrary-code",
        "rule": "PatchProposal.value MUST NOT contain shell, exec, eval, "
                "subprocess, os.system, or __import__ payloads.",
    },
    {
        "name": "no-authority-mutation",
        "rule": "Patches whose target is an AuthorityRule require explicit "
                "user approval and cannot be self-applied by the agent.",
    },
    {
        "name": "no-external-side-effects",
        "rule": "v1 patches MUST NOT include network calls, file writes "
                "outside the graph, or environment mutations.",
    },
    {
        "name": "allowed-change-kinds",
        "rule": "Allowed v1 change kinds: add_object, add_relation, "
                "add_policy, add_state_bucket, add_task, "
                "add_evaluation, bind_behavior.",
    },
]


def extract_capabilities(graph: Graph, *, use_llm: Optional[bool] = None) -> dict:
    """Run extraction over every Chunk in the graph. Returns counts."""
    if use_llm is None:
        use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))

    print("[extract] seeding capabilities and authority rules")
    seeded = _seed(graph)

    print("[extract] scanning chunks (deterministic pass)")
    counts = {"chunks": 0, **{k: 0 for k in (
        "API", "Behavior", "Example", "Constraint", "ObjectType",
        "RelationType",
    )}}
    for chunk in graph.objects(type="Chunk"):
        counts["chunks"] += 1
        delta = _scan_chunk(graph, chunk, seeded)
        for k, v in delta.items():
            counts[k] = counts.get(k, 0) + v

    if use_llm:
        try:
            print("[extract] LLM augmentation pass")
            counts["llm_added"] = _llm_augment(graph, seeded)
        except Exception as e:  # noqa: BLE001
            print(f"[extract] LLM pass skipped: {e}")
            counts["llm_added"] = 0
    else:
        counts["llm_added"] = 0
        print("[extract] no ANTHROPIC_API_KEY; skipping LLM augmentation")

    print(f"[extract] done: {counts}")
    return counts


# ---------- seeding ----------


def _seed(graph: Graph) -> dict[str, str]:
    """Create the stable Capability + AuthorityRule anchors. Returns a
    name → object-id map the deterministic pass uses to wire relations."""
    seeded: dict[str, str] = {}
    for name, desc in _SEED_CAPABILITIES:
        if not _find_by_name(graph, "Capability", name):
            o = graph.add_object(
                "Capability", {"name": name, "description": desc},
                actor="extract",
            )
            seeded[f"cap:{name}"] = o.id
    for rule in _AUTHORITY_RULES:
        if not _find_by_name(graph, "AuthorityRule", rule["name"]):
            o = graph.add_object("AuthorityRule", rule, actor="extract")
            seeded[f"rule:{rule['name']}"] = o.id
    # Tie every Capability to no-authority-mutation so the
    # CAPABILITY_REQUIRES_APPROVAL edge exists for graph readers.
    auth_obj = _find_by_name(graph, "AuthorityRule", "no-authority-mutation")
    if auth_obj:
        for c in graph.objects(type="Capability"):
            existing = graph.relations(source=c.id, target=auth_obj.id)
            if not existing:
                graph.add_relation(
                    c.id, auth_obj.id, "CAPABILITY_REQUIRES_APPROVAL",
                    actor="extract",
                )
    return seeded


def _find_by_name(graph: Graph, type_: str, name: str):
    for o in graph.objects(type=type_):
        if o.data.get("name") == name:
            return o
    return None


# ---------- deterministic scan ----------


def _scan_chunk(graph: Graph, chunk, seeded: dict[str, str]) -> dict:
    text = chunk.data.get("text", "")
    path = chunk.data.get("file_path", "")
    # Every Object the extractor emits carries source_chunk_id +
    # source_file_path so the grounding-trace step can cite back to a
    # real ingested artifact (a File or a module:// pseudo-file).
    src = {"source_chunk_id": chunk.id, "source_file_path": path}
    delta = {"API": 0, "Behavior": 0, "Example": 0, "Constraint": 0,
             "ObjectType": 0, "RelationType": 0}

    # Behaviors — @behavior, @llm_behavior, @relation_behavior decorators
    for kind, args, fname in _RE_BEHAVIOR_DECO.findall(text):
        b = _add_unique(graph, "Behavior", {
            "name": fname, "kind": kind,
            "on": _parse_list(_RE_ON_LIST.search(args)),
            "creates": _parse_list(_RE_CREATES_LIST.search(args)),
            **src,
        })
        if b:
            delta["Behavior"] += 1
            graph.add_relation(b.id, chunk.id, "EXAMPLE_DEMONSTRATES",
                               actor="extract")
            for ev in _parse_list(_RE_ON_LIST.search(args)):
                t = _add_unique(graph, "EventType", {"name": ev, **src})
                if t:
                    graph.add_relation(b.id, t.id, "BEHAVIOR_SUBSCRIBES_TO",
                                       actor="extract")

    # Tool registrations
    for fname in _RE_TOOL_DECO.findall(text):
        api = _add_unique(graph, "API", {
            "name": fname, "kind": "tool", **src,
        })
        if api:
            delta["API"] += 1

    # API surface from module:// synthetic files
    if path.startswith("module://"):
        for sym, sig in _RE_API_SIG.findall(text):
            api = _add_unique(graph, "API", {
                "name": sym, "signature": (sig or "").strip(),
                "module": path.removeprefix("module://"),
                **src,
            })
            if api:
                delta["API"] += 1
                # crude write/read inference from name
                lname = sym.lower()
                rel = (
                    "API_CREATES" if any(k in lname for k in
                                         ("add_", "create", "emit", "propose"))
                    else "API_WRITES" if any(k in lname for k in
                                             ("apply", "patch", "update", "remove"))
                    else "API_READS"
                )
                # Tie API to the Capability that most likely uses it.
                for cap_key, cap_id in seeded.items():
                    if not cap_key.startswith("cap:"):
                        continue
                    if any(tok in lname for tok in cap_key[4:].split("-")):
                        graph.add_relation(cap_id, api.id, rel, actor="extract")
                        break

    # Object types referenced via add_object("Type", ...) or via
    # ObjectType(name="...") constructor calls. Union of both regex
    # passes; the constructor pass picks up activegraph-runtime
    # lowercase names the original literal-string pass misses.
    typenames = set(_RE_OBJTYPE_HINT.findall(text))
    typenames |= set(_RE_OBJTYPE_CONSTRUCTOR.findall(text))
    for typename in typenames:
        t = _add_unique(graph, "ObjectType", {"name": typename, **src})
        if t:
            delta["ObjectType"] += 1

    # Markdown examples
    if "```" in text and path.endswith(".md"):
        ex = _add_unique(graph, "Example", {
            "snippet": text[:600], **src,
        })
        if ex:
            delta["Example"] += 1

    # Constraints: any "must" / "must not" sentence
    for sent in _RE_MUST.findall(text):
        c = _add_unique(graph, "Constraint", {
            "text": sent.strip(), **src,
        })
        if c:
            delta["Constraint"] += 1

    return delta


def _add_unique(graph: Graph, type_: str, data: dict):
    """Add an object only if no object of the same type has the same name
    (or first non-empty key value, for shapes with no name)."""
    key = data.get("name") or data.get("text") or data.get("snippet", "")
    if not key:
        return graph.add_object(type_, data, actor="extract")
    for o in graph.objects(type=type_):
        existing_key = o.data.get("name") or o.data.get("text") \
                       or o.data.get("snippet", "")
        if existing_key == key:
            return None
    return graph.add_object(type_, data, actor="extract")


def _parse_list(match) -> list[str]:
    if not match:
        return []
    raw = match.group(1)
    return [
        s.strip().strip("'\"") for s in raw.split(",") if s.strip()
    ]


# ---------- optional LLM pass ----------


def _llm_augment(graph: Graph, seeded: dict[str, str]) -> int:
    """Use Claude (Anthropic SDK) to pull soft constraints and capability
    descriptions out of long-form docs. Skipped silently if the SDK or
    key is missing — the deterministic pass is the contract; this is gravy.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed")

    client = Anthropic()
    chunks = [
        c for c in graph.objects(type="Chunk")
        if c.data.get("file_path", "").endswith(".md")
    ][:8]
    if not chunks:
        return 0
    added = 0
    for ch in chunks:
        prompt = (
            "Extract a JSON object with keys 'capabilities' (list of "
            "{name, description}) and 'constraints' (list of strings) "
            "from the following docs chunk. Capabilities are things the "
            "system can do; constraints are limits/rules. JSON only.\n\n"
            f"{ch.data.get('text', '')[:1500]}"
        )
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            import json
            text = resp.content[0].text
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                continue
            data = json.loads(text[start : end + 1])
            for cap in data.get("capabilities", []) or []:
                if isinstance(cap, dict) and cap.get("name"):
                    if _add_unique(graph, "Capability", {
                        "name": cap["name"],
                        "description": cap.get("description", ""),
                        "source": "llm",
                    }):
                        added += 1
            for con in data.get("constraints", []) or []:
                if isinstance(con, str) and len(con) > 4:
                    if _add_unique(graph, "Constraint", {
                        "text": con, "source": "llm",
                        "source_file": ch.data.get("file_path"),
                    }):
                        added += 1
        except Exception as e:  # noqa: BLE001
            print(f"[extract.llm] chunk skipped: {e}")
    return added
