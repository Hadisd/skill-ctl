"""Execution runner for npx skills with environment handling."""

import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Optional

from skill_ctl.config import load_config

def get_detected_global_agents() -> list[str]:
    """Returns agents to target for global installation without creating unused directories in home.
    Always includes 'universal' (~/.agents/skills) which is used by Antigravity, Cursor, Codex, Cline, etc.
    Also includes 'claude-code' if ~/.claude exists.
    """
    agents = ["universal"]
    home = Path.home()
    if (home / ".claude").is_dir():
        agents.append("claude-code")
    if (home / ".codeium" / "windsurf").is_dir() or (home / ".windsurf").is_dir():
        agents.append("windsurf")
    return agents

REGISTER_MJS = Path(__file__).parent / "register.mjs"

def run_npx_skills(args: list[str], cwd: Optional[str] = None) -> int:
    """Executes npx skills with the given arguments in the given cwd."""
    cmd_args = list(args)
    env = dict(os.environ)

    # For 'add' without -g/--global, target project scope (-p) and inject the loader
    # to avoid the redundant "Installation scope: Project / Global" prompt.
    #
    # The loader edits upstream's own source as Node reads it, so it is the one
    # thing here that can break when `npx skills` changes. Nothing depends on it
    # working: without it the scope question simply comes back, which is why
    # `npx.patch_scope_prompt: false` can switch it off for good.
    if cmd_args and cmd_args[0] == "add":
        if "-g" not in cmd_args and "--global" not in cmd_args:
            if "-p" not in cmd_args and "--project" not in cmd_args:
                cmd_args.append("-p")
            patch = load_config().get("npx", {}).get("patch_scope_prompt", True)
            if patch and REGISTER_MJS.exists():
                loader_opt = f"--no-deprecation --import={REGISTER_MJS.resolve().as_uri()}"
                env["NODE_OPTIONS"] = f"{loader_opt} {env.get('NODE_OPTIONS', '')}".strip()
            elif patch:
                warnings.warn(f"Loader missing at {REGISTER_MJS}; 'npx skills' may ask for scope.", RuntimeWarning)

    # Resolve npx via PATH ourselves: on Windows it is 'npx.cmd', and subprocess
    # does not consult PATHEXT, so a bare "npx" would never be found there.
    npx = shutil.which("npx")
    if npx is None:
        warnings.warn("'npx' not found on PATH. Install Node.js and retry.", RuntimeWarning)
        return 1

    # If -y / --yes is not passed, ensure npx skills runs interactively by clearing
    # AI agent detection env vars so it prompts the user for agent selection.
    if "-y" not in cmd_args and "--yes" not in cmd_args:
        for k in ("ANTIGRAVITY_AGENT", "AI_AGENT", "CURSOR_AGENT", "CURSOR_TRACE_ID", "GEMINI_CLI", "CODEX_SANDBOX"):
            env.pop(k, None)

    return subprocess.run([npx, "skills"] + cmd_args, cwd=cwd, env=env).returncode
