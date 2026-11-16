from datetime import timedelta

import pytest

from conftest import Client, CUSTOMER, bundle, runtime_credentials
from iew.contracts import now
from iew.runtime import require_runtime
from iew.store import Store


def test_runtime_cannot_backdate_terminal_or_shadow_audit_expiry(application,database):
    url,operator=application
    client=Client(url).login("analyst")
    assert client.call("POST","/api/intake",bundle(CUSTOMER))[0]==201
    with runtime_credentials([database[0]]) as dsns:
        runtime=Store(dsns[0])
        require_runtime(runtime)
        with runtime.connect() as db:
            count=db.execute("SELECT count(*) n FROM audit").fetchone()["n"]
            case=db.execute("SELECT id FROM cases").fetchone()["id"]
            db.execute("UPDATE cases SET terminal=%s,data=jsonb_set(data,'{status}','\"Confirmed\"')",
                       (now()-timedelta(days=200),))
            age=db.execute("SELECT clock_timestamp()-terminal age FROM cases").fetchone()["age"]
            assert timedelta(0)<=age<timedelta(seconds=2)
            db.execute("SAVEPOINT clock_attack")
            with pytest.raises(Exception,match="immutable"):
                db.execute("UPDATE cases SET terminal=clock_timestamp()-interval '200 days'")
            db.execute("ROLLBACK TO SAVEPOINT clock_attack")
            db.execute("CREATE TEMP TABLE cases(id uuid,org text,terminal timestamptz)")
            db.execute("INSERT INTO pg_temp.cases VALUES(%s,'cedar',clock_timestamp()-interval '200 days')",(case,))
            db.execute("SELECT iew_expire_audit('cedar',clock_timestamp())")
            assert db.execute("SELECT count(*) n FROM audit").fetchone()["n"]==count
            db.execute("SAVEPOINT future_expiry")
            with pytest.raises(Exception,match="future clock"):
                db.execute("SELECT iew_expire_audit('cedar',clock_timestamp()+interval '200 days')")
            db.execute("ROLLBACK TO SAVEPOINT future_expiry")
            assert db.execute("SELECT count(*) n FROM audit").fetchone()["n"]==count


def test_runtime_guard_refuses_operator_credentials(application):
    _,operator=application
    with pytest.raises(ValueError,match="restricted runtime"):
        require_runtime(operator)
