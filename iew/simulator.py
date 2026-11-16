from datetime import date, datetime
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from time import sleep
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import rules
from .contracts import Data, Problem, digest, encode, fields, object_json, stamp
from .intake import CUSTOMER_FIELDS, DECIMAL, key_valid
from .store import DB, Database

IDENTITY = ("org", "profile", "kind", "business_key")
MODES = {"response_lost_after_commit", "ambiguous_before_write", "throttle_2", "throttle_90", "reject", "response_delay_after_commit", "receipt_without_readback"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_original (
  org text, profile text, kind text, business_key text, first timestamptz NOT NULL,
  PRIMARY KEY (org,profile,kind,business_key)
);
CREATE TABLE IF NOT EXISTS sim_original_attempt (
  org text, profile text, kind text, business_key text, attempt text, result jsonb NOT NULL,
  PRIMARY KEY (org,profile,kind,business_key,attempt)
);
CREATE TABLE IF NOT EXISTS sim_operation (
  operation text PRIMARY KEY, identity jsonb NOT NULL, hash text NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_attempt (
  attempt text PRIMARY KEY, operation text NOT NULL REFERENCES sim_operation(operation), result jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_effect (
  identity jsonb PRIMARY KEY, operation text UNIQUE NOT NULL, hash text NOT NULL,
  payload jsonb NOT NULL, receipt jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_fault (
  identity jsonb PRIMARY KEY, mode text NOT NULL
);
"""


def identity_of(value: Data) -> Data:
    return {key: value[key] for key in IDENTITY}


def valid_wire(kind: str, payload: Any) -> bool:
    if kind == "customer":
        return (fields(payload, set(CUSTOMER_FIELDS))
                and all(isinstance(payload[k], str) and 0 < len(payload[k]) <= 1000 for k in CUSTOMER_FIELDS))
    required = set(rules.ORDER_HEAD) | {"destination_customer_id", "lines"}
    if not fields(payload, required) or any(not key_valid(payload[key]) for key in required - {"lines"}):
        return False
    try:
        if date.fromisoformat(payload["order_date"]).isoformat() != payload["order_date"]:
            return False
    except ValueError:
        return False
    lines = payload["lines"]
    if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
        return False
    seen = 0
    for line in lines:
        if not fields(line, set(rules.ORDER_LINE) | {"line_no"}) or type(line["line_no"]) is not int:
            return False
        if not seen < line["line_no"] <= 100:
            return False
        seen = line["line_no"]
        if any(not key_valid(line[k]) for k in ("item_reference", "unit_code", "ship_to_reference")):
            return False
        for key in ("quantity", "unit_price"):
            value = line[key]
            if not isinstance(value, str) or len(value) > 1000 or not DECIMAL.fullmatch(value) or rules.plain_decimal(value) != value:
                return False
    return True


class Simulator(Database):
    def initialize(self) -> None:
        with self.connect() as db:
            db.execute(SCHEMA)

    def original(self, org: str, profile: str, kind: str, key: str, attempt: str, first: datetime,
                 verdict: str, payload: Data | None = None) -> None:
        identity = dict(zip(IDENTITY, (org, profile, kind, key)))
        with self.connect() as db:
            db.execute("INSERT INTO sim_original VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (org, profile, kind, key, first))
            if verdict == "committed" and payload:
                result = self.commit_effect(db, identity, "original:" + digest(encode([identity, attempt])), digest(encode(payload)), payload)
            elif verdict in {"fenced_no_write", "unknown"}:
                result = {"verdict": verdict}
            else:
                raise ValueError("Provide a valid synthetic original verdict and payload where applicable.")
            db.execute("INSERT INTO sim_original_attempt VALUES (%s,%s,%s,%s,%s,%s)", (org, profile, kind, key, attempt, Jsonb(result)))

    def fault(self, org: str, profile: str, kind: str, key: str, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("Unknown simulator fault.")
        with self.connect() as db:
            db.execute("INSERT INTO sim_fault VALUES (%s,%s) ON CONFLICT(identity) DO UPDATE SET mode=EXCLUDED.mode",
                       (Jsonb(dict(zip(IDENTITY, (org, profile, kind, key)))), mode))

    def effect_count(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT count(*) AS n FROM sim_effect").fetchone()
            return row["n"] if row else 0

    def commit_effect(self, db: DB, identity: Data, key: str, hash_: str, payload: Data) -> Data:
        receipt = {"id": uuid4().hex, "operation": key, "hash": hash_,
                   "destination_id": ("C-" if identity["kind"] == "customer" else "O-") + uuid4().hex[:16]}
        effect = db.execute(
            "INSERT INTO sim_effect VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING operation",
            (Jsonb(identity), key, hash_, Jsonb(payload), Jsonb(receipt))).fetchone()
        if not effect:
            return {"verdict": "conflict"}
        return {"verdict": "committed", "receipt": receipt}

    def current_effect(self, db: DB, identity: Data, result: Data) -> Data:
        if result["verdict"] != "committed":
            return result
        effect = db.execute("SELECT operation,hash,payload,receipt FROM sim_effect WHERE identity=%s",
                            (Jsonb(identity),)).fetchone()
        if not effect or effect["receipt"] != result["receipt"]:
            return {"verdict": "conflict"}
        return {**result, "readback": effect}

    def admission_evidence(self, body: Data) -> Data:
        business_keys = body.get("business_keys")
        if (any(not key_valid(body.get(key)) for key in ("org", "profile", "attempt"))
                or body.get("kind") not in ("customer", "order") or not isinstance(business_keys, list)
                or not 1 <= len(business_keys) <= 25000 or any(not key_valid(key) for key in business_keys)):
            raise Problem(422, "IDENTITY", "Invalid original-import admission scope.")
        with self.connect() as db:
            eligible = db.execute(
                """SELECT a.business_key FROM sim_original_attempt a
                   JOIN sim_original o USING (org, profile, kind, business_key)
                   WHERE a.org=%s AND a.profile=%s AND a.kind=%s AND a.attempt=%s AND a.business_key=ANY(%s)
                     AND a.result->>'verdict'='fenced_no_write' AND o.first <= clock_timestamp()""",
                (body["org"], body["profile"], body["kind"], body["attempt"], business_keys)).fetchall()
        return {"verdict": "ok", "fenced_keys": [row["business_key"] for row in eligible]}

    def handle(self, path: str, body: Data) -> tuple[int, Data]:
        if path == "/v1/original-admission":
            return 200, self.admission_evidence(body)
        if any(not key_valid(body.get(k)) for k in IDENTITY) or body["kind"] not in {"customer", "order"}:
            raise Problem(422, "IDENTITY", "Invalid intended-create identity.")
        identity = identity_of(body)
        if path == "/v1/validate":
            valid = (body.get("contract") == "1" and valid_wire(body["kind"], body.get("payload"))
                     and body["payload"][f"source_{body['kind']}_id"] == body["business_key"])
            return 200, {"verdict": "valid" if valid else "invalid", "errors": [] if valid else ["PAYLOAD_SCHEMA"]}
        with self.connect() as db:
            if path == "/v1/original":
                first = db.execute("SELECT first FROM sim_original WHERE org=%s AND profile=%s AND kind=%s AND business_key=%s",
                                   tuple(identity.values())).fetchone()
                record = db.execute(
                    "SELECT result FROM sim_original_attempt WHERE org=%s AND profile=%s AND kind=%s AND business_key=%s AND attempt=%s",
                    (*identity.values(), body.get("attempt"))).fetchone()
                if not first or not record:
                    return 200, {"identity": identity, "verdict": "unknown"}
                return 200, {**self.current_effect(db, identity, record["result"]), "first": stamp(first["first"]),
                             "identity": identity, "original_attempt": body["attempt"]}
            if path not in {"/v1/lookup", "/v1/submit"}:
                raise Problem(404, "NOT_FOUND", "Simulator operation not found.")
            key, hash_, attempt = body.get("operation"), body.get("hash"), body.get("attempt")
            if any(not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value) for value in (key, hash_, attempt)):
                raise Problem(422, "OPERATION", "Invalid operation, attempt or hash.")
            assert isinstance(key, str) and isinstance(hash_, str) and isinstance(attempt, str)
            db.execute("INSERT INTO sim_operation VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                       (key, Jsonb(identity), hash_))
            bound = db.execute("SELECT * FROM sim_operation WHERE operation=%s FOR UPDATE", (key,)).fetchone()
            assert bound is not None
            if bound["hash"] != hash_ or bound["identity"] != identity:
                return 200, {"verdict": "conflict"}
            found = db.execute("SELECT operation,result FROM sim_attempt WHERE attempt=%s", (attempt,)).fetchone()
            if found:
                if found["operation"] != key:
                    return 200, {"verdict": "conflict"}
                return 200, self.current_effect(db, identity, found["result"])
            existing = db.execute("SELECT operation,hash,receipt FROM sim_effect WHERE identity=%s",
                                  (Jsonb(identity),)).fetchone()
            if existing:
                result = {"verdict": "committed", "receipt": existing["receipt"]} if existing["operation"] == key and existing["hash"] == hash_ else {"verdict": "conflict"}
                self.record_attempt(db, attempt, key, result)
                return 200, self.current_effect(db, identity, result)
            if path == "/v1/lookup":
                result = {"verdict": "fenced_no_write", "retryable": True}
                self.record_attempt(db, attempt, key, result)
                return 200, result
            canonical = body.get("canonical")
            payload: Data = {}
            raw = b""
            if isinstance(canonical, str):
                try:
                    raw = canonical.encode("utf-8")
                    payload = object_json(raw)
                except (ValueError, UnicodeError, RecursionError):
                    pass
            if (body.get("contract") != "1" or not valid_wire(body["kind"], payload)
                    or payload[f"source_{body['kind']}_id"] != body["business_key"]
                    or encode(payload) != raw or digest(raw) != hash_):
                result = {"verdict": "fenced_no_write", "retryable": False, "reason": "PAYLOAD_SCHEMA"}
                self.record_attempt(db, attempt, key, result)
                return 200, result
            assert isinstance(payload, dict)
            fault = db.execute("DELETE FROM sim_fault WHERE identity=%s RETURNING mode", (Jsonb(identity),)).fetchone()
            mode = fault["mode"] if fault else ""
            if mode == "ambiguous_before_write":
                return 503, {"verdict": "unknown"}
            if mode.startswith("throttle"):
                result = {"verdict": "fenced_no_write", "retryable": True, "retry_after": int(mode.split("_")[1])}
            elif mode == "reject":
                result = {"verdict": "fenced_no_write", "retryable": False, "reason": "DEFINITIVE_REJECTION"}
            else:
                result = self.commit_effect(db, identity, key, hash_, payload)
            self.record_attempt(db, attempt, key, result)
            if mode == "response_lost_after_commit":
                return 503, {"verdict": "unknown"}
            if mode == "response_delay_after_commit":
                return 200, {**self.current_effect(db, identity, result), "delay_test_response": True}
            if mode == "receipt_without_readback":
                return 200, result
            return 200, self.current_effect(db, identity, result)

    def record_attempt(self, db: DB, attempt: Any, key: Any, result: Data) -> None:
        db.execute("INSERT INTO sim_attempt VALUES (%s,%s,%s)", (attempt, key, Jsonb(result)))


class SimulatorServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], simulator: Simulator, credential: str):
        if address[0] != "127.0.0.1" or len(credential) < 20:
            raise ValueError("Bind the simulator to loopback with a separate service credential.")
        self.simulator = simulator
        self.credential = credential
        super().__init__(address, SimulatorHandler)


class SimulatorHandler(BaseHTTPRequestHandler):
    server: SimulatorServer

    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:
        self.connection.settimeout(15)
        value = self.headers.get("Authorization", "")
        if not compare_digest(value.encode(), f"Bearer {self.server.credential}".encode()):
            self.reply(403, {"code": "SERVICE_IDENTITY"})
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= size <= 4 * 1024 * 1024:
                raise Problem(413, "SIZE", "Simulator envelope exceeded.")
            body = object_json(self.rfile.read(size))
            if self.path == "/v1/health":
                with self.server.simulator.connect() as db:
                    db.execute("SELECT 1")
                code, result = 200, {"verdict": "ready"}
            else:
                code, result = self.server.simulator.handle(self.path, body)
        except Problem as error:
            code, result = error.status, error.body
        except (ValueError, RecursionError):
            code, result = 422, {"code": "JSON"}
        if result.pop("delay_test_response", False):
            sleep(10)
        try:
            self.reply(code, result)
        except (ConnectionError, OSError):
            return

    def reply(self, code: int, result: Data) -> None:
        raw = encode(result)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
