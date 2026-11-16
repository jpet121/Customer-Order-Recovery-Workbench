import json
import logging
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from typing import Any
from uuid import uuid4

from psycopg import Error, sql
from psycopg.types.json import Jsonb

from . import delivery, review
from .contracts import Data, Problem, digest, encode, now, stamp, utc
from .store import DB, audit, organization, permit

SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_checkpoint (
  org text PRIMARY KEY REFERENCES org_controls(org), checkpoint jsonb NOT NULL
);
"""


def checkpoint(engine: delivery.Delivery, org: str, prior: int | None = None) -> tuple[Data, list[Data]]:
    with engine.journal.connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        identity = db.execute("SELECT journal_id FROM delivery_journal_identity").fetchone()
        if identity is None:
            raise Problem(423, "JOURNAL_IDENTITY", "External delivery journal is missing.")
        entries = db.execute("SELECT * FROM delivery_attempts WHERE identity->>'org'=%s ORDER BY seq", (org,)).fetchall()
        head = db.execute("SELECT entry_count,hash FROM delivery_heads WHERE org=%s", (org,)).fetchone()
    chain = delivery.EMPTY_JOURNAL
    for entry in entries:
        chain = delivery.journal_link(chain, entry)
    if (entries and head is None) or (head is not None and (head["entry_count"] != len(entries) or head["hash"] != chain)):
        raise Problem(423, "JOURNAL_COVERAGE", "The outside-backup journal is incomplete or changed.")
    limit = (entries[-1]["seq"] if entries else 0) if prior is None else prior
    prefix = [{key: value for key, value in row.items() if key != "reserved"} for row in entries if row["seq"] <= limit]
    return {"journal": str(identity["journal_id"]), "seq": limit, "count": len(prefix),
            "prefix": digest(encode(prefix))}, entries


BACKUP_INTERVAL_SECONDS = 60 * 60


def purge_expired(engine: delivery.Delivery, target: Path) -> None:
    cutoff = now() - timedelta(days=7)
    for entry in sorted(target.glob("iew-*.json")):
        old = json.loads(entry.read_bytes())
        if utc(old["created"]) > cutoff or old["dump"] != entry.with_suffix(".pgdump").name:
            continue
        detail = {"checkpoint": entry.name, "hash": old["digest"], "backup_created": old["created"]}
        def record_purge(event: str) -> None:
            with engine.store.connect() as db:
                for org in old["organizations"]:
                    audit(db, {"name": "backup-operator", "org": org}, event, detail)
        record_purge("backup_purge_started")
        (target / old["dump"]).unlink(missing_ok=True)
        entry.unlink()
        record_purge("backup_purged")


def backup(engine: delivery.Delivery, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    prefix = "iew-" + now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    dump, manifest = target / (prefix + ".pgdump"), target / (prefix + ".json")
    temporary = manifest.with_suffix(".tmp")
    with engine.store.connect() as lock:
        # Serialize operator backups and expiry so one schedule cannot prune
        # the same archive while another process is recording its removal.
        lock.execute("SELECT pg_advisory_xact_lock(61849, 1)")
        purge_expired(engine, target)
        try:
            with engine.store.connect() as db:
                orgs = db.execute("SELECT org FROM org_controls ORDER BY org FOR UPDATE").fetchall()
                points = {row["org"]: checkpoint(engine, row["org"])[0] for row in orgs}
                captured = now()
                postgres("pg_dump", engine.store.dsn, "--format=custom", "--no-owner", "--no-privileges", "--file", postgres_path(dump))
                body = {"version": 1, "created": stamp(now()), "captured_at": stamp(captured), "dump": dump.name,
                        "digest": digest(dump.read_bytes()), "organizations": points}
                temporary.write_bytes(encode(body))
                temporary.replace(manifest)
        except (Error, OSError, RuntimeError, ValueError):
            temporary.unlink(missing_ok=True)
            if not manifest.exists():
                dump.unlink(missing_ok=True)
            raise
    return manifest


def run_hourly_backups(engine: delivery.Delivery, target: Path, stopped: Event) -> None:
    due = monotonic()
    while not stopped.wait(max(0, due - monotonic())):
        try:
            print(backup(engine, target), flush=True)
        except (Error, OSError, RuntimeError, ValueError) as error:
            logging.error("scheduled_backup_failed=%s", type(error).__name__)
        elapsed = monotonic() - due
        due += BACKUP_INTERVAL_SECONDS * (1 + max(0, int(elapsed // BACKUP_INTERVAL_SECONDS)))


def postgres_path(path: Path) -> str:
    value = str(path.absolute())
    # Native Windows tools need an extended path even when Python accepts it.
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return value


def postgres(program: str, dsn: str, *arguments: str) -> None:
    path = os.getenv("IEW_PG_BIN")
    command = str((Path(path) / program).with_suffix(".exe")) if path and os.name == "nt" else str(Path(path) / program) if path else program
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    config = conninfo_to_dict(dsn)
    password = config.pop("password", None)
    environment = os.environ.copy()
    if password is not None:
        environment["PGPASSWORD"] = str(password)
    destination = ["--dbname", make_conninfo(**{k: str(v) for k,v in config.items() if v is not None})] if dsn else []
    result = subprocess.run([command, *destination, *arguments],
                            capture_output=True, check=False, env=environment)
    if result.returncode:
        raise RuntimeError(f"{program} failed with exit status {result.returncode}; verify local database configuration.")


def restore(engine: delivery.Delivery, manifest: Path) -> Data:
    data = json.loads(manifest.read_bytes())
    if data["version"] != 1 or not timedelta(0) <= now() - utc(data["created"]) < timedelta(days=7):
        raise ValueError("Backup schema is unsupported, future-dated or expired.")
    if data["dump"] != manifest.with_suffix(".pgdump").name:
        raise ValueError("Backup filename does not match the manifest.")
    dump = manifest.parent / data["dump"]
    if digest(dump.read_bytes()) != data["digest"]:
        raise ValueError("Backup digest mismatch.")
    with engine.store.connect() as db:
        existing = db.execute(
            """SELECT count(*) AS n FROM pg_catalog.pg_tables
               WHERE schemaname NOT IN ('pg_catalog','information_schema')""").fetchone()
        if existing and existing["n"]:
            raise ValueError("Restore requires a new empty, isolated workbench database.")
    with TemporaryDirectory(prefix="iew-restore-") as folder:
        scripts = Path(folder)
        commands, guards = scripts / "restore.sql", scripts / "guards.sql"
        postgres("pg_restore", "", "--exit-on-error", "--no-owner", "--no-privileges", "--file", postgres_path(commands), postgres_path(dump))
        statements = ["SET LOCAL search_path=public,pg_catalog;", SCHEMA, "DELETE FROM sessions;",
            "UPDATE org_controls SET access_hold=true,delivery_hold=true,hold_reason='Restored backup requires retention and attempt reconciliation.';"]
        with engine.store.connect() as db:
            for org, point in data["organizations"].items():
                update = sql.SQL("INSERT INTO recovery_checkpoint VALUES ({},{}::jsonb) ON CONFLICT(org) DO UPDATE SET checkpoint=EXCLUDED.checkpoint;").format(
                    sql.Literal(org), sql.Literal(encode(point).decode()))
                statements.append(update.as_string(db))
        guards.write_text("\n".join(statements), encoding="utf-8")
        postgres("psql", engine.store.dsn, "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--single-transaction",
                 "--file", postgres_path(commands), "--file", postgres_path(guards))
    return {"access_hold": True, "delivery_hold": True}


def open_delivery(db: DB, actor: Data, engine: delivery.Delivery) -> Data:
    permit(actor, "administrator")
    org = actor["org"]
    control = organization(db, org, lock=True)
    if control["access_hold"]:
        raise Problem(423, "RESTORE_ACCESS", "Apply verified retention before delivery recovery.")
    saved = db.execute("SELECT checkpoint FROM recovery_checkpoint WHERE org=%s", (org,)).fetchone()
    if not saved or not control["delivery_hold"]:
        raise Problem(409, "RECOVERY_STATE", "No restorable checkpoint is awaiting delivery reconciliation.")
    old = saved["checkpoint"]
    if not engine.journal_matches(db):
        raise Problem(423, "JOURNAL_IDENTITY", "Restore requires the original outside-backup journal.")
    current, attempts = checkpoint(engine, org, old["seq"])
    if current != old:
        raise Problem(423, "JOURNAL_COVERAGE", "The external journal lacks complete backup-boundary evidence.")
    counts: dict[bytes, int] = {}
    affected: dict[str, tuple[Data, Data, Data]] = {}
    uncertain = db.execute("SELECT attempt FROM outbox WHERE org=%s AND state IN ('Sending','Outcome unknown')", (org,)).fetchall()
    unresolved = {row["attempt"] for row in uncertain}
    for entry in attempts:
        identity = entry["identity"]
        key = encode(identity)
        counts[key] = counts.get(key, 0) + 1
        if entry["number"] != counts[key]:
            raise Problem(423, "JOURNAL_COVERAGE", "The external journal has a gap in attempt numbering.")
        if entry["seq"] <= old["seq"] and entry["attempt"] not in unresolved:
            continue
        case = db.execute(
            """SELECT * FROM cases WHERE org=%s AND profile=%s AND kind=%s AND business_key=%s FOR UPDATE""",
            (org, identity["profile"], identity["kind"], identity["business_key"])).fetchone()
        if not case:
            raise Problem(423, "RECOVERY_CASE", "An external attempt has no matching restored case. Preserve the pilot hold.")
        result = engine.lookup(case, entry)
        verdict = result.get("verdict")
        if verdict == "committed" and not engine.confirmed(result, entry["hash"], entry["operation"]):
            raise Problem(423, "RECOVERY_RECEIPT", "A restored attempt has conflicting receipt evidence.")
        if verdict not in {"committed", "fenced_no_write"}:
            raise Problem(423, "RECOVERY_OUTCOME", "A restored attempt has no authoritative outcome.")
        case_id = str(case["id"])
        if case_id in affected and affected[case_id][2]["verdict"] == "committed":
            raise Problem(423, "RECOVERY_EFFECT", "A later attempt follows an already confirmed destination write.")
        affected[case_id] = (case, entry, result)
        unresolved.discard(entry["attempt"])
    if unresolved:
        raise Problem(423, "JOURNAL_COVERAGE", "A restored in-flight attempt is missing from the external journal.")
    for case, entry, result in affected.values():
        state = case["data"]
        state.update(attempts=entry["number"], operation=entry["operation"])
        if result["verdict"] == "committed":
            engine.set_confirmed(db, actor, case, result, control["reference"], original=False,
                                 dispatch_approval=entry["dispatch_evidence"])
            final_state = "Confirmed"
        else:
            final_state = "Held"
            state.update(status=final_state, approval=None, delivery_reason="Restored no-write attempt accounted for. Revalidate before Send.")
            review.write_case(db, actor, case, "restored_attempt_accounted", {"attempt": entry["attempt"], "number": entry["number"]})
        db.execute("UPDATE outbox SET state=%s,attempt=%s,reserved=%s WHERE case_id=%s",
                   (final_state, entry["attempt"], entry["number"], case["id"]))
    db.execute("DELETE FROM recovery_checkpoint WHERE org=%s", (org,))
    db.execute("UPDATE org_controls SET delivery_hold=false,hold_reason=NULL WHERE org=%s", (org,))
    audit(db, actor, "delivery_recovery_verified", {"attempts": len(attempts), "checkpoint": old["seq"]})
    return {"delivery_hold": False, "attempts_accounted": len(attempts)}
