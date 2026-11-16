from datetime import date, timedelta
from decimal import Decimal
import re
from typing import Any

from .contracts import Data, Problem, digest, encode, now, utc
from .intake import CUSTOMER_FIELDS, DATE, ID_FIELDS, key_valid

ORDER_HEAD = ("source_order_id", "customer_reference", "order_date", "currency_code")
ORDER_LINE = ("item_reference", "quantity", "unit_code", "unit_price", "ship_to_reference")
EDITABLE = {"customer_reference", "ship_to_reference", "item_reference", "unit_code", "order_date"}
VERSION = "cedar-business-v1"


def ranked_items(rows: list[Data], ref: Data) -> list[tuple[str, str]]:
    words = set(re.findall(r"[\w-]+", " ".join(
        str(row.get(key) or "") for row in rows for key in ("dealer_description", "item_reference")
    ).casefold()))
    matches = []
    for key, item in ref["items"].items():
        description = item["description"]
        description_words = set(re.findall(r"[\w-]+", description.casefold()))
        rank = (10000 if key.casefold() in words else 0) + len(words & description_words)
        if rank:
            matches.append((-rank, key, description))
    return [(key, description) for _, key, description in sorted(matches)]


def identity(ref: Data, key: str, exact_reference: str | None) -> Data | None:
    record = ref["identities"].get(key)
    if not record or exact_reference != record.get("reference") or exact_reference in ref["customers"]:
        return None
    if sum(r.get("reference") == exact_reference or r.get("id") == record.get("id")
           for r in ref["identities"].values()) != 1:
        return None
    if not all(record.get(field) for field in ("id", "version", "steward", "evidence", "ship_to")):
        return None
    return record


def validation(kind: str, key: str, rows: list[Data], ref: Data, binding: Data | None) -> list[Data]:
    issues: list[Data] = []

    def issue(field: str, code: str, detail: str, line: Any = None) -> None:
        issues.append({"field": field, "code": code, "detail": detail, "line": line})

    try:
        age = now() - utc(ref["created"])
        if age < timedelta(0) or age > timedelta(hours=24):
            issue("reference", "REFERENCE_STALE", "Publish a current reference snapshot.")
    except ValueError:
        issue("reference", "REFERENCE_STALE", "Reference timestamp is invalid.")
    first = rows[0]
    required = CUSTOMER_FIELDS if kind == "customer" else ORDER_HEAD + ORDER_LINE
    for row in rows:
        line = row.get("line_no")
        for field in required:
            value = row.get(field)
            if value is None:
                issue(field, "REQUIRED", "Supply a value.", line)
            elif field in ID_FIELDS and not key_valid(value):
                issue(field, "REFERENCE_LENGTH", "Identifier must contain 1 to 64 characters.", line)
        if kind == "customer":
            if row["country_code"] not in ref["countries"]:
                issue("country_code", "COUNTRY", "Country is absent from the approved reference snapshot.")
            if not identity(ref, key, row["customer_reference"]):
                issue("customer_reference", "IDENTITY", "A current steward-approved identity for this exact reference is required.")
            continue
        for field in ORDER_HEAD:
            if row[field] != first[field]:
                issue(field, "ORDER_HEADER", "This value must agree across the entire order.", line)
        if row["currency_code"] not in ref["currencies"]:
            issue("currency_code", "CURRENCY", "Currency is absent from the approved snapshot.", line)
        item = ref["items"].get(row["item_reference"])
        if not item:
            issue("item_reference", "ITEM", "An approved item reference is required.", line)
        if row["unit_code"] not in ref["units"] or (item and row["unit_code"] not in item["units"]):
            issue("unit_code", "UNIT", "Unit is invalid for this item.", line)
        for field, scale in (("quantity", 3), ("unit_price", 2)):
            if row[field] is None:
                continue
            value = Decimal(row[field])
            if len(row[field].partition(".")[2]) > scale or (field == "quantity" and not 0 < value <= 100_000):
                issue(field, "DECIMAL_RANGE", "Value or decimal scale is outside the pilot contract.", line)
        if not binding or binding.get("blocked"):
            issue("customer_reference", "DEPENDENCY", "Confirm the exact order customer reference.", line)
        elif row["ship_to_reference"] not in binding["ship_to"]:
            issue("ship_to_reference", "SHIP_TO", "Use an approved ship-to for this customer.", line)
    return issues


def plain_decimal(value: str) -> str:
    normalized = format(Decimal(value), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def destination_payload(kind: str, rows: list[Data], binding: Data | None) -> Data:
    if kind == "customer":
        return {field: rows[0][field] for field in CUSTOMER_FIELDS}
    assert binding is not None
    header = {field: rows[0][field] for field in ORDER_HEAD}
    header["destination_customer_id"] = binding["id"]
    header["lines"] = [
        {**{field: row[field] for field in ORDER_LINE}, "line_no": int(row["line_no"]),
         "quantity": plain_decimal(row["quantity"]), "unit_price": plain_decimal(row["unit_price"])}
        for row in sorted(rows, key=lambda item: int(item["line_no"]))
    ]
    return header


def apply_patch(kind: str, rows: list[Data], changes: Any) -> list[Data]:
    line_fields = {"ship_to_reference", "item_reference", "unit_code"}
    permitted = {"customer_reference"} if kind == "customer" else {"customer_reference", "order_date", "lines"}
    if not isinstance(changes, dict) or not changes.keys() <= permitted:
        raise Problem(422, "PATCH_FIELDS", "Use approved header fields and individual order line edits.")
    updated = [row.copy() for row in rows]
    for field, value in changes.items():
        if field == "lines":
            continue
        check_edit(field, value)
        for row in updated:
            row[field] = value
    if "lines" in changes:
        by_line = {str(int(row["line_no"])): row for row in updated}
        line_changes = changes["lines"]
        if not isinstance(line_changes, dict) or not line_changes.keys() <= by_line.keys():
            raise Problem(422, "PATCH_LINES", "Use original order line numbers for corrections.")
        for line, edit in line_changes.items():
            if not isinstance(edit, dict) or not edit.keys() <= line_fields:
                raise Problem(422, "PATCH_FIELDS", "Only item, unit and ship-to are editable on an order line.", line=line)
            for field, value in edit.items():
                check_edit(field, value)
                by_line[line][field] = value
    return updated


def check_edit(field: str, value: Any) -> None:
    if not key_valid(value):
        raise Problem(422, "PATCH_VALUE", "Supply a valid field value.", field=field)
    if field == "order_date":
        try:
            if not DATE.fullmatch(value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as error:
            raise Problem(422, "PATCH_DATE", "Enter an unambiguous ISO date.", field=field) from error


def preview_record(kind: str, rows: list[Data], binding: Data | None) -> Data:
    payload = destination_payload(kind, rows, binding)
    return {"payload": payload, "hash": digest(encode(payload)), "canonical": encode(payload).decode()}
