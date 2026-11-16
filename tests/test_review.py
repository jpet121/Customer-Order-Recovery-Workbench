from conftest import Client, CUSTOMER, bundle


def test_claimed_correction_can_only_be_approved_by_a_peer(application):
    url, _ = application
    analyst = Client(url).login("analyst")
    peer = Client(url).login("peer")
    assert analyst.call("POST", "/api/intake", bundle(CUSTOMER))[0] == 201
    case = analyst.call("GET", "/api/cases")[1]["cases"][0]
    path = f"/api/cases/{case['id']}"
    assert analyst.call("POST", path + "/claim", {"claim_revision": 0})[0] == 200
    query = {"revision": 0, "claim_revision": 1, "patch": {}, "reason": "Source verified", "evidence": ["steward-C100"]}
    status, correction = analyst.call("POST", path + "/preview", query)
    assert status == 200, correction
    bound = {"revision": 1, "preview_hash": correction["preview"]["hash"]}
    assert analyst.call("POST", path + "/approve", bound)[0] == 403
    status, approved = peer.call("POST", path + "/approve", bound)
    assert status == 200, approved
    assert approved["status"] == "Approved"
    assert approved["preview"]["payload"]["display_name"] == "Cedar, Vale"
