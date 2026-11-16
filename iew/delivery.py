import random
from datetime import timedelta
from http.client import HTTPException
from itertools import batched
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import review, rules
from .contracts import Data, Problem, digest, encode, now, object_json, stamp, utc
from .simulator import identity_of
from .store import DB, Database, Store, audit, organization, permit

OUTBOX = """
CREATE TABLE IF NOT EXISTS outbox (
  case_id uuid PRIMARY KEY REFERENCES cases(id), org text NOT NULL,
  operation text NOT NULL, hash text NOT NULL, canonical text NOT NULL, contract text NOT NULL,
  revision integer NOT NULL, state text NOT NULL,
  available timestamptz NOT NULL, sender text NOT NULL, sender_version integer NOT NULL,
  reserved integer, attempt text
);
CREATE INDEX IF NOT EXISTS outbox_frontier ON outbox(org,state,available);
CREATE TABLE IF NOT EXISTS journal_binding (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), id uuid NOT NULL);
"""
JOURNAL = """
CREATE TABLE IF NOT EXISTS delivery_journal_identity (
  journal_id uuid PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS delivery_heads (
  org text PRIMARY KEY, entry_count bigint NOT NULL, hash text NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
  seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  identity jsonb NOT NULL, number integer NOT NULL CHECK (number BETWEEN 1 AND 3),
  operation text NOT NULL, hash text NOT NULL, attempt text UNIQUE NOT NULL,
  dispatch_evidence jsonb NOT NULL,
  reserved timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (identity,number)
);
"""
EMPTY_JOURNAL = digest(b"")


