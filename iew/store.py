import base64
import binascii
from hashlib import scrypt
from hmac import compare_digest
from pathlib import Path
from secrets import token_hex
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import Data, Problem, digest, fields, now, stamp
from . import audit_chain, intake

type DB = psycopg.Connection[Data]
type PreparedIntake = tuple[str, Data, dict[str, Data]] | Problem | None
ROLES = frozenset({"analyst", "approver", "administrator", "auditor"})


def password_digest(password: str, salt: str) -> str:
    return scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def permit(actor: Data, *roles: str) -> None:
    if not set(actor["roles"]).intersection(roles):
        raise Problem(403, "ROLE", "Your role cannot perform this action.")


def lookup_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as error:
        raise Problem(404, "NOT_FOUND", "Record not found.") from error


def missing() -> Problem:
    return Problem(404, "NOT_FOUND", "Record not found.")


def audit(db: DB, actor: Data, event: str, detail: Data, case_id: UUID | None = None) -> None:
    org, scope = actor["org"], str(case_id or "")
    db.execute("INSERT INTO audit_heads VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
               (org, scope, audit_chain.EMPTY))
    head = db.execute("SELECT hash FROM audit_heads WHERE org=%s AND scope=%s FOR UPDATE", (org, scope)).fetchone()
    numbered = db.execute("SELECT nextval('application_audit_seq') AS seq").fetchone()
    assert head is not None and numbered is not None
    seq, time, previous = numbered["seq"], now(), head["hash"]
    if actor.get("request_id"):
        detail = {**detail, "request_id": actor["request_id"]}
    values = audit_chain.projection(seq, time, actor["name"], case_id, event, detail)
    hash_ = audit_chain.link(previous, values)
    db.execute("INSERT INTO audit VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (seq, org, actor["name"], case_id, event, Jsonb(detail), time, previous, hash_))
    db.execute("UPDATE audit_heads SET hash=%s WHERE org=%s AND scope=%s", (hash_, org, scope))


def organization(db: DB, org: str, lock: bool = False) -> Data:
    query = "SELECT * FROM org_controls WHERE org=%s FOR UPDATE" if lock else "SELECT * FROM org_controls WHERE org=%s"
    row = db.execute(query, (org,)).fetchone()
    if row is None:
        raise missing()
    return row


def audit_cases(db: DB, actor: Data, events: list[tuple[str, Data, UUID]]) -> None:
    """Append one event per distinct case, retaining locks and independent chains."""
    if not events:
        return
    scopes = [str(case_id) for _, _, case_id in events]
    if len(set(scopes)) != len(scopes):
        raise ValueError("Batch audit requires distinct case scopes")
    org = actor["org"]
    db.execute(
        """INSERT INTO audit_heads SELECT %s,scope,%s FROM unnest(%s::text[]) AS scope
           ON CONFLICT DO NOTHING""", (org, audit_chain.EMPTY, scopes))
    heads = {row["scope"]: row["hash"] for row in db.execute(
        """SELECT scope,hash FROM audit_heads WHERE org=%s AND scope=ANY(%s)
           ORDER BY scope FOR UPDATE""", (org, scopes)).fetchall()}
    numbers = db.execute(
        "SELECT nextval('application_audit_seq') AS seq FROM generate_series(1,%s)", (len(events),)).fetchall()
    rows = []
    updates = []
    for (event, detail, case_id), numbered in zip(events, numbers, strict=True):
        if actor.get("request_id"):
            detail = {**detail, "request_id": actor["request_id"]}
        seq, recorded, previous = numbered["seq"], now(), heads[str(case_id)]
        values = audit_chain.projection(seq, recorded, actor["name"], case_id, event, detail)
        hash_ = audit_chain.link(previous, values)
        rows.append({"seq": seq, "case_id": str(case_id), "event": event, "detail": detail,
                     "recorded": stamp(recorded), "previous_hash": previous, "hash": hash_})
        updates.append((hash_, org, str(case_id)))
    db.execute(
        """INSERT INTO audit SELECT seq,%s,%s,case_id,event,detail,recorded,previous_hash,hash
           FROM jsonb_to_recordset(%s) AS e(seq bigint,case_id uuid,event text,detail jsonb,
                                         recorded timestamptz,previous_hash text,hash text)""",
        (org, actor["name"], Jsonb(rows)))
    db.execute(
        """UPDATE audit_heads h SET hash=v.hash FROM unnest(%s::text[],%s::text[]) AS v(scope,hash)
           WHERE h.org=%s AND h.scope=v.scope""", ([r[2] for r in updates], [r[0] for r in updates], org))


def default_reference() -> Data:
    return {
        "version": "cedar-ref-1", "created": stamp(now()), "contract": "1", "destination": "synthetic-cedar",
        "countries": ["US", "CA"], "currencies": ["USD", "CAD"], "units": ["EA", "BOX"],
        "items": {"ITEM-1": {"id": "ITEM-1", "description": "Safety gloves", "units": ["EA", "BOX"]}},
        "customers": {"REF-EXIST": {"id": "D-EXIST", "ship_to": ["MAIN"], "blocked": False}},
        "identities": {"C100": {"id": "identity-C100", "reference": "REF-100", "version": "1",
                                "steward": "synthetic-data-steward", "evidence": "steward-C100",
                                "ship_to": ["MAIN"]}},
        "excerpts": {"BR02": "Use a versioned reference. Quantity and price cannot be edited.",
                     "BR03": "An order requires a confirmed customer and all original lines."}
    }


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self) -> DB:
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)

