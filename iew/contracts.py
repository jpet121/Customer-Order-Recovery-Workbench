import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

type Data = dict[str, Any]


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str, **info: Any):
        super().__init__(detail)
        self.status = status
        self.body = {"code": code, "detail": detail, **info}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def no_duplicates(pairs: list[tuple[str, Any]]) -> Data:
    result: Data = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def reject_number(_: str) -> None:
    raise ValueError("Nonfinite JSON value")


def object_json(raw: bytes) -> Data:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=reject_number)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def fields(value: Any, required: set[str], optional: frozenset[str] = frozenset()) -> bool:
    return isinstance(value, dict) and required <= value.keys() <= required | optional


def utc(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value
    ):
        raise ValueError("Expected UTC timestamp with Z suffix")
    return datetime.fromisoformat(value)


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
