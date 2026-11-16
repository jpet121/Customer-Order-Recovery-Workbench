"""Disposable local fixture worker. Never a live-provider or network adapter."""
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from time import monotonic

import cloudpickle  # type: ignore[import-untyped]

from .contracts import Data, encode


def call_before(deadline: float) -> Data:
    if monotonic() >= deadline:
        return {"status": "deadline"}
    payload = sys.stdin.buffer.read()
    if monotonic() >= deadline:
        return {"status": "deadline"}
    try:
        provider, context = cloudpickle.loads(payload)
    except Exception:
        return {"status": "setup_error"}
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        if monotonic() >= deadline:
            return {"status": "deadline"}
        try:
            value = provider(context)
        except OSError:
            return {"status": "transport_error"}
    return {"status": "ok", "value": value}


def main() -> None:
    try:
        deadline = float(os.environ.get("IEW_OFFLINE_FIXTURE_DEADLINE", "-inf"))
        raw = encode(call_before(deadline))
        if len(raw) > 64 * 1024:
            raw = encode({"status": "unavailable"})
    except Exception:
        raw = encode({"status": "unavailable"})
    sys.stdout.buffer.write(raw)


if __name__ == "__main__":
    main()
