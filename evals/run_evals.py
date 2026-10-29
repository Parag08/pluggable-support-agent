"""Repeatable agent evals against the live model.

    .venv/bin/python -m evals.run_evals --list               # surfaces, what they cover, which files map to them
    .venv/bin/python -m evals.run_evals --changed            # the surfaces your uncommitted changes touch
    .venv/bin/python -m evals.run_evals --surface injection cascade
    .venv/bin/python -m evals.run_evals --release            # unit tests, then every case at release_trials
    .venv/bin/python -m evals.run_evals                      # all cases, EVAL_TRIALS trials
    .venv/bin/python -m evals.run_evals --trials 1 --only PI-01 UA-02
    .venv/bin/python -m evals.run_evals --category prompt_injection

Each case × trial runs on its own freshly seeded SQLite database, so cases can't
affect each other and results don't depend on order. Faults (API errors,
malformed model output, data-layer failures) are injected deterministically.

Writes evals/results/<timestamp>.json and .md. A run of every case also updates latest.md; a partial run
(--surface, --changed, --only, --category) writes latest-partial.md instead, so latest.md is always the full picture.
Exits non-zero if any category falls below its gate in cases.json — usable in CI.
"""

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import tempfile
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.adapters.commerce.sql_reference import SqlReferenceCommerce
from app.adapters.llm import build_llm_client
from app.agent.llm import LLMError, LLMResult
from app.wiring import build_agent
from app.agent.preprocess import ImageInput
from app.config import get_settings
from app.auth.grant import mark_identity_validated
from app.db.base import get_sessionmaker, make_engine
from app.db.init_db import create_schema
from app.db.models import (
    Escalation,
    Order,
    ParkedIntent,
    ParkedIntentStatus,
    ProductVariant,
    ReturnRequest,
    Turn,
)
from app.db.seed import seed

from evals import surfaces as surf

EVAL_DIR = Path(__file__).resolve().parent
CLASSIFY_SCHEMA = "support_classification"


# --- fault injection --------------------------------------------------------


class FaultInjectingLLM:
    """Wraps the real client; replaces specific calls with failures."""

    def __init__(self, inner, faults: list[dict]):
        self.inner = inner
        self.provider = inner.provider
        self.faults = [f for f in faults if "llm" in f]
        self.counts = {"classify": 0, "respond": 0}

    def complete(self, **kw) -> LLMResult:
        kind = "classify" if kw["schema_name"] == CLASSIFY_SCHEMA else "respond"
        self.counts[kind] += 1
        for f in self.faults:
            if f["llm"] == kind and f["on_call"] == self.counts[kind]:
                if f["mode"] == "error":
                    raise LLMError("injected fault: APITimeoutError")
                if f["mode"] == "malformed":
                    return LLMResult("I think the customer is asking about stores.", "stop", kw["model"], 0, 0, 0, {})
                if f["mode"] == "truncated":
                    real = self.inner.complete(**kw)
                    return LLMResult((real.content or "")[:40], "length", real.model,
                                     real.input_tokens, real.output_tokens, real.latency_ms, {})
        return self.inner.complete(**kw)


class FaultyCommerce:
    """Wraps a CommercePort; the listed functions fail before or after doing their work.

    `times` limits how many calls fail (default: all), so a fault can be a one-off glitch
    that a retry survives, or a persistent outage.
    """

    def __init__(self, inner, faults: list[dict]):
        self._inner = inner
        self._faults = {f["commerce"]: f for f in faults if "commerce" in f}
        self._calls: dict[str, int] = {}

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        fault = self._faults.get(name)
        if fault is None or not callable(attr):
            return attr

        def call(*args, **kwargs):
            n = self._calls[name] = self._calls.get(name, 0) + 1
            if n > fault.get("times", 10**9):
                return attr(*args, **kwargs)
            if fault["mode"] == "raise_before":
                raise ConnectionError(f"injected fault: {name} unavailable")
            attr(*args, **kwargs)
            raise ConnectionError(f"injected fault: {name} failed after writing")
        return call


