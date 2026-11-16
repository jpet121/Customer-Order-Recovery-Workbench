from conftest import Client, CUSTOMER, bundle


def test_retention_uses_terminal_clock_and_keeps_unresolved_source(application):
    url, store = application
    analyst, admin = Client(url).login("analyst"), Client(url).login("admin")
    _, job = analyst.call("POST", "/api/intake", bundle(CUSTOMER))
    source = f"/api/intake/{job['id']}/csv"
    with store.connect() as db:
        db.execute("UPDATE intake SET created=clock_timestamp()-interval '45 days'")
        db.execute("UPDATE cases SET inserted=clock_timestamp()-interval '45 days'")
    assert admin.call("POST", "/api/retention", {})[0] == 200
    assert analyst.call("GET", source, raw=True) == (200, CUSTOMER)
    with store.connect() as db:
        db.execute("""UPDATE cases SET data=jsonb_set(data,'{status}','"Confirmed"'),terminal=clock_timestamp()-interval '1 day'""")
    assert admin.call("POST", "/api/retention", {})[0] == 200
    assert analyst.call("GET", source, raw=True) == (200, CUSTOMER)
    with store.connect() as db:
        db.execute("UPDATE cases SET terminal=clock_timestamp()-interval '31 days'")
    assert admin.call("POST", "/api/retention", {})[0] == 200
    assert analyst.call("GET", source)[0] == 410
    _, report = analyst.call("GET", f"/api/intake/{job['id']}")
    assert report["csv_sha256"] == job["csv_sha256"]
    events = admin.call("GET", "/api/history")[1]["events"]
    assert "source_payload_purged" in {event["event"] for event in events}
