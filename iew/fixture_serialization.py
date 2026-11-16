"""Bounded serialization for trusted, in-process synthetic fixture functions."""

import dis
from importlib import import_module
from functools import partial
from io import BytesIO
from pathlib import Path, PosixPath, PurePath, PurePosixPath, PureWindowsPath, WindowsPath
from pickle import PicklingError
import sys
from time import monotonic
from types import BuiltinFunctionType, CellType, CodeType, FunctionType, GenericAlias, ModuleType
from typing import Any

from cloudpickle import Pickler  # type: ignore[import-untyped]
from cloudpickle.cloudpickle import (  # type: ignore[import-untyped]
    _builtin_type, _function_setstate, _make_cell, _make_empty_cell, _make_function,
)

from .contracts import Data, Problem
from .inference_budget import Reservations
from .store import Store

MAX_VISITS = 8192
MAX_BYTES = 128 * 1024
SERIALIZATION_ERRORS = (PicklingError, ValueError, TypeError, LookupError, RuntimeError)
LOAD_GLOBAL = dis.opmap["LOAD_GLOBAL"]
WRITTEN_GLOBALS = (dis.opmap["STORE_GLOBAL"], dis.opmap["DELETE_GLOBAL"])
PATH_TYPES = (PosixPath, PurePosixPath, PureWindowsPath, WindowsPath)
SCALAR_TYPES = (type(None), bool, float, complex, str, bytes, int, type(Ellipsis))
VALUE_TYPES = (*SCALAR_TYPES, tuple, list, dict, set, frozenset, CellType, CodeType, *PATH_TYPES)
REFERENCE_TYPES = (*VALUE_TYPES, object, Path, PurePath, Problem, GenericAlias, partial, Any, Data)

def restore_configuration(kind: type, field: str, data: Any) -> Any:
    instance: Any = object.__new__(kind)
    setattr(instance, field, data)
    return instance


def restore_module_member(module: str, name: str) -> Any:
    return getattr(import_module(module), name)


RECONSTRUCTORS = (_builtin_type, _function_setstate, _make_cell, _make_empty_cell, _make_function,
                  import_module, restore_configuration, restore_module_member)


class FixtureDeadline(Exception):
    """The serialization cutoff has passed."""


