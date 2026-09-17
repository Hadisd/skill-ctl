#!/usr/bin/env python3
"""
skill-ctl (skctl) - AI Agent Skill & Preset Manager powered by npx skills (skills.sh).
Uses typer for typed CLI parsing.
"""

import os
import shlex
import shutil
import sys
from pathlib import Path

# Ensure package directory is in sys.path when executed directly as a script
if __package__ is None or __package__ == "":
    parent = Path(__file__).resolve().parent.parent
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))

from skill_ctl.config import ensure_config, load_config
from skill_ctl.constants import SELECT_INTERACTIVELY, PRESETS_DIR, BASE_DIR
from skill_ctl.theme import install_typer_styling
from skill_ctl.ui import console, set_theme

SUBCOMMAND_LOADERS = {
    "add": ("skill_ctl.skills", "add"),
    "apply": ("skill_ctl.presets", "apply"),
    "unapply": ("skill_ctl.presets", "unapply"),
    "presets": ("skill_ctl.presets", "presets"),
    "status": ("skill_ctl.presets", "status"),
    "list": ("skill_ctl.skills", "list_skills"),
    "remove": ("skill_ctl.skills", "remove"),
    "update": ("skill_ctl.skills", "update"),
    "find": ("skill_ctl.skills", "find"),
    "search": ("skill_ctl.search", "search"),
    "config": ("skill_ctl.config_commands", "config_cmd"),
    "theme": ("skill_ctl.config_commands", "theme_cmd"),
    "backup": ("skill_ctl.backup", "backup"),
    "doctor": ("skill_ctl.doctor", "doctor"),
    "completion": ("skill_ctl.completion", "completion"),
    "self-update": ("skill_ctl.self_update", "self_update"),
}

_LAZY_EXPORTS = {
    "add": ("skill_ctl.skills", "add"),
    "list_skills": ("skill_ctl.skills", "list_skills"),
    "remove": ("skill_ctl.skills", "remove"),
    "update": ("skill_ctl.skills", "update"),
    "find": ("skill_ctl.skills", "find"),
    "apply": ("skill_ctl.presets", "apply"),
    "unapply": ("skill_ctl.presets", "unapply"),
    "presets": ("skill_ctl.presets", "presets"),
    "status": ("skill_ctl.presets", "status"),
    "get_preset_skills": ("skill_ctl.presets", "get_preset_skills"),
    "search": ("skill_ctl.search", "search"),
    "backup": ("skill_ctl.backup", "backup"),
    "doctor": ("skill_ctl.doctor", "doctor"),
    "completion": ("skill_ctl.completion", "completion"),
    "config_cmd": ("skill_ctl.config_commands", "config_cmd"),
    "theme_cmd": ("skill_ctl.config_commands", "theme_cmd"),
    "self_update": ("skill_ctl.self_update", "self_update"),
}


