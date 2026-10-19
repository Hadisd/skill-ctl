"""Order local picker rows by name matches, using fzf's matching rules."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def prioritize_names(lines: list[str], query: str) -> list[str]:
    """Put name matches first and retain all rows so selections survive reloads."""
    if not query:
        return lines

    env = dict(os.environ, FZF_DEFAULT_OPTS="")
    env.pop("FZF_DEFAULT_OPTS_FILE", None)

    def matches(fields: str) -> list[str]:
        result = subprocess.run(
            [
                "fzf",
                "--ansi",
                "--delimiter",
                "\t",
                "--with-nth",
                "1,2,3,4",
                "--nth",
                fields,
                "--filter",
                query,
            ],
            input="\n".join(lines),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if result.returncode not in (0, 1):
            result.check_returncode()
        return result.stdout.splitlines()

    all_matches = matches("1..4")
    if not all_matches:
        return lines
    ranks = {line: (1, index) for index, line in enumerate(all_matches)}
    ranks.update({line: (0, index) for index, line in enumerate(matches("1"))})
    return sorted(lines, key=lambda line: ranks.get(ANSI_ESCAPE.sub("", line), (2, 0)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lines = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print("\n".join(prioritize_names(lines, sys.argv[2])))
