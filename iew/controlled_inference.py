"""Reserved, bounded local experiment seam. No HTTP route or live network adapter."""

from math import isfinite
from time import monotonic, sleep
from typing import Any, Callable

from psycopg.types.json import Jsonb

from .advisory import (
    ATTEMPT_SECONDS, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, invoke_fixture, invoke_prepared_fixture, within_fixture_budget,
)
from .contracts import Data, Problem, fields
from .fixture_serialization import MAX_BYTES, FixtureDeadline, SERIALIZATION_ERRORS, serialize_fixture
from .inference_budget import Reservations, authorize, positive_cost


AUTHORIZATION_FIELDS = frozenset({
    "components", "primary_slots", "stress_cases", "approved_terms", "priced_configuration",
    "separate_call_authorization", "ceiling", "documented_owner_revision",
})
AUTHORIZATION_NAME_LIMIT = max(map(len, AUTHORIZATION_FIELDS))


def snapshot_input(value: Any, cutoff: float) -> Any:
    """Copy plain values while bounding the work before JSON and policy validation."""
    remaining = MAX_INPUT_TOKENS

    def copy(item: Any) -> Any:
        nonlocal remaining
        if monotonic() >= cutoff:
            raise FixtureDeadline
        remaining -= 1
        kind = type(item)
        if kind is str:
            remaining -= len(item)
        elif kind is int:
            remaining -= max(0, (item.bit_length() - 1) // 4)
        if remaining < 0:
            raise ValueError("Input exceeds its safe copy budget.")
        if kind is dict:
            if 2 * len(item) > remaining:
                raise ValueError("Input contains too many object members.")
            return {copy(key): copy(member) for key, member in item.items()}
        if kind is list or kind is tuple:
            if len(item) > remaining:
                raise ValueError("Input contains too many array values.")
            values = [copy(member) for member in item]
            return values if kind is list else tuple(values)
        if any(kind is scalar for scalar in (str, int, float, bool, type(None))):
            return item
        raise ValueError("Input contains a non-JSON value.")

    return copy(value)


def authorization_snapshot(authorization: Data, cutoff: float) -> Data:
    selected = {}
    if type(authorization) is dict:
        for key, value in authorization.items():
            if monotonic() >= cutoff:
                raise FixtureDeadline
            if type(key) is str and len(key) <= AUTHORIZATION_NAME_LIMIT and key in AUTHORIZATION_FIELDS:
                selected[key] = value
                if selected.keys() == AUTHORIZATION_FIELDS:
                    break
    try:
        return snapshot_input(selected, cutoff)
    except (ValueError, TypeError, RuntimeError):
        return {}


class ControlledInference:
    def __init__(self, reservations: Reservations):
        self.reservations = reservations
        with reservations.store.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS inference_outcome (
                  reservation uuid PRIMARY KEY REFERENCES inference_reservation(id),
                  result jsonb NOT NULL, recorded timestamptz NOT NULL DEFAULT clock_timestamp()
                )
            """)

    def run(self, plan: str, authorization: Data, diagnosis: str, worst: str,
            provider: Callable[[Data], Data], context: Data, retry_wait: float = 0) -> Data:
        started = monotonic()
        if type(retry_wait) not in (int, float) or not isfinite(retry_wait) or not 0 <= retry_wait <= 5:
            raise Problem(422, "RETRY_WAIT", "Use a finite retry wait of at most five seconds.")
        deadline = started + 42

        try:
            authorization = authorization_snapshot(authorization, deadline)
            authorize(authorization)
            try:
                worst = snapshot_input(worst, deadline)
            except (ValueError, TypeError, RuntimeError):
                worst = ""
            positive_cost(worst)
            try:
                checked_context = snapshot_input(context, deadline)
            except (ValueError, TypeError, RuntimeError):
                checked_context = None
            if checked_context is None or not within_fixture_budget(checked_context, MAX_INPUT_TOKENS):
                raise Problem(422, "AI_CONTEXT_LIMIT", "Controlled context exceeds the conservative offline envelope.")
            payload = serialize_fixture(provider, checked_context, deadline)
        except (FixtureDeadline, *SERIALIZATION_ERRORS):
            return {"status": "Unavailable", "output": None, "attempts": [], "seconds": monotonic()-started,
                    "reason": "The offline fixture could not be prepared."}

        def execute(_):
            try:
                return self._run(plan, authorization, diagnosis, worst, payload, {}, retry_wait, deadline)
            except Problem as error:
                return {"problem": {"status": error.status, "body": error.body}}
        # Bound database waits as well as inference. Kill the whole owned process
        # tree on expiry; a killed attempt stays reserved until usage is reconciled.
        # The supervisor carries the bounded fixture bytes and a small policy record.
        envelope = invoke_fixture(execute, {}, timeout=44, deadline=started + 44,
                                  payload_limit=MAX_BYTES + 4 * MAX_INPUT_TOKENS)
        if envelope.get("status") != "ok":
            return {"status": "Unavailable", "output": None, "attempts": [], "seconds": monotonic()-started,
                    "reason": "Controlled diagnosis stopped. Inspect durable reservations before retrying."}
        result = envelope["value"]
        if "problem" in result:
            error = result["problem"]
            body = error["body"]
            raise Problem(error["status"], body["code"], body["detail"])
        result["seconds"] = monotonic()-started
        return result

    def _run(self, plan: str, authorization: Data, diagnosis: str, worst: str,
             provider: Callable[[Data], Data] | bytes, context: Data, retry_wait: float, deadline: float) -> Data:
        started = monotonic()
        attempts = []
        for number in (1, 2):
            if monotonic() >= deadline:
                break
            key = self.reservations.reserve(plan, authorization, diagnosis, number, worst)
            begin = monotonic()
            if isinstance(provider, bytes):
                response = invoke_prepared_fixture(provider, ATTEMPT_SECONDS, deadline=deadline)
            else:
                response = invoke_fixture(provider, context, ATTEMPT_SECONDS, deadline=deadline)
            status = response.get("status")
            value: Any = response.get("value")
            if status in {"deadline", "setup_error"}:
                self.reservations.account(key, "0")
            if status == "ok":
                if not fields(value, {"output", "usage"}):
                    status = "unavailable"
                else:
                    self.reservations.account(key, value["usage"])
                    if not within_fixture_budget(value["output"], MAX_OUTPUT_TOKENS):
                        status = "output_limit"
            result = {"attempt": number, "reservation": key, "status": status, "seconds": monotonic()-begin}
            with self.reservations.store.connect() as db:
                db.execute("SET LOCAL statement_timeout='1s'")
                db.execute("INSERT INTO inference_outcome(reservation,result) VALUES (%s,%s)", (key, Jsonb(result)))
            attempts.append(result)
            if status == "ok":
                return {"status": "Offline fixture", "output": value["output"], "attempts": attempts,
                        "seconds": monotonic()-started}
            if status not in {"timeout", "transport_error"}:
                break
            if number == 1:
                if monotonic() + retry_wait >= deadline:
                    break
                sleep(retry_wait)
        return {"status": "Unavailable", "output": None, "attempts": attempts, "seconds": monotonic()-started}
