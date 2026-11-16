from typing import TYPE_CHECKING

from . import advisory, operations, recovery, review
from .api import QueueSnapshot, Reply
from .contracts import Data, Problem, digest, encode
from .store import DB, organization, permit

if TYPE_CHECKING:
    from .api import Handler, WorkbenchServer


def group_id(profile: str, code: str, field: str) -> str:
    return digest(encode([profile, code, field]))


# Every case mutation and the bulk reference/retention writers allocate these
# audit sequences while holding the same organization lock. Unrelated audit
# writers can commit out of order without obscuring the latest queue mutation.
QUEUE_WATERMARK = """SELECT coalesce(max(seq),0) AS revision,
    coalesce(max(seq) FILTER (WHERE case_id IS NOT NULL
                OR event IN ('references_published','retention_applied')),0) AS mutation_revision
    FROM audit WHERE org=%s"""


def queue_projection(db: DB, org: str) -> QueueSnapshot:
    rows = db.execute(
        f"""SELECT mutation_revision,revision,id,kind,profile,business_key,inserted,deadline,
                  jsonb_build_object('status',data->'status','claimant',data->'claimant','revision',data->'revision',
                   'issues',data->'issues','errors',data->'errors','row_count',jsonb_array_length(data->'rows'),
                   'line_count',jsonb_array_length(data->'line_numbers')) AS data
           FROM ({QUEUE_WATERMARK}) AS watermark
           LEFT JOIN cases ON cases.org=%s
           ORDER BY deadline ASC NULLS LAST,inserted,id""", (org, org)
    ).fetchall()
    groups: dict[str, Data] = {}
    group_ids_by_key: dict[tuple[str, str, str], str] = {}
    cases: list[Data] = []
    for row in rows:
        if row["id"] is None:
            continue
        data = row["data"]
        group_ids = []
        for code, field in operations.group_keys(data.pop("issues"), data.pop("errors")):
            signature = (row["profile"], code, field)
            key = group_ids_by_key.get(signature)
            if key is None:
                key = group_ids_by_key[signature] = group_id(*signature)
            group = groups.setdefault(key, {"id": key, "code": code, "field": field, "profile": row["profile"], "case_count": 0})
            group["case_count"] += 1
            group_ids.append(key)
        row["groups"] = sorted(group_ids)
        cases.append(row)
    return QueueSnapshot(org, rows[0]["mutation_revision"], rows[0]["revision"], cases,
                         sorted(groups.values(), key=lambda group: (group["code"], group["field"])))


def queue(db: DB, actor: Data, query: dict[str, list[str]], server: "WorkbenchServer | None" = None) -> Data:
    org = actor["org"]
    snapshot = server.queue_snapshot if server is not None else None
    revision = 0
    if snapshot is not None and snapshot.org == org:
        current = db.execute(QUEUE_WATERMARK, (org,)).fetchall()[0]
        revision = current["revision"]
        if snapshot.mutation_revision != current["mutation_revision"]:
            snapshot = None
    else:
        snapshot = None
    if snapshot is None:
        # The read model and watermark come from one statement and snapshot.
        snapshot = queue_projection(db, org)
        revision = snapshot.revision
        if server is not None:
            server.queue_snapshot = snapshot
    state_filter = query.get("status", [""])[0]
    group_filter = query.get("group", [""])[0]
    own = query.get("own", ["false"])[0] == "true"
    filtered = [row for row in snapshot.cases if (not state_filter or row["data"]["status"] == state_filter)
                and (not group_filter or group_filter in row["groups"])
                and (not own or row["data"]["claimant"] == actor["name"])]
    cursor = query.get("after", [""])[0]
    requested_snapshot = query.get("snapshot", [str(snapshot.mutation_revision)])[0]
    if requested_snapshot != str(snapshot.mutation_revision):
        cursor = ""
    start = next((idx + 1 for idx, row in enumerate(filtered) if str(row["id"]) == cursor), 0) if cursor else 0
    page = [{k: row[k] for k in ("id","kind","profile","business_key","inserted","deadline")}
            | {k: row["data"][k] for k in ("status","claimant","revision","row_count","line_count")}
            | {"groups": row["groups"]} for row in filtered[start:start + 100]]
    return {"cases": page, "groups": snapshot.groups,
            "case_count": len(filtered), "line_count": sum(row["data"]["line_count"] or 0 for row in filtered),
            "row_count": sum(row["data"]["row_count"] or 0 for row in filtered),
            "order_count": sum(row["kind"] == "order" for row in filtered),
            "data_version": revision, "snapshot_version": snapshot.mutation_revision,
            "next": str(page[-1]["id"]) if len(filtered) > start + 100 else None}


