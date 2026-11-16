import csv
import io
import re
from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import audit_chain
from .contracts import Data, Problem, encode, fields, now, stamp, utc
from .store import DB, audit, organization, permit

def group_keys(issues: list[Data] | None, errors: list[Data] | None) -> set[tuple[str, str]]:
    return {(issue["code"], issue["field"]) for issue in issues or []} | {
        (entry["error_code"], "source") for entry in errors or []}


def history(db: DB, actor: Data, after: int = 0) -> list[Data]:
    rows = db.execute(
        """SELECT seq, actor, case_id, event, detail, recorded FROM audit
           WHERE org=%s AND seq>%s ORDER BY seq LIMIT 500""", (actor["org"], after)).fetchall()
    allowed = audit_chain.SAFE | {"prior", "claimant", "reason"}
    for row in rows:
        row["detail"] = {k: audit_chain.sanitize(v) if isinstance(v, str) and audit_chain.CREDENTIAL.search(v) else v
                         for k,v in row["detail"].items() if k in allowed and isinstance(v, (str,int,bool,type(None)))}
    return rows


def audit_export(db: DB, actor: Data) -> bytes:
    permit(actor, "auditor", "approver", "administrator")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(audit_chain.COLUMNS)
    with db.cursor(name="audit_export") as cursor:
        cursor.execute("SELECT * FROM audit WHERE org=%s ORDER BY seq", (actor["org"],))
        for row in cursor:
            data = audit_chain.projection(row["seq"], row["recorded"], row["actor"], row["case_id"], row["event"], row["detail"])
            writer.writerow([*data, row["previous_hash"], row["hash"]])
    return stream.getvalue().encode("utf-8")


def repair_packet(db: DB, actor: Data, group: Data) -> bytes:
    permit(actor, "analyst", "approver", "auditor")
    items = []
    current = organization(db, actor["org"])["reference"]
    rows = db.execute(
        """SELECT id,data->'issues' AS issues,data->'errors' AS errors,
                  data->'locations' AS locations FROM cases WHERE org=%s AND profile=%s ORDER BY id""",
        (actor["org"], group["profile"])).fetchall()
    for row in rows:
        selected = (group["code"], group["field"])
        if selected in group_keys(row["issues"], row["errors"]):
            items.append({"case_id": str(row["id"]), "field": audit_chain.sanitize(group["field"]),
                          "rule_code": audit_chain.sanitize(group["code"]), "rows": row["locations"] or [],
                          "proposed_action": "Review this failure and correct the upstream source or approved mapping."})
    return encode({"version": 1, "contract": current["contract"], "reference": current["version"],
                   "profile": audit_chain.sanitize(group["profile"]), "cases": items})


def account_bundle(db: DB, actor: Data, job_id: Any) -> Data:
    rows = db.execute(
        """SELECT cases.id,cases.kind,data->>'status' AS status,data->'receipt' AS receipt,
                  data->'readback' AS readback,data->'attempts' AS attempts
           FROM cases JOIN bundle_cases ON cases.id=bundle_cases.case_id
           WHERE bundle_cases.bundle=%s AND org=%s ORDER BY cases.id""", (job_id, actor["org"])).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"counts": counts, "unresolved": sum(count for status,count in counts.items() if status != "Confirmed"),
            "confirmations": [{"case_id":str(row["id"]), "receipt":row["receipt"], "readback":row["readback"],
                               "attempts":row["attempts"] or 0} for row in rows if row["status"] == "Confirmed"]}


def user_admin(db: DB, actor: Data, body: Data) -> Data:
    from .store import ROLES, password_digest
    from secrets import token_hex

    permit(actor, "administrator")
    name, active, roles = body.get("name"), body.get("active"), body.get("roles")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or type(active) is not bool:
        raise Problem(422, "USER", "Supply a valid user name and active status.")
    if not isinstance(roles, list) or not roles or any(not isinstance(r, str) or r not in ROLES for r in roles):
        raise Problem(422, "USER_ROLES", "Select recognized roles.")
    if name == actor["name"] and (not active or "administrator" not in roles):
        raise Problem(409, "ADMIN_SELF", "An administrator cannot revoke their own administrative account.")
    password = body.get("password")
    if password is not None and (not isinstance(password, str) or not 12 <= len(password) <= 256):
        raise Problem(422, "USER_PASSWORD", "Use 12–256 characters.")
    if password is None:
        result = db.execute(
            """UPDATE users SET roles=%s,active=%s,version=version+1
               WHERE name=%s AND org=%s RETURNING version""",
            (roles, active, name, actor["org"])).fetchone()
    else:
        salt = token_hex(16)
        result = db.execute(
            """INSERT INTO users(name,org,roles,password,active) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (name) DO UPDATE SET roles=EXCLUDED.roles,password=EXCLUDED.password,active=EXCLUDED.active,
               version=users.version+1 WHERE users.org=EXCLUDED.org RETURNING version""",
            (name, actor["org"], roles, f"{salt}:{password_digest(password, salt)}", active)).fetchone()
    if not result:
        raise Problem(404, "USER", "User could not be changed.")
    audit(db, actor, "user_authority_changed", {"revision": result["version"]})
    return {"name": name, "roles": roles, "active": active}


