"""Local policy accounting for controlled provider-attempt tests."""

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from .contracts import Data, Problem
from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS inference_budget (
  plan text PRIMARY KEY, ceiling numeric NOT NULL, used numeric NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inference_reservation (
  id uuid PRIMARY KEY, plan text NOT NULL REFERENCES inference_budget(plan),
  diagnosis uuid NOT NULL, attempt integer NOT NULL CHECK(attempt IN (1,2)),
  worst numeric NOT NULL, usage numeric, UNIQUE(plan,diagnosis,attempt)
);
"""


def positive_cost(value: str) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError
        cost = Decimal(value)
        if not cost.is_finite() or cost < 0 or cost > 10000:
            raise ValueError
        return cost
    except (InvalidOperation, TypeError, ValueError) as error:
        raise Problem(422, "COST", "Supply verified nonnegative cost values.") from error


def authorize(plan: Data) -> Decimal:
    components = plan.get("components")
    if (not isinstance(components, dict) or set(components) != {"development","primary","stress","retries"}
            or plan.get("primary_slots") != 720 or plan.get("stress_cases") != 100
            or plan.get("approved_terms") is not True or plan.get("priced_configuration") is not True
            or plan.get("separate_call_authorization") is not True):
        raise Problem(423, "PLAN", "Complete pricing and separate authorization are required in the controlled test plan.")
    ceiling = positive_cost(plan.get("ceiling", "50"))
    total = sum((positive_cost(value) for value in components.values()), Decimal(0))
    if (ceiling != 50 and plan.get("documented_owner_revision") is not True) or total > ceiling:
        raise Problem(423, "AFFORDABILITY", "The full priced plan is above its authorized ceiling.")
    return ceiling


class Reservations:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.execute(SCHEMA)

    def reserve(self, plan: str, authorization: Data, diagnosis: str, attempt: int, worst: str) -> str:
        ceiling = authorize(authorization)
        cost = positive_cost(worst)
        if type(attempt) is not int or attempt not in (1,2):
            raise Problem(409, "RETRY_LIMIT", "At most one transport retry is allowed.")
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='1s'")
            db.execute("INSERT INTO inference_budget VALUES (%s,%s,0) ON CONFLICT DO NOTHING", (plan, ceiling))
            profile = db.execute("SELECT ceiling,used FROM inference_budget WHERE plan=%s FOR UPDATE", (plan,)).fetchone()
            assert profile is not None
            prior = db.execute("SELECT attempt FROM inference_reservation WHERE plan=%s AND diagnosis=%s ORDER BY attempt",
                               (plan, diagnosis)).fetchall()
            if [row["attempt"] for row in prior] != list(range(1, attempt)):
                raise Problem(409, "ATTEMPT_ORDER", "Only the next allowed attempt may reserve budget.")
            if ceiling != profile["ceiling"] or cost + profile["used"] > ceiling:
                raise Problem(423, "BUDGET", "Worst-case reservation exceeds the remaining authorized budget.")
            key = uuid4()
            db.execute("INSERT INTO inference_reservation (id,plan,diagnosis,attempt,worst) VALUES (%s,%s,%s,%s,%s)",
                       (key, plan, diagnosis, attempt, cost))
            db.execute("UPDATE inference_budget SET used=used+%s WHERE plan=%s", (cost, plan))
            return str(key)

    def account(self, reservation: str, recorded_usage: str | None) -> None:
        if recorded_usage is None:
            return
        actual = positive_cost(recorded_usage)
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='1s'")
            result = db.execute("SELECT plan,worst,usage FROM inference_reservation WHERE id=%s", (reservation,)).fetchone()
            if not result:
                raise Problem(404, "RESERVATION", "No matching provider attempt.")
            db.execute("SELECT 1 FROM inference_budget WHERE plan=%s FOR UPDATE", (result["plan"],))
            row = db.execute("SELECT worst,usage FROM inference_reservation WHERE id=%s FOR UPDATE", (reservation,)).fetchone()
            assert row is not None
            if row["usage"] is not None or actual > row["worst"]:
                raise Problem(409, "USAGE", "Usage conflicts with the recorded reservation.")
            db.execute("UPDATE inference_reservation SET usage=%s WHERE id=%s", (actual, reservation))
            db.execute("UPDATE inference_budget SET used=used-%s WHERE plan=%s", (row["worst"]-actual, result["plan"]))
