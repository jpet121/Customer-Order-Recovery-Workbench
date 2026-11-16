import argparse
import csv
import io
import logging
import os
from datetime import timedelta
from getpass import getpass
from pathlib import Path
from threading import Event, Thread
import psycopg

from .api import WorkbenchServer
from .contracts import Data, digest, encode, now, stamp
from .delivery import Delivery
from .recovery import backup, restore, run_hourly_backups
from .simulator import Simulator, SimulatorServer
from .store import Store

USERS = {"analyst": ["analyst"], "analyst2": ["analyst"], "analyst3": ["analyst"],
         "analyst4": ["analyst"], "approver": ["approver"], "approver2": ["approver"],
         "administrator": ["administrator"], "auditor": ["auditor"]}


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Set {name} in the local operator environment.")
    return value


def configs() -> tuple[Store, Simulator, Delivery]:
    store = Store(require("IEW_WORKBENCH_DSN"))
    simulator = Simulator(require("IEW_SIMULATOR_DSN"))
    target = os.getenv("IEW_SIMULATOR_URL", "http://127.0.0.1:8766")
    credential = require("IEW_SERVICE_TOKEN")
    engine = Delivery(store, require("IEW_JOURNAL_DSN"), target, credential)
    locations = set()
    for component in (store, simulator, engine.journal):
        with component.connect() as db:
            row = db.execute("SELECT current_database() AS name,inet_server_addr()::text AS addr,inet_server_port() AS port").fetchone()
            assert row is not None
            locations.add((row["addr"], row["port"], row["name"]))
    if len(locations) != 3 or len(credential) < 20:
        raise SystemExit("Use three separate PostgreSQL databases and a separate service token of at least 20 characters.")
    return store, simulator, engine


def example(folder: Path, filename: str, kind: str, key: str, attempt: str, rows: list[list[str]], created: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    raw = output.getvalue().encode()
    (folder / filename).write_bytes(raw)
    entry: Data = {f"source_{kind}_id": key,
                   "errors": [{"error_code": "REFERENCE", "message": "Review the imported reference and correction."}]}
    if kind == "order":
        entry["expected_line_numbers"] = [1, 2]
    (folder / "errors.json").write_bytes(encode({"schema_version": 1, "source_profile_id": "cedar-v1",
        "attempt_id": attempt, "file_sha256": digest(raw), "attempted_at": created, "failed_entities": [entry]}) + b"\n")


def seed(store: Store, sim: Simulator, folder: Path, password: str) -> None:
    first = now() - timedelta(hours=1)
    for name, roles in USERS.items():
        with store.connect() as db:
            exists = db.execute("SELECT 1 FROM users WHERE name=%s", (name,)).fetchone()
        if not exists:
            store.add_user(name, "cedar", roles, password)
    records = [("customer", "C100", "demo-customers"), ("order", "O200", "demo-orders")]
    for kind, key, attempt in records:
        with sim.connect() as db:
            exists = db.execute("SELECT 1 FROM sim_original_attempt WHERE org='cedar' AND profile='cedar-v1' AND kind=%s AND business_key=%s AND attempt=%s",
                                (kind, key, attempt)).fetchone()
        if not exists:
            sim.original("cedar", "cedar-v1", kind, key, attempt, first, "fenced_no_write")
    example(folder / "customer", "customers.csv", *records[0], [
        ["source_customer_id","display_name","country_code","postal_code","customer_reference"],
        ["C100","Cedar Valley Maintenance","US","75001","REF-100"]
    ], stamp(first))
    example(folder / "order", "orders.csv", *records[1], [
        ["source_order_id","line_no","customer_reference","order_date","item_reference","quantity",
         "unit_code","unit_price","currency_code","ship_to_reference","dealer_description"],
        ["O200","2","REF-100","2026-11-14","ITEM-1","3.0","EA","7.50","USD","MAIN","Safety gloves"],
        ["O200","1","REF-100","2026-11-14","ALIAS-GLOVE","2","EA","7.50","USD","MAIN","Safety gloves"]
    ], stamp(first))


def work(engine: Delivery, stopped: Event) -> None:
    while not stopped.is_set():
        try:
            if engine.tick():
                continue
        except Exception as error:
            logging.error("worker_error=%s", type(error).__name__)
        stopped.wait(0.6)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="IEW-FRESH local simulator workbench")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("init")
    commands.add_parser("check-compatibility")
    permissions = commands.add_parser("grant-runtime")
    permissions.add_argument("role")
    demo = commands.add_parser("demo")
    demo.add_argument("folder", type=Path)
    service = commands.add_parser("simulator")
    service.add_argument("--port", type=int, default=8766)
    app = commands.add_parser("serve")
    app.add_argument("--port", type=int, default=8765)
    commands.add_parser("worker")
    save = commands.add_parser("backup")
    save.add_argument("folder", type=Path)
    save.add_argument("--hourly", action="store_true", help="start immediately and continue on an hourly cadence")
    load = commands.add_parser("restore")
    load.add_argument("manifest", type=Path)
    args = parser.parse_args()
    store, simulator, engine = configs()
    if args.action == "init":
        try:
            store.initialize()
            simulator.initialize()
            engine.initialize()
        except (psycopg.Error, ValueError):
            raise SystemExit("Database initialization failed; its failed transaction was rolled back. Check schema compatibility and operator permissions. Existing data must not be reset.") from None
        return
    if args.action != "restore":
        try:
            with store.connect() as db:
                store.require_compatible(db)
        except (psycopg.Error, ValueError) as error:
            raise SystemExit("Schema compatibility check failed. Run operator init for a supported schema or restore a compatible isolated backup; do not overwrite existing data. " + type(error).__name__) from None
    if args.action == "check-compatibility":
        print("Workbench schema 1 is compatible. This is not release authorization.")
        return
    if args.action == "grant-runtime":
        from .runtime import grant_runtime
        for component in (store, simulator, engine.journal):
            grant_runtime(component, args.role)
        return
    if args.action == "demo":
        password = os.getenv("IEW_DEMO_PASSWORD") or getpass("Synthetic local user password (at least 12 characters): ")
        seed(store, simulator, args.folder, password)
        return
    if args.action == "backup":
        if args.hourly:
            try:
                run_hourly_backups(engine, args.folder, Event())
            except KeyboardInterrupt:
                pass
        else:
            print(backup(engine, args.folder))
        return
    if args.action == "restore":
        print(restore(engine, args.manifest))
        return
    stopped = Event()
    from .runtime import require_runtime
    try:
        for component in (store, simulator, engine.journal):
            require_runtime(component)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    try:
        if args.action == "worker":
            print("Local delivery worker ready", flush=True)
            work(engine, stopped)
        elif args.action == "simulator":
            with SimulatorServer(("127.0.0.1", args.port), simulator, engine.credential) as server:
                print(f"Local simulator on port {args.port}", flush=True)
                server.serve_forever()
        else:
            with WorkbenchServer(("127.0.0.1", args.port), store, engine) as web:
                worker = Thread(target=work, args=(engine, stopped), daemon=True)
                worker.start()
                print(f"http://127.0.0.1:{args.port}/", flush=True)
                web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()


if __name__ == "__main__":
    main()
