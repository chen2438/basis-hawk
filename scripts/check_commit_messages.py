#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TITLE = re.compile(r"^(feat|fix|docs|test|refactor|build|ci|chore)(\([^)]+\))?!?: .+")
TRAILER = re.compile(r"^(Co-authored-by: .+ <[^>]+>|Human-authored: true)$", re.MULTILINE)


def validate(message: str) -> list[str]:
    lines = message.rstrip().splitlines()
    errors: list[str] = []
    if not lines or not TITLE.match(lines[0]):
        errors.append("title must use Conventional Commit format")
    if len(lines) < 3 or lines[1] != "" or not any(line.strip() for line in lines[2:]):
        errors.append("commit must contain a non-empty description after a blank line")
    if not TRAILER.search(message):
        errors.append("commit must end with an authorship trailer")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args()
    messages: list[tuple[str, str]] = []
    if args.message_file:
        messages.append((str(args.message_file), args.message_file.read_text()))
    elif args.commit:
        value = subprocess.check_output(
            ["git", "show", "-s", "--format=%B", args.commit], text=True
        )
        messages.append((args.commit, value))
    elif args.base and args.head:
        raw = subprocess.check_output(
            ["git", "log", "--format=%H%x00%B%x00", f"{args.base}..{args.head}"], text=True
        )
        fields = raw.split("\0")
        messages.extend(zip(fields[0::2], fields[1::2], strict=False))
    else:
        parser.error("choose --message-file, --commit, or --base/--head")
    failed = False
    for label, message in messages:
        for error in validate(message):
            failed = True
            print(f"{label}: {error}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
