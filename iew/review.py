from datetime import timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import rules
from .contracts import Data, Problem, digest, encode, now, stamp, utc
from .store import DB, audit, audit_cases, lookup_id, missing, organization, permit

LOCKED = {"Queued", "Sending", "Outcome unknown", "Confirmed"}


def order_binding(db: DB, org: str, profile: str, reference: str | None, ref: Data) -> Data | None:
    if reference in ref["customers"]:
        return ref["customers"][reference]
    records = [(key, item) for key, item in ref["identities"].items() if item["reference"] == reference]
    if len(records) != 1:
        return None
    business_key, identity = records[0]
    row = db.execute(
        """SELECT data->'binding' AS binding FROM cases
           WHERE org=%s AND profile=%s AND kind='customer' AND business_key=%s AND data->>'status'='Confirmed'""",
        (org, profile, business_key)).fetchone()
    binding = row["binding"] if row else None
    if (binding and binding.get("identity_hash") == digest(encode(identity)) and binding["reference"] == reference
            and binding["destination"] == ref["destination"]):
        return binding
    return None


def evaluate(db: DB, case: Data, rows: list[Data], ref: Data) -> tuple[list[Data], Data | None]:
    binding = None
    if case["kind"] == "order":
        binding = order_binding(db, case["org"], case["profile"], rows[0]["customer_reference"], ref)
    return rules.validation(case["kind"], case["business_key"], rows, ref, binding), binding


def admit(db: DB, actor: Data, job: Data, kind: str, manifest: Data, entries: dict[str, Data]) -> int:
    ref = organization(db, actor["org"], lock=True)["reference"]
    count = 0
    existing = {row["business_key"]: row for row in db.execute(
        """SELECT id,business_key,data->'rows' AS rows,data->>'status' AS status FROM cases
           WHERE org=%s AND profile=%s AND kind=%s AND business_key=ANY(%s)
           ORDER BY business_key FOR UPDATE""",
        (actor["org"], job["profile"], kind, list(entries))).fetchall()}
    reuploads = []
    links = []
    for key, entry in sorted(entries.items()):
        if key in existing:
            prior = existing[key]
            comparison = ("Confirmed payload conflict" if prior["rows"] != entry["rows"]
                          and prior["status"] == "Confirmed" else "Linked")
            reuploads.append(("reupload", {"job": str(job["id"]), "outcome": comparison}, prior["id"]))
            links.append((job["id"], prior["id"]))
            continue
        case_id = uuid4()
        case = {"org": actor["org"], "kind": kind, "business_key": key, "profile": job["profile"]}
        issues, _ = evaluate(db, case, entry["rows"], ref)
        original_pending = entry.get("original_delivery_pending", False)
        state = {**entry, "original_attempt": manifest["attempt_id"], "transport_attempted_at": manifest["attempted_at"],
                 "source_hash": job["csv_hash"], "revision": 0, "status": "Held" if issues or original_pending else "Ready for review",
                 "delivery_reason": "Reconcile the original import before editing or sending." if original_pending else None,
                 "issues": issues, "claimant": None, "claim_revision": 0, "history": [], "editors": [],
                 "reference_version": ref["version"], "rule_version": rules.VERSION,
                 "contract_version": ref["contract"]}
        row = db.execute(
            """INSERT INTO cases (id,org,profile,kind,business_key,intake_id,deadline,data)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (org,profile,kind,business_key) DO NOTHING RETURNING id""",
            (case_id, actor["org"], job["profile"], kind, key, job["id"], entry["deadline"], Jsonb(state))).fetchone()
        if row:
            count += 1
            audit(db, actor, "case_admitted", {"job": str(job["id"]), "status": state["status"], "revision": 0}, case_id)
        else:
            row = db.execute("SELECT id,data FROM cases WHERE org=%s AND profile=%s AND kind=%s AND business_key=%s FOR UPDATE",
                             (actor["org"], job["profile"], kind, key)).fetchone()
            assert row is not None
            prior = row["data"]
            comparison = "Linked"
            if prior.get("rows") != entry["rows"] and prior["status"] == "Confirmed":
                comparison = "Confirmed payload conflict"
            audit(db, actor, "reupload", {"job": str(job["id"]), "outcome": comparison}, row["id"])
        links.append((job["id"], row["id"]))
    audit_cases(db, actor, reuploads)
    db.execute("INSERT INTO bundle_cases (bundle,case_id) SELECT %s,unnest(%s::uuid[])",
               (job["id"], [case_id for _, case_id in links]))
    return count


def read_case(db: DB, actor: Data, key: str, lock: bool = False) -> Data:
    query = "SELECT * FROM cases WHERE id=%s AND org=%s" + (" FOR UPDATE" if lock else "")
    row = db.execute(query, (lookup_id(key), actor["org"])).fetchone()
    if row is None:
        raise missing()
    return row


def visible(case: Data) -> Data:
    return {"id": str(case["id"]), "kind": case["kind"], "business_key": case["business_key"],
            "profile": case["profile"], "bundle": str(case["intake_id"]), **case["data"]}


