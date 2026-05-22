"""Repo + module ingestion.

Walks file trees and module trees and emits one ``File`` object per
artifact (path, type, sha256, ts, content). Long files are chunked into
``Chunk`` objects related to their parent File via ``FILE_HAS_CHUNK``.

Everything goes through ``graph.add_object`` / ``graph.add_relation`` so
each ingest action shows up in the event log — the trace is the proof
the agent really read what it claims to know.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import os
import pkgutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from activegraph import Graph


TEXT_EXT = {
    ".md", ".rst", ".txt", ".py", ".toml", ".yaml", ".yml",
    ".json", ".cfg", ".ini",
}
CHUNK_CHARS = 2000


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_file(graph: Graph, path: str, kind: str, content: str) -> str:
    """Create a File object + Chunk objects. Returns the file's object id.

    Dedupes on (path, sha256): if an unchanged File already exists for
    this path, return its id and skip re-emitting chunks. Re-ingesting
    a modified file emits a new File (content-addressed by hash), so
    history is preserved without piling up identical copies.
    """
    digest = _sha(content)
    for existing in graph.objects(type="File"):
        if (existing.data.get("path") == path
                and existing.data.get("sha256") == digest):
            return existing.id
    f = graph.add_object(
        "File",
        {
            "path": path,
            "kind": kind,                  # "repo" | "module"
            "ext": Path(path).suffix.lower(),
            "sha256": digest,
            "ingested_at": _now(),
            "size": len(content),
            "preview": content[:400],
        },
        actor="ingest",
    )
    for i in range(0, len(content), CHUNK_CHARS):
        chunk_text = content[i : i + CHUNK_CHARS]
        c = graph.add_object(
            "Chunk",
            {
                "file_path": path,
                "offset": i,
                "text": chunk_text,
                "sha256": _sha(chunk_text),
            },
            actor="ingest",
        )
        graph.add_relation(f.id, c.id, "FILE_HAS_CHUNK", actor="ingest")
    return f.id


def ingest_paths(
    graph: Graph,
    roots: Iterable[str],
    *,
    exts: Optional[set[str]] = None,
    skip_dirs: Iterable[str] = (".git", "__pycache__", ".venv", "node_modules"),
    max_bytes: int = 200_000,
) -> list[str]:
    """Walk ``roots`` and emit a File per text artifact. Returns file ids."""
    exts = exts or TEXT_EXT
    skip = set(skip_dirs)
    ids: list[str] = []
    for root in roots:
        root_p = Path(root)
        if root_p.is_file():
            files = [root_p]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(root_p):
                # Sort filesystem walk so ingestion order is stable
                # across machines (os.walk yields entries in
                # filesystem-dependent order otherwise). This is a
                # determinism fix, not a behavior change: the same
                # files are ingested either way; only the sequence is
                # canonical now.
                dirnames[:] = sorted(d for d in dirnames if d not in skip)
                for fn in sorted(filenames):
                    files.append(Path(dirpath) / fn)
        for fp in files:
            if fp.suffix.lower() not in exts:
                continue
            try:
                if fp.stat().st_size > max_bytes:
                    continue
                content = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            print(f"  [ingest] {fp}")
            ids.append(_emit_file(graph, str(fp), "repo", content))
    return ids


def ingest_module_docs(
    graph: Graph,
    module_name: str,
    *,
    max_submodules: int = 50,
) -> list[str]:
    """Introspect a Python package as if its source were a docs corpus.

    For each (sub)module, build a synthetic text artifact containing the
    module docstring plus the signature + docstring of each public class
    and function. Emits a File per module so the capability extractor
    can read structured Python APIs the same way it reads markdown.
    """
    pkg = importlib.import_module(module_name)
    targets = [(module_name, pkg)]
    if hasattr(pkg, "__path__"):
        # Sort the package walk so synthetic-file ingestion order
        # is stable across machines (pkgutil.walk_packages yields
        # entries in filesystem order otherwise).
        walked = sorted(
            pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."),
            key=lambda info: info.name,
        )
        for info in walked:
            if len(targets) >= max_submodules:
                break
            # Skip script-style entry points (e.g. activegraph/__main__.py
            # which raises SystemExit on import).
            if info.name.endswith(".__main__") or info.name.endswith(".cli.main"):
                continue
            try:
                targets.append((info.name, importlib.import_module(info.name)))
            except SystemExit as e:
                # A module that calls sys.exit() at import (CLI shims).
                print(f"  [ingest] skip {info.name}: SystemExit({e.code})")
            except Exception as e:  # noqa: BLE001 — optional deps / import errors
                print(f"  [ingest] skip {info.name}: {e}")
    ids: list[str] = []
    for name, mod in targets:
        text = _render_module(name, mod)
        if not text.strip():
            continue
        synthetic_path = f"module://{name}"
        print(f"  [ingest] {synthetic_path}")
        ids.append(_emit_file(graph, synthetic_path, "module", text))
    return ids


def _render_module(name: str, mod) -> str:
    lines: list[str] = [f"# module {name}", ""]
    doc = inspect.getdoc(mod)
    if doc:
        lines += [doc, ""]
    members = [
        (n, m)
        for n, m in inspect.getmembers(mod)
        if not n.startswith("_") and getattr(m, "__module__", None) == name
    ]
    for n, m in members:
        try:
            if inspect.isclass(m):
                sig = ""
                try:
                    sig = str(inspect.signature(m))
                except (TypeError, ValueError):
                    pass
                lines += [f"## class {n}{sig}"]
                cdoc = inspect.getdoc(m) or ""
                if cdoc:
                    lines += [cdoc]
                for mn, mm in inspect.getmembers(m, predicate=inspect.isfunction):
                    if mn.startswith("_"):
                        continue
                    try:
                        msig = str(inspect.signature(mm))
                    except (TypeError, ValueError):
                        msig = ""
                    mdoc = inspect.getdoc(mm) or ""
                    lines += [f"### {n}.{mn}{msig}"]
                    if mdoc:
                        lines += [mdoc]
                lines += [""]
            elif inspect.isfunction(m):
                try:
                    sig = str(inspect.signature(m))
                except (TypeError, ValueError):
                    sig = ""
                fdoc = inspect.getdoc(m) or ""
                lines += [f"## def {n}{sig}"]
                if fdoc:
                    lines += [fdoc]
                lines += [""]
        except Exception as e:  # noqa: BLE001
            lines += [f"## {n} (introspection failed: {e})", ""]
    return "\n".join(lines)
