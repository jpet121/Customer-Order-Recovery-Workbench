from datetime import timedelta

from conftest import Client, bundle, running
from test_safety import ORDER


def test_at_most_three_workbench_attempts_across_changed_keys(database):
    from iew.api import WorkbenchServer
    from iew.contracts import now
    from iew.delivery import Delivery
    from iew.simulator import Simulator, SimulatorServer
    from iew.store import Store

    store, sim = Store(database[0]), Simulator(database[1])
    store.initialize()
    sim.initialize()
    for user, role in [("analyst","analyst"),("peer","approver")]:
        store.add_user(user, "cedar", [role], "test password only")
    token = "isolated-test-service-token"
    sim.original("cedar", "cedar-v1", "order", "O900", "original-a", now()-timedelta(days=1), "fenced_no_write")
    with running(SimulatorServer(("127.0.0.1", 0), sim, token)) as dest:
        engine = Delivery(store, database[2], dest, token)
        engine.initialize()
        with running(WorkbenchServer(("127.0.0.1", 0), store, engine)) as url:
            analyst, peer = Client(url).login("analyst"), Client(url).login("peer")
            assert analyst.call("POST", "/api/intake", bundle(ORDER,"order","O900",lines=[1,4]))[0] == 201
            path = "/api/cases/" + analyst.call("GET", "/api/cases")[1]["cases"][0]["id"]
            assert analyst.call("POST", path + "/claim", {"claim_revision":0})[0] == 200
            keys = []
            variants = [{}, {"lines":{"1":{"unit_code":"BOX"}}}, {"lines":{"4":{"unit_code":"BOX"}}},
                        {"lines":{"1":{"unit_code":"EA"}}}]
            for index, patch in enumerate(variants):
                status, candidate = analyst.call("POST", path+"/preview", {"revision":index,"claim_revision":1,
                    "patch":patch,"reason":"Verify unit","evidence":["ITEM-1"]})
                assert status == 200, candidate
                revision = {"revision": index+1, "preview_hash": candidate["preview"]["hash"]}
                status, approved = peer.call("POST", path+"/approve", revision)
                assert status == 200
                assert approved["status"] == ("Held" if index == 3 else "Approved")
                if index == 3:
                    status,result = peer.call("POST", path+"/send", revision)
                    assert status == 409 and result["code"] == "ATTEMPTS"
                    break
                sim.fault("cedar", "cedar-v1", "order", "O900", "reject")
                status, queued = peer.call("POST", path+"/send", revision)
                assert status == 200, queued
                assert engine.tick()
                current = peer.call("GET", path)[1]
                assert current["status"] == ("Rejected" if index < 2 else "Held")
                assert current["attempts"] == index+1
                keys.append(current["operation"])
            assert len(set(keys)) == 3 and sim.effect_count() == 0
