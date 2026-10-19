"""Terminal rendering regressions for the skill picker."""

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(os.name == "nt", reason="PTY rendering needs a Unix terminal")
@pytest.mark.parametrize("skills, query, first_match, initial_query, select_before", [
    ([("alpha", "A long description about building and reviewing code with a needle at the end."),
      ("beta", "needle near the start")], "needle", "beta", False, None),
    ([("tutorial", "dbds"), ("database-design", "Build database schemas")],
     "dbds", "database-design", False, None),
    ([("tutorial", "database"), ("database-design", "Build schemas")],
     "database", "database-design", False, None),
    ([("tutorial", "dbds"), ("database-design", "Build database schemas")],
     "dbds", "database-design", True, None),
    ([("alpha", "First skill"), ("beta", "Second skill")],
     "beta", "beta", False, "alpha"),
])
def test_fzf_keeps_columns_and_prioritizes_names_when_typing(
    tmp_path, skills, query, first_match, initial_query, select_before,
):
    """Name matches lead, description matches remain, and columns stay aligned."""
    import re
    import shutil

    if not shutil.which("fzf"):
        pytest.skip("fzf is not installed")

    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import time

    candidates = []
    for name, description in skills:
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Preview\n", encoding="utf-8")
        candidates.append({"skill": name, "preset": "tools", "description": description,
                           "path": str(skill_dir)})
    script = (
        f"import sys, json; sys.path.insert(0, {str(REPO_ROOT)!r}); "
        "from skill_ctl.picker import pick_with_fzf; "
        f"chosen, ran = pick_with_fzf({candidates!r}, query={query if initial_query else ''!r}); "
        "print('PICKED=' + json.dumps([row['skill'] for row in chosen]), flush=True)"
    )
    pid, terminal = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.environ["TERM"] = "xterm-256color"
        os.environ["FZF_DEFAULT_OPTS"] = ""
        os.environ.pop("FZF_DEFAULT_OPTS_FILE", None)
        os.execv(sys.executable, [sys.executable, "-c", script])

    def read_frame(ready):
        output = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.2)[0]:
                try:
                    chunk = os.read(terminal, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            elif ready(output.decode("utf-8", errors="replace")):
                break
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.decode("utf-8", errors="replace"))

    try:
        initial = read_frame(lambda text: all(name in text for name, _ in skills) and "2/2" in text)
        assert all(name in initial for name, _ in skills) and "2/2" in initial
        if select_before:
            os.write(terminal, b"\t")
            selected = read_frame(lambda text: "(1)" in text)
            assert "(1)" in selected
        if initial_query:
            filtered = initial
        else:
            os.write(terminal, query.encode("utf-8"))
            filtered = read_frame(lambda text: query in text)
        # Visible rows keep their name and location in the same columns.
        for name, _ in skills:
            if name == select_before:
                continue
            assert name in filtered, "matching a description hid the skill's name"
            assert f"{name:<22}  {'tools':<12}" in filtered
        if select_before:
            os.write(terminal, b"\t")
        os.write(terminal, b"\r")
        accepted = read_frame(lambda text: "PICKED=" in text)
        expected = [select_before, first_match] if select_before else [first_match]
        assert "PICKED=" + json.dumps(expected) in accepted
    finally:
        try:
            os.write(terminal, b"\x1b")
        except OSError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        os.close(terminal)
