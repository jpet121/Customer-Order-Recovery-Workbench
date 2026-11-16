from concurrent.futures import ThreadPoolExecutor
from dis import get_instructions, opmap
from functools import partial
import json
from json import dumps
from pathlib import Path
from time import monotonic
from types import FunctionType, ModuleType
from uuid import uuid4

from iew.advisory import compiled_context, invoke_fixture, safe_output
from iew.contracts import Data, Problem
from iew.fixture_serialization import MAX_BYTES, MAX_VISITS
from iew.inference_budget import Reservations
from iew.store import Store, default_reference

import pytest


def test_allowlisted_advice_and_forbidden_patch():
    reference = default_reference()
    case = {"kind": "order", "data": {"revision": 1, "rows": [
        {"item_reference": "ITEM-1", "dealer_description": "gloves", "unit_price": "100",
         "secret_token": "OUT-OF-SCOPE"}
    ]}}
    context = compiled_context(case, reference)
    assert [item["id"] for item in context["candidates"]] == ["ITEM-1"]
    assert "unit_price" not in context["record"][0] and "secret_token" not in context["record"][0]
    response = {"schema_version": 1, "disposition": "suggest", "explanation": "Matches the approved item.",
                "evidence_ids": ["BR02"], "candidate_ids": ["ITEM-1"], "proposed_patch": {"item_reference":"ITEM-1"},
                "question": None}
    assert safe_output(response, context) == response
    with pytest.raises(Problem):
        safe_output({**response, "proposed_patch": {"unit_price": "0"}}, context)


def test_concurrent_worst_case_budget_reservations_and_unknown_usage(database):
    reservations = Reservations(Store(database[0]))
    plan = {"components": {"development":"10","primary":"35","stress":"4","retries":"1"},
            "primary_slots":720,"stress_cases":100,"approved_terms":True,"priced_configuration":True,
            "separate_call_authorization":True}

    def reserve(_):
        try:
            return reservations.reserve("labeled-local-test", plan, str(uuid4()), 1, "31")
        except Problem:
            return None

    with ThreadPoolExecutor(2) as executor:
        keys = [key for key in executor.map(reserve, [0, 1]) if key]
    assert len(keys) == 1
    reservations.account(keys[0], None)
    assert reserve(0) is None
    reservations.account(keys[0], "1")
    assert reserve(0) is not None


def test_offline_fixture_rejects_custom_hooks_in_captured_values(tmp_path):
    marker = tmp_path / "reducer"

    class CustomValue:
        def __reduce__(self):
            marker.touch()
            return str, ("unexpected",)

    capture = CustomValue()

    def fixture(_):
        return str(capture)

    assert invoke_fixture(fixture, {}) == {"status": "setup_error"}
    assert not marker.exists()


@pytest.mark.parametrize("capture", ["x" * (MAX_BYTES + 1), 1 << MAX_BYTES,
                                    [False] * MAX_VISITS, ["x" * MAX_BYTES, b"x" * MAX_BYTES]],
                         ids=["oversized-string", "oversized-integer", "too-many-values", "oversized-payload"])
def test_offline_fixture_rejects_oversized_serialization(capture):
    def fixture(_):
        return capture

    assert invoke_fixture(fixture, {}) == {"status": "setup_error"}


def test_offline_fixture_copies_plain_captures_to_worker(tmp_path):
    marker = tmp_path / "fixture-ran"
    values = {"labels": ["one", "two"]}

    def fixture(context: dict[str, bool], record: bool) -> dict[str, object]:
        marker.write_text(Path.__name__)
        labels = list(label for label in values["labels"])
        return {"labels": labels, "context": json.loads(dumps(context)), "ran": record and monotonic() > 0}

    context = {"accepted": True}
    assert invoke_fixture(partial(fixture, record=True), context) == {
        "status": "ok", "value": {"labels": ["one", "two"], "context": context, "ran": True}}
    assert marker.read_text() == "Path"


def test_offline_fixture_never_calls_function_or_module_namespace_hooks():
    def fixture(_):
        return target_name

    class HookDict(dict):
        def __contains__(self, key):
            raise AssertionError("Globals lookup ran a caller hook")

    class CollidingKey:
        def __hash__(self):
            return hash("target_name")

        def __eq__(self, other):
            raise AssertionError("Globals lookup ran a custom equality check")

    for namespace in (HookDict(target_name=True), {CollidingKey(): True}):
        assert invoke_fixture(FunctionType(fixture.__code__, namespace), {}) == {"status": "setup_error"}

    class ModuleKey(CollidingKey):
        def __hash__(self):
            return hash("__name__")

    module = ModuleType("synthetic")
    module.__dict__.clear()
    module.__dict__[ModuleKey()] = "synthetic"

    def captured_module(_):
        return module

    assert invoke_fixture(captured_module, {}) == {"status": "setup_error"}


