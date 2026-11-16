from hashlib import sha256

from conftest import Client, CUSTOMER, bundle


def test_authenticated_intake_preserves_exact_original_bytes(application):
    url, _ = application
    analyst = Client(url).login("analyst")
    code, job = analyst.call("POST", "/api/intake", bundle(CUSTOMER))
    assert code == 201, job
    assert job["state"] == "Committed"
    assert job["csv_sha256"] == sha256(CUSTOMER).hexdigest()
    assert analyst.call("GET", f"/api/intake/{job['id']}/csv", raw=True) == (200, CUSTOMER)
    other = Client(url).login("other")
    assert other.call("GET", f"/api/intake/{job['id']}/csv")[0] == 404



def test_cancel_and_commit_share_one_observed_result(application):
    url, _ = application
    analyst = Client(url).login("analyst")
    code, job = analyst.call("POST", "/api/intake/start", bundle(CUSTOMER))
    assert code == 201 and job["state"] == "Validating"
    base = f"/api/intake/{job['id']}"
    assert analyst.call("POST", base + "/cancel", {"revision":0})[1]["state"] == "Cancelled"
    assert analyst.call("POST", base + "/commit", {"revision":0})[1]["state"] == "Cancelled"
    assert analyst.call("GET", "/api/cases")[1]["case_count"] == 0
    code, job = analyst.call("POST", "/api/intake", bundle(CUSTOMER))
    assert code == 201
    assert analyst.call("POST", f"/api/intake/{job['id']}/cancel", {"revision":0})[1]["state"] == "Committed"
    assert analyst.call("GET", "/api/cases")[1]["case_count"] == 1
