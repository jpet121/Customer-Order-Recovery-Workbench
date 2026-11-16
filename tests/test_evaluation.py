from iew.evaluation import STRATA, score


def test_incomplete_trial_stays_in_denominator_and_blocks_qualification():
    cases = [{"id": f"case-{n}", "stratum": stratum, "requires_clarification": n == 0, "semantic_eligible": n < 5}
             for n, stratum in enumerate(s for s in sorted(STRATA) for _ in range(20))]
    ledger = [{"model": "test-a", "case": "case-0", "trial": n, "status": "complete",
               "rubric": {"M01": True, "M02": True, "M05": True}} for n in (1, 2)]
    result = score({"models": ["test-a", "test-b"], "cases": cases}, ledger)
    first = result["models"]["test-a"]["overall"]["M01"]
    assert (first["case_n"], first["case_pass"], first["trial_n"], first["trial_pass"]) == (120, 0, 360, 2)
    assert result["completed_primary_slots"] == 2 and result["qualification"] == "HOLD"
    semantic = result["models"]["test-a"]["overall"]["M02"]
    assert (semantic["case_n"], semantic["trial_n"], semantic["assessment"]) == (5, 15, "Inconclusive")
