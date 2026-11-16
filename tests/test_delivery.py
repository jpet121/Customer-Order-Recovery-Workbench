from datetime import timedelta
import pytest

from conftest import Client, CUSTOMER, bundle, running


@pytest.mark.parametrize("fault", ["response_lost_after_commit", "receipt_without_readback"])
def test_unverified_response_requires_readback_and_never_duplicates_effect(database, fault):
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
    simulator.original("cedar", "cedar-v1", "customer", "C100", "original-a", now() - timedelta(days=1), "fenced_no_write")
    simulator.fault("cedar", "cedar-v1", "customer", "C100", fault)
    credential = "isolated-test-service-token"
    with running(SimulatorServer(("127.0.0.1", 0), simulator, credential)) as target:
        worker = Delivery(store, database[2], target, credential)
        worker.initialize()
        with running(WorkbenchServer(("127.0.0.1", 0), store, worker)) as url:
            analyst = Client(url).login("analyst")
            peer = Client(url).login("peer")
            assert analyst.call("POST", "/api/intake", bundle(CUSTOMER))[0] == 201
            case = analyst.call("GET", "/api/cases")[1]["cases"][0]
            path = f"/api/cases/{case['id']}"
            assert analyst.call("POST", path + "/claim", {"claim_revision": 0})[0] == 200
            status, preview = analyst.call("POST", path + "/preview", {
                "revision": 0, "claim_revision": 1, "patch": {}, "reason": "Verified",
                "evidence": ["steward-C100"]
            })
            assert status == 200, preview
            revision = {"revision": 1, "preview_hash": preview["preview"]["hash"]}
            assert peer.call("POST", path + "/approve", revision)[0] == 200
            assert simulator.effect_count() == 0
            status, queued = peer.call("POST", path + "/send", revision)
            assert status == 200, queued
            assert queued["status"] == "Queued"
            worker.tick()
            pending = peer.call("GET", path)[1]
            assert pending["status"] == "Outcome unknown" and pending["operation"]
            if fault == "receipt_without_readback":
                assert pending["unverified_receipt"]["hash"] == revision["preview_hash"]
            assert simulator.effect_count() == 1
            peer.call("POST", path + "/reconcile", revision)
            verified = peer.call("GET", path)[1]
            assert verified["status"] == "Confirmed", verified
            assert verified["attempts"] == 1
            worker.tick()
            assert simulator.effect_count() == 1
