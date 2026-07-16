"""Exclude the manual graphiti integration scripts from pytest collection (#854).

``test_graphiti_memory.py`` and ``test_ollama_embedding_memory.py`` are not unit
tests: they are manual, interactive integration scripts (argparse CLIs with an
``if __name__ == "__main__"`` runner, whose ``test_*`` functions take positional
arguments pytest can't supply and require live external services — an embedded
graph store and a running Ollama). They match ``test_*.py`` only by coincidence.
Ignore them here so ``pytest apps/backend`` (now a CI gate) collects the real
tests in this package without erroring on these scripts.
"""

collect_ignore = [
    "test_graphiti_memory.py",
    "test_ollama_embedding_memory.py",
]
