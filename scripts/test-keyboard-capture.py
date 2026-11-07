#!/usr/bin/env python3
"""Only synthetic events; never opens an input device or starts a live capture."""
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "packaging/ubuntu-gts9u-companion/usr/libexec/tab-companion-hardware"
module = runpy.run_path(str(source))
capture = module["KeyboardCapture"]()
with patch.object(module["time"], "monotonic", return_value=100):
    capture.record(30, 1)
    assert not capture.events
    capture.begin(":test-owner")
    capture.record(30, 1)
with patch.object(module["time"], "monotonic", return_value=119.999):
    capture.record(30, 2)
    capture.record(30, 0)
with patch.object(module["time"], "monotonic", return_value=120):
    capture.record(31, 1)
    assert len(capture.events) == 3
    try:
        capture.finish(":other-caller")
        raise AssertionError("Another caller could retrieve the keys")
    except ValueError:
        pass
    report = capture.finish(":test-owner")
    assert [e["value"] for e in report["events"]] == [1, 2, 0]
    assert capture.owner is None and not capture.events
    capture.record(32, 1)
    assert not capture.events
    capture.begin(":test-owner")
    for _ in range(10001):
        capture.record(30, 1)
    assert len(capture.events) == 10000 and capture.dropped == 1
    capture.clear()
    assert not capture.events and capture.owner is None
print("PASS opt-in capture, 20-second deadline, press/release/repeat, caller isolation, clearing and size cap")

# The capture needs the one-time token the root diagnostics helper writes.
import os
import tempfile
valid = module["capture_token_valid"]
lifetime = module["CAPTURE_TOKEN_LIFETIME"]
me = os.getuid()
with tempfile.TemporaryDirectory() as directory:
    token_file = Path(directory) / "keyboard-capture"
    assert not valid("abc", token_file, uid=me, now=10.0, issuer=me)
    token_file.write_text(f"{me} 10.000 {'ab' * 32}\n", encoding="ascii")
    assert valid("ab" * 32, token_file, uid=me, now=10.0, issuer=me)
    assert valid("ab" * 32, token_file, uid=me, now=10.0 + lifetime, issuer=me)
    assert not valid("ab" * 32, token_file, uid=me, now=10.1 + lifetime, issuer=me)
    assert not valid("ab" * 32, token_file, uid=me, now=9.0, issuer=me)
    assert not valid("ab" * 31 + "ac", token_file, uid=me, now=10.0, issuer=me)
    assert not valid("", token_file, uid=me, now=10.0, issuer=me)
    assert not valid("ab" * 32, token_file, uid=me + 1, now=10.0, issuer=me)
    # Only a root-owned file counts on the tablet.
    assert me == 0 or not valid("ab" * 32, token_file, uid=me, now=10.0)
    token_file.write_text("garbage\n", encoding="ascii")
    assert not valid("garbage", token_file, uid=me, now=10.0, issuer=me)
print("PASS capture token: owner, issuer, lifetime, exact match and malformed files")
