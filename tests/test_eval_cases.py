"""The eval suite's own structure: every case belongs to a surface, every source file maps to one.

No network. These fail when someone adds a module or a case without saying which eval surface covers it,
which is what stops `run_evals --changed` from silently testing nothing.
"""

import subprocess
from pathlib import Path

import pytest

from evals import surfaces as surf

SPEC = surf.load_spec()
ROOT = Path(__file__).resolve().parent.parent


def test_case_ids_are_unique_and_every_case_has_valid_surfaces_and_a_gate():
    ids = [c["id"] for c in SPEC["cases"]]
    assert len(ids) == len(set(ids))
    for c in SPEC["cases"]:
        assert c["surfaces"], f"{c['id']} has no surfaces"
        assert set(c["surfaces"]) <= set(SPEC["surfaces"]), c["id"]
        assert c["category"] in SPEC["gates"], f"{c['id']}: no gate for category {c['category']}"
        assert c["steps"], c["id"]


def test_every_surface_has_cases_files_and_resolvable_links():
    for name, s in SPEC["surfaces"].items():
        assert surf.cases_for(SPEC, [name]), f"surface {name} has no eval case"
        assert s["paths"] and s["description"], name
        assert set(s.get("also", [])) <= set(SPEC["surfaces"]), name
        for t in s.get("tests", []):
            assert (ROOT / t).exists(), f"{name}: unit test path {t} does not exist"


def tracked_sources() -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "app", "agent_config"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    return [f for f in out if (ROOT / f).exists()]


def test_every_source_file_belongs_to_a_surface_or_is_explicitly_shared_or_ignored():
    _, unmapped, _ = surf.surfaces_for_files(tracked_sources(), SPEC)
    assert not unmapped, f"add these to a surface (or ignore_paths) in evals/cases.json: {unmapped}"


@pytest.mark.parametrize("files, surfaces, forces_all", [
    (["app/agent/injection_guard.py"], ["injection"], False),
    (["agent_config/injection_guard.json"], ["injection"], False),
    (["app/agent/policy.py"], ["policy"], False),
    (["agent_config/prompts/classifier.md"], ["classifier", "injection"], False),
    (["app/adapters/classifier/cascade.py"], ["classifier", "cascade", "injection"], False),
    (["app/agent/output_check.py", "app/adapters/handoff/email.py"], ["responder", "handoff"], False),
    (["README.md", "docs/ARCHITECTURE.md", "tests/test_cascade.py"], [], False),
    (["app/agent/orchestrator.py"], [], True),
    (["app/agent/brand_new_module.py"], [], True),
])
def test_changed_files_map_to_the_right_surfaces(files, surfaces, forces_all):
    got, _, forces = surf.surfaces_for_files(files, SPEC)
    assert got == [n for n in SPEC["surfaces"] if n in surfaces]
    assert bool(forces) == forces_all


def test_cases_for_selects_by_surface_and_none_means_all():
    assert surf.cases_for(SPEC, None) == SPEC["cases"]
    assert {c["id"] for c in surf.cases_for(SPEC, ["handoff"])} >= {"HO-01", "HO-02"}
    assert all("injection" in c["surfaces"] for c in surf.cases_for(SPEC, ["injection"]))


def test_unknown_surface_is_an_error():
    with pytest.raises(SystemExit):
        surf.expand(["nope"], SPEC)


def test_cascade_cases_declare_the_provider_they_need():
    for c in surf.cases_for(SPEC, ["cascade"]):
        if c["category"] == "cascade_routing":
            assert c["requires"] == {"classifier_provider": "cascade"}