# --- running a case -----------------------------------------------------------


@dataclass
class CaseRun:
    case_id: str
    trial: int
    passed: bool = True
    error: str | None = None
    steps: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)


def _criterion(name: str, expected: Any, actual: Any, ok: bool) -> dict:
    return {"criterion": name, "expected": expected, "actual": actual, "pass": bool(ok)}


def _observe(db, reply) -> dict:
    if reply is None:
        return {"outcome": None}
    turn = db.get(Turn, reply.turn_id)
    esc = db.scalars(select(Escalation).where(Escalation.turn_id == reply.turn_id)).first()
    return {
        "outcome": reply.outcome.value,
        "reason_code": turn.policy_reason_code,
        "escalation_reason": esc.reason.value if esc else None,
        "handler": turn.handler,
        "intent": turn.intent,
        "confidence": turn.classifier_confidence,
        "stages": [s.stage.value for s in turn.steps],
        "output_check_passed": turn.output_check_passed,
        "quick_replies": len((reply.payload or {}).get("quick_replies") or []),
        "route": next((s.params.get("route") for s in turn.steps if s.stage.value == "classify"), None),
        "reply": reply.text,
        "turn_id": reply.turn_id,
    }


def _evaluate(exp: dict, actual: dict, templates: dict) -> list[dict]:
    out = []
    reply = actual.get("reply") or ""
    if "outcome" in exp:
        out.append(_criterion("outcome", exp["outcome"], actual["outcome"], actual["outcome"] == exp["outcome"]))
    if "outcome_in" in exp:
        out.append(_criterion("outcome in", exp["outcome_in"], actual["outcome"], actual["outcome"] in exp["outcome_in"]))
    if "not_outcome" in exp:
        out.append(_criterion("outcome not in", exp["not_outcome"], actual["outcome"],
                              actual["outcome"] not in exp["not_outcome"]))
    if "reason_code" in exp:
        out.append(_criterion("reason code", exp["reason_code"], actual.get("reason_code"),
                              actual.get("reason_code") == exp["reason_code"]))
    if "reason_code_in" in exp:
        out.append(_criterion("reason code in", exp["reason_code_in"], actual.get("reason_code"),
                              actual.get("reason_code") in exp["reason_code_in"]))
    if "escalation_reason" in exp:
        out.append(_criterion("escalation", exp["escalation_reason"], actual.get("escalation_reason"),
                              actual.get("escalation_reason") == exp["escalation_reason"]))
    if "handler" in exp:
        out.append(_criterion("handler", exp["handler"], actual.get("handler"), actual.get("handler") == exp["handler"]))
    if "output_check_passed" in exp:
        out.append(_criterion("output check passed", exp["output_check_passed"], actual.get("output_check_passed"),
                              actual.get("output_check_passed") == exp["output_check_passed"]))
    if "reply_equals_template" in exp:
        key = exp["reply_equals_template"]
        out.append(_criterion("reply is template", key, reply[:80], reply == templates[key]))
    for text in exp.get("reply_contains", []):
        out.append(_criterion("reply contains", text, reply[:80], text.lower() in reply.lower()))
    for text in exp.get("reply_not_contains", []):
        out.append(_criterion("reply excludes", text, "present" if text.lower() in reply.lower() else "absent",
                              text.lower() not in reply.lower()))
    for stage in exp.get("no_stage", []):
        out.append(_criterion("stage not run", stage, actual.get("stages"), stage not in actual.get("stages", [])))
    if "route" in exp:
        out.append(_criterion("classifier route", exp["route"], actual.get("route"), actual.get("route") == exp["route"]))
    if "quick_replies_min" in exp:
        out.append(_criterion("quick replies ≥", exp["quick_replies_min"], actual.get("quick_replies"),
                              actual.get("quick_replies", 0) >= exp["quick_replies_min"]))
    return out


