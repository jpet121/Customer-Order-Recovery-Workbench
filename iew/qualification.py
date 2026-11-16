"""Fail-closed documentary gates. This program cannot grant release authority."""

import argparse
import re
from decimal import Decimal
from pathlib import Path

from .contracts import Data, Problem, digest, encode, now, object_json, utc
from .evaluation import STRATA, score, validate_report
from .inference_budget import authorize, positive_cost
from .workflow_evaluation import validate_workflow_report

ENGINEERING = {"production", "coverage", "mutation", "stateful", "fault", "security",
               "recovery", "accessibility", "performance", "deployment"}
AI_ONLY = {f"AC-{n:03}" for n in (41, 42, 43, 44, 51, 52, 53, 54, 90)}


def assess(dossier: Data, evidence_root: Path) -> Data:
    try:
        return _assess(dossier, evidence_root.resolve())
    except (AttributeError, KeyError, TypeError, ValueError, OSError, Problem):
        return {"documentary_verdict": "HOLD", "release_authorized": False,
                "reasons": ["Malformed, unreadable or inconsistent qualification evidence."]}


def _assess(dossier: Data, root: Path) -> Data:
    holds: list[str] = []
    failures: list[str] = []
    candidate = dossier["candidate_sha256"]
    if not isinstance(candidate, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate):
        raise ValueError("Candidate identity required")
    verified = set()
    for key, artifact in dossier.get("artifacts", {}).items():
        path = (root/artifact["path"]).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or artifact.get("candidate_sha256") != candidate
                or digest(path.read_bytes()) != artifact.get("sha256")):
            holds.append("Missing, changed or wrong-candidate artifact: " + key)
        else:
            verified.add(key)

    def evidence(record: Data) -> bool:
        ids = record.get("evidence")
        return isinstance(ids, list) and bool(ids) and all(key in verified for key in ids)

    def decision(record: Data, purpose: str) -> bool:
        return bool(record.get("purpose") == purpose and record.get("approved") is True
                    and record.get("by") and record.get("candidate_sha256") == candidate
                    and utc(record["at"]) <= now() and evidence(record))

    scope = dossier.get("scope")
    scope_record = dossier.get("scope_decision", {})
    if scope not in {"rules-only", "ai-enabled"}:
        holds.append("An explicit candidate scope is required.")
    exemptions = set(scope_record.get("criterion_exemptions", []))
    if scope == "rules-only":
        if not decision(scope_record, "rules-only-scope") or not exemptions <= AI_ONLY:
            holds.append("Rules-only scope or its limited exemptions lack exact-candidate owner approval.")
    elif exemptions:
        holds.append("AI-enabled qualification cannot exempt its AI criteria.")
    criteria = dossier.get("criteria", [])
    ids = [row["id"] for row in criteria]
    if len(ids) != 101 or set(ids) != {f"AC-{n:03}" for n in range(1,102)}:
        holds.append("All 101 distinct criterion dispositions are required.")
    for row in criteria:
        if row.get("verdict") == "FAIL":
            failures.append("Criterion failed: " + row["id"])
        elif row.get("verdict") == "NOT APPLICABLE" and scope == "rules-only" and row["id"] in exemptions:
            continue
        elif row.get("verdict") != "PASS" or not evidence(row):
            holds.append("Criterion lacks complete passing evidence: " + row["id"])
    checks = dossier.get("engineering", {})
    for name in sorted(ENGINEERING):
        record = checks.get(name, {})
        if record.get("verdict") == "FAIL":
            failures.append("Engineering check failed: " + name)
        elif record.get("verdict") != "PASS" or not evidence(record):
            holds.append("Engineering evidence missing: " + name)
    safety = dossier.get("safety", {})
    if type(safety.get("attempted_operations")) is not int or safety["attempted_operations"] <= 0 or not evidence(safety):
        holds.append("Safety observations and operation denominator are required.")
    for field in ("unauthorized_effects", "duplicate_effects", "unsupported_ai_outputs", "unresolved_unheld"):
        value = safety.get(field)
        if type(value) is not int or value < 0:
            holds.append("Missing safety observation: " + field)
        elif value:
            failures.append("Observed safety failure: " + field)
    workflow = dossier.get("workflow")
    if workflow is None:
        holds.append("The applicable independently observed workflow comparison is missing.")
    else:
        validate_workflow_report(workflow["plan"], workflow["observations"], workflow["report"])
        if workflow["plan"]["scope"] != scope or not evidence(workflow):
            holds.append("Workflow scope or evidence does not match this candidate.")
        if workflow["report"]["assessment"] != "PASS":
            failures.append("Workflow M07/M08 target failed.")
    started = utc(dossier["evaluation_started"])
    builders = dossier.get("builders", [])
    appointments = dossier.get("appointments", [])
    required_roles = {"domain", "second"} | ({"custodian"} if scope == "ai-enabled" else set())
    accepted = {}
    if not builders:
        holds.append("Builder identities are required to check independence.")
    for row in appointments:
        person, role = row.get("person"), row.get("role")
        if (not person or person in builders or row.get("independent") is not True
                or row.get("candidate_sha256") != candidate or utc(row["appointed"]) > started
                or not started <= utc(row["reviewed"]) <= now()
                or row.get("accepted") is not True or not evidence(row) or role in accepted):
            holds.append("Independent appointment/review record is incomplete or inconsistent.")
        else:
            accepted[role] = row
    if not required_roles <= accepted.keys() or len({r["person"] for r in accepted.values()}) != len(accepted):
        holds.append("Required independent reviewers must be named, distinct and appointed before evaluation.")
    adjudication = dossier.get("adjudication", {})
    if (adjudication.get("reviewer") != accepted.get("second", {}).get("person")
            or adjudication.get("resolved") is not True or not evidence(adjudication)):
        holds.append("Second-reviewer adjudication evidence is required.")
    if scope == "ai-enabled":
        custody = dossier.get("custody", {})
        if custody.get("contaminated") is True:
            failures.append("Held-out contamination invalidates qualifying evidence.")
        if (custody.get("contaminated") is not False or custody.get("builder_access_denied") is not True
                or not custody.get("development_version") or not custody.get("heldout_version") or not evidence(custody)):
            holds.append("Independent held-out custody/access evidence is missing.")
        evaluation = dossier.get("evaluation")
        if evaluation is None:
            holds.append("Full paired live-model evaluation evidence is missing.")
        else:
            validate_report(evaluation["plan"], evaluation["ledger"], evaluation["report"], attempts=evaluation["attempts"])
            report = score(evaluation["plan"], evaluation["ledger"], attempts=evaluation["attempts"])
            if (evaluation.get("registered_plan_sha256") != digest(encode(evaluation["plan"]))
                    or utc(evaluation["registered_at"]) > started
                    or not {"dataset","rubric","prompt","context","response_schema","rules","reference"} <= report["versions"].keys()):
                holds.append("Frozen pre-evaluation registration and complete versions are required.")
            if report["qualification"] == "FAIL":
                failures.append("Model output safety evidence failed.")
            if report["completed_primary_slots"] != 720 or evaluation["plan"].get("budget_interrupted"):
                holds.append("An incomplete or budget-interrupted AI run is diagnostic only.")
            if not evidence(evaluation) or evaluation.get("live_authorized") is not True:
                holds.append("Approved live evaluation evidence is required.")
            budget = evaluation.get("budget")
            if budget is None or evaluation.get("development_spend") is None:
                holds.append("Complete authorized budget and actual development spend are required.")
            else:
                ceiling = authorize(budget)
                spent = positive_cost(evaluation["development_spend"]) + sum(
                    (positive_cost(row["cost"]) for row in evaluation["attempts"]), Decimal(0))
                if spent > ceiling:
                    failures.append("Total recorded inference spend exceeds the authorized ceiling.")
            case_ceiling = positive_cost(evaluation["plan"].get("case_cost_ceiling", "0.05"))
            if case_ceiling != Decimal("0.05"):
                approval = evaluation.get("case_ceiling_approval", {})
                if not decision(approval, "case-cost-ceiling") or utc(approval["at"]) > started:
                    holds.append("A different per-case cost ceiling needs explicit approval before evaluation.")
            for model in report["models"]:
                stress = {r["case"] for r in evaluation["attempts"] if r["model"] == model and r["cohort"] == "stress"}
                if len(stress) != 100:
                    holds.append(model + ": registered stress evidence is incomplete.")
                model_holds, model_failures = ai_targets(report, model, case_ceiling)
                holds.extend(model_holds)
                failures.extend(model_failures)
    owner = dossier.get("owner_release", {})
    if (not decision(owner, "final-release") or utc(owner["at"]) < started
            or any(utc(owner["at"]) < utc(r["reviewed"]) for r in accepted.values())):
        holds.append("Separate final owner release approval for this exact candidate is missing.")
    return {
        "candidate_sha256": candidate, "scope": scope,
        "documentary_verdict": "FAIL" if failures else "HOLD" if holds else "PASS",
        "release_authorized": False, "reasons": failures + holds,
        "limitation": "Checks record consistency and artifact identity, not signatory authenticity or semantic truth. Never deploys or grants release authority.",
    }