def current_approval(case: Data, control: Data, db: DB) -> bool:
    state, ref = case["data"], control["reference"]
    approval = state.get("approval")
    if not approval or not state.get("preview") or utc(approval["expires"]) <= now():
        return False
    user = db.execute("SELECT version,roles,active FROM users WHERE name=%s AND org=%s FOR SHARE",
                      (approval["by"], case["org"])).fetchone()
    current = {"revision": state["revision"], "hash": state["preview"]["hash"], "policy": control["policy"],
               "reference": ref["version"], "destination": ref["destination"], "contract": ref["contract"],
               "rules": rules.VERSION}
    return bool(user and user["active"] and "approver" in user["roles"] and user["version"] == approval["authority"]
                and all(approval.get(k) == v for k, v in current.items()))


def write_case(db: DB, actor: Data, case: Data, event: str, detail: Data) -> Data:
    previous_terminal = case["terminal"]
    terminal = previous_terminal or now() if case["data"]["status"] in {"Confirmed", "Rejected"} else None
    db.execute("UPDATE cases SET data=%s,terminal=%s WHERE id=%s", (Jsonb(case["data"]), terminal, case["id"]))
    audit(db, actor, event, detail, case["id"])
    return visible(case)


def claim(db: DB, actor: Data, case: Data, action: str, body: Data) -> Data:
    state = case["data"]
    permit(actor, "administrator" if action == "reassign" else "analyst")
    if type(body.get("claim_revision")) is not int or body["claim_revision"] != state["claim_revision"]:
        raise Problem(409, "CLAIM_CONFLICT", "The claim changed. Review the current owner and try again.",
                      claimant=state["claimant"], claim_revision=state["claim_revision"])
    old, reason = state["claimant"], body.get("reason")
    if action == "release":
        if old != actor["name"]:
            raise Problem(403, "CLAIM_OWNER", "Only the current claimant can release a claim.")
        new = None
    elif action == "reassign":
        new = body.get("analyst")
        if not old or not isinstance(new, str) or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise Problem(422, "REASSIGN", "Supply an eligible analyst and a reason for reassigning the abandoned claim.")
        eligible = db.execute("SELECT 1 FROM users WHERE name=%s AND org=%s AND active AND 'analyst'=ANY(roles)",
                              (new, actor["org"])).fetchone()
        if not eligible or old == new:
            raise Problem(422, "REASSIGN", "Choose a different eligible analyst in this organization.")
    else:
        if old or state["status"] == "Confirmed" or not state.get("rows"):
            raise Problem(409, "CLAIM_CONFLICT", "This case cannot be claimed.", claimant=old)
        new = actor["name"]
    before = state["claim_revision"]
    state.update(claimant=new, claim_revision=before + 1)
    return write_case(db, actor, case, action, {"prior": old, "claimant": new, "reason": reason,
                                                "prior_revision": before, "claim_revision": before + 1})


def detail(db: DB, case: Data, control: Data) -> Data:
    state, ref = case["data"], control["reference"]
    view = visible(case)
    if state.get("rows"):
        rows = state.get("corrected", state["rows"])
        issues, binding = evaluate(db, case, rows, ref)
        locations = {row.get("line_no"): source for row, source in zip(state["rows"], state.get("locations", []))}
        for issue in issues:
            line = issue.get("line")
            if line in locations and (line is not None or issue["field"] in state["rows"][0]):
                issue["source_row"] = locations[line]
        view.update(issues=issues, reference_version=ref["version"], binding=binding or state.get("binding"),
                    evidence_ids=evidence_ids(case, ref), excerpts=ref["excerpts"],
                    candidate_ids=candidate_ids(case, ref, rows, issues, binding),
                    identity=rules.identity(ref, case["business_key"], state["rows"][0].get("customer_reference"))
                        if case["kind"] == "customer" else None)
    view["approval_current"] = current_approval(case, control, db)
    if view["status"] == "Approved" and not view["approval_current"]:
        view["status"] = "Held"
        view["blocking_reason"] = "Approval expired or a bound version or reviewer authority changed."
    return view


def candidate_ids(case: Data, ref: Data, rows: list[Data], issues: list[Data], binding: Data | None) -> list[str]:
    if case["kind"] == "customer":
        record = ref["identities"].get(case["business_key"])
        return [record["id"]] if record else []
    fields = {issue["field"] for issue in issues} & {"item_reference", "customer_reference", "ship_to_reference", "unit_code"}
    candidates: list[str] = []
    if not fields or "item_reference" in fields:
        candidates.extend(key for key, _ in rules.ranked_items(rows, ref))
    if not fields or "customer_reference" in fields:
        references = {row["customer_reference"] for row in rows}
        candidates.extend(sorted(ref["customers"][key]["id"] for key in references if key in ref["customers"]))
    if "ship_to_reference" in fields and binding:
        candidates.extend(sorted(binding["ship_to"]))
    if "unit_code" in fields:
        units = {unit for row in rows for unit in ref["items"].get(row["item_reference"], {}).get("units", [])}
        candidates.extend(sorted(units & set(ref["units"])))
    return list(dict.fromkeys(candidates))