class Store(Database):
    def initialize(self) -> None:
        from psycopg import sql as query
        sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as db:
            schema = db.execute("SELECT current_schema() AS name").fetchone()
            assert schema is not None
            db.execute(query.SQL("SET LOCAL search_path={},pg_catalog,pg_temp").format(query.Identifier(schema["name"])))
            self.require_compatible(db, allow_unregistered=True)
            db.execute(sql)
            db.execute("""CREATE TABLE IF NOT EXISTS iew_schema_version (
                singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), version integer NOT NULL);
                INSERT INTO iew_schema_version VALUES (true,1) ON CONFLICT DO NOTHING;""")

    def require_compatible(self, db: DB, allow_unregistered: bool = False) -> None:
        present = db.execute("SELECT to_regclass('iew_schema_version') AS name").fetchone()
        if not present or not present["name"]:
            if allow_unregistered:
                return
            raise ValueError("Schema is unregistered. Run operator init before starting services.")
        versions = db.execute("SELECT version FROM iew_schema_version").fetchall()
        if len(versions) != 1 or type(versions[0]["version"]) is not int or versions[0]["version"] != 1:
            raise ValueError("Unsupported schema version. Keep services stopped; use a compatible application or isolated backup restore.")

    def add_user(self, name: str, org: str, roles: list[str], password: str) -> None:
        if not name or not org or not roles or not set(roles) <= ROLES or len(password) < 12:
            raise ValueError("Supply a username, organization, valid role and a password of at least 12 characters.")
        salt = token_hex(16)
        hashed = f"{salt}:{password_digest(password, salt)}"
        with self.connect() as db:
            db.execute("INSERT INTO org_controls (org,reference) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                       (org, Jsonb(default_reference())))
            db.execute("INSERT INTO users (name, org, roles, password) VALUES (%s,%s,%s,%s)",
                       (name, org, roles, hashed))

    def login(self, db: DB, body: Data) -> tuple[str, Data]:
        if not fields(body, {"user", "password"}) or any(
            not isinstance(body[k], str) or not 1 <= len(body[k]) <= 256 or "\x00" in body[k] for k in body
        ):
            raise Problem(400, "LOGIN_FIELDS", "Enter a user and password.")
        user = db.execute("SELECT * FROM users WHERE name=%s FOR SHARE", (body["user"],)).fetchone()
        salt, secret = user["password"].split(":") if user else ("0" * 32, "0" * 128)
        matches = compare_digest(password_digest(body["password"], salt), secret)
        if not user or not user["active"] or not matches:
            raise Problem(401, "LOGIN", "User or password is incorrect.")
        token, csrf = token_hex(32), token_hex(32)
        db.execute("DELETE FROM sessions WHERE touched <= clock_timestamp() - interval '30 minutes'")
        db.execute("INSERT INTO sessions (token,username,user_version,csrf) VALUES (%s,%s,%s,%s)",
                   (digest(token.encode()), user["name"], user["version"], csrf))
        return token, {"name": user["name"], "roles": user["roles"], "csrf": csrf, "org": user["org"]}

    def lock_session_organization(self, db: DB, token: str) -> None:
        row = db.execute(
            "SELECT u.org FROM sessions s JOIN users u ON u.name=s.username WHERE s.token=%s",
            (digest(token.encode()),)).fetchone()
        if row is None:
            raise Problem(401, "SESSION", "Sign in to continue.")
        organization(db, row["org"], lock=True)

    def authenticate(self, db: DB, token: str, csrf: str | None, touch: bool = True) -> Data:
        key = digest(token.encode())
        query = """SELECT u.name,u.org,u.roles,s.csrf FROM sessions s JOIN users u ON u.name=s.username
                   WHERE s.token=%s AND s.user_version=u.version AND u.active
                     AND s.touched > clock_timestamp() - interval '30 minutes'"""
        actor = db.execute(query + (" FOR SHARE OF u" if touch else ""), (key,)).fetchone()
        if not actor:
            raise Problem(401, "SESSION", "Sign in to continue.")
        if csrf is not None and not compare_digest(actor["csrf"], csrf):
            raise Problem(403, "CSRF", "Request could not be authorized.")
        if touch:
            db.execute("UPDATE sessions SET touched=clock_timestamp() WHERE token=%s", (key,))
        return actor

    def start_job(self, db: DB, actor: Data, body: Data) -> Data:
        permit(actor, "analyst")
        if (not fields(body, {"file_name", "profile", "csv", "manifest"})
                or body["profile"] != intake.PROFILE
                or not all(isinstance(body[name], str) for name in ("csv","manifest","file_name"))
                or body["file_name"] not in intake.NAMES):
            raise Problem(400, "INTAKE_FIELDS", "Choose the Cedar Vale profile, a CSV file and errors.json.")
        try:
            csv_bytes, manifest = [base64.b64decode(body[name], validate=True) for name in ("csv", "manifest")]
        except (ValueError, binascii.Error) as error:
            raise Problem(422, "FILE_ENCODING", "Invalid file encoding.") from error
        job = uuid4()
        result = {"id": str(job), "state": "Validating", "revision": 0, "csv_sha256": digest(csv_bytes),
                  "manifest_sha256": digest(manifest), "uploader": actor["name"]}
        over_limit = len(csv_bytes) + len(manifest) > intake.MAX_BYTES
        state = "Rejected" if over_limit else "Validating"
        result["state"] = state
        if over_limit:
            result["diagnostics"] = [intake.bundle_size_error(len(csv_bytes) + len(manifest)).body]
        db.execute(
            """INSERT INTO intake (id,org,actor,name,profile,csv,manifest,csv_hash,manifest_hash,result,state)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (job, actor["org"], actor["name"], body["file_name"], body["profile"],
             csv_bytes if not over_limit else None, manifest if not over_limit else None,
             result["csv_sha256"], result["manifest_sha256"], Jsonb(result), state))
        audit(db, actor, "intake_started" if not over_limit else "intake_rejected", {"job": str(job), "state": state})
        return result

    def job(self, db: DB, actor: Data, key: str, lock: bool = False) -> Data:
        query = "SELECT * FROM intake WHERE id=%s AND org=%s"
        row = db.execute(query + (" FOR UPDATE" if lock else ""), (lookup_id(key), actor["org"])).fetchone()
        if row is None:
            raise missing()
        return row

    def prepare_job(self, db: DB, actor: Data, key: str, revision: int) -> PreparedIntake:
        permit(actor, "analyst")
        job = self.job(db, actor, key)
        if job["actor"] != actor["name"]:
            raise Problem(403, "UPLOADER", "Only the uploader can complete validation.")
        if job["state"] != "Validating" or job["revision"] != revision:
            return None
        if job["csv"] is None or job["manifest"] is None:
            return Problem(410, "PURGED", "The original bundle is no longer available.")
        try:
            return intake.parse(job["name"], job["csv"], job["manifest"])
        except Problem as error:
            return error

    def commit_job(self, db: DB, actor: Data, key: str, revision: int, prepared: PreparedIntake) -> Data:
        permit(actor, "analyst")
        job = self.job(db, actor, key, lock=True)
        if job["actor"] != actor["name"]:
            raise Problem(403, "UPLOADER", "Only the uploader can complete validation.")
        if job["state"] != "Validating" or job["revision"] != revision:
            return job["result"]
        if prepared is None:
            raise Problem(409, "INTAKE_REVISION", "The prepared bundle no longer matches this job revision.")
        if isinstance(prepared, Problem):
            return self.finish_job(db, actor, key, revision, "Rejected", {"diagnostics": [prepared.body], "case_count": 0})
        kind, manifest, entries = prepared
        from .review import admit
        new_count = admit(db, actor, job, kind, manifest, entries)
        return self.finish_job(db, actor, key, revision, "Committed", {
            "kind": kind, "case_count": len(entries), "new_cases": new_count,
            "row_count": sum(len(entry["rows"]) for entry in entries.values()),
            "order_count": len(entries) if kind == "order" else 0,
            "line_count": sum(len(entry["line_numbers"]) for entry in entries.values()), "bundle_id": key
        })

    def finish_job(self, db: DB, actor: Data, key: str, revision: int, state: str, detail: Data) -> Data:
        job = self.job(db, actor, key, lock=True)
        if job["actor"] != actor["name"]:
            raise Problem(403, "UPLOADER", "Only the uploader can change the intake job.")
        if job["state"] != "Validating" or job["revision"] != revision:
            return job["result"]
        result = {**job["result"], **detail, "state": state, "revision": revision + 1}
        db.execute("UPDATE intake SET state=%s,revision=%s,result=%s WHERE id=%s",
                   (state, revision + 1, Jsonb(result), job["id"]))
        audit(db, actor, f"intake_{state.lower()}", {"job": key, "state": state, "case_count": result.get("case_count", 0)})
        return result
