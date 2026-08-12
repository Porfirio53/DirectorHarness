#!/usr/bin/env python3
"""Replace credential values in HarnessBench result, trace, session, and log artifacts."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from dotenv import dotenv_values

_SENSITIVE_NAME = re.compile(
    r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
    re.IGNORECASE,
)
_REDACTED = b"[REDACTED]"


def sensitive_values(env: Mapping[str, str]) -> tuple[bytes, ...]:
    values = {
        value.encode("utf-8")
        for name, value in env.items()
        if _SENSITIVE_NAME.search(name) and len(value) >= 8
    }
    return tuple(sorted(values, key=len, reverse=True))


def redact_tree(root: Path, secrets: Sequence[bytes]) -> tuple[int, int]:
    scanned = 0
    changed = 0
    if not root.exists():
        return scanned, changed
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        scanned += 1
        try:
            original = path.read_bytes()
        except OSError:
            continue
        redacted = original
        for secret in secrets:
            redacted = redacted.replace(secret, _REDACTED)
        if redacted == original:
            continue
        temporary = path.with_name(path.name + ".redacting")
        temporary.write_bytes(redacted)
        os.replace(temporary, path)
        changed += 1
    return scanned, changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        print(f"env file not found: {env_file}", file=sys.stderr)
        return 2
    env = os.environ.copy()
    for key, value in dotenv_values(env_file).items():
        if value is not None:
            env[str(key)] = str(value)
    secrets = sensitive_values(env)
    total_scanned = 0
    total_changed = 0
    for raw_root in args.root:
        root = raw_root.expanduser().resolve()
        scanned, changed = redact_tree(root, secrets)
        total_scanned += scanned
        total_changed += changed
    print(
        {
            "roots": len(args.root),
            "secret_values_checked": len(secrets),
            "files_scanned": total_scanned,
            "files_redacted": total_changed,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