def evidence_ids(case: Data, ref: Data) -> list[str]:
    identity = ref["identities"].get(case["business_key"], {})
    return sorted({*ref["items"], *ref["customers"], *ref["excerpts"], *([identity["evidence"]] if identity else [])})


def correction(db: DB, actor: Data, case: Data, control: Data, action: str, body: Data) -> Data:
    permit(actor, "analyst")
    state, ref = case["data"], control["reference"]
    if state["claimant"] != actor["name"] or type(body.get("claim_revision")) is not int or body["claim_revision"] != state["claim_revision"]:
        raise Problem(409, "CLAIM_CONFLICT", "You can no longer save or submit this case.", claimant=state["claimant"])
    expect_revision(state, body)
    if (state["status"] in LOCKED or state.get("delivery_uncertain")
            or state.get("original_delivery_pending") or not state.get("rows")):
        raise Problem(409, "CASE_LOCKED", "Resolve the outstanding delivery or inspect the retained evidence.")
    patch, reason, evidence = body.get("patch"), body.get("reason"), body.get("evidence")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise Problem(422, "REASON", "Enter the reason for this revision.")
    approved_evidence = evidence_ids(case, ref)
    if not isinstance(evidence, list) or not evidence or any(item not in approved_evidence for item in evidence):
        raise Problem(422, "EVIDENCE", "Select current supporting evidence.")
    if not isinstance(patch, dict):
        raise Problem(422, "PATCH_FIELDS", "Supply an object of controlled field edits.")
    rows = rules.apply_patch(case["kind"], state.get("corrected", state["rows"]), patch)
    prior = state.get("corrected", state["rows"])
    for old, new in zip(prior, rows):
        for field in ("item_reference", "customer_reference"):
            value = new.get(field)
            if old.get(field) != value and value in approved_evidence and value not in evidence:
                raise Problem(422, "EVIDENCE", "Select the approved reference evidence for the change.", field=field)
    issues, binding = evaluate(db, case, rows, ref)
    if action == "preview" and issues:
        raise Problem(422, "VALIDATION", "Resolve the listed fields before submitting a preview.", issues=issues)
    editors = sorted(set(state["editors"]) | {actor["name"]})
    number = state["revision"] + 1
    preview = rules.preview_record(case["kind"], rows, binding) if action == "preview" else None
    revision = {"revision": number, "author": actor["name"], "editors": editors, "reason": reason,
                "patch": patch, "evidence": evidence, "at": stamp(now()), "preview": preview,
                "reference_version": ref["version"], "rule_version": rules.VERSION, "contract_version": ref["contract"]}
    state.update(revision=number, corrected=rows, preview=preview, approval=None, editors=editors,
                 reference_version=ref["version"], rule_version=rules.VERSION, contract_version=ref["contract"],
                 issues=issues, status="Pending approval" if preview else "Draft correction")
    state["history"].append(revision)
    return write_case(db, actor, case, action, {"revision": number, "hash": preview["hash"] if preview else None})


def expect_revision(state: Data, body: Data) -> None:
    if type(body.get("revision")) is not int or body["revision"] != state["revision"]:
        raise Problem(409, "REVISION", "Case revision changed. Refresh before trying again.")


def approval(db: DB, actor: Data, case: Data, control: Data, action: str, body: Data) -> Data:
    permit(actor, "approver")
    state, ref = case["data"], control["reference"]
    expect_revision(state, body)
    if actor["name"] in state["editors"]:
        raise Problem(403, "PEER", "A reviewer must differ from every editor of this revision.")
    preview = state.get("preview")
    if state["status"] != "Pending approval" or not preview or body.get("preview_hash") != preview["hash"]:
        raise Problem(409, "PREVIEW_STALE", "A current exact preview is required.")
    if action == "return":
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise Problem(422, "REASON", "Enter a return reason.")
        state.update(status="Draft correction", approval=None, return_reason=reason)
        return write_case(db, actor, case, "returned", {"revision": state["revision"], "reason": reason})
    issues, binding = evaluate(db, case, state["corrected"], ref)
    if (issues or ref["version"] != state["reference_version"] or state.get("rule_version") != rules.VERSION
            or rules.preview_record(case["kind"], state["corrected"], binding)["hash"] != preview["hash"]):
        raise Problem(409, "PREVIEW_STALE", "Revalidate against current reference evidence.", issues=issues)
    user = db.execute("SELECT version FROM users WHERE name=%s", (actor["name"],)).fetchone()
    assert user is not None
    identity = rules.identity(ref, case["business_key"], state["corrected"][0]["customer_reference"])
    state["approval"] = {"by": actor["name"], "authority": user["version"], "expires": stamp(now() + timedelta(minutes=30)),
                         "revision": state["revision"], "hash": preview["hash"], "policy": control["policy"],
                         "reference": ref["version"], "contract": ref["contract"], "destination": ref["destination"],
                         "identity": identity, "binding": binding, "rules": rules.VERSION}
    if state.get("attempts", 0) >= 3:
        state.update(status="Held", delivery_reason="All three workbench attempts have been used.")
    else:
        state["status"] = "Approved"
    return write_case(db, actor, case, "approved", {"revision": state["revision"], "hash": preview["hash"]})
