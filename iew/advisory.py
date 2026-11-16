from typing import Any, Callable
import os
import subprocess
import signal
import sys
from pathlib import Path
from time import monotonic

import cloudpickle  # type: ignore[import-untyped]

from . import audit_chain, review
from .contracts import Data, Problem, encode, fields
from .fixture_serialization import MAX_BYTES, FixtureDeadline, SERIALIZATION_ERRORS, serialize_fixture
from .rules import EDITABLE, ranked_items
from .store import DB


MAX_INPUT_TOKENS = 6000
MAX_OUTPUT_TOKENS = 900
ATTEMPT_SECONDS = 20
FIXTURE_CHILD = "IEW_OFFLINE_FIXTURE_CHILD"
FIXTURE_DEADLINE = "IEW_OFFLINE_FIXTURE_DEADLINE"


def within_fixture_budget(value: Any, limit: int) -> bool:
    # The local UTF-8 fixture counts bytes as a conservative upper bound.
    # A live adapter must enforce its approved provider's tokenizer.
    try:
        return len(encode(value)) <= limit
    except (TypeError, ValueError, UnicodeError):
        return False


def compiled_context(case: Data, ref: Data) -> Data:
    rows = case["data"].get("corrected") or case["data"].get("rows") or []
    candidates = []
    if case["kind"] == "customer":
        references = {row.get("customer_reference") for row in rows}
        for identity in ref["identities"].values():
            candidate_id, reference = identity["id"], identity["reference"]
            if reference in references and not any(audit_chain.CREDENTIAL.search(value) for value in (candidate_id, reference)):
                candidates.append({"id": candidate_id, "reference": reference})
        candidates.sort(key=lambda match: (match["id"], match["reference"]))
    else:
        for candidate_id, description in ranked_items(rows, ref):
            if not any(audit_chain.CREDENTIAL.search(value) for value in (candidate_id, description)):
                candidates.append({"id": candidate_id, "description": description})
    allowed = {"customer_reference", "item_reference", "unit_code", "ship_to_reference", "order_date", "dealer_description"}
    omissions = []
    for index, row in enumerate(rows):
        for key in sorted(row):
            if key not in allowed or isinstance(row[key], str) and audit_chain.CREDENTIAL.search(row[key]):
                # Do not echo untrusted field names or values into provider context.
                omissions.append({"record": index, "field": key if key in allowed else "non_allowlisted",
                                  "reason": "credential_like" if key in allowed else "not_allowlisted"})
    errors = {entry["error_code"] for entry in case["data"].get("errors", [])}
    visible_errors = sorted(code for code in errors if not audit_chain.CREDENTIAL.search(code))
    if len(visible_errors) != len(errors):
        omissions.append({"field": "error_code", "reason": "credential_like"})
    excerpts = [key for key in sorted(ref["excerpts"])
                if not audit_chain.CREDENTIAL.search(key) and not audit_chain.CREDENTIAL.search(ref["excerpts"][key])]
    payload = {"revision": case["data"]["revision"], "kind": case["kind"],
               "errors": visible_errors,
               "record": [{key: row[key] for key in sorted(allowed & row.keys())
                           if not isinstance(row[key], str) or not audit_chain.CREDENTIAL.search(row[key])} for row in rows],
               "candidates": candidates[:5],
               "excerpts": [{"id": key, "text": ref["excerpts"][key]} for key in excerpts[:3]],
               "omitted_fields": omissions}
    if not within_fixture_budget(payload, MAX_INPUT_TOKENS):
        raise Problem(422, "AI_CONTEXT_LIMIT", "Approved case context exceeds the conservative offline envelope.")
    return payload


def safe_output(output: Any, context: Data) -> Data:
    if not fields(output, {"schema_version","disposition","explanation","evidence_ids","candidate_ids","proposed_patch","question"}):
        raise Problem(422, "AI_SCHEMA", "Advisory response did not match the versioned schema.")
    if not within_fixture_budget(output, MAX_OUTPUT_TOKENS):
        raise Problem(422, "AI_OUTPUT_LIMIT", "Advisory output exceeds the conservative offline envelope.")
    cited = {x["id"] for x in context["excerpts"]}
    candidates = {x["id"] for x in context["candidates"]}
    mode, patch = output["disposition"], output["proposed_patch"]
    evidence, identifiers = output["evidence_ids"], output["candidate_ids"]
    explanation, question = output["explanation"], output["question"]
    if (type(output["schema_version"]) is not int or output["schema_version"] != 1
            or mode not in ("suggest","clarify","abstain") or not isinstance(explanation, str) or len(explanation) > 1200
            or (question is not None and (not isinstance(question, str) or len(question) > 1200))
            or not isinstance(patch, dict) or not patch.keys() <= EDITABLE
            or any(not isinstance(value, str) or len(value) > 64 for value in patch.values())
            or not isinstance(evidence, list) or any(not isinstance(e, str) or e not in cited for e in evidence)
            or not isinstance(identifiers, list) or any(not isinstance(i, str) or i not in candidates for i in identifiers)
            or (mode == "suggest" and (not evidence or not identifiers or not patch))
            or (mode == "clarify" and not question)
            or (mode != "suggest" and patch)
            or (mode == "suggest" and (patch.get("item_reference") not in identifiers or set(patch) - {"item_reference"}))):
        raise Problem(422, "AI_REJECTED", "Advisory response could not be supported by approved evidence.")
    return output