def publish(db: DB, actor: Data, control: Data, ref: Data) -> Data:
    permit(actor, "administrator")
    required = {"version", "created", "contract", "destination", "countries", "currencies", "units",
                "items", "customers", "identities", "excerpts"}
    previous = control["reference"]
    if (not fields(ref, required) or not valid_text(ref["version"]) or ref["version"] == previous["version"]
            or ref["contract"] != previous["contract"] or ref["destination"] != previous["destination"]):
        raise Problem(422, "REFERENCE", "Use a new snapshot version and the configured pilot contract and destination.")
    try:
        age = now() - utc(ref["created"])
        if not timedelta(0) <= age <= timedelta(hours=24):
            raise ValueError
    except ValueError as error:
        raise Problem(422, "REFERENCE_AGE", "Use a verifiably current UTC snapshot.") from error
    if any(not text_list(ref[key]) for key in ("countries","currencies","units")):
        raise Problem(422, "REFERENCE", "Snapshot allowlists require approved values.")
    for name, required_fields, list_key in [
        ("items", {"id","description","units"}, "units"),
        ("customers", {"id","ship_to","blocked"}, "ship_to"),
        ("identities", {"id","reference","version","steward","evidence","ship_to"}, "ship_to")
    ]:
        if not isinstance(ref[name], dict) or any(
                not valid_text(key) or not fields(row, required_fields) or not text_list(row[list_key])
                or any(not valid_text(row[k]) for k in required_fields - {list_key, "blocked"})
                or (name == "customers" and type(row["blocked"]) is not bool)
                for key,row in ref[name].items()):
            raise Problem(422, "REFERENCE", "Invalid versioned reference records.", field=name)
    if not isinstance(ref["excerpts"], dict) or any(not valid_text(k) or not valid_text(v) for k,v in ref["excerpts"].items()):
        raise Problem(422, "REFERENCE", "Excerpts require approved identifiers and text.")
    db.execute("UPDATE org_controls SET reference=%s,policy=policy+1 WHERE org=%s", (Jsonb(ref), actor["org"]))
    db.execute(
        """UPDATE cases SET data=(data - 'approval') ||
           '{"status":"Held","delivery_reason":"Reference version changed. Revalidate the complete case."}'::jsonb
           WHERE org=%s AND data->>'status' IN ('Approved','Pending approval','Queued')""", (actor["org"],))
    db.execute("UPDATE outbox SET state='Held' WHERE org=%s AND state='Queued'", (actor["org"],))
    audit(db, actor, "references_published", {"reference": ref["version"]})
    return {"version": ref["version"]}


def valid_text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 1000 and "\x00" not in value


def text_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(valid_text(item) for item in value)


def retention(db: DB, actor: Data, reviewed_at: Any = None) -> Data:
    permit(actor, "administrator")
    processed_at = reviewed_at if reviewed_at is not None else now()
    org = actor["org"]
    control = organization(db, org, lock=True)
    if control["access_hold"]:
        uncertain = db.execute(
            """SELECT count(*) n FROM cases WHERE org=%s AND
               data->>'status' IN ('Confirmed','Rejected') AND terminal IS NULL""", (org,)).fetchone()
        if uncertain and uncertain["n"]:
            raise Problem(423, "RETENTION_EVIDENCE", "Terminal date is missing for restored evidence.")
    payload_cutoff = processed_at - timedelta(days=30)
    source = db.execute(
        """UPDATE intake i SET csv=NULL,manifest=NULL WHERE org=%s AND created <= %s
           AND (csv IS NOT NULL OR manifest IS NOT NULL)
           AND NOT EXISTS (
               SELECT 1 FROM bundle_cases b JOIN cases c ON c.id=b.case_id
               WHERE b.bundle=i.id AND (c.terminal IS NULL OR c.terminal > %s)
           ) RETURNING i.csv_hash""", (org, payload_cutoff, payload_cutoff)).fetchall()
    purged = db.execute(
        """UPDATE cases SET data=(data - ARRAY['rows','corrected','history','preview','approval','candidate_data',
             'excerpts','errors','locations','original_attempt','receipt','binding','return_reason','issues']) ||
             '{"payload_purged":true}'::jsonb
           WHERE org=%s AND terminal <= %s AND NOT data ? 'payload_purged'
             AND data->>'status' NOT IN ('Sending','Outcome unknown') RETURNING id""",
        (org, payload_cutoff)).fetchall()
    present = db.execute("SELECT to_regclass('outbox') AS table").fetchone()
    if present and present["table"]:
        db.execute(
            """UPDATE outbox SET state='Held', canonical='' WHERE org=%s AND case_id IN
                (SELECT id FROM cases WHERE org=%s AND data ? 'payload_purged') AND state <> 'Sending'""", (org, org))
    for row in source:
        audit(db, actor, "source_payload_purged", {"csv_hash": row["csv_hash"]})
    for row in purged:
        audit(db, actor, "case_payload_purged", {}, row["id"])
    terminal_cutoff = processed_at - timedelta(days=180)
    db.execute("SELECT iew_expire_audit(%s,%s)", (org, processed_at))
    db.execute(
        """DELETE FROM audit_heads WHERE org=%s AND scope IN (
           SELECT id::text FROM cases WHERE org=%s AND terminal <= %s)""", (org, org, terminal_cutoff))
    if present and present["table"]:
        db.execute("DELETE FROM outbox WHERE org=%s AND case_id IN (SELECT id FROM cases WHERE org=%s AND terminal <= %s)",
                   (org, org, terminal_cutoff))
    db.execute("DELETE FROM bundle_cases WHERE case_id IN (SELECT id FROM cases WHERE org=%s AND terminal <= %s)",
               (org, terminal_cutoff))
    db.execute("DELETE FROM cases WHERE org=%s AND terminal <= %s", (org, terminal_cutoff))
    db.execute("UPDATE org_controls SET access_hold=false WHERE org=%s", (org,))
    result = {"purged_files": len(source), "purged_cases": len(purged), "access_hold": False}
    audit(db, actor, "retention_applied", result)
    return result
