from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
import os

import psycopg
from psycopg import sql

from conftest import Client, CUSTOMER, bundle, running


@contextmanager
def isolated_databases():
    names = ["iew_test_" + uuid4().hex for _ in range(4)]
    dsn = os.environ["IEW_TEST_DSN"]
    with psycopg.connect(dsn, autocommit=True) as db:
        try:
            for name in names:
                db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            yield [psycopg.conninfo.make_conninfo(dsn, dbname=name) for name in names]
        finally:
            for name in names:
                db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_restore_holds_access_then_delivery_and_accounts_for_later_attempt(tmp_path: Path):
    from iew.api import WorkbenchServer
    from iew.contracts import now
    from iew.delivery import Delivery
    from iew.recovery import backup, restore
    from iew.simulator import Simulator, SimulatorServer
    from iew.store import Store

    with isolated_databases() as databases:
        store, restored, sim = Store(databases[0]), Store(databases[3]), Simulator(databases[1])
        store.initialize()
        sim.initialize()
        store.add_user("analyst", "cedar", ["analyst"], "test password only")
        store.add_user("peer", "cedar", ["approver"], "test password only")
        store.add_user("admin", "cedar", ["administrator"], "test password only")
        sim.original("cedar", "cedar-v1", "customer", "C100", "original-a", now() - timedelta(days=1), "fenced_no_write")
        credential = "isolated-test-service-token"
        with running(SimulatorServer(("127.0.0.1", 0), sim, credential)) as destination:
            engine = Delivery(store, databases[2], destination, credential)
            engine.initialize()
            with running(WorkbenchServer(("127.0.0.1", 0), store, engine)) as url:
                analyst, peer = Client(url).login("analyst"), Client(url).login("peer")
                assert analyst.call("POST", "/api/intake", bundle(CUSTOMER))[0] == 201
                path = "/api/cases/" + analyst.call("GET", "/api/cases")[1]["cases"][0]["id"]
                assert analyst.call("POST", path + "/claim", {"claim_revision": 0})[0] == 200
                code, result = analyst.call("POST", path + "/preview", {"revision": 0, "claim_revision": 1,
                    "patch": {}, "reason": "Approved source", "evidence": ["steward-C100"]})
                assert code == 200, result
                revision = {"revision": 1, "preview_hash": result["preview"]["hash"]}
                manifest = backup(engine, tmp_path)
                assert peer.call("POST", path + "/approve", revision)[0] == 200
                assert peer.call("POST", path + "/send", revision)[0] == 200
                engine.tick()
                assert sim.effect_count() == 1
            recovered = Delivery(restored, databases[2], destination, credential)
            blocked = restore(recovered, manifest)
            assert blocked == {"access_hold": True, "delivery_hold": True}
            assert recovered.tick() is False
            with running(WorkbenchServer(("127.0.0.1", 0), restored, recovered)) as url:
                admin, peer = Client(url).login("admin"), Client(url).login("peer")
                assert peer.call("GET", "/api/cases")[0] == 423
                assert admin.call("GET", "/api/audit.csv")[0] == 423
                assert admin.call("POST", "/api/retention", {})[0] == 200
                assert peer.call("GET", path)[0] == 200
                assert peer.call("POST", path + "/send", revision)[0] == 409
                assert recovered.tick() is False
                from psycopg.types.json import Jsonb
                with recovered.journal.connect() as db:
                    missing = db.execute("DELETE FROM delivery_attempts RETURNING *").fetchone()
                assert admin.call("POST", "/api/recovery", {})[1]["code"] == "JOURNAL_COVERAGE"
                assert admin.call("GET", "/api/status")[1]["delivery_hold"]
                with recovered.journal.connect() as db:
                    db.execute("""INSERT INTO delivery_attempts
                      (seq,identity,number,operation,hash,attempt,dispatch_evidence,reserved) OVERRIDING SYSTEM VALUE
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (missing["seq"],Jsonb(missing["identity"]),missing["number"],missing["operation"],missing["hash"],
                       missing["attempt"],Jsonb(missing["dispatch_evidence"]),missing["reserved"]))
                status, result = admin.call("POST", "/api/recovery", {})
                assert status == 200 and result["attempts_accounted"] == 1
                state = peer.call("GET", path)[1]
                assert state["status"] == "Confirmed" and state["attempts"] == 1
                assert state["binding"]["reference"] == "REF-100"
                assert sim.effect_count() == 1
