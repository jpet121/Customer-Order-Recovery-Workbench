import csv
import io
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from .contracts import Data, digest, encode, stamp

SAFE = {"job", "state", "status", "case_count", "revision", "hash", "operation", "attempt", "number",
        "verdict", "outcome", "reference", "prior_revision", "claim_revision", "attempts", "csv_hash",
        "purged_files", "purged_cases", "original_proof_hash", "receipt_id", "readback_hash", "paused", "checkpoint", "request_id", "code", "backup_created"}
EMPTY = digest(b"")
CREDENTIAL = re.compile(r"(?:api.?key|password|credential|bearer|secret|token)\s*[:=]|\b(?:sk|ghp|gho|xoxb)-[A-Za-z0-9_-]{8,}", re.I)
COLUMNS = ("event_id", "timestamp_utc", "actor", "case_id", "event", "metadata", "previous_hash", "hash")


def spreadsheet(value: str) -> str:
    if value[:1] in {"\t", "\r", "\n"} or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def sanitize(value: str) -> str:
    return "[REDACTED]" if CREDENTIAL.search(value) else spreadsheet(value)


def projection(event_id: int, timestamp: datetime, actor: str, case: UUID | None, event: str, detail: Data) -> list[str]:
    safe = {key: sanitize(value) if isinstance(value, str) else value
            for key, value in detail.items() if key in SAFE and isinstance(value, (str, int, bool))}
    return [str(event_id), stamp(timestamp), sanitize(actor), str(case or ""), event, encode(safe).decode()]


def link(previous: str, row: list[str]) -> str:
    return digest(encode([previous, row]))


def verify(raw: bytes) -> bool:
    source = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""))
    if next(source, None) != list(COLUMNS):
        return False
    chains: dict[str, str] = {}
    previous_id = 0
    for row in source:
        if len(row) != len(COLUMNS) or not row[0].isascii() or not row[0].isdigit() or int(row[0]) <= previous_id:
            return False
        previous_id = int(row[0])
        scope, previous, hash_ = row[3], row[-2], row[-1]
        if chains.get(scope, previous) != previous or link(previous, row[:-2]) != hash_:
            return False
        chains[scope] = hash_
    return True