def __getattr__(name: str):
    if name in _LAZY_EXPORTS:
        mod_name, func_name = _LAZY_EXPORTS[name]
        module = __import__(mod_name, fromlist=[func_name])
        val = getattr(module, func_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def get_all_commands() -> dict:
    commands = {}
    for cmd, (mod, func_name) in SUBCOMMAND_LOADERS.items():
        module = __import__(mod, fromlist=[func_name])
        commands[cmd] = getattr(module, func_name)
    return commands


SUBCOMMAND_ALIASES = {"preset": "presets", "rm": "remove", "sync": "backup"}

# Aliases that also imply the action, so `skctl restore` never means `backup push`.
SUBCOMMAND_EXPANSIONS = {"restore": ["backup", "pull"]}

PASTE_NOISE = ("npx", "skills", "add")

ROOT_COMMAND_GROUPS = (
    ("Preset workflow", (
        ("add", "Install a skill into a preset, project, or global folders"),
        ("apply", "Link a preset into the current project"),
        ("unapply", "Remove a preset's links from the current project"),
        ("presets", "Browse and manage presets"),
    )),
    ("Skills", (
        ("find", "Find and install skills from skills.sh"),
        ("search", "Search installed preset and global skills"),
        ("list", "List installed skills"),
        ("remove", "Remove an installed skill"),
        ("update", "Update installed skills"),
    )),
    ("Setup", (
        ("status", "Show presets applied to a project"),
        ("config", "View and edit configuration"),
        ("theme", "View or change the color theme"),
        ("backup", "Back up or restore presets"),
        ("doctor", "Find broken skill links"),
        ("completion", "Print shell completion code"),
        ("self-update", "Check for and install a new skctl release"),
        ("version", "Print the installed skctl version"),
    )),
)

PRESET_ACTION_USAGE = {
    "create": "skctl presets create <name> [--from PATH]",
    "clone": "skctl presets clone <source> <new-name> [--replace]",
    "combine": "skctl presets combine <new-name> <preset> <preset> [...] [--replace] [--dry-run]",
    "history": "skctl presets history <name>",
    "rename": "skctl presets rename <old-name> <new-name>",
    "delete": "skctl presets delete <name>",
    "export": "skctl presets export <name> [--output ARCHIVE]",
    "import": "skctl presets import <archive> [--rename NAME] [--replace] [--dry-run]",
}


def print_usage(line: str) -> None:
    """Print a usage line the way typer prints its own.

    Wrapped with a hanging indent, so a long one stays readable. Text() takes
    the line literally, so the square brackets in a usage string are safe.
    """
    import textwrap

    from rich.text import Text

    wrapped = textwrap.wrap(
        f"Usage: {line}", width=max(console.width - 2, 20),
        initial_indent=" ", subsequent_indent=" " * 8,
        break_long_words=False, break_on_hyphens=False,
    )
    text = Text("\n".join(wrapped))
    text.stylize("bold", 1, 7)
    console.print(text)


def print_help_panel(title: str, lines: list[str]) -> None:
    """Render a help section in the rounded panel typer uses for its own.

    Lines are rich markup; a caller passing text with square brackets (usage
    strings do) has to escape it first.
    """
    from rich.box import ROUNDED
    from rich.panel import Panel

    console.print(Panel("\n".join(lines), title=title, title_align="left",
                        border_style="header", box=ROUNDED, padding=(0, 1)))


def print_preset_help(action: str = "") -> None:
    from rich.markup import escape

    if action:
        usage = PRESET_ACTION_USAGE.get(action)
        if usage:
            print_usage(usage)
            return
    print_usage("skctl presets <action> [arguments]")
    console.print()
    actions = [escape(usage.removeprefix("skctl presets "))
               for usage in PRESET_ACTION_USAGE.values()]
    print_help_panel("Actions", actions + ["list | applied | browse"])


def split_pasted_command(command: str) -> list[str]:
    """Split a pasted command without treating Windows path separators as escapes."""
    if os.name != "nt":
        return shlex.split(command)
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)

def preprocess_args(args: list[str]) -> list[str]:
    """Cleans up args if someone pasted a raw 'npx skills add ...' command from skills.sh."""
    # 1. skctl npx skills ... / skctl skills ... -> skctl ...
    while args and args[0] in ("npx", "skills"):
        args = args[1:]

    # 2. The same paste after the subcommand, whether the shell split it
    # (skctl add npx skills add y) or it arrived as one quoted argument
    # (skctl add "npx skills add y --skill foo").
    if len(args) >= 2 and args[0] == "add":
        rest = args[1:]
        if rest[0].startswith(tuple(word + " " for word in PASTE_NOISE)):
            rest = split_pasted_command(rest[0]) + rest[1:]
        while rest and rest[0] in PASTE_NOISE:
            rest = rest[1:]
        args = ["add"] + rest

    # 3. Subcommand aliases.
    if args and args[0] in SUBCOMMAND_ALIASES:
        args = [SUBCOMMAND_ALIASES[args[0]]] + args[1:]
    elif args and args[0] in SUBCOMMAND_EXPANSIONS:
        args = SUBCOMMAND_EXPANSIONS[args[0]] + args[1:]

    # 4. Expand valueless flags: bare -p/--project means the current directory,
    # and bare -P/--preset means the configured default preset.
    result = []
    for i, arg in enumerate(args):
        result.append(arg)
        bare = i + 1 >= len(args) or args[i + 1].startswith("-")
        if arg in ("-p", "--project") and bare:
            result.append(".")
        elif arg in ("-P", "--preset") and bare:
            result.append(load_config().get("default_preset", "default"))
        elif arg in ("-s", "--skill") and bare and args[0] == "apply":
            result.append(SELECT_INTERACTIVELY)

    return result

