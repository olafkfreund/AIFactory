#!/usr/bin/env python3
"""RFC-0021 baseline: count exact and near-duplicate pairs in pooled memory.

Read-only. Usage: memory_dup_baseline.py <project_dir> [--embedder]
Prints JSON. Default similarity is a lexical bag-of-words cosine (no model
call); --embedder uses the Graphiti embedder and falls back to lexical.
"""

import argparse
import asyncio
import json
import math
import re
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

THRESHOLD = 0.88
INSIGHT_KEYS = (
    "what_worked",
    "what_failed",
    "recommendations_for_next_session",
    "discoveries",
)


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for n in node:
            yield from _strings(n)
    elif isinstance(node, dict):
        for n in node.values():
            yield from _strings(n)


def fold(text: str) -> str:
    return " ".join(text.lower().split())


def load_items(memory_dir: Path) -> list[str]:
    """Gotcha bullets/paragraphs plus every insight string of 15+ chars."""
    items: list[str] = []
    gotchas = memory_dir / "gotchas.md"
    if gotchas.exists():
        for block in re.split(r"\n\s*\n", gotchas.read_text()):
            lines = [ln for ln in block.splitlines() if not ln.startswith("#")]
            bullets = [ln[2:] for ln in lines if ln.startswith("- ")]
            items += bullets or ["\n".join(lines)]
    for f in sorted((memory_dir / "session_insights").glob("session_*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        items += [s for k in INSIGHT_KEYS for s in _strings(data.get(k))]
    return [i.strip() for i in items if len(i.strip()) >= 15]


def lexical_vector(text: str) -> Counter:
    return Counter(re.findall(r"\w+", text.lower()))


def cosine(a, b) -> float:
    if isinstance(a, Counter):
        dot = sum(v * b.get(k, 0) for k, v in a.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def count_pairs(texts: list[str], vectors: list) -> tuple[int, int]:
    """(exact pairs, near pairs). Near excludes exact; O(n^2), fine at pool size."""
    exact = near = 0
    for i, j in combinations(range(len(texts)), 2):
        if fold(texts[i]) == fold(texts[j]):
            exact += 1
        elif cosine(vectors[i], vectors[j]) >= THRESHOLD:
            near += 1
    return exact, near


def embed_all(texts: list[str]) -> list | None:
    """Vectors from the existing Graphiti embedder, or None if unavailable."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps/backend"))
        from integrations.graphiti.config import GraphitiConfig
        from integrations.graphiti.providers_pkg.factory import create_embedder

        embedder = create_embedder(GraphitiConfig.from_env())

        async def run():
            return [await embedder.create(t) for t in texts]

        return asyncio.run(run())
    except Exception as e:  # unconfigured, unreachable, or missing package
        print(
            f"embedder unavailable ({type(e).__name__}); using lexical", file=sys.stderr
        )
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir", type=Path)
    ap.add_argument("--embedder", action="store_true")
    args = ap.parse_args()
    aif = args.project_dir / ".aifactory"
    texts = load_items(aif / "memory")
    vectors = embed_all(texts) if args.embedder else None
    measure = "embedder-cosine" if vectors else "lexical-bow-cosine"
    exact, near = count_pairs(texts, vectors or [lexical_vector(t) for t in texts])
    specs = aif / "specs"
    print(
        json.dumps(
            {
                "items": len(texts),
                "exact_pairs": exact,
                "near_pairs": near,
                "threshold": THRESHOLD,
                "measure": measure,
                "specs": len(list(specs.iterdir())) if specs.is_dir() else 0,
            }
        )
    )


if __name__ == "__main__":
    main()
