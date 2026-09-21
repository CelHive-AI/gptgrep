#!/usr/bin/env python3
"""Run a command with one explicitly selected dev credential, without echoing it.

The product itself reads OPENROUTER_API_KEY from its environment. This optional
development helper never sources shell code, copies the env file, or logs values.
"""
import argparse
import os
import shlex
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a command after --")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        with args.env_file.expanduser().open(encoding="utf-8") as handle:
            for line in handle:
                candidate = line.strip()
                if candidate.startswith("export "):
                    candidate = candidate[7:].lstrip()
                name, separator, value = candidate.partition("=")
                if separator and name.strip() == "OPENROUTER_API_KEY":
                    try:
                        parts = shlex.split(value, comments=True, posix=True)
                    except ValueError:
                        sys.exit("Credential entry is malformed (value omitted)")
                    if len(parts) != 1 or not parts[0]:
                        sys.exit("Credential entry is empty or malformed (value omitted)")
                    key = parts[0]
                    break
    if not key:
        sys.exit("OPENROUTER_API_KEY is not configured")
    environment = dict(os.environ)
    environment["OPENROUTER_API_KEY"] = key
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
