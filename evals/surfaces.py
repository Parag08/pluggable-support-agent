"""Eval surfaces: which cases to run for the part of the agent you changed.

`cases.json` has a `surfaces` registry (name → description, the source files that belong to it, the
unit-test files that cover it) and every case lists the surfaces it protects. This module is pure
(no model, no database), so the mapping is itself unit-tested (tests/test_eval_cases.py).
"""

import fnmatch
import json
import subprocess
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent


def load_spec() -> dict[str, Any]:
    return json.loads((EVAL_DIR / "cases.json").read_text())


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def expand(names: list[str], spec: dict[str, Any]) -> list[str]:
    """Surfaces plus the ones they say must be re-checked with them (`also`), in registry order."""
    registry = spec["surfaces"]
    unknown = [n for n in names if n not in registry]
    if unknown:
        raise SystemExit(f"Unknown surface(s) {unknown}. Known: {sorted(registry)}")
    wanted, stack = set(), list(names)
    while stack:
        n = stack.pop()
        if n not in wanted:
            wanted.add(n)
            stack.extend(registry[n].get("also", []))
    return [n for n in registry if n in wanted]


def surfaces_for_files(files: list[str], spec: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Return (surfaces to run, unmapped files, files that force a full run).

    A changed file under app/ or agent_config/ that no surface claims is treated as "run everything":
    an unmapped file must never mean "nothing to test"."""
    registry = spec["surfaces"]
    everything = spec["run_everything_paths"]
    hit: set[str] = set()
    unmapped: list[str] = []
    shared: list[str] = []
    for f in files:
        if _matches(f, everything):
            shared.append(f)
            continue
        owners = [n for n, s in registry.items() if _matches(f, s["paths"])]
        if owners:
            hit.update(owners)
        elif f.startswith(("app/", "agent_config/")) and not _matches(f, spec["ignore_paths"]):
            unmapped.append(f)
    return (expand(sorted(hit), spec) if hit else []), unmapped, shared + unmapped


def changed_files(base: str = "HEAD") -> list[str]:
    """Files changed against `base` (working tree included), plus new untracked files."""
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]
    return sorted(set(git("diff", "--name-only", base)) | set(git("ls-files", "--others", "--exclude-standard")))


def cases_for(spec: dict[str, Any], surfaces: list[str] | None) -> list[dict[str, Any]]:
    if surfaces is None:
        return spec["cases"]
    wanted = set(surfaces)
    return [c for c in spec["cases"] if wanted & set(c["surfaces"])]


def unit_tests_for(spec: dict[str, Any], surfaces: list[str]) -> list[str]:
    seen: list[str] = []
    for n in surfaces:
        for t in spec["surfaces"][n].get("tests", []):
            if t not in seen:
                seen.append(t)
    return seen
