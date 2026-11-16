import re
from datetime import date
from typing import Any, Iterator

from .contracts import Data, Problem, digest, fields, object_json, utc

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 25_000
PROFILE = "cedar-v1"
CUSTOMER_FIELDS = (
    "source_customer_id", "display_name", "country_code", "postal_code", "customer_reference"
)
ORDER_FIELDS = (
    "source_order_id", "line_no", "customer_reference", "order_date", "item_reference",
    "quantity", "unit_code", "unit_price", "currency_code", "ship_to_reference"
)
ID_FIELDS = {"source_customer_id", "source_order_id", "customer_reference", "item_reference", "ship_to_reference"}
NAMES = {"customers.csv": ("customer", CUSTOMER_FIELDS), "orders.csv": ("order", ORDER_FIELDS)}
CELL = re.compile(r'(?:"((?:[^"]|"")*)"|([^,\r\n"]*))(,|\r\n|\n|\Z)')
INT = re.compile(r"[0-9]+")
DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def rejected(code: str, message: str, **context: Any) -> Problem:
    return Problem(422, code, message, **context)


def bundle_size_error(size: int) -> Problem:
    return rejected("BUNDLE_SIZE", "Bundle exceeds 20 MiB.", file="bundle",
                    field="combined_bytes", byte=MAX_BYTES, expected=MAX_BYTES, received=size)


def records(csv_text: str, file_name: str = "CSV") -> Iterator[tuple[int, list[str]]]:
    offset, line = 0, 1
    while offset < len(csv_text):
        start = line
        row: list[str] = []
        while True:
            match = CELL.match(csv_text, offset)
            if not match:
                raise rejected("CSV_SYNTAX", "Invalid quoting or line ending.", file=file_name, line=line)
            quoted, plain, delimiter = match.groups()
            row.append(quoted.replace('""', '"') if quoted is not None else plain)
            line += match[0].count("\n")
            offset = match.end()
            if delimiter != ",":
                break
        if row == [""] and match[0] in {"\n", "\r\n"}:
            raise rejected("BLANK_RECORD", "Blank records are disallowed.", file=file_name, line=start)
        yield start, row


def key_valid(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 64 and "\x00" not in value
            and not any(0xD800 <= ord(character) <= 0xDFFF for character in value))


def manifest_entries(raw: bytes, csv_bytes: bytes, kind: str) -> tuple[Data, dict[str, Data]]:
    key_name = f"source_{kind}_id"
    try:
        manifest = object_json(raw)
        required = {"schema_version", "source_profile_id", "attempt_id", "file_sha256", "attempted_at", "failed_entities"}
        if (not fields(manifest, required) or type(manifest["schema_version"]) is not int
                or manifest["schema_version"] != 1 or manifest["source_profile_id"] != PROFILE
                or not key_valid(manifest["attempt_id"]) or not isinstance(manifest["failed_entities"], list)):
            raise ValueError("Invalid manifest fields or profile.")
        utc(manifest["attempted_at"])
        if manifest["file_sha256"] != digest(csv_bytes):
            raise rejected("CSV_HASH", "Manifest SHA-256 does not match CSV bytes.",
                           file="errors.json", field="file_sha256")
        entries = {}
        for index, entry in enumerate(manifest["failed_entities"]):
            required = {key_name, "errors"} | ({"expected_line_numbers"} if kind == "order" else set())
            if not fields(entry, required, frozenset({"dispatch_deadline"})):
                key = entry.get(key_name) if isinstance(entry, dict) else None
                missing = sorted(required - entry.keys()) if isinstance(entry, dict) else sorted(required)
                raise rejected("MANIFEST_SCHEMA", "Invalid manifest entity fields.",
                               file="errors.json", key=key if key_valid(key) else None, field=f"failed_entities[{index}]",
                               missing=missing, expected=sorted(required))
            key = entry[key_name]
            if not key_valid(key) or key in entries:
                raise rejected("MANIFEST_KEY", "Invalid or duplicate entity key.", key=key if key_valid(key) else None,
                               file="errors.json", field=f"failed_entities[{index}].{key_name}")
            if "dispatch_deadline" in entry:
                utc(entry["dispatch_deadline"])
            errors = entry["errors"]
            if not isinstance(errors, list) or not errors or any(
                not fields(e, {"error_code", "message"}) or not key_valid(e["error_code"])
                or not isinstance(e["message"], str) or len(e["message"]) > 2000 or "\x00" in e["message"]
                or any(0xD800 <= ord(c) <= 0xDFFF for c in e["message"])
                for e in errors
            ):
                raise rejected("MANIFEST_ERRORS", "Entity requires typed errors and messages of at most 2,000 characters.", key=key,
                               file="errors.json", field=f"failed_entities[{index}].errors")
            if kind == "order":
                lines = entry["expected_line_numbers"]
                if not isinstance(lines, list) or not lines or any(type(n) is not int or not 1 <= n <= 100 for n in lines) or len(set(lines)) != len(lines):
                    raise rejected("MANIFEST_LINES", "Expected distinct original line numbers from 1 to 100.", key=key,
                                   expected=lines if isinstance(lines, list) and all(type(n) is int for n in lines) else None,
                                   file="errors.json", field=f"failed_entities[{index}].expected_line_numbers")
            entries[key] = entry
        return manifest, entries
    except (ValueError, RecursionError) as error:
        raise rejected("MANIFEST_SCHEMA", "Invalid UTF-8 JSON manifest, fields or timestamps.",
                       file="errors.json", field="manifest") from error


