import csv
import io
import json
from itertools import product
from random import Random

import pytest

from iew import rules
from iew.contracts import digest
from iew.intake import ORDER_FIELDS, parse


# Fixed source values make the expected destination representation independent of the implementation.
EXPECTED = (b'{"currency_code":"USD","customer_reference":"REF-EXIST","destination_customer_id":"D-EXIST",'
            b'"lines":[{"item_reference":"ITEM-1","line_no":1,"quantity":"2","ship_to_reference":"MAIN","unit_code":"EA","unit_price":"1.3"},'
            b'{"item_reference":"ITEM-1","line_no":4,"quantity":"2","ship_to_reference":"MAIN","unit_code":"EA","unit_price":"1.3"}],'
            b'"order_date":"2026-11-14","source_order_id":"O900"}')


def header_orders():
    shuffled = Random(20261115)
    return [ORDER_FIELDS, tuple(reversed(ORDER_FIELDS)),
            *(tuple(shuffled.sample(ORDER_FIELDS, len(ORDER_FIELDS))) for _ in range(40))]


@pytest.mark.parametrize("headers,line_numbers,quantity,newline", product(
    header_orders(), ((1, 4), (4, 1)), ("2", "02.000", "2.00"), ("\n", "\r\n")), ids=str)
def test_equivalent_lexical_forms_preserve_approved_whole_order(headers, line_numbers, quantity, newline):
    values = dict(zip(ORDER_FIELDS, ("O900","1","REF-EXIST","2026-11-14","ITEM-1",quantity,"EA","01.30","USD","MAIN")))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator=newline)
    writer.writerow(headers)
    for line in line_numbers:
        values["line_no"] = str(line)
        writer.writerow(values[field] for field in headers)
    source = output.getvalue().encode()
    manifest = {"schema_version": 1, "source_profile_id": "cedar-v1", "attempt_id": "original",
        "file_sha256": digest(source), "attempted_at": "2026-11-14T12:00:00Z",
        "failed_entities": [{"source_order_id": "O900", "expected_line_numbers": [4,1],
          "errors":[{"error_code":"REFERENCE","message":"review"}]}]}
    kind, _, cases = parse("orders.csv", source, json.dumps(manifest).encode())
    binding = {"id":"D-EXIST"}
    preview = rules.preview_record(kind, cases["O900"]["rows"], binding)
    assert preview["canonical"].encode() == EXPECTED
    assert preview["hash"] == digest(EXPECTED)
    assert rules.preview_record(kind, cases["O900"]["rows"], {"id":"D-OTHER"})["hash"] != preview["hash"]
