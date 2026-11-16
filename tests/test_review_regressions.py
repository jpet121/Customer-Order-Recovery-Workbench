from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from conftest import Client, CUSTOMER, bundle, running


def prepare(analyst, peer):
    status, job = analyst.call("POST", "/api/intake", bundle(CUSTOMER))
    assert status == 201, job
    path = "/api/cases/" + analyst.call("GET", "/api/cases")[1]["cases"][0]["id"]
    assert analyst.call("POST", path + "/claim", {"claim_revision": 0})[0] == 200
    status, correction = analyst.call("POST", path + "/preview", {
        "revision": 0, "claim_revision": 1, "patch": {}, "reason": "Approved reference",
        "evidence": ["steward-C100"]
    })
    assert status == 200, correction
    revision = {"revision": 1, "preview_hash": correction["preview"]["hash"]}
    assert peer.call("POST", path + "/approve", revision)[0] == 200
    return job, path, revision


def test_auditor_cannot_download_raw_and_access_denial_is_audited(application):
    from iew.audit_chain import verify

    url, _ = application
    analyst = Client(url).login("analyst")
    auditor = Client(url).login("auditor")
    _, job = analyst.call("POST", "/api/intake", bundle(CUSTOMER))
    assert auditor.call("GET", f"/api/intake/{job['id']}/csv")[0] == 403
    status, audit = auditor.call("GET", "/api/audit.csv", raw=True)
    assert status == 200 and verify(audit)
    assert b"request_denied" in audit
    assert not verify(audit.replace(b"request_denied", b"request_permit"))


def test_malformed_filename_has_typed_error(application):
    url, _ = application
    client = Client(url).login("analyst")
    code, response = client.call("POST", "/api/intake", {**bundle(CUSTOMER), "file_name": []})
    assert code == 400 and response["code"] == "INTAKE_FIELDS"


def test_revocation_races_with_case_action_without_deadlock(application):
    url, _ = application
    first, admin = Client(url).login("analyst"), Client(url).login("admin")
    assert first.call("POST", "/api/intake", bundle(CUSTOMER))[0] == 201
    case = first.call("GET", "/api/cases")[1]["cases"][0]
    with ThreadPoolExecutor(2) as pool:
        mutation = pool.submit(first.call, "POST", f"/api/cases/{case['id']}/claim", {"claim_revision": 0})
        revocation = pool.submit(admin.call, "POST", "/api/users", {"name":"analyst","roles":["analyst"],"active":False})
        assert revocation.result()[0] == 200
        assert mutation.result()[0] in {200,401}
    assert first.call("GET", "/api/cases")[0] == 401


def test_long_throttle_stays_held_and_a_replacement_attempt_journal_is_rejected(database):
    from iew.api import WorkbenchServer
    from iew.contracts import now
    from iew.delivery import Delivery, JOURNAL
    from iew.simulator import Simulator, SimulatorServer
    from iew.store import Store

    store, sim = Store(database[0]), Simulator(database[1])
    store.initialize()
    sim.initialize()
    store.add_user("analyst", "cedar", ["analyst"], "test password only")
    store.add_user("peer", "cedar", ["approver"], "test password only")
    sim.original("cedar", "cedar-v1", "customer", "C100", "original-a", now()-timedelta(days=1), "fenced_no_write")
    sim.fault("cedar", "cedar-v1", "customer", "C100", "throttle_90")
    token = "isolated-test-service-token"
    with running(SimulatorServer(("127.0.0.1", 0), sim, token)) as dest:
        engine = Delivery(store, database[2], dest, token)
        engine.initialize()
        with running(WorkbenchServer(("127.0.0.1", 0), store, engine)) as url:
            analyst, peer = Client(url).login("analyst"), Client(url).login("peer")
            _, path, revision = prepare(analyst, peer)
            assert peer.call("POST", path + "/send", revision)[0] == 200
            assert engine.tick()
            held = peer.call("GET", path)[1]
            assert held["status"] == "Held" and held["retry_at"] and held["attempts"] == 1
            assert peer.call("POST", path + "/retry", revision)[1]["code"] == "RETRY_DELAY"
            with engine.journal.connect() as db:
                db.execute("UPDATE delivery_journal_identity SET journal_id=gen_random_uuid()")
            assert peer.call("POST", path + "/retry", revision)[1]["code"] == "SEND_HELD"
            assert sim.effect_count() == 0
