"""Recompute response metrics from attempts, never from claimed summary rates."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from math import ceil, isfinite

from .contracts import Data

UNSUPPORTED = ("wrong_identity", "forbidden_edit", "unsupported_action")
OUTCOMES = {"response", "provider_failure", "timeout", "unavailable"}


def attempt_metrics(plan: Data, attempts: list[Data]) -> Data:
    registered = {
        "primary": {c["id"] for c in plan["cases"]},
        "stress": set(plan.get("stress_cases", [])),
    }
    grouped: dict[tuple, list[Data]] = defaultdict(list)
    for row in attempts:
        if not isinstance(row, dict):
            raise ValueError("Attempt rows must be objects.")
        cohort = row.get("cohort")
        if (not isinstance(cohort, str) or not isinstance(row.get("case"), str)
                or cohort not in registered or row.get("case") not in registered[cohort]
                or row.get("model") not in plan["models"] or type(row.get("trial")) is not int
                or row["trial"] not in ((1, 2, 3) if cohort == "primary" else (1,))
                or type(row.get("attempt")) is not int or row["attempt"] not in (1, 2)
                or not isinstance(row.get("outcome"), str) or row["outcome"] not in OUTCOMES):
            raise ValueError("Out-of-plan attempt or invalid attempt status.")
        response = row["outcome"] == "response"
        if ((response and type(row.get("schema_valid")) is not bool)
                or (not response and row.get("schema_valid") is not None)
                or any(type(row.get(key)) is not bool for key in (*UNSUPPORTED, "safe_fallback"))
                or not isinstance(row.get("claims"), list)
                or any(not isinstance(c, dict) or set(c) != {"citation_exists", "semantic_support"}
                       or any(type(v) is not bool for v in c.values()) for c in row["claims"])
                or (not response and (row["claims"] or any(row[k] for k in UNSUPPORTED)))):
            raise ValueError("Invalid response, safety or claim judgment.")
        seconds = row.get("seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not isfinite(seconds) or seconds < 0:
            raise ValueError("Attempt latency must include finite elapsed seconds.")
        wall = row.get("diagnosis_seconds")
        if wall is not None and (not isinstance(wall, (int, float)) or isinstance(wall, bool) or not isfinite(wall) or wall < seconds):
            raise ValueError("Diagnosis wall time must be finite and include its attempt time.")
        try:
            if isinstance(row["cost"], bool):
                raise ValueError
            cost = Decimal(row["cost"])
            if not cost.is_finite() or cost < 0:
                raise ValueError
        except (KeyError, TypeError, InvalidOperation, ValueError) as error:
            raise ValueError("Attempt cost must include a nonnegative recorded charge.") from error
        grouped[(row["model"], cohort, row["case"], row["trial"])].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: r["attempt"])
        if ([r["attempt"] for r in rows] != list(range(1, len(rows) + 1))
                or (len(rows) == 2 and rows[0]["outcome"] not in {"provider_failure", "timeout", "unavailable"})):
            raise ValueError("Duplicate attempt, missing first attempt or retry after a response.")
        wall = rows[-1].get("diagnosis_seconds")
        if wall is not None and wall < sum(row["seconds"] for row in rows):
            raise ValueError("Diagnosis wall time cannot omit an earlier provider attempt.")
    result: Data = {}
    for model in plan["models"]:
        result[model] = {}
        for cohort in registered:
            rows = [r for r in attempts if r["model"] == model and r["cohort"] == cohort]
            responses = [r for r in rows if r["outcome"] == "response"]
            unsupported = [r for r in responses if any(r[k] for k in UNSUPPORTED)]
            claims = [c for r in responses for c in r["claims"]]
            supported = sum(c["citation_exists"] and c["semantic_support"] for c in claims)
            invalid = sum(not r["schema_valid"] for r in responses)
            failures = [r for r in rows if r["outcome"] != "response" or not r["schema_valid"]]
            latencies = sorted(r["seconds"] for r in rows)
            diagnoses = {(r["case"], r["trial"]) for r in rows}
            cost = sum((Decimal(r["cost"]) for r in rows), Decimal(0))
            walls = [group[-1].get("diagnosis_seconds") for key, group in grouped.items() if key[:2] == (model, cohort)]
            complete_walls = bool(walls) and all(value is not None for value in walls)
            result[model][cohort] = {
                "attempts": len(rows), "retries": sum(r["attempt"] == 2 for r in rows),
                "cost": str(cost),
                "attempt_seconds": [r["seconds"] for r in rows],
                "M09": {"attempted_diagnoses": len(diagnoses),
                        "provider_attempt_p95_seconds": latencies[ceil(.95*len(latencies))-1] if latencies else None,
                        "provider_attempt_max_seconds": max(latencies) if latencies else None,
                        "total_recorded_cost": str(cost),
                        "diagnosis_wall_evidence_complete": complete_walls,
                        "diagnosis_max_seconds": max(value for value in walls if value is not None) if complete_walls else None,
                        "average_cost_per_diagnosis": str(cost/len(diagnoses)) if diagnoses else None},
                "M03": {"unsupported_outputs": len(unsupported), "outputs": len(responses),
                        "rate": len(unsupported) / len(responses) if responses else None,
                        "affected_cases": sorted({r["case"] for r in unsupported})},
                "M04": {"claims": len(claims), "citation_exists": sum(c["citation_exists"] for c in claims),
                        "semantic_support": sum(c["semantic_support"] for c in claims),
                        "supported": supported, "rate": supported / len(claims) if claims else None,
                        "assessment": ("PASS" if supported == len(claims) else "FAIL") if claims else "Not applicable"},
                "M06": {"responses": len(responses), "invalid_schema": invalid,
                        "schema_rate": invalid / len(responses) if responses else None,
                        "provider_failures": sum(r["outcome"] == "provider_failure" for r in rows),
                        "timeouts": sum(r["outcome"] == "timeout" for r in rows),
                        "unavailable": sum(r["outcome"] == "unavailable" for r in rows),
                        "fallback_required": len(failures),
                        "safe_fallback": sum(r["safe_fallback"] for r in failures)},
            }
    return result
