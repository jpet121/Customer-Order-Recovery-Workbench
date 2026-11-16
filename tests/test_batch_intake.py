import base64
import json

from conftest import Client, CUSTOMER, bundle
from iew.audit_chain import verify


def test_batched_duplicates_keep_case_state_provenance_and_audit_chains(application):
    url,store=application
    client=Client(url).login("analyst")
    assert client.call("POST","/api/intake",bundle(CUSTOMER))[0]==201
    original=client.call("GET","/api/cases")[1]["cases"][0]
    with store.connect() as db:
        db.execute("UPDATE cases SET terminal=clock_timestamp(),data=jsonb_set(data,'{status}','\"Confirmed\"')")
        before=db.execute("SELECT data FROM cases WHERE id=%s",(original["id"],)).fetchone()["data"]
    changed=CUSTOMER.replace(b"Cedar, Vale",b"Different legal name")
    raw=changed+CUSTOMER.split(b"\r\n",1)[1].replace(b"C100",b"C101").replace(b"REF-100",b"REF-101")
    payload=bundle(raw)
    manifest=json.loads(base64.b64decode(payload["manifest"]))
    manifest["failed_entities"].append({**manifest["failed_entities"][0],"source_customer_id":"C101"})
    payload["manifest"]=base64.b64encode(json.dumps(manifest).encode()).decode()
    for expected_new in (1,0):
        code,result=client.call("POST","/api/intake",payload)
        assert code==201 and result["new_cases"]==expected_new and result["case_count"]==2
        assert client.call("GET",f"/api/intake/{result['id']}/csv",raw=True)[1]==raw
    with store.connect() as db:
        assert db.execute("SELECT data FROM cases WHERE id=%s",(original["id"],)).fetchone()["data"]==before
        assert db.execute("SELECT count(*) n FROM cases").fetchone()["n"]==2
        assert db.execute("SELECT count(*) n FROM bundle_cases").fetchone()["n"]==5
        conflicts=db.execute("SELECT detail FROM audit WHERE event='reupload' AND case_id=%s",
                             (original["id"],)).fetchall()
        assert len(conflicts)==2 and all(r["detail"]["outcome"]=="Confirmed payload conflict" for r in conflicts)
        assert all(r["detail"]["request_id"] for r in conflicts)
        assert db.execute("SELECT count(*) n FROM audit WHERE event='reupload'").fetchone()["n"]==3
    code,export=Client(url).login("auditor").call("GET","/api/audit.csv",raw=True)
    assert code==200 and verify(export)
