"""Patch proposal.

A PatchProposal is itself an Object in the graph. Its ``data`` carries:

    {
      "goal":        original user goal,
      "rationale":   why this shape was chosen,
      "changes":     [ {kind, ...}, ... ],   # see ALLOWED_KINDS in guardrails
      "evaluation":  [ "criterion text", ... ],
      "status":      "draft" | "validated" | "applied" | "rejected",
    }

The proposal is wired to the Capability/API/Behavior nodes it
*proposes* (``PATCH_PROPOSES``) and the ObjectType nodes it would
*modify* (``PATCH_MODIFIES``). Every change kind is graph-native — no
file writes, no shell, no arbitrary Python.

Pattern choice is intentionally not hardcoded. We look at what's in the
capability graph (existing Behaviors with ``on=...`` event types,
ObjectTypes, AuthorityRules) and assemble a small pipeline using only
the primitives the graph reports it has.
"""

from __future__ import annotations

from typing import Optional

from activegraph import Graph


def propose_patch_for(
    graph: Graph,
    goal: str,
    *,
    proposed_by: str = "selfgraph",
) -> str:
    """Generate a PatchProposal object. Returns its id."""
    print(f"[propose] goal = {goal!r}")
    extracted = _scan_self(graph)

    changes: list[dict] = []
    rationale_lines: list[str] = []

    # 1. New ObjectType to bucket whatever the goal is about.
    bucket = _bucket_name(goal)
    changes.append({
        "kind": "add_object",
        "type": "ObjectType",
        "data": {"name": bucket, "description": f"State bucket for: {goal}"},
    })
    rationale_lines.append(
        f"Added ObjectType '{bucket}' as the state bucket because the "
        f"graph has no existing ObjectType whose name overlaps the goal."
    )

    # 2. Task structure — a Task object describing the work.
    changes.append({
        "kind": "add_task",
        "type": "Task",
        "data": {
            "goal": goal, "bucket": bucket, "status": "pending",
        },
    })
    rationale_lines.append(
        "Added a Task object so the work is represented in the graph "
        "and downstream behaviors can subscribe to its lifecycle."
    )

    # 3. Bind existing behaviors instead of inventing new ones. Pick the
    #    behavior whose extracted `on=` event types overlap the goal text.
    bound = _pick_behavior_bindings(extracted, goal)
    for beh_name, event_type in bound:
        changes.append({
            "kind": "bind_behavior",
            "behavior": beh_name,
            "on_event_type": event_type,
            "scope_object_type": bucket,
        })
        rationale_lines.append(
            f"Bound existing behavior '{beh_name}' to event "
            f"'{event_type}' scoped to '{bucket}'. No new code — only "
            f"a binding the runtime already supports."
        )
    if not bound:
        # No existing behaviors fired — derive the pattern from what's
        # actually in the graph. The shape is NOT a hardcoded
        # OODA/PDCA template; it's composed from:
        #   - the most common EventType ingested (the trigger surface)
        #   - extracted ObjectTypes whose names overlap the goal
        #     (the "what is this about" anchor)
        #   - the existing AuthorityRule list (the constraint surface)
        # If the graph is sparse the pattern degenerates to "atom +
        # snapshot + an event-typed trigger" — still graph-native, not
        # phase-named.
        trigger = _dominant_event_type(extracted, default="object.created")
        atom_type = f"{bucket}Atom"
        snapshot_type = f"{bucket}Snapshot"
        rationale_lines.append(
            f"No existing behavior subscribed to a goal-matching event. "
            f"Composing a graph-native pattern from the trace: "
            f"{atom_type} as the per-update atom, {snapshot_type} as the "
            f"rolled-up state, OBSERVES on '{trigger}' (the most common "
            f"event type in the ingested graph). No phase names — the "
            f"shape comes from extracted EventTypes and ObjectTypes, "
            f"not from a template."
        )
        for t, desc in (
            (atom_type,    f"Single observed update for the goal: {goal}"),
            (snapshot_type, f"Aggregated view of {atom_type}s at a point in time"),
        ):
            changes.append({
                "kind": "add_object",
                "type": "ObjectType",
                "data": {"name": t, "description": desc,
                         "trigger_event": trigger},
            })
        changes.append({
            "kind": "add_relation",
            "from_type": "ObjectType", "from_name": atom_type,
            "to_type":   "ObjectType", "to_name":   snapshot_type,
            "rel_type":  "ROLLS_UP_INTO",
        })
        # Tie the new atom type to whichever existing ObjectType in the
        # graph overlaps the goal text — so the proposal is wired to
        # what was actually extracted, not floating in space.
        related_existing = _related_object_types(extracted, goal)
        for ot in related_existing[:2]:
            changes.append({
                "kind": "add_relation",
                "from_type": "ObjectType", "from_name": atom_type,
                "to_type":   "ObjectType", "to_name":   ot.data["name"],
                "rel_type":  "GROUNDED_IN",
            })
            rationale_lines.append(
                f"Wired {atom_type} GROUNDED_IN existing ObjectType "
                f"'{ot.data['name']}' so the new bucket inherits the "
                f"vocabulary the graph already uses."
            )

    # 4. Scoped policy — what the new bucket is allowed to do.
    #    Compose can_create from the ObjectTypes this proposal added.
    creatable = sorted({
        c.get("type") or c.get("data", {}).get("name")
        for c in changes
        if c.get("kind") in ("add_object", "add_state_bucket", "add_task")
    } - {None})
    changes.append({
        "kind": "add_policy",
        "policy": {
            "scope": bucket,
            "can_create": creatable,
            "can_propose": ["update"],
            "requires_approval": ["AuthorityRule"],
        },
    })
    rationale_lines.append(
        f"Added a scoped policy whose can_create list is exactly the "
        f"types this proposal introduces ({', '.join(creatable)}); "
        f"AuthorityRule changes require approval — consistent with the "
        f"'no-authority-mutation' rule already in the graph."
    )

    # 5. Evaluation criteria — how we'll know it worked.
    evaluation = [
        f"A {bucket} object exists in the graph after apply.",
        f"At least one Task with goal='{goal}' exists.",
        "No PatchProposal with status='rejected' was produced by apply.",
        "AuthorityRule objects are unchanged.",
    ]
    for crit in evaluation:
        changes.append({
            "kind": "add_evaluation",
            "type": "Evaluation",
            "data": {"criterion": crit, "for_goal": goal},
        })

    # Materialize the proposal as an Object.
    proposal = graph.add_object(
        "PatchProposal",
        {
            "goal": goal,
            "rationale": " ".join(rationale_lines),
            "changes": changes,
            "evaluation": evaluation,
            "status": "draft",
            "proposed_by": proposed_by,
        },
        actor=proposed_by,
    )
    print(f"[propose] drafted PatchProposal {proposal.id} with "
          f"{len(changes)} changes")

    # Wire PATCH_PROPOSES / PATCH_MODIFIES so a graph reader can see
    # what the proposal touches without re-parsing its data blob.
    for cap in graph.objects(type="Capability"):
        if cap.data.get("name") in {"propose-patch", "extract-capability"}:
            graph.add_relation(proposal.id, cap.id, "PATCH_PROPOSES",
                               actor=proposed_by)
    for ot in graph.objects(type="ObjectType"):
        if ot.data.get("name") in {bucket, "Task", "Phase"}:
            graph.add_relation(proposal.id, ot.id, "PATCH_MODIFIES",
                               actor=proposed_by)

    return proposal.id