def test_offline_fixture_never_calls_hooks_on_known_configuration_instances():
    def hook(*_):
        raise AssertionError("Pickle ran an injected configuration hook")

    store = Store("unused")
    store.__reduce_ex__ = hook

    def fixture(_):
        return store

    assert invoke_fixture(fixture, {}) == {"status": "setup_error"}


def test_offline_fixture_never_calls_hooks_in_code_metadata():
    class Constants(tuple):
        def __len__(self):
            raise AssertionError("Pickle inspected overridden constants")

    class SourceName(str):
        def __hash__(self):
            raise AssertionError("Pickle hashed overridden code metadata")

    def fixture(_):
        return 1

    for change in ({"co_consts": Constants((None, 1))}, {"co_name": SourceName("fixture")},
                   {"co_consts": (None, [])}, {"co_linetable": bytes(MAX_BYTES)}):
        updated = FunctionType(fixture.__code__.replace(**change), {})
        assert invoke_fixture(updated, {}) == {"status": "setup_error"}


def test_offline_fixture_does_not_iterate_overridden_path_parts():
    class PathParts(list):
        def __iter__(self):
            raise AssertionError("Pickle iterated overridden path parts")

    path = Path("synthetic")
    path._raw_paths = PathParts()

    def fixture(_):
        return path

    assert invoke_fixture(fixture, {}) == {"status": "setup_error"}


def test_fixture_named_like_stdlib_uses_its_own_plain_namespace():
    def fixture(_):
        return "synthetic"

    namespace = {"__name__": "json"}
    local = FunctionType(fixture.__code__, namespace)
    local.__qualname__ = "fixture"
    namespace["fixture"] = local
    assert invoke_fixture(local, {}) == {"status": "ok", "value": "synthetic"}


def test_fixture_can_use_the_project_context_annotation():
    def fixture(context: Data) -> Data:
        return context

    assert invoke_fixture(fixture, {"accept": True}) == {"status": "ok", "value": {"accept": True}}


def test_fixture_never_calls_hooks_on_captured_code():
    class UnsafeNames(tuple):
        def __iter__(self):
            raise AssertionError("Serializing captured code iterated overridden names")

    def ordinary(_):
        return len([])

    code = ordinary.__code__.replace(co_names=UnsafeNames(ordinary.__code__.co_names))

    def fixture(_):
        return code

    assert invoke_fixture(fixture, {}) == {"status": "setup_error"}


def test_fixture_serialization_visits_shared_code_constants_once():
    def ordinary(_):
        return "synthetic"

    leaf = ordinary.__code__
    code = leaf
    for _ in range(48):
        code = leaf.replace(co_consts=(*leaf.co_consts, code, code))
    fixture = FunctionType(code, {})
    assert invoke_fixture(fixture, {}, timeout=2) == {"status": "ok", "value": "synthetic"}


MODULE_TEXT = "parent"


def test_fixture_preserves_read_and_write_globals_in_nested_code():
    def fixture(_):
        def nested():
            global MODULE_TEXT
            previous = MODULE_TEXT
            MODULE_TEXT = "worker"
            return [previous, MODULE_TEXT]
        return nested()

    assert invoke_fixture(fixture, {}) == {"status": "ok", "value": ["parent", "worker"]}
    assert MODULE_TEXT == "parent"


def test_fixture_with_invalid_global_operand_returns_setup_error():
    def ordinary(_):
        return len([])

    code = ordinary.__code__
    global_reference = next(op for op in get_instructions(code) if op.opname == "LOAD_GLOBAL")
    bytecode = bytearray(code.co_code)
    bytecode[global_reference.offset + 1] = 254
    fixture = FunctionType(code.replace(co_code=bytes(bytecode)), {})
    assert invoke_fixture(fixture, {}) == {"status": "setup_error"}


def test_fixture_rejects_a_negative_extended_global_operand():
    from pickle import PicklingError
    from iew.fixture_serialization import serialize_fixture

    def fixture(_):
        return len([])

    code = fixture.__code__
    global_op = next(op for op in get_instructions(code) if op.opname == "LOAD_GLOBAL")
    prefix = bytes((opmap["EXTENDED_ARG"], 255)) * 3
    before, after = code.co_code[:global_op.offset], code.co_code[global_op.offset + 2:]
    updated = code.replace(co_code=before + prefix + bytes((opmap["LOAD_GLOBAL"], 255)) + after)
    with pytest.raises(PicklingError, match="invalid global reference"):
        serialize_fixture(FunctionType(updated, {}), {}, monotonic() + 2)


def top_level_fixture(context):
    return context


def test_offline_fixture_copies_a_fixture_from_a_non_runtime_module():
    assert invoke_fixture(top_level_fixture, {"approve": True}) == {"status": "ok", "value": {"approve": True}}