class FixturePickler(Pickler):  # type: ignore[misc]
    # Keep cloudpickle's code and closure-cell reducers. The global copyreg
    # registry can contain arbitrary caller code and is not used here.
    dispatch_table = {kind: Pickler.dispatch_table[kind] for kind in (CodeType, CellType)}

    def __init__(self, target: BytesIO, cutoff: float):
        from .controlled_inference import ControlledInference

        super().__init__(target)
        self.cutoff = cutoff
        self.visits = 0
        self.code_bytes = 0
        self.checked_code: dict[int, CodeType] = {}
        self.configuration_fields = ((ControlledInference, "reservations"), (Reservations, "store"), (Store, "dsn"))
        self.namespaces: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}

    def check_visit(self) -> None:
        if monotonic() >= self.cutoff:
            raise FixtureDeadline
        self.visits += 1
        if self.visits > MAX_VISITS:
            raise PicklingError("Offline fixture exceeds its serialization budget.")

    def persistent_id(self, value: Any) -> None:
        # Unlike reducer_override, this hook also visits built-in values.
        self.check_visit()
        kind = type(value)
        if kind is str or kind is bytes:
            if len(value) > MAX_BYTES:
                raise PicklingError("Offline fixture contains an oversized value.")
        if kind is int and value.bit_length() > MAX_BYTES:
            raise PicklingError("Offline fixture contains an oversized integer.")
        if (any(kind is supported for supported in VALUE_TYPES)
                or kind is FunctionType or kind is ModuleType or kind is partial or kind is GenericAlias
                or (kind is BuiltinFunctionType and type(value.__self__) is ModuleType)
                or any(value is supported for supported in REFERENCE_TYPES + RECONSTRUCTORS)
                or any(kind is config or value is config for config, _ in self.configuration_fields)):
            return None
        raise PicklingError("Use a fixture function with plain captured values.")

    def check_code(self, code: CodeType) -> None:
        if id(code) in self.checked_code:
            return
        self.check_visit()
        constants = code.co_consts
        if type(constants) is not tuple or len(constants) > MAX_VISITS:
            raise PicklingError("Offline fixture code exceeds its serialization budget.")
        metadata = (code.co_code, code.co_linetable, code.co_exceptiontable, code.co_filename, code.co_name,
                    code.co_qualname, code.co_names, code.co_varnames, code.co_cellvars, code.co_freevars)
        for value in metadata:
            self.check_constant(value)
        for constant in constants:
            self.check_constant(constant)
        self.checked_code[id(code)] = code

    def check_constant(self, value: Any) -> None:
        # Check metadata before delegating any code reduction to cloudpickle.
        self.persistent_id(value)
        kind = type(value)
        if kind is str or kind is bytes:
            self.code_bytes += len(value)
            if self.code_bytes > MAX_BYTES:
                raise PicklingError("Offline fixture code exceeds its serialization budget.")
        if kind is CodeType:
            self.check_code(value)
        elif kind is tuple or kind is frozenset:
            for item in value:
                self.check_constant(item)
        elif not any(kind is scalar for scalar in SCALAR_TYPES):
            raise PicklingError("Offline fixture code contains a nonconstant value.")

    def read_namespace(self, namespace: dict[str, Any]) -> dict[str, Any]:
        if type(namespace) is not dict:
            raise PicklingError("Offline fixtures require a plain namespace.")
        plain = {}
        # Iterate exact dict entries before any lookup that could compare a
        # foreign key. Keep only string keys with bounded size.
        for key, value in namespace.items():
            self.check_visit()
            if type(key) is not str or len(key) > MAX_BYTES:
                raise PicklingError("Offline fixture has an invalid namespace key.")
            plain[key] = value
        return plain

    def function_namespaces(self, function: FunctionType) -> tuple[dict[str, Any], dict[str, Any]]:
        key = id(function.__globals__)
        if key not in self.namespaces:
            source = self.read_namespace(function.__globals__)
            base = {name: source[name] for name in ("__package__", "__name__", "__file__") if name in source}
            self.namespaces[key] = source, base
        return self.namespaces[key]

    def global_names(self, code: CodeType) -> dict[str, None]:
        # Do not hash a code object. Shared child constants form a graph and
        # CPython's structural hash can visit a shared child exponentially.
        names: dict[str, None] = {}
        visited: set[int] = set()
        pending = [code]
        while pending:
            current = pending.pop()
            key = id(current)
            if key in visited:
                continue
            visited.add(key)
            # CPython 3.12's bytecode decoder skips inline caches and handles
            # EXTENDED_ARG without formatting constants or hashing CodeType.
            for _, opcode, argument in getattr(dis, "_unpack_opargs")(current.co_code):
                self.check_visit()
                if opcode == LOAD_GLOBAL or opcode in WRITTEN_GLOBALS:
                    if argument < 0:
                        raise PicklingError("Offline fixture contains an invalid global reference.")
                    index = argument >> 1 if opcode == LOAD_GLOBAL else argument
                    names[current.co_names[index]] = None
            pending.extend(value for value in current.co_consts if type(value) is CodeType)
        return names

    def reduce_function(self, function: FunctionType) -> Any:
        source, base = self.function_namespaces(function)
        module, name = source.get("__name__"), function.__qualname__
        if (type(module) is str and len(module) <= MAX_BYTES and type(name) is str and len(name) <= MAX_BYTES
                and source.get(name) is function):
            root = module.partition(".")[0]
            if root == "iew" or root in sys.stdlib_module_names:
                owner = sys.modules.get(module)
                if type(owner) is ModuleType and vars(owner) is function.__globals__:
                    return restore_module_member, (module, name)

        code = function.__code__
        self.check_code(code)
        # Keep only referenced globals. Resolve imports in the owned worker.
        names = self.global_names(code)
        globals_used = {name: source[name] for name in names if name in source}
        state = {"__name__": function.__name__, "__qualname__": name, "__doc__": function.__doc__,
                 "__module__": function.__module__, "__defaults__": function.__defaults__,
                 "__kwdefaults__": function.__kwdefaults__, "__annotations__": function.__annotations__,
                 "__closure__": function.__closure__, "__globals__": globals_used, "_cloudpickle_submodules": ()}
        cells = tuple(_make_empty_cell() for _ in code.co_freevars) if function.__closure__ is not None else None
        return _make_function, (code, base, None, None, cells), (function.__dict__, state), None, None, _function_setstate

    def reducer_override(self, value: Any) -> Any:
        kind = type(value)
        if kind is CodeType:
            self.check_code(value)
            return NotImplemented
        if value is Data:
            return restore_module_member, ("iew.contracts", "Data")
        if kind is FunctionType and not any(value is helper for helper in RECONSTRUCTORS):
            return self.reduce_function(value)
        if kind is ModuleType:
            return import_module, (self.read_namespace(vars(value))["__name__"],)
        if kind is BuiltinFunctionType:
            return restore_module_member, (self.read_namespace(vars(value.__self__))["__name__"], value.__name__)
        if kind is partial:
            return partial.__reduce__(value)
        if kind is GenericAlias:
            return GenericAlias, (value.__origin__, value.__args__)
        if any(kind is path_type for path_type in PATH_TYPES):
            # Reading the stored strings avoids PurePath.parts, which can
            # invoke methods on a caller-provided string subclass.
            parts = value._raw_paths
            if type(parts) is not list or len(parts) > MAX_VISITS:
                raise PicklingError("Offline fixture has invalid path parts.")
            return kind, tuple(parts)
        if kind is type:
            return super().reducer_override(value)
        for configuration, field in self.configuration_fields:
            if kind is configuration:
                state = self.read_namespace(vars(value))
                if state.keys() != {field}:
                    raise PicklingError("Offline fixture configuration contains unexpected state.")
                return restore_configuration, (kind, field, state[field])
        return NotImplemented


class FixturePayload(BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self.limit:
            raise PicklingError("Offline fixture payload is too large.")
        return super().write(data)


def serialize_fixture(provider: Any, context: Any, cutoff: float, *, payload_limit: int = MAX_BYTES) -> bytes:
    target = FixturePayload(payload_limit)
    FixturePickler(target, cutoff).dump((provider, context))
    return target.getvalue()
