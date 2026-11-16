"""Frozen six-participant workflow arithmetic; not a substitute for observation."""

from itertools import product
from math import ceil, isfinite
from statistics import median

from .contracts import Data, digest, encode
from .evaluation import STRATA

COMPARISONS = {"rules-only": ("rules-only", "manual"), "ai-enabled": ("ai-assisted", "rules-only")}


def workflow_score(plan: Data, observations: list[Data]) -> Data:
    scope = plan.get("scope")
    if scope not in COMPARISONS or plan.get("conditions") != list(COMPARISONS[scope]):
        raise ValueError("The workflow comparator must match the proposed release scope.")
    candidate, comparator = COMPARISONS[scope]
    participants, assignments = plan["participants"], plan["assignments"]
    if (len(participants) != 6 or len(set(participants)) != 6 or len(assignments) != 72
            or len({a["id"] for a in assignments}) != 72 or not plan.get("rubric_version")
            or (scope == "rules-only" and not plan.get("manual_procedure_version"))):
        raise ValueError("Freeze six participants, 72 assignments, rubric and applicable manual procedure.")
    first_conditions = []
    for participant in participants:
        tasks = sorted((a for a in assignments if a["participant"] == participant), key=lambda a: a["position"])
        if (len(tasks) != 12 or {a["position"] for a in tasks} != set(range(1, 13))
                or len({a["case"] for a in tasks}) != 12):
            raise ValueError("Each participant requires 12 ordered tasks without repeated underlying cases.")
        for condition in (candidate, comparator):
            selected = [a for a in tasks if a["condition"] == condition]
            if len(selected) != 6 or {a["stratum"] for a in selected} != STRATA:
                raise ValueError("Each participant requires six balanced tasks per condition.")
        for stratum in STRATA:
            if len({a["difficulty"] for a in tasks if a["stratum"] == stratum}) != 1:
                raise ValueError("Paired task difficulty must be frozen and matched.")
        if len({a["condition"] for a in tasks[:6]}) != 1 or len({a["condition"] for a in tasks[6:]}) != 1:
            raise ValueError("Use the frozen counterbalanced two-block condition order.")
        first_conditions.append(tasks[0]["condition"])
    if first_conditions.count(candidate) != 3 or first_conditions.count(comparator) != 3:
        raise ValueError("Counterbalance the six participants equally between condition orders.")
    actual = {r["id"]: r for r in observations}
    if len(actual) != len(observations) or set(actual) != {a["id"] for a in assignments}:
        raise ValueError("Every assigned task needs one observation, including abandoned and timed-out tasks.")
    joined = []
    for task in assignments:
        row = actual[task["id"]]
        seconds = row.get("active_seconds")
        if (row.get("status") not in {"complete", "abandoned", "timeout"}
                or type(row.get("correct")) is not bool
                or (row["status"] != "complete" and row["correct"])
                or not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not isfinite(seconds) or not 0 <= seconds <= 600
                or any(type(row.get(k)) is not int or row[k] < 0 for k in ("manual_edits", "help_requests"))):
            raise ValueError("Invalid observed time, completion, adjudicated correctness or effort.")
        joined.append({**task, **{k: row[k] for k in ("status", "correct", "manual_edits", "help_requests")},
                       "seconds": seconds if row["status"] == "complete" else 600})
    results: Data = {}
    for condition in (candidate, comparator):
        tasks = [r for r in joined if r["condition"] == condition]
        complete = [r for r in tasks if r["status"] == "complete"]
        correct = sum(r["correct"] for r in tasks)
        results[condition] = {
            "assigned": len(tasks), "completed": len(complete), "correct": correct,
            "abandoned": sum(r["status"] == "abandoned" for r in tasks),
            "timeouts": sum(r["status"] == "timeout" for r in tasks),
            "median_active_seconds": median(r["seconds"] for r in tasks),
            "correct_completion": correct / len(tasks),
            "completed_correctness": correct / len(complete) if complete else None,
            "median_manual_edits": median(r["manual_edits"] for r in complete) if complete else None,
            "help_requests": sum(r["help_requests"] for r in tasks),
        }
    c, b = results[candidate], results[comparator]
    reduction = 1 - c["median_active_seconds"]/b["median_active_seconds"] if b["median_active_seconds"] else None
    correctness = c["completed_correctness"] is not None and b["completed_correctness"] is not None and c["completed_correctness"] >= b["completed_correctness"]
    effort = c["median_manual_edits"] is not None and b["median_manual_edits"] is not None and c["median_manual_edits"] <= b["median_manual_edits"]
    paired = []
    for participant in participants:
        medians = {condition: median(r["seconds"] for r in joined if r["participant"] == participant
                                     and r["condition"] == condition) for condition in (candidate, comparator)}
        paired.append({"participant": participant, "medians": medians,
                       "candidate_minus_comparator_seconds": medians[candidate] - medians[comparator]})
    differences = [p["candidate_minus_comparator_seconds"] for p in paired]
    bootstrap = sorted(sum(sample)/6 for sample in product(differences, repeat=6))
    m07 = reduction is not None and reduction >= .25 and correctness
    m08 = c["correct_completion"] >= .9 and correctness and effort
    return {
        "scope": scope, "qualification": "HOLD", "assessment": "PASS" if m07 and m08 else "FAIL",
        "conditions": results, "M07": {"assessment": "PASS" if m07 else "FAIL", "median_time_reduction": reduction},
        "M08": {"assessment": "PASS" if m08 else "FAIL"}, "paired_participants": paired,
        "paired_mean_difference_seconds": sum(differences)/6,
        "paired_mean_difference_interval": [bootstrap[ceil(.025*len(bootstrap))-1], bootstrap[ceil(.975*len(bootstrap))-1]],
        "interval_method": "Exact enumeration of all 6^6 participant-cluster bootstrap resamples; 95% percentile interval.",
        "limitations": "Synthetic or self-reviewed inputs do not establish observed independent acceptance. Six participants provide directional pilot evidence only.",
        "input_sha256": digest(encode({"plan": plan, "observations": observations})),
    }


def validate_workflow_report(plan: Data, observations: list[Data], report: Data) -> None:
    if encode(workflow_score(plan, observations)) != encode(report):
        raise ValueError("Workflow report differs from frozen assignments or observed outcomes.")
