import logging
from dataclasses import dataclass
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from .contracts import Data, Problem, digest, encode, object_json, stamp
from .store import Store, DB, PreparedIntake, audit, organization, permit


@dataclass
class Reply:
    value: Any
    status: int = 200
    content_type: str = "application/json"
    cookie: str | None = None
    attachment: str | None = None


class QueueSnapshot(NamedTuple):
    org: str
    mutation_revision: int
    revision: int
    cases: list[Data]
    groups: list[Data]


def json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return stamp(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_value(item) for item in value]
    return value


class WorkbenchServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: Store, delivery: Any = None,
                 offline_provider: Callable[[Data], Any] | None = None):
        if address[0] != "127.0.0.1":
            raise ValueError("The local workbench binds only to loopback.")
        self.store = store
        self.delivery = delivery
        self.offline_provider = offline_provider
        self.queue_snapshot: QueueSnapshot | None = None
        super().__init__(address, Handler)


STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchServer
    server_version = "IEW"

    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.handle_request()

    def do_POST(self) -> None:
        self.handle_request()

    def token(self) -> str:
        try:
            value = SimpleCookie(self.headers.get("Cookie", ""))
            return value["iew_session"].value if "iew_session" in value else ""
        except CookieError:
            return ""

    def body(self) -> Data:
        if self.headers.get_content_type() != "application/json":
            raise Problem(415, "CONTENT_TYPE", "Use application/json.")
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if size < 0 or size > 30 * 1024 * 1024:
                raise Problem(413, "REQUEST_SIZE", "Request exceeds the upload envelope.")
            return object_json(self.rfile.read(size))
        except (ValueError, RecursionError) as error:
            raise Problem(400, "JSON", "Malformed JSON request.") from error

    def handle_request(self) -> None:
        self.connection.settimeout(30)
        trace = uuid4().hex
        path = urlsplit(self.path)
        try:
            if self.command == "GET" and path.path in STATIC:
                name, mime = STATIC[path.path]
                asset = Path(__file__).resolve().parents[1] / "web" / "static" / name
                if not asset.is_file():
                    raise Problem(503, "WEB_BUILD", "Build the TypeScript application before opening the workbench.")
                response = Reply(asset.read_bytes(), content_type=mime)
            else:
                if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                    raise Problem(403, "HOST", "Use the configured loopback address.")
                response = self.api(path.path, parse_qs(path.query), trace)
        except Problem as error:
            if error.status in {401, 403, 404} and path.path.startswith("/api/"):
                self.record_denial(trace, error)
            response = Reply(error.body, error.status)
        except Exception as error:
            logging.error("request=%s internal_error=%s", trace, type(error).__name__)
            response = Reply({"code": "SERVICE_ERROR", "detail": "The service could not complete the request."}, 500)
        payload = response.value if isinstance(response.value, bytes) else encode(json_value(response.value))
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("X-Request-ID", trace)
        if response.cookie:
            self.send_header("Set-Cookie", response.cookie)
        if response.attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{response.attachment}"')
        self.end_headers()
        self.wfile.write(payload)

    def record_denial(self, trace: str, problem: Problem) -> None:
        try:
            with self.server.store.connect() as db:
                actor = db.execute(
                    "SELECT u.name,u.org FROM sessions s JOIN users u ON u.name=s.username WHERE s.token=%s",
                    (digest(self.token().encode()),)).fetchone()
                if actor is not None:
                    audit(db, {**actor, "request_id": trace}, "request_denied",
                          {"code": problem.body["code"], "status": problem.status})
        except Exception as error:
            logging.warning("request=%s denial_log_error=%s", trace, type(error).__name__)

    def prepare_intake(self, db: DB, actor: Data, key: str, revision: int) -> PreparedIntake:
        prepared = self.server.store.prepare_job(db, actor, key, revision)
        if isinstance(prepared, tuple) and self.server.delivery is not None:
            kind, manifest, entries = prepared
            self.server.delivery.original_admission(actor["org"], kind, manifest, entries)
        return prepared

    def api(self, path: str, query: dict[str, list[str]], trace: str) -> Reply:
        if not path.startswith("/api/"):
            raise Problem(404, "NOT_FOUND", "Page not found.")
        mutating = self.command == "POST"
        origin = self.headers.get("Origin")
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if mutating and origin and origin != expected_origin:
            raise Problem(403, "ORIGIN", "Requests must originate from the workbench.")
        body = self.body() if mutating else {}
        store = self.server.store
        with store.connect() as db:
            if mutating and path == "/api/login":
                token, user = store.login(db, body)
                return Reply(user, cookie=f"iew_session={token}; Path=/; HttpOnly; SameSite=Strict")
            token = self.token()
            parts = path.strip("/").split("/")
            prepared: PreparedIntake = None
            if mutating and len(parts) == 4 and parts[:2] == ["api", "intake"] and parts[3] == "commit":
                revision = body.get("revision")
                if type(revision) is not int:
                    raise Problem(400, "REVISION", "An expected job revision is required.")
                uploader = store.authenticate(db, token, self.headers.get("X-CSRF-Token", ""), touch=False)
                if organization(db, uploader["org"])["access_hold"]:
                    raise Problem(423, "RESTORE_ACCESS", "Restored data is held pending retention.")
                prepared = self.prepare_intake(db, uploader, parts[2], revision)
                db.rollback()
            if mutating:
                store.lock_session_organization(db, token)
            actor = store.authenticate(db, token, self.headers.get("X-CSRF-Token", "") if mutating else None)
            if path == "/api/session" and not mutating:
                return Reply(actor)
            if path == "/api/logout" and mutating:
                db.execute("DELETE FROM sessions WHERE token=%s", (digest(token.encode()),))
                return Reply({"ok": True}, cookie="iew_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict")
            actor["request_id"] = trace
            control = organization(db, actor["org"])
            if control["access_hold"] and path not in {"/api/status", "/api/retention"}:
                raise Problem(423, "RESTORE_ACCESS", "Restored data is held pending retention verification.")
            if path == "/api/status" and not mutating:
                destination = self.server.delivery.http("health", {}) if self.server.delivery else {"verdict": "unknown"}
                return Reply({"database": "ready", "inference": "disabled", "paused": control["paused"],
                              "simulator": "ready" if destination["verdict"] == "ready" else "unavailable",
                              "access_hold": control["access_hold"], "delivery_hold": control["delivery_hold"],
                              "hold_reason": control["hold_reason"]})
            return self.route(db, actor, control, path, query, body, mutating, prepared)

    def route(self, db: DB, actor: Data, control: Data, path: str, query: dict[str, list[str]], body: Data, mutating: bool,
              prepared: PreparedIntake) -> Reply:
        store = self.server.store
        parts = path.strip("/").split("/")
        if mutating and path in {"/api/intake", "/api/intake/start"}:
            result = store.start_job(db, actor, body)
            if path == "/api/intake" and result["state"] == "Validating":
                db.commit()
                prepared = self.prepare_intake(db, actor, result["id"], 0)
                db.rollback()
                store.lock_session_organization(db, self.token())
                actor = {**store.authenticate(db, self.token(), self.headers.get("X-CSRF-Token", "")),
                         "request_id": actor["request_id"]}
                if organization(db, actor["org"])["access_hold"]:
                    raise Problem(423, "RESTORE_ACCESS", "Restored data is held pending retention.")
                result = store.commit_job(db, actor, result["id"], 0, prepared)
            return Reply(result, 201 if result["state"] != "Rejected" else 422)
        if parts[:2] == ["api", "intake"] and len(parts) in {3, 4}:
            key = parts[2]
            action = parts[3] if len(parts) == 4 else ""
            if mutating and action in {"commit", "cancel"}:
                revision = body.get("revision")
                if type(revision) is not int:
                    raise Problem(400, "REVISION", "An expected job revision is required.")
                if action == "cancel":
                    permit(actor, "analyst")
                    return Reply(store.finish_job(db, actor, key, revision, "Cancelled", {"case_count": 0}))
                result = store.commit_job(db, actor, key, revision, prepared)
                return Reply(result, 422 if result["state"] == "Rejected" else 200)
            if not mutating:
                job = store.job(db, actor, key)
                if action in {"csv", "manifest"}:
                    permit(actor, "analyst", "approver")
                    if job[action] is None:
                        raise Problem(410, "PURGED", "Source bytes are unavailable.")
                    return Reply(job[action], content_type="application/octet-stream",
                                 attachment=job["name"] if action == "csv" else "errors.json")
                if action == "":
                    from .operations import account_bundle
                    permit(actor, "analyst", "approver")
                    return Reply({**job["result"], "created": job["created"], **account_bundle(db, actor, job["id"])})
        from .routes import route
        return route(self, db, actor, control, path, parts, query, body, mutating)