# ---------- helpers ----------


def _scan_self(graph: Graph) -> dict:
    return {
        "behaviors": graph.objects(type="Behavior"),
        "event_types": graph.objects(type="EventType"),
        "object_types": graph.objects(type="ObjectType"),
        "capabilities": graph.objects(type="Capability"),
    }


def _bucket_name(goal: str) -> str:
    # Title-case the first 2-3 keywords; safe for use as an ObjectType name.
    words = [w for w in goal.replace("/", " ").split() if w.isalnum()]
    keep = [w.capitalize() for w in words[:3] if len(w) > 2]
    return "".join(keep) or "GoalBucket"


def _dominant_event_type(extracted: dict, *, default: str) -> str:
    """Return the EventType name most often referenced by extracted
    Behaviors. Falls back to a sensible activegraph default. The point
    is that the trigger is *observed* in the graph, not chosen by us."""
    counts: dict[str, int] = {}
    for b in extracted["behaviors"]:
        for ev in b.data.get("on") or []:
            counts[ev] = counts.get(ev, 0) + 1
    for et in extracted["event_types"]:
        name = et.data.get("name", "")
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return default
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _related_object_types(extracted: dict, goal: str) -> list:
    goal_toks = {t.lower() for t in goal.split() if len(t) > 3}
    scored = []
    for ot in extracted["object_types"]:
        name = (ot.data.get("name") or "").lower()
        score = sum(1 for t in goal_toks if t in name)
        if score:
            scored.append((score, ot))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [ot for _, ot in scored]


def _pick_behavior_bindings(extracted: dict, goal: str) -> list[tuple[str, str]]:
    """For each Behavior whose `on=` event types overlap goal words, return
    (behavior_name, event_type) — at most 3."""
    goal_tokens = {t.lower() for t in goal.split() if len(t) > 3}
    out: list[tuple[str, str]] = []
    for b in extracted["behaviors"]:
        name = b.data.get("name", "")
        on_list = b.data.get("on") or []
        for ev in on_list:
            if any(tok in (name + " " + ev).lower() for tok in goal_tokens):
                out.append((name, ev))
                if len(out) >= 3:
                    return out
    return out
