from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from conftest import Client, CUSTOMER, bundle, running


ORDER = (b"source_order_id,line_no,customer_reference,order_date,item_reference,quantity,unit_code,unit_price,currency_code,ship_to_reference\n"
         b"O900,4,REF-EXIST,2026-11-14,ITEM-1,2.00,EA,1.30,USD,MAIN\n"
         b"O900,1,REF-EXIST,2026-11-14,ITEM-1,1.0,EA,1.30,USD,MAIN\n")


def test_exact_noncontiguous_order_lines_and_atomic_rejection(application):
    url, _ = application
    analyst = Client(url).login("analyst")
    good = bundle(ORDER, "order", "O900", lines=[1, 4])
    bad = bundle(ORDER, "order", "O900", lines=[1, 3])
    code, rejection = analyst.call("POST", "/api/intake", bad)
    assert code == 422 and rejection["case_count"] == 0
    assert rejection["diagnostics"][0]["expected"] == [1, 3]
    assert rejection["diagnostics"][0]["received"] == [1, 4]
    assert analyst.call("GET", "/api/cases")[1]["case_count"] == 0
    assert analyst.call("GET", f"/api/intake/{rejection['id']}/csv", raw=True)[1] == ORDER
    assert analyst.call("POST", "/api/intake", good)[1]["row_count"] == 2
    assert analyst.call("POST", "/api/intake", good)[1]["new_cases"] == 0
    assert analyst.call("GET", "/api/cases")[1]["order_count"] == 1


def test_simultaneous_claim_and_reasoned_reassignment(application):
    url, _ = application
    first = Client(url).login("analyst")
    second = Client(url).login("second")
    admin = Client(url).login("admin")
    assert first.call("POST", "/api/intake", bundle(CUSTOMER))[0] == 201
    path = "/api/cases/" + first.call("GET", "/api/cases")[1]["cases"][0]["id"]
    with ThreadPoolExecutor(2) as workers:
        outcomes = list(workers.map(lambda client: client.call("POST", path + "/claim", {"claim_revision": 0}), [first, second]))
    assert sorted(code for code, _ in outcomes) == [200,409]
    winner = next(body["claimant"] for code, body in outcomes if code == 200)
    stale, new = (first, "second") if winner == "analyst" else (second, "analyst")
    assert admin.call("POST", path + "/reassign", {"claim_revision": 1, "analyst": new})[0] == 422
    assert admin.call("POST", path + "/reassign", {"claim_revision": 1, "analyst": new, "reason": "Owner is unavailable"})[0] == 200
    code, rejected = stale.call("POST", path + "/preview", {"revision": 0, "claim_revision": 1})
    assert code == 409 and rejected["claimant"] == new
    history = admin.call("GET", "/api/history")[1]["events"]
    event = next(row for row in history if row["event"] == "reassign")
    detail = event["detail"]
    assert detail.pop("request_id")
    assert detail == {"prior": winner, "claimant": new, "reason": "Owner is unavailable", "prior_revision": 1, "claim_revision": 2}


def test_authorized_real_group_export_has_safe_exact_membership(application):
    import json
    url, _ = application
    analyst = Client(url).login("analyst")
    fixture = bundle(CUSTOMER)
    manifest = json.loads(__import__("base64").b64decode(fixture["manifest"]))
    manifest["failed_entities"][0]["errors"] = [{"error_code": "=2+2", "message": "api_key=DELIBERATELY-PRIVATE"}]
    fixture["manifest"] = __import__("base64").b64encode(json.dumps(manifest).encode()).decode()
    assert analyst.call("POST", "/api/intake", fixture)[0] == 201
    queue = analyst.call("GET", "/api/cases")[1]
    group = next(row for row in queue["groups"] if row["code"] == "=2+2")
    path = "/api/repair.json?group=" + group["id"]
    code, raw = analyst.call("GET", path, raw=True)
    assert code == 200 and b"DELIBERATELY-PRIVATE" not in raw
    packet = json.loads(raw)
    assert packet["contract"] == "1"
    assert [case["case_id"] for case in packet["cases"]] == [queue["cases"][0]["id"]]
    assert packet["cases"][0]["rule_code"] == "'=2+2"
    assert Client(url).login("other").call("GET", path)[0] == 404


def test_permission_boundary_rejects_forged_cross_session_mutation(application):
    url, _ = application
    analyst = Client(url).login("analyst")
    analyst.csrf = "forged"
    assert analyst.call("POST", "/api/intake", bundle(CUSTOMER))[0] == 403
    assert Client(url).login("auditor").call("POST", "/api/intake", bundle(CUSTOMER))[0] == 403
    assert Client(url).login("second").call("GET", "/api/audit.csv")[0] == 403


def test_timestamp_change_holds_queued_dispatch_without_reserving(database):
    from iew.api import WorkbenchServer
    from iew.contracts import now
    from iew.delivery import Delivery
    from iew.simulator import Simulator, SimulatorServer
    from iew.store import Store

    store = Store(database[0])
    store.initialize()
    store.add_user("analyst", "cedar", ["analyst"], "test password only")
    store.add_user("peer", "cedar", ["approver"], "test password only")
    simulator = Simulator(database[1])
    simulator.initialize()
    simulator.original("cedar", "cedar-v1", "customer", "C100", "original-a",
                       now() - timedelta(days=180) + timedelta(seconds=30), "fenced_no_write")
    token = "isolated-test-service-token"
    with running(SimulatorServer(("127.0.0.1", 0), simulator, token)) as destination:
        delivery = Delivery(store, database[2], destination, token)
        delivery.initialize()
        with running(WorkbenchServer(("127.0.0.1", 0), store, delivery)) as url:
            analyst, peer = Client(url).login("analyst"), Client(url).login("peer")
            assert analyst.call("POST", "/api/intake", bundle(CUSTOMER))[0] == 201
            path = "/api/cases/" + analyst.call("GET", "/api/cases")[1]["cases"][0]["id"]
            assert analyst.call("POST", path + "/claim", {"claim_revision": 0})[0] == 200
            status, result = analyst.call("POST", path + "/preview", {"revision": 0, "claim_revision": 1,
                "patch": {}, "reason": "Review approved evidence", "evidence": ["steward-C100"]})
            assert status == 200, result
            expected = {"revision": 1, "preview_hash": result["preview"]["hash"]}
            assert peer.call("POST", path + "/approve", expected)[0] == 200
            assert peer.call("POST", path + "/send", expected)[0] == 200
            # Advance the queue against a deliberately conflicting simulator clock fixture.
            with simulator.connect() as db:
                db.execute("UPDATE sim_original SET first=first-interval '1 minute'")
            delivery.tick()
            case = peer.call("GET", path)[1]
            assert case["status"] == "Held" and case["attempts"] == 0
            assert simulator.effect_count() == 0
