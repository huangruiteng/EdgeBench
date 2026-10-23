"""Repair one stale checksum in the published Order Addition judge at build time."""
import argparse
import hashlib
from pathlib import Path


TEST_SHA256 = "4f1d5fad34da78a1c4083009c0ebcd6de0f2853c13e380ab446b4ad51d8301cc"
OLD_HELPER_SHA256 = "3023b9a449119e862d4ca86d3ab45599e2496e182be82959a642522f915dbbac"
HELPER_SHA256 = "337837af7067b3dae8d4ef068d26d8dd8ff779f9a627d95451af2ca411c99630"


def repair(source: bytes, private_helper: bytes, public_helper: bytes) -> bytes:
    """Accept only the inspected release, never learn a checksum from a submission."""
    if hashlib.sha256(source).hexdigest() != TEST_SHA256:
        raise ValueError("Unsupported judge test revision")
    for helper in (private_helper, public_helper):
        if hashlib.sha256(helper).hexdigest() != HELPER_SHA256:
            raise ValueError("Unsupported score helper revision")
    if private_helper != public_helper:
        raise ValueError("Public and private score helpers differ")
    old = f'EXPECTED_SCORE_HELPER_SHA256 = "{OLD_HELPER_SHA256}"'.encode()
    new = f'EXPECTED_SCORE_HELPER_SHA256 = "{HELPER_SHA256}"'.encode()
    if source.count(old) != 1:
        raise ValueError("Expected exactly one checksum declaration")
    result = source.replace(old, new, 1)
    compile(result, "<repaired judge>", "exec")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("private_helper", type=Path)
    parser.add_argument("public_helper", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = repair(args.source.read_bytes(), args.private_helper.read_bytes(),
                    args.public_helper.read_bytes())
    # A fresh destination preserves the original and refuses ambiguous reruns.
    with args.output.open("xb") as output:
        output.write(result)


if __name__ == "__main__":
    main()