def route(handler: "Handler", db: DB, actor: Data, control: Data, path: str, parts: list[str],
          query: dict[str, list[str]], body: Data, mutating: bool) -> Reply:
    if path == "/api/cases" and not mutating:
        return Reply(queue(db, actor, query, handler.server))
    if not mutating and path == "/api/history":
        text_after = query.get("after", ["0"])[0]
        if not text_after.isdecimal() or len(text_after) > 15:
            raise Problem(422, "CURSOR", "Use a valid history cursor.")
        return Reply({"events": operations.history(db, actor, int(text_after))})
    if not mutating and path == "/api/audit.csv":
        return Reply(operations.audit_export(db, actor), content_type="text/csv; charset=utf-8", attachment="iew-audit.csv")
    if not mutating and path == "/api/repair.json":
        group_id_value = query.get("group", [""])[0]
        group = next((group for group in queue(db, actor, {})["groups"] if group["id"] == group_id_value), None)
        if group is None:
            raise Problem(404, "GROUP", "Select an available failure group.")
        return Reply(operations.repair_packet(db, actor, group), attachment="iew-repair-packet.json")
    if not mutating and path == "/api/admin":
        permit(actor, "administrator")
        users = db.execute("SELECT name,roles,active FROM users WHERE org=%s ORDER BY name", (actor["org"],)).fetchall()
        return Reply({"users": users, "reference": control["reference"], "paused": control["paused"],
                      "delivery_hold": control["delivery_hold"], "hold_reason": control["hold_reason"]})
    if mutating and path in {"/api/dispatch-pause", "/api/references", "/api/retention", "/api/recovery", "/api/users"}:
        permit(actor, "administrator")
        control = organization(db, actor["org"], lock=True)
        if path == "/api/retention":
            return Reply(operations.retention(db, actor))
        if control["access_hold"]:
            raise Problem(423, "RESTORE_ACCESS", "Restored data is held pending retention.")
        if path == "/api/references":
            return Reply(operations.publish(db, actor, control, body))
        if path == "/api/users":
            return Reply(operations.user_admin(db, actor, body))
        if path == "/api/recovery":
            if handler.server.delivery is None:
                raise Problem(503, "SIMULATOR", "Local delivery recovery is unavailable.")
            return Reply(recovery.open_delivery(db, actor, handler.server.delivery))
        if type(body.get("paused")) is not bool:
            raise Problem(422, "PAUSE", "Specify the dispatch pause state.")
        db.execute("UPDATE org_controls SET paused=%s WHERE org=%s", (body["paused"], actor["org"]))
        operations.audit(db, actor, "dispatch_pause_changed", {"paused": body["paused"]})
        return Reply({"paused": body["paused"]})
    if parts[:2] == ["api", "cases"] and len(parts) in {3, 4}:
        action = parts[3] if len(parts) == 4 else ""
        if not mutating and action == "":
            case = review.read_case(db, actor, parts[2])
            if set(actor["roles"]).intersection({"analyst", "approver"}):
                return Reply(review.detail(db, case, control))
            public = review.visible(case)
            return Reply({k: public.get(k) for k in ("id", "kind", "business_key","profile","bundle","status",
                                                     "claimant","claim_revision","revision")})
        if mutating:
            control = organization(db, actor["org"], lock=True)
            if control["access_hold"]:
                raise Problem(423, "RESTORE_ACCESS", "Restored data is held.")
            case = review.read_case(db, actor, parts[2], lock=True)
            if action in {"claim", "release", "reassign"}:
                return Reply(review.claim(db, actor, case, action, body))
            if action == "diagnose":
                permit(actor, "analyst")
                provider = handler.server.offline_provider
                if provider is None:
                    return Reply(advisory.unavailable(case, body))
                # Provider latency must not hold business row/session locks.
                db.commit()
                failure = None
                try:
                    response = advisory.offline(case, control["reference"], body, provider)
                except Problem as error:
                    failure = error
                    response = error.body
                handler.server.store.lock_session_organization(db, handler.token())
                current_actor = handler.server.store.authenticate(db, handler.token(), handler.headers.get("X-CSRF-Token", ""))
                permit(current_actor, "analyst")
                current_control = organization(db, current_actor["org"])
                if current_control["access_hold"]:
                    raise Problem(423, "RESTORE_ACCESS", "Restored data is held.")
                current_case = review.read_case(db, current_actor, parts[2], lock=True)
                advisory.unavailable(current_case, body)
                if failure is None and response.get("advice"):
                    try:
                        if current_control["policy"] != control["policy"]:
                            raise Problem(409, "AI_REFERENCE_CHANGED", "Reference evidence changed during diagnosis. Request a new diagnosis.")
                        advisory.check_suggestion(db, current_case, current_control["reference"], response["advice"])
                    except Problem as error:
                        failure = error
                        response = error.body
                event = "ai_validation_failed" if failure else "ai_diagnosis_completed"
                operations.audit(db, actor, event, {
                    "revision": body["revision"],
                    "code": failure.body["code"] if failure else response["status"]}, case["id"])
                return Reply(response, failure.status if failure else 200)
            if action in {"save", "preview"}:
                return Reply(review.correction(db, actor, case, control, action, body))
            if action in {"approve", "return"}:
                return Reply(review.approval(db, actor, case, control, action, body))
            if action in {"send", "retry", "reconcile", "cancel"}:
                delivery = handler.server.delivery
                if delivery is None:
                    raise Problem(503, "SIMULATOR", "The local delivery service is unavailable.")
                if action == "reconcile":
                    return Reply(delivery.reconcile(db, actor, case, control))
                if action == "cancel":
                    return Reply(delivery.cancel(db, actor, case, body))
                return Reply(delivery.send(db, actor, case, control, body, retry=action == "retry"))
    raise Problem(404, "NOT_FOUND", "Action not found.")