def _db_check(db, check: dict) -> dict:
    (kind, spec), = check.items()
    if kind == "returns_count":
        q = select(func.count()).select_from(ReturnRequest)
        q = q.where(ReturnRequest.order_id == spec["order"]) if "order" in spec else \
            q.where(ReturnRequest.customer_id == spec["customer"], ReturnRequest.created_by == "agent")
        actual = db.scalar(q)
        label = f"returns for {spec.get('order') or spec.get('customer')}"
    elif kind == "order_status":
        actual = db.get(Order, spec["order"]).status.value
        label = f"status of {spec['order']}"
    elif kind == "item_status":
        actual = db.get(Order, spec["order"]).items[0].status.value
        label = f"item status in {spec['order']}"
    elif kind == "variant_stock":
        actual = db.scalars(select(ProductVariant).where(ProductVariant.sku == spec["sku"])).one().stock_qty
        label = f"stock of {spec['sku']}"
    elif kind == "parked_active":
        actual = db.scalar(select(func.count()).select_from(ParkedIntent)
                           .where(ParkedIntent.status == ParkedIntentStatus.PARKED))
        label = "parked requests"
    else:
        raise ValueError(f"unknown check {kind}")
    return _criterion(label, spec["eq"], actual, actual == spec["eq"])


def run_case(case: dict, trial: int, templates: dict) -> CaseRun:
    base = get_settings()
    run = CaseRun(case["id"], trial)
    with tempfile.TemporaryDirectory() as tmp:
        settings = base.model_copy(update={"database_url": f"sqlite:///{tmp}/eval.db"})
        engine = make_engine(settings.database_url, echo=False)
        create_schema(engine)
        db = get_sessionmaker(engine)()
        try:
            seed(db, settings)
            faults = case.get("faults", [])
            commerce = FaultyCommerce(SqlReferenceCommerce(db, settings), faults)
            agent = build_agent(db, settings, commerce=commerce,
                                llm=FaultInjectingLLM(build_llm_client(settings), faults))
            session = agent.start_session("eval")
            cards: list[str] = []

            for i, step in enumerate(case["steps"]):
                reply, action = None, None
                if "signin" in step:
                    now = datetime.now(timezone.utc)
                    mark_identity_validated(db, session.id, step["signin"], now, now + timedelta(hours=1))
                    db.commit()
                    action = f"sign in as {step['signin']}"
                    reply = agent.resume_after_auth(session.id)
                elif "set_stock" in step:
                    v = db.scalars(select(ProductVariant).where(ProductVariant.sku == step["set_stock"]["sku"])).one()
                    v.stock_qty = step["set_stock"]["qty"]
                    db.commit()
                    action = f"set stock {step['set_stock']['sku']} = {step['set_stock']['qty']}"
                elif "confirm" in step:
                    idx = step.get("card", -1)
                    action_id = cards[idx] if cards else "no-card"
                    action = f"{'confirm' if step['confirm'] else 'decline'} card {idx}"
                    reply = agent.confirm_action(session.id, action_id, accept=step["confirm"])
                else:
                    images = []
                    if step.get("image"):
                        path = EVAL_DIR / step["image"]
                        images = [ImageInput("image/png", path.read_bytes())]
                    action = f"say: {step['say']}" + (f" [+image {step['image']}]" if images else "")
                    reply = agent.handle_message(session.id, step["say"], images)

                if reply is not None and reply.payload and reply.payload.get("type") == "confirm":
                    cards.append(reply.payload["action_id"])
                actual = _observe(db, reply)
                criteria = _evaluate(step["expect"], actual, templates) if "expect" in step else []
                run.steps.append({"step": i, "action": action, "actual": actual, "criteria": criteria})

            run.checks = [_db_check(db, c) for c in case.get("checks", [])]
        except Exception as exc:  # the harness itself failing is a failed case, never a silent pass
            run.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
        finally:
            db.close()
            engine.dispose()

    all_criteria = [c for s in run.steps for c in s["criteria"]] + run.checks
    run.passed = run.error is None and all(c["pass"] for c in all_criteria)
    return run