def print_status_summary() -> None:
    """Print the skill store location and its preset and skill counts."""
    from skill_ctl.skillscan import get_preset_skills
    preset_dirs = sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir()) if PRESETS_DIR.is_dir() else []
    skill_count = sum(len(get_preset_skills(d)) for d in preset_dirs)
    console.print()
    console.print(f"[dim]{BASE_DIR}[/dim]")
    console.print(
        f"[dim]{len(preset_dirs)} preset{'s' if len(preset_dirs) != 1 else ''}, "
        f"{skill_count} skill{'s' if skill_count != 1 else ''}[/dim]"
    )


def print_root_help() -> None:
    console.print("\n [bold]Store skills once. Use them where they fit.[/bold]\n")
    print_usage("skctl <COMMAND>")
    console.print()
    for group, commands in ROOT_COMMAND_GROUPS:
        print_help_panel(group, [f"[header]{name:<12}[/header] {description}"
                                 for name, description in commands])
    print_help_panel("Examples", [
        "skctl add owner/repo --preset writing",
        "skctl apply writing",
    ])
    console.print("[dim]Run `skctl <command> --help` for command options.[/dim]")
    if not shutil.which("fzf"):
        console.print("[dim]Tip: install fzf for interactive selection.[/dim]")
    if not (shutil.which("bat") or shutil.which("batcat")):
        console.print("[dim]Tip: install bat for richer previews.[/dim]")
    print_status_summary()

def run_app(app, args: list[str], prog_name: str) -> None:
    """Invoke a Typer app, returning on success instead of exiting.

    Click ends a successful command with `sys.exit(0)`, but main() is expected
    to return so its callers keep going. A non-zero exit is a real failure (or
    a usage error Click has already reported), so it is left to propagate.

    Theming typer's help renderer is armed here rather than in main(), because
    this is the only path that lets typer render anything. It is armed, not
    applied: typer imports its renderer only for help and errors, so the
    styling waits for that import and a command that just runs pays nothing.
    """
    import typer

    install_typer_styling(load_config().get("theme"))
    try:
        typer.main.get_command(app)(args=args, prog_name=prog_name)
    except SystemExit as exit_signal:
        if exit_signal.code not in (0, None):
            raise


def main() -> None:
    cleaned_args = preprocess_args(sys.argv[1:])
    if cleaned_args and cleaned_args[0] in ("version", "--version", "-V"):
        # Answered locally, before config or theme setup: it must work in a
        # broken install, and it is what a bug report asks for first.
        from skill_ctl import __version__
        print(f"skctl {__version__}")
        return
    dry_run = cleaned_args[:1] == ["apply"] and "--dry-run" in cleaned_args
    update_check = cleaned_args[:1] == ["self-update"] and "--check" in cleaned_args
    # Dry runs and update checks must not create or migrate config.yaml.
    if not (dry_run or update_check):
        ensure_config()
    set_theme(load_config().get("theme"))

    # Fast path: targeted command execution without loading the other 14 modules
    if cleaned_args and cleaned_args[0] in SUBCOMMAND_LOADERS:
        cmd_name = cleaned_args[0]
        if cmd_name == "presets" and (
            cleaned_args[1:] == ["--help"]
            or (len(cleaned_args) == 3 and cleaned_args[2] == "--help")
        ):
            print_preset_help(cleaned_args[1] if len(cleaned_args) == 3 else None)
            return
        mod_name, func_name = SUBCOMMAND_LOADERS[cmd_name]
        module = __import__(mod_name, fromlist=[func_name])
        func = getattr(module, func_name)
        import typer
        app = typer.Typer(add_completion=False)
        app.command(cmd_name)(func)
        run_app(app, cleaned_args[1:], f"skctl {cmd_name}")
        return

    if not cleaned_args:
        print_root_help()
        return

    import typer

    app = typer.Typer(
        add_completion=False,
        help="AI Agent Skill & Preset Manager powered by npx skills (skills.sh)",
    )
    for cmd_name, func in get_all_commands().items():
        app.command(cmd_name)(func)
    run_app(app, cleaned_args, "skctl")

if __name__ == "__main__":
    main()