def check_suggestion(db: DB, case: Data, ref: Data, advice: Data) -> None:
    if advice["disposition"] != "suggest":
        return
    rows = case["data"].get("corrected") or case["data"].get("rows") or []
    if case["kind"] != "order" or len(rows) != 1:
        raise Problem(422, "AI_RULE", "The suggested edit must identify one unambiguous order line.")
    hypothetical = [{**rows[0], **advice["proposed_patch"]}]
    issues, _ = review.evaluate(db, case, hypothetical, ref)
    if issues:
        raise Problem(422, "AI_RULE", "The proposed edit failed deterministic case validation.", issues=issues)


def unavailable(case: Data, body: Data) -> Data:
    review.expect_revision(case["data"], body)
    if case["data"].get("payload_purged"):
        raise Problem(410, "PURGED", "Source payload has expired.")
    return {"status": "Unavailable", "reason": "No live inference provider is authorized. Continue deterministic review.",
            "revision": body["revision"], "proposed_patch": {}}


def invoke_fixture(provider: Callable[[Data], Any], context: Data, timeout: float = ATTEMPT_SECONDS,
                   *, deadline: float | None = None, payload_limit: int = MAX_BYTES) -> Data:
    return _invoke_fixture(
        lambda cutoff: serialize_fixture(provider, context, cutoff, payload_limit=payload_limit), timeout, deadline)


def invoke_prepared_fixture(payload: bytes, timeout: float, *, deadline: float) -> Data:
    return _invoke_fixture(lambda _: payload, timeout, deadline)


def _invoke_fixture(prepare: Callable[[float], bytes], timeout: float, deadline: float | None) -> Data:
    # A thread cannot stop an arbitrary timed-out callback. A disposable process
    # keeps the existing callable seam while preventing late fixture effects.
    started = monotonic()
    cutoff = min(started + timeout, deadline) if deadline is not None else started + timeout
    if started >= cutoff:
        return {"status": "deadline"}
    cleanup = min(0.75, (cutoff - started) / 4)
    admission_cutoff = cutoff - cleanup
    try:
        encoded = prepare(admission_cutoff)
    except FixtureDeadline:
        return {"status": "deadline"}
    except SERIALIZATION_ERRORS:
        return {"status": "setup_error"}
    executable = getattr(sys, "_base_executable", sys.executable)
    inherited = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR",
                 "COVERAGE_PROCESS_START", "COVERAGE_FILE", "IEW_COVERAGE_ACTIVE")
    environment = {name: os.environ[name] for name in inherited if name in os.environ}
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1]), str(Path(cloudpickle.__file__).resolve().parents[1])])
    environment[FIXTURE_CHILD] = "1"
    environment[FIXTURE_DEADLINE] = str(cutoff)
    owns_group = os.name != "nt" and FIXTURE_CHILD not in os.environ
    if monotonic() >= admission_cutoff:
        return {"status": "deadline"}
    with subprocess.Popen([executable, "-m", "iew.offline_worker"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
                          start_new_session=owns_group) as process:
        try:
            output, _ = process.communicate(encoded, timeout=max(0, admission_cutoff - monotonic()))
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=max(.05, cleanup / 2), check=False)
                except subprocess.TimeoutExpired:
                    process.kill()
            elif owns_group:
                getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
            if process.poll() is None:
                process.kill()
            process.wait(timeout=max(.05, cutoff - monotonic()))
            return {"status": "timeout"}
    if process.returncode != 0:
        return {"status": "unavailable"}
    from .contracts import object_json
    try:
        envelope = object_json(output)
    except (ValueError, UnicodeError):
        return {"status": "unavailable"}
    return envelope


def offline(case: Data, ref: Data, body: Data, provider: Callable[[Data], Any]) -> Data:
    unavailable(case, body)
    context = compiled_context(case, ref)
    envelope = invoke_fixture(provider, context)
    if envelope.get("status") == "timeout":
        return {**unavailable(case, body), "reason": "Offline provider attempt exceeded 20 seconds."}
    if envelope.get("status") != "ok":
        return unavailable(case, body)
    result = envelope.get("value")
    advice = safe_output(result, context)
    support = {"candidates": [row for row in context["candidates"] if row["id"] in advice["candidate_ids"]],
               "excerpts": [row for row in context["excerpts"] if row["id"] in advice["evidence_ids"]]}
    return {"status": "Offline fixture", "reason": "Labeled test response. Review supporting evidence before editing.",
            "revision": body["revision"], "advice": advice, "supporting_values": support}