# --- reporting ----------------------------------------------------------------


def _fmt(v: Any) -> str:
    text = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 90 else text[:87] + "..."


def write_report(cases: list[dict], runs: list[CaseRun], gates: dict, trials: int, settings, started: datetime,
                 scope: str = "all cases", skipped: list[dict] | None = None,
                 only_surfaces: list[str] | None = None) -> tuple[str, bool]:
    by_case: dict[str, list[CaseRun]] = defaultdict(list)
    for r in runs:
        by_case[r.case_id].append(r)
    by_cat: dict[str, list[CaseRun]] = defaultdict(list)
    for c in cases:
        by_cat[c["category"]].extend(by_case[c["id"]])

    lines = [
        "# Agent eval results", "",
        f"- **Run:** {started:%Y-%m-%d %H:%M UTC}",
        f"- **Classifier model:** `{settings.classifier_model}` · **Vision:** `{settings.classifier_vision_model}` · "
        f"**Responder:** `{settings.responder_model}`",
        f"- **Classifier provider:** `{settings.classifier_provider}`",
        f"- **Prompt version:** `{settings.prompt_version}` · **Policy version:** `{settings.policy_config_version}`",
        f"- **Scope:** {scope}",
        f"- **Trials per case:** {trials} · **Cases:** {len(cases)}"
        + (f" · **Skipped:** {', '.join(c['id'] for c in skipped)} (need another classifier provider)" if skipped else ""), "",
        "## Summary", "",
        "| Category | Cases | Trials passed | Pass rate | Gate | Result |",
        "|---|---|---|---|---|---|",
    ]
    all_ok = True
    for cat, gate in gates.items():
        if cat.startswith("_"):
            continue
        cat_runs = by_cat.get(cat, [])
        if not cat_runs:
            continue
        passed = sum(r.passed for r in cat_runs)
        rate = passed / len(cat_runs)
        ok = rate >= gate
        all_ok &= ok
        n_cases = len({r.case_id for r in cat_runs})
        lines.append(f"| {cat} | {n_cases} | {passed}/{len(cat_runs)} | {rate:.0%} | ≥{gate:.0%} | {'✅ pass' if ok else '❌ FAIL'} |")
    total_pass = sum(r.passed for r in runs)
    lines += ["", f"**Overall:** {total_pass}/{len(runs)} trials passed — suite {'✅ PASSED' if all_ok else '❌ FAILED'}", ""]

    by_surface: dict[str, list[CaseRun]] = defaultdict(list)
    for c in cases:
        for sname in c["surfaces"]:
            if only_surfaces is None or sname in only_surfaces:
                by_surface[sname].extend(by_case[c["id"]])
    lines += ["### By surface", "", "| Surface | Cases | Trials passed | Pass rate |", "|---|---|---|---|"]
    for sname, srs in by_surface.items():
        lines.append(f"| {sname} | {len({r.case_id for r in srs})} | {sum(r.passed for r in srs)}/{len(srs)} | "
                     f"{sum(r.passed for r in srs) / len(srs):.0%} |")
    lines.append("")

    lines += ["## Cases", "", "| ID | Case | Trials passed |", "|---|---|---|"]
    for c in cases:
        rs = by_case[c["id"]]
        mark = "✅" if all(r.passed for r in rs) else ("⚠️" if any(r.passed for r in rs) else "❌")
        lines.append(f"| {c['id']} | {c['title']} | {mark} {sum(r.passed for r in rs)}/{len(rs)} |")

    lines += ["", "## Expected vs actual", "",
              "Shown for the first failing trial if any trial failed, otherwise trial 1.", ""]
    current_cat = None
    for c in cases:
        if c["category"] != current_cat:
            current_cat = c["category"]
            lines += [f"### {current_cat.replace('_', ' ').title()}", ""]
        rs = by_case[c["id"]]
        shown = next((r for r in rs if not r.passed), rs[0])
        lines += [f"#### {c['id']} — {c['title']}  ({sum(r.passed for r in rs)}/{len(rs)})", ""]
        if shown.error:
            lines += ["```", shown.error.strip(), "```", ""]
        lines += ["| Step | Criterion | Expected | Actual | |", "|---|---|---|---|---|"]
        for s in shown.steps:
            for cr in s["criteria"]:
                lines.append(f"| {s['step']}. {_fmt(s['action'])} | {cr['criterion']} | {_fmt(cr['expected'])} | "
                             f"{_fmt(cr['actual'])} | {'✅' if cr['pass'] else '❌'} |")
        for cr in shown.checks:
            lines.append(f"| after | {cr['criterion']} | {_fmt(cr['expected'])} | {_fmt(cr['actual'])} | "
                         f"{'✅' if cr['pass'] else '❌'} |")
        replies = [s for s in shown.steps if s["actual"].get("reply")]
        if replies:
            lines += ["", "<details><summary>Agent replies</summary>", ""]
            for s in replies:
                a = s["actual"]
                lines.append(f"- **{_fmt(s['action'])}** → `{a['outcome']}`"
                             f"{' / ' + a['reason_code'] if a.get('reason_code') else ''}"
                             f" (intent `{a.get('intent')}`, confidence {a.get('confidence')}): {a['reply']}")
            lines += ["", "</details>"]
        lines.append("")
    return "\n".join(lines), all_ok


