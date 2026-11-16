import base64
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Client:
    def __init__(self, url):
        self.url = url
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf = ""

    def call(self, method, path, data=None, raw=False):
        body = None if data is None else json.dumps(data).encode()
        request = Request(self.url + path, body, {
            "Content-Type": "application/json", "X-CSRF-Token": self.csrf
        }, method=method)
        try:
            stream = self.opener.open(request, timeout=10)
        except HTTPError as error:
            stream = error
        with stream:
            response = stream.read()
            return stream.status, response if raw else json.loads(response)

    def login(self, user, password="test password only"):
        status, result = self.call("POST", "/api/login", {"user": user, "password": password})
        assert status == 200, result
        self.csrf = result["csrf"]
        return self


@pytest.fixture
def database():
    dsn = os.getenv("IEW_TEST_DSN")
    if not dsn:
        pytest.fail("Set IEW_TEST_DSN to a dedicated PostgreSQL test database")
    schemas = ["test_" + uuid4().hex for _ in range(3)]
    with psycopg.connect(dsn, autocommit=True) as connection:
        try:
            for schema in schemas:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            yield tuple(psycopg.conninfo.make_conninfo(dsn, options=f"-c search_path={schema}") for schema in schemas)
        finally:
            for schema in schemas:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def application(database):
    from iew.api import WorkbenchServer
    from iew.store import Store

    store = Store(database[0])
    store.initialize()
    for name, org, roles in [
        ("analyst", "cedar", ["analyst", "approver"]), ("peer", "cedar", ["approver"]),
        ("other", "other", ["analyst"]), ("admin", "cedar", ["administrator"]),
        ("auditor", "cedar", ["auditor"]), ("second", "cedar", ["analyst"])
    ]:
        store.add_user(name, org, roles, "test password only")
    with running(WorkbenchServer(("127.0.0.1", 0), store)) as url:
        yield url, store


def bundle(raw, entity="customer", key="C100", attempt="original-a", lines=None, **fields):
    key_name = f"source_{entity}_id"
    entry = {key_name: key, "errors": [{"error_code": "REFERENCE", "message": "Check reference"}], **fields}
    if lines is not None:
        entry["expected_line_numbers"] = lines
    manifest = {"schema_version": 1, "source_profile_id": "cedar-v1", "attempt_id": attempt,
                "file_sha256": hashlib.sha256(raw).hexdigest(), "attempted_at": "2026-11-14T12:00:00Z",
                "failed_entities": [entry]}
    return {"file_name": entity + "s.csv", "profile": "cedar-v1",
            "csv": base64.b64encode(raw).decode(), "manifest": base64.b64encode(json.dumps(manifest).encode()).decode()}


CUSTOMER = (b"source_customer_id,display_name,country_code,postal_code,customer_reference\r\n"
            b'C100,"Cedar, Vale",US,75001,REF-100\r\n')


@contextmanager
def runtime_credentials(dsns):
    from iew.runtime import grant_runtime
    from iew.store import Database
    role = "iew_runtime_" + uuid4().hex
    with psycopg.connect(dsns[0], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(role)))
        try:
            for dsn in dsns:
                grant_runtime(Database(dsn), role)
            yield [psycopg.conninfo.make_conninfo(dsn, user=role) for dsn in dsns]
        finally:
            for dsn in dsns:
                with psycopg.connect(dsn, autocommit=True) as db:
                    db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