def parse(file_name: str, csv_bytes: bytes, manifest_bytes: bytes) -> tuple[str, Data, dict[str, Data]]:
    if file_name not in NAMES:
        raise rejected("FILE_NAME", "Choose customers.csv or orders.csv.", file="bundle", field="file_name")
    if len(csv_bytes) + len(manifest_bytes) > MAX_BYTES:
        raise bundle_size_error(len(csv_bytes) + len(manifest_bytes))
    kind, required_fields = NAMES[file_name]
    key_name = f"source_{kind}_id"
    manifest, entries = manifest_entries(manifest_bytes, csv_bytes, kind)
    try:
        source = csv_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise rejected("CSV_ENCODING", "CSV must be UTF-8 without BOM.",
                       file=file_name, byte=error.start, line=csv_bytes[:error.start].count(b"\n") + 1) from error
    if source.startswith("\ufeff") or "\x00" in source:
        offset = 0 if source.startswith("\ufeff") else csv_bytes.index(b"\x00")
        raise rejected("CSV_ENCODING", "CSV contains a BOM or NUL character.",
                       file=file_name, byte=offset, line=csv_bytes[:offset].count(b"\n") + 1)
    rows = records(source, file_name)
    _, headers = next(rows, (1, []))
    extra = {"dealer_description"} if kind == "order" else set()
    if len(headers) != len(set(headers)) or not set(required_fields) <= set(headers) <= set(required_fields) | extra:
        raise rejected("CSV_HEADER", "Header is missing, duplicated or unknown.", file=file_name,
                       line=1, expected=list(required_fields), received=headers)
    cases: dict[str, Data] = {}
    for index, (line, values) in enumerate(rows, 1):
        if index > MAX_ROWS:
            raise rejected("CSV_LIMIT", "CSV exceeds 25,000 data rows.", file=file_name, line=line)
        if len(values) != len(headers):
            raise rejected("CSV_WIDTH", "Record has the wrong field count.", file=file_name,
                           line=line, expected=len(headers), received=len(values))
        row = dict(zip(headers, values))
        key = row[key_name]
        if not key_valid(key):
            raise rejected("CSV_KEY", "Invalid transport identity.", file=file_name, line=line, field=key_name)
        for field, value in row.items():
            if len(value) > 1000:
                raise rejected("CSV_FIELD_LIMIT", "Field exceeds 1,000 characters.", file=file_name, line=line, field=field)
        item = cases.setdefault(key, {"rows": [], "locations": [], "line_numbers": []})
        if kind == "customer" and item["rows"]:
            raise rejected("CSV_KEY_DUPLICATE", "A customer occurs more than once.", file=file_name, key=key, line=line)
        if kind == "order":
            check_order_syntax(row, line)
            item["line_numbers"].append(int(row["line_no"]))
            if len(item["line_numbers"]) > 100:
                raise rejected("CSV_LINE_LIMIT", "Order exceeds 100 lines.", file=file_name, key=key, line=line)
        item["rows"].append({field: value if value != "" else None for field, value in row.items()})
        item["locations"].append(line)
    missing = sorted(entries.keys() - cases.keys())
    extra_keys = sorted(cases.keys() - entries.keys())
    if missing or extra_keys:
        raise rejected("ENTITY_COVERAGE", "CSV and manifest keys differ.", file="bundle",
                       field="entity_membership", missing=missing, extra=extra_keys)
    for key, item in cases.items():
        entry = entries[key]
        if kind == "order":
            expected, actual = sorted(entry["expected_line_numbers"]), sorted(item["line_numbers"])
            if expected != actual:
                raise rejected("LINE_COVERAGE", "CSV and original order line membership differ.", file=file_name,
                               field="line_no", key=key, expected=expected, received=actual, locations=item["locations"])
        item.update(errors=entry["errors"], deadline=entry.get("dispatch_deadline"))
    return kind, manifest, cases


def check_order_syntax(row: dict[str, str], line: int) -> None:
    if not INT.fullmatch(row["line_no"]) or not 1 <= int(row["line_no"]) <= 100:
        raise rejected("LINE_NUMBER", "Line number must be 1 to 100.", file="orders.csv", line=line, field="line_no")
    for field in ("quantity", "unit_price"):
        if row[field] and not DECIMAL.fullmatch(row[field]):
            raise rejected("DECIMAL_SYNTAX", "Use unsigned plain ASCII decimal notation.", file="orders.csv", line=line, field=field)
    if not row["order_date"]:
        return
    try:
        if not DATE.fullmatch(row["order_date"]):
            raise ValueError
        date.fromisoformat(row["order_date"])
    except ValueError as error:
        raise rejected("DATE_SYNTAX", "Use a calendar date in YYYY-MM-DD format.", file="orders.csv", line=line, field="order_date") from error