def _print_surfaces(spec: dict) -> None:
    print(f"{'surface':<11} {'cases':>5}  {'also runs':<22} what it covers / files")
    for name, sd in spec["surfaces"].items():
        n = len(surf.cases_for(spec, [name]))
        print(f"{name:<11} {n:>5}  {', '.join(sd.get('also', [])) or '-':<22} {sd['description']}")
        print(f"{'':<40} files: {', '.join(sd['paths'])}")
        print(f"{'':<40} unit tests: {', '.join(sd.get('tests', [])) or '-'}")
    print(f"\nAny change to {', '.join(spec['run_everything_paths'][:4])}, … or to an unmapped file under app/ "
          f"or agent_config/ runs every surface.\nCategory gates: {', '.join(f'{k} ≥{v:.0%}' for k, v in spec['gates'].items() if not k.startswith('_'))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--only", nargs="*", help="case IDs to run")
    parser.add_argument("--category", help="run one category")
    parser.add_argument("--surface", nargs="+", help="run the cases for these surfaces (see --list)")
    parser.add_argument("--changed", nargs="?", const="HEAD", metavar="BASE",
                        help="run the surfaces touched by files changed against BASE (default HEAD, incl. untracked)")
    parser.add_argument("--release", action="store_true",
                        help="unit tests, then every case at release_trials trials; the gate for shipping")
    parser.add_argument("--classifier", help="override CLASSIFIER_PROVIDER for this run (e.g. cascade)")
    parser.add_argument("--list", action="store_true", help="list surfaces and exit")
    args = parser.parse_args()

    spec = surf.load_spec()
    if args.list:
        _print_surfaces(spec)
        return

    # Decide what to run before settings are read, because a surface may need another classifier.
    chosen: list[str] | None = None
    scope = "all cases"
    if args.surface:
        chosen = surf.expand(args.surface, spec)
        scope = f"surfaces {', '.join(chosen)}"
    if args.changed:
        files = surf.changed_files(args.changed)
        chosen, unmapped, forces_all = surf.surfaces_for_files(files, spec)
        print(f"Changed against {args.changed}: {len(files)} file(s)")
        for u in unmapped:
            print(f"  not mapped to a surface (add it to cases.json): {u}")
        if forces_all:
            shown = ", ".join(forces_all[:4]) + (f" and {len(forces_all) - 4} more" if len(forces_all) > 4 else "")
            print(f"  shared or unmapped files changed ({shown}): running every surface")
            chosen, scope = None, f"all cases (shared files changed against {args.changed})"
        elif not chosen:
            print("No changed file belongs to an eval surface; nothing to run.")
            return
        else:
            scope = f"surfaces {', '.join(chosen)} (changed against {args.changed})"
            print(f"  surfaces: {', '.join(chosen)}")
    if args.release:
        chosen, scope = None, "release: all cases"
        print("Release gate: unit tests first.")
        if subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=EVAL_DIR.parent).returncode:
            raise SystemExit("Unit tests failed; not running evals.")

    needs = {spec["surfaces"][n].get("classifier_provider") for n in (chosen or [])} - {None}
    provider = args.classifier or (needs.pop() if len(needs) == 1 else None)
    if provider:
        os.environ["CLASSIFIER_PROVIDER"] = provider
        from app.config import get_settings as _gs
        _gs.cache_clear()
        print(f"Classifier provider for this run: {provider}")

    settings = get_settings()
    trials = args.trials or (spec["release_trials"] if args.release else settings.eval_trials)
    workers = args.workers or settings.eval_workers

    cases = [c for c in surf.cases_for(spec, chosen)
             if (not args.only or c["id"] in args.only) and (not args.category or c["category"] == args.category)]
    skipped = [c for c in cases if c.get("requires", {}).get("classifier_provider", settings.classifier_provider)
               != settings.classifier_provider]
    cases = [c for c in cases if c not in skipped]
    if skipped:
        print(f"Skipping {', '.join(c['id'] for c in skipped)}: they need CLASSIFIER_PROVIDER="
              f"{skipped[0]['requires']['classifier_provider']} (use --classifier, or --surface cascade).")
    if not cases:
        raise SystemExit("No cases selected.")
    partial = bool(args.only or args.category or chosen is not None)
    if args.only or args.category:
        scope += " · filtered by --only/--category"
    if skipped and not partial:
        scope += f" · {len(skipped)} cases need another classifier"
    templates = json.loads((settings.agent_config_dir / "templates.json").read_text())
    # The agent fills in the company name; compare against the same text.
    templates = {k: v.replace("{brand_name}", settings.brand_name) if isinstance(v, str) else v
                 for k, v in templates.items()}
    started = datetime.now(timezone.utc)

    jobs = [(c, t) for t in range(1, trials + 1) for c in cases]
    runs: list[CaseRun] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_case, c, t, templates): (c, t) for c, t in jobs}
        for fut in cf.as_completed(futures):
            r = fut.result()
            runs.append(r)
            print(f"{'PASS' if r.passed else 'FAIL'}  {r.case_id}  trial {r.trial}"
                  + (f"  ({r.error.splitlines()[0]})" if r.error else ""), flush=True)

    order = {c["id"]: i for i, c in enumerate(cases)}
    runs.sort(key=lambda r: (order[r.case_id], r.trial))
    report, ok = write_report(cases, runs, spec["gates"], trials, settings, started, scope, skipped, chosen)

    out = EVAL_DIR / "results"
    out.mkdir(exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    (out / f"{stamp}.md").write_text(report)
    (out / ("latest-partial.md" if partial else "latest.md")).write_text(report)
    (out / f"{stamp}.json").write_text(json.dumps([r.__dict__ for r in runs], indent=2, default=str))
    print(f"\nReport: {out / ('latest-partial.md' if partial else 'latest.md')}")
    if chosen:
        tests = surf.unit_tests_for(spec, chosen)
        print(f"Also run the unit tests for these surfaces: pytest -q {' '.join(tests)}")
    print("SUITE PASSED" if ok else "SUITE FAILED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