def journal_link(previous: str, row: Data) -> str:
    content = {key: row[key] for key in ("seq", "identity", "number", "operation", "hash", "attempt", "dispatch_evidence")}
    return digest(encode([previous, content]))


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class Delivery:
    def __init__(self, store: Store, journal_dsn: str, url: str, credential: str):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not parsed.port):
            raise ValueError("The local simulator must use an explicit loopback HTTP port.")
        self.store = store
        self.journal = Database(journal_dsn)
        self.url = url.rstrip("/")
        self.credential = credential

    def initialize(self, create_journal: bool = True) -> None:
        from .recovery import SCHEMA
        with self.store.connect() as db:
            db.execute(OUTBOX + SCHEMA)
        if create_journal:
            with self.journal.connect() as journal:
                journal.execute(JOURNAL)
                journal.execute("LOCK TABLE delivery_journal_identity IN EXCLUSIVE MODE")
                journal.execute("INSERT INTO delivery_journal_identity SELECT %s WHERE NOT EXISTS (SELECT 1 FROM delivery_journal_identity)", (uuid4(),))
                identity = journal.execute("SELECT journal_id FROM delivery_journal_identity").fetchone()
                assert identity is not None
            with self.store.connect() as db:
                db.execute("INSERT INTO journal_binding (id) VALUES (%s) ON CONFLICT DO NOTHING", (identity["journal_id"],))
                if not self.journal_matches(db):
                    raise ValueError("Configured journal differs from the journal bound to this workbench.")

    def journal_matches(self, db: DB) -> bool:
        from psycopg import Error

        expected = db.execute("SELECT id FROM journal_binding").fetchone()
        try:
            with self.journal.connect() as journal:
                actual = journal.execute("SELECT journal_id FROM delivery_journal_identity").fetchone()
        except Error:
            return False
        return bool(expected and actual and expected["id"] == actual["journal_id"])


    def http(self, action: str, body: Data) -> Data:
        request = Request(f"{self.url}/v1/{action}", encode(body), {
            "Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"
        })
        try:
            with build_opener(ProxyHandler({}), RejectRedirect()).open(request, timeout=5) as response:
                return object_json(response.read(4 * 1024 * 1024))
        except HTTPError as error:
            error.close()
            return {"verdict": "unauthorized" if error.code in {401, 403} else "unknown"}
        except (URLError, TimeoutError, OSError, ValueError, HTTPException):
            return {"verdict": "unknown"}

    def journal_rows(self, case: Data) -> list[Data]:
        with self.journal.connect() as db:
            return db.execute("SELECT * FROM delivery_attempts WHERE identity=%s ORDER BY number",
                              (Jsonb(identity_of(case)),)).fetchall()

    def original_admission(self, org: str, kind: str, manifest: Data, entries: dict[str, Data]) -> None:
        # 8,000 maximum-length JSON keys fit in both 4 MiB HTTP envelopes.
        for keys in batched(entries, 8000):
            evidence = self.http("original-admission", {"org": org, "profile": manifest["source_profile_id"],
                "kind": kind, "attempt": manifest["attempt_id"], "business_keys": keys})
            reported = evidence.get("fenced_keys")
            fenced = set()
            if evidence.get("verdict") == "ok" and isinstance(reported, list):
                fenced = {key for key in reported if isinstance(key, str)}
            for key in keys:
                if key not in fenced:
                    entries[key]["original_delivery_pending"] = True

    def original(self, case: Data) -> Data:
        identity = identity_of(case)
        source = case["data"]
        result = self.http("original", {**identity, "attempt": source["original_attempt"]})
        if result.get("identity") != identity or result.get("original_attempt") != source["original_attempt"]:
            return {"verdict": "unknown"}
        try:
            first = utc(result.get("first"))
        except ValueError:
            return {"verdict": "unknown"}
        if first > now() or (source.get("first_original") and result["first"] != source["first_original"]):
            return {"verdict": "unknown"}
        return result

    def check(self, db: DB, case: Data, control: Data, sender: str, sender_version: int) -> str | None:
        state, ref = case["data"], control["reference"]
        if control["access_hold"] or control["delivery_hold"]:
            return "Restore recovery holds delivery pilot-wide."
        if control["paused"]:
            return "New dispatch is paused."
        if not self.journal_matches(db):
            return "The original outside-backup attempt journal could not be verified."
        user = db.execute("SELECT version,roles,active FROM users WHERE name=%s AND org=%s FOR SHARE",
                          (sender, case["org"])).fetchone()
        if not user or not user["active"] or "approver" not in user["roles"] or user["version"] != sender_version:
            return "Sender authority changed."
        if not review.current_approval(case, control, db):
            return "Approval expired or a bound version or authority changed."
        issues, binding = review.evaluate(db, case, state["corrected"], ref)
        approval = state["approval"]
        if issues or binding != approval["binding"]:
            return "Current references or customer dependency no longer match the preview."
        if case["kind"] == "customer" and rules.identity(ref, case["business_key"], state["corrected"][0]["customer_reference"]) != approval["identity"]:
            return "Approved identity evidence changed."
        original = self.original(case)
        if original["verdict"] != "fenced_no_write":
            return "Prove the original import did not write and cannot still commit."
        cutoff = utc(original["first"]) + timedelta(days=180)
        state.update(first_original=original["first"], replay_cutoff=stamp(cutoff),
                     original_verdict=original["verdict"], original_proof_hash=digest(encode(original)))
        if now() >= cutoff:
            return "The verified 180-day replay window has expired."
        return None

    def send(self, db: DB, actor: Data, case: Data, control: Data, body: Data, retry: bool = False) -> Data:
        permit(actor, "approver")
        state = case["data"]
        review.expect_revision(state, body)
        expected = {"Rejected", "Held"} if retry else {"Approved"}
        if state.get("attempts", 0) >= 3:
            expected.add("Held")
        if state["status"] not in expected or not state.get("approval") or body.get("preview_hash") != state["preview"]["hash"]:
            raise Problem(409, "SEND_STATE", "Current approval and eligible case state required.")
        user = db.execute("SELECT version FROM users WHERE name=%s", (actor["name"],)).fetchone()
        assert user is not None
        reason = self.check(db, case, control, actor["name"], user["version"])
        if reason:
            raise Problem(409, "SEND_HELD", reason)
        if retry:
            pending = db.execute("SELECT available FROM outbox WHERE case_id=%s", (case["id"],)).fetchone()
            if pending and pending["available"] > now():
                raise Problem(409, "RETRY_DELAY", "Wait for the destination retry delay to elapse.")
        rows = self.journal_rows(case)
        if len(rows) < state.get("attempts", 0):
            raise Problem(409, "JOURNAL_COVERAGE", "An earlier delivery attempt is missing from the journal.")
        if len(rows) >= 3:
            raise Problem(409, "ATTEMPTS", "All three workbench attempts have been used.")
        preview = state["preview"]
        operation = digest(encode([identity_of(case), preview["hash"]]))
        for row in rows:
            result = self.lookup(case, row)
            if result["verdict"] != "fenced_no_write":
                raise Problem(409, "ATTEMPT_UNRESOLVED", "Finish reconciliation of earlier attempts.")
        state.pop("delivery_uncertain", None)
        db.execute(
            """INSERT INTO outbox(case_id,org,operation,hash,canonical,contract,revision,state,available,sender,sender_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'Queued',clock_timestamp(),%s,%s)
               ON CONFLICT (case_id) DO UPDATE SET operation=EXCLUDED.operation,hash=EXCLUDED.hash,
               canonical=EXCLUDED.canonical,contract=EXCLUDED.contract,revision=EXCLUDED.revision,
               state=EXCLUDED.state,available=EXCLUDED.available,sender=EXCLUDED.sender,sender_version=EXCLUDED.sender_version,
               reserved=NULL,attempt=NULL""",
            (case["id"], case["org"], operation, preview["hash"], preview["canonical"],
             control["reference"]["contract"], state["revision"], actor["name"], user["version"]))
        state.update(status="Queued", operation=operation, attempts=len(rows), delivery_reason=None)
        return review.write_case(db, actor, case, "queued", {"operation": operation, "revision": state["revision"],
                                 "original_proof_hash": state["original_proof_hash"]})

    def lookup(self, case: Data, record: Data) -> Data:
        return self.http("lookup", {**identity_of(case), **{k: record[k] for k in ("operation", "hash", "attempt")}})

    def confirmed(self, result: Data, hash_: str, operation: str | None = None) -> bool:
        receipt, readback = result.get("receipt"), result.get("readback")
        return bool(isinstance(receipt, dict) and isinstance(readback, dict) and receipt.get("id")
                    and receipt.get("destination_id") and receipt.get("hash") == hash_
                    and receipt.get("operation") == readback.get("operation")
                    and (operation is None or receipt["operation"] == operation)
                    and receipt == readback.get("receipt") and readback.get("hash") == hash_
                    and digest(encode(readback.get("payload"))) == hash_)

    def set_confirmed(self, db: DB, actor: Data, case: Data, result: Data, ref: Data, original: bool,
                      dispatch_approval: Data | None = None) -> Data:
        state = case["data"]
        state.pop("unverified_receipt", None)
        state.pop("delivery_uncertain", None)
        state.pop("original_delivery_pending", None)
        state.update(status="Confirmed", receipt=result["receipt"], delivery_reason=None)
        state["readback"] = {"operation": result["readback"]["operation"], "hash": result["readback"]["hash"]}
        if case["kind"] == "customer":
            reference = result["readback"]["payload"]["customer_reference"]
            approval = dispatch_approval or state.get("approval") or {}
            identity = rules.identity(ref, case["business_key"], reference) if original else approval.get("identity")
            if identity and identity["reference"] == reference:
                state["binding"] = {"id": result["receipt"]["destination_id"], "version": identity["version"],
                                    "reference": reference, "ship_to": identity["ship_to"],
                                    "identity_hash": digest(encode(identity)),
                                    "destination": ref["destination"] if original else approval["destination"],
                                    "blocked": False}
        return review.write_case(db, actor, case, "original_confirmed" if original else "confirmed",
                                 {"receipt": result["receipt"], "receipt_id": result["receipt"]["id"],
                                  "readback_hash": result["readback"]["hash"],
                                  "revision": (dispatch_approval or state)["revision"],
                                  "attempts": state.get("attempts", 0)})

    def reconcile(self, db: DB, actor: Data, case: Data, control: Data) -> Data:
        permit(actor, "analyst", "approver")
        state = case["data"]
        if state["status"] == "Confirmed":
            return review.visible(case)
        row = db.execute("SELECT * FROM outbox WHERE case_id=%s", (case["id"],)).fetchone()
        if row and row["state"] == "Sending":
            raise Problem(409, "DISPATCH_IN_PROGRESS", "Wait for the in-flight request or worker recovery.")
        if row and row["state"] == "Outcome unknown":
            return self.apply_result(db, actor, case, control, row, self.lookup(case, row), readback=True)
        if row and row["state"] == "Queued":
            raise Problem(409, "ALREADY_QUEUED", "This work is queued.")
        original = self.original(case)
        if original.get("first"):
            state.update(first_original=original["first"], replay_cutoff=stamp(utc(original["first"]) + timedelta(days=180)))
        state["original_verdict"] = original["verdict"]
        if original["verdict"] == "committed" and state.get("rows"):
            ref = control["reference"]
            rows = state["rows"]
            _, binding = review.evaluate(db, case, rows, ref)
            if case["kind"] == "customer" or binding:
                canonical = rules.preview_record(case["kind"], rows, binding)
                if self.confirmed(original, canonical["hash"]):
                    return self.set_confirmed(db, actor, case, original, ref, original=True)
            state["original_verdict"] = "conflict"
        if row is None:
            if original["verdict"] == "fenced_no_write":
                if state.pop("original_delivery_pending", False):
                    issues, _ = review.evaluate(db, case, state["rows"], control["reference"])
                    state.update(status="Held" if issues else "Ready for review", issues=issues, delivery_reason=None)
            else:
                state.update(original_delivery_pending=True, status="Held",
                             delivery_reason="Reconcile the original import before editing or sending.")
        return review.write_case(db, actor, case, "original_checked", {"verdict": state["original_verdict"]})

    def cancel(self, db: DB, actor: Data, case: Data, body: Data) -> Data:
        permit(actor, "approver")
        review.expect_revision(case["data"], body)
        row = db.execute("UPDATE outbox SET state='Cancelled' WHERE case_id=%s AND state='Queued' RETURNING case_id",
                         (case["id"],)).fetchone()
        if not row:
            raise Problem(409, "ALREADY_DISPATCHED", "This operation cannot be cancelled. Reconcile a sent operation.")
        case["data"].update(status="Cancelled", delivery_reason="Queued delivery was cancelled before dispatch.")
        return review.write_case(db, actor, case, "cancelled", {})

    def reserve(self, case: Data, outbox: Data, number: int) -> tuple[Data, bool]:
        identity = identity_of(case)
        attempt = digest(encode([identity, number, outbox["operation"]]))
        with self.journal.connect() as db:
            org = identity["org"]
            db.execute("INSERT INTO delivery_heads VALUES (%s,0,%s) ON CONFLICT DO NOTHING", (org, EMPTY_JOURNAL))
            head = db.execute("SELECT * FROM delivery_heads WHERE org=%s FOR UPDATE", (org,)).fetchone()
            assert head is not None
            created = db.execute(
                """INSERT INTO delivery_attempts(identity,number,operation,hash,attempt,dispatch_evidence)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (identity,number) DO NOTHING RETURNING *""",
                (Jsonb(identity), number, outbox["operation"], outbox["hash"], attempt,
                 Jsonb(case["data"]["approval"]))).fetchone()
            if created:
                db.execute("UPDATE delivery_heads SET entry_count=entry_count+1,hash=%s WHERE org=%s",
                           (journal_link(head["hash"], created), org))
                return created, True
            existing = db.execute("SELECT * FROM delivery_attempts WHERE identity=%s AND number=%s",
                                  (Jsonb(identity), number)).fetchone()
            assert existing is not None
            return existing, False

    def tick(self) -> bool:
        with self.store.connect() as db:
            choice = db.execute(
                """SELECT o.org,o.case_id FROM outbox o JOIN org_controls c ON c.org=o.org
                   WHERE NOT c.access_hold AND NOT c.delivery_hold AND
                    (o.state='Sending' AND o.available <= clock_timestamp() OR
                     NOT c.paused AND o.state='Queued' AND o.available <= clock_timestamp())
                   ORDER BY o.available,o.case_id LIMIT 1""").fetchone()
            if not choice:
                return False
            actor = {"name": "workbench-worker", "org": choice["org"], "request_id": uuid4().hex}
            control = organization(db, choice["org"], lock=True)
            if control["access_hold"] or control["delivery_hold"]:
                return False
            case = review.read_case(db, actor, str(choice["case_id"]), lock=True)
            row = db.execute("SELECT * FROM outbox WHERE case_id=%s FOR UPDATE", (case["id"],)).fetchone()
            assert row is not None
            if row["available"] > now():
                return False
            if row["state"] == "Sending":
                self.apply_result(db, actor, case, control, row, self.lookup(case, row), readback=True)
                return True
            if row["state"] != "Queued" or control["paused"]:
                return False
            case["data"]["delivery_reason"] = None
            reason = self.check(db, case, control, row["sender"], row["sender_version"])
            if reason:
                db.execute("UPDATE outbox SET state='Held' WHERE case_id=%s", (case["id"],))
                case["data"].update(status="Held", delivery_reason=reason)
                review.write_case(db, actor, case, "dispatch_held", {"reason": reason})
                return True
            number = case["data"].get("attempts", 0) + 1
            if number > 3:
                db.execute("UPDATE outbox SET state='Held' WHERE case_id=%s", (case["id"],))
                case["data"].update(status="Held", delivery_reason="All three workbench attempts have been used.")
                review.write_case(db, actor, case, "dispatch_held", {"reason": "ATTEMPTS"})
                return True
            reservation, created = self.reserve(case, row, number)
            if reservation["operation"] != row["operation"] or reservation["hash"] != row["hash"]:
                self.hold_pilot(db, actor, "Unmatched durable attempt reservation.")
                return True
            attempt = reservation["attempt"]
            db.execute(
                """UPDATE outbox SET state='Sending',available=clock_timestamp()+interval '15 seconds',
                   attempt=%s,reserved=%s WHERE case_id=%s""", (attempt, number, case["id"]))
            row.update(state="Sending", attempt=attempt, reserved=number)
            case["data"].update(status="Sending", attempts=number)
            review.write_case(db, actor, case, "reserved", {"attempt": attempt, "number": number,
                                                           "operation": row["operation"], "revision": row["revision"]})
            db.commit()
        with self.store.connect() as db:
            control = organization(db, choice["org"], lock=True)
            case = review.read_case(db, actor, str(choice["case_id"]), lock=True)
            current = db.execute("SELECT * FROM outbox WHERE case_id=%s FOR UPDATE", (case["id"],)).fetchone()
            if current and current["state"] == "Sending" and current["attempt"] == attempt:
                reason = self.check(db, case, control, current["sender"], current["sender_version"]) if created else "readback"
                if reason:
                    outcome = self.lookup(case, current)
                else:
                    outcome = self.http("submit", {**identity_of(case),
                        **{k: current[k] for k in ("operation", "hash", "attempt", "contract")},
                        "canonical": current["canonical"]})
                self.apply_result(db, actor, case, control, current, outcome, readback=bool(reason))
        return True

    def hold_pilot(self, db: DB, actor: Data, reason: str) -> None:
        db.execute("UPDATE org_controls SET delivery_hold=true,hold_reason=%s WHERE org=%s", (reason, actor["org"]))
        audit(db, actor, "delivery_hold", {"reason": reason})

    def apply_result(self, db: DB, actor: Data, case: Data, control: Data, row: Data, result: Data, readback: bool) -> Data:
        state = case["data"]
        verdict = result.get("verdict")
        if verdict == "committed" and self.confirmed(result, row["hash"], row["operation"]):
            db.execute("UPDATE outbox SET state='Confirmed' WHERE case_id=%s", (case["id"],))
            return self.set_confirmed(db, actor, case, result, control["reference"], original=False)
        if verdict == "committed" and isinstance(result.get("receipt"), dict):
            state["unverified_receipt"] = result["receipt"]
        if verdict == "conflict" or (verdict == "committed" and result.get("readback")):
            self.hold_pilot(db, actor, "Destination receipt or payload identity conflicts.")
        status, reason, delay = "Outcome unknown", "Read back the authoritative outcome before another attempt.", 0.0
        if verdict == "unauthorized":
            status, reason = "Held", "Service authorization failed. Reconcile the authoritative outcome before editing or retrying."
            state["delivery_uncertain"] = True
        if verdict == "fenced_no_write":
            state.pop("delivery_uncertain", None)
            status, reason = "Rejected", "Simulator proved no write and fenced delayed commit."
            delay = float(result.get("retry_after", (2 if row["reserved"] == 1 else 8) + random.uniform(0, .5)))
            if row["reserved"] >= 3:
                status, reason = "Held", "All three workbench attempts have been used."
            elif result.get("retryable") and delay <= 60 and not control["delivery_hold"] and not control["access_hold"]:
                status, reason = "Queued", "Retry scheduled after authoritative no-write evidence."
            elif delay > 60:
                status, reason = "Held", "The destination requested a delay over 60 seconds. Resume explicitly when it expires."
                state["retry_at"] = stamp(now() + timedelta(seconds=delay))
        db.execute("UPDATE outbox SET state=%s,available=%s WHERE case_id=%s",
                   ("Outcome unknown" if state.get("delivery_uncertain") else status,
                    now() + timedelta(seconds=delay), case["id"]))
        state.update(status=status, delivery_reason=reason)
        return review.write_case(db, actor, case, "readback" if readback else "submit_result",
                                 {"status": status, "attempt": row["attempt"], "verdict": verdict, "revision": row["revision"]})