def ai_targets(report: Data, model: str, case_ceiling: Decimal = Decimal("0.05")) -> tuple[list[str], list[str]]:
    holds, failures = [], []
    overall = report["models"][model]["overall"]
    baseline = report["rules_baseline"]["overall"]["M01"]
    if baseline is None:
        holds.append(model + ": frozen rules baseline missing.")
    for name, threshold, minimum in (("M01", .95, 120), ("M02", .8, 30), ("M05", .95, 1)):
        metric = overall[name]
        if metric["case_n"] < minimum:
            holds.append(model + ": " + name + " sample is inconclusive.")
        elif metric["case_rate"] < threshold:
            failures.append(model + ": " + name + " target failed.")
    if baseline is not None and overall["M01"]["case_rate"] < baseline["case_rate"]:
        failures.append(model + ": correctness decreased from rules-only.")
    if any(report["models"][model][s]["M01"]["case_rate"] < .9 for s in STRATA):
        failures.append(model + ": stratum correctness target failed.")
    unnecessary = overall["M05"]["unnecessary_rate"]
    if unnecessary is None:
        holds.append(model + ": resolvable-case abstention evidence missing.")
    elif unnecessary > .2:
        failures.append(model + ": unnecessary abstention target failed.")
    for cohort in ("primary", "stress"):
        attempts = report["attempts"][model][cohort]
        if not attempts["M03"]["outputs"] or attempts["M06"]["schema_rate"] is None:
            holds.append(model + ": " + cohort + " output evidence incomplete.")
        elif (attempts["M04"]["rate"] is not None and attempts["M04"]["rate"] < 1) or attempts["M06"]["schema_rate"] > .01:
            failures.append(model + ": " + cohort + " support/schema target failed.")
        latency = attempts["M09"]
        if not latency["diagnosis_wall_evidence_complete"] or latency["average_cost_per_diagnosis"] is None:
            holds.append(model + ": " + cohort + " latency/cost evidence incomplete.")
        elif (latency["provider_attempt_p95_seconds"] > 20 or latency["diagnosis_max_seconds"] > 45
              or Decimal(latency["average_cost_per_diagnosis"]) > case_ceiling):
            failures.append(model + ": " + cohort + " latency/cost target failed.")
    return holds, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Check documentary gates without granting release authority.")
    parser.add_argument("dossier", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    result = assess(object_json(args.dossier.read_bytes()), args.dossier.parent)
    args.report.write_bytes(encode(result) + b"\n")


if __name__ == "__main__":
    main()
