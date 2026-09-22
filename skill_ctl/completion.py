"""Shell completion scripts (completion fish|bash|zsh|powershell).

The scripts read preset names straight from ~/.skill-ctl/presets, so completion
keeps working without calling back into Python on every keystroke.
"""

import os
from typing import Annotated, Literal, Optional

import typer

COMMANDS = "add apply unapply presets status list remove update self-update search config theme backup doctor completion version"

FISH = """# skctl completion for fish.
# Install: skctl completion fish > ~/.config/fish/completions/skctl.fish

function __skctl_presets
    test -d ~/.skill-ctl/presets; or return
    for d in ~/.skill-ctl/presets/*/
        printf '%s\t%s\n' (basename $d) $d
    end
end

function __skctl_needs_command
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 1
end

function __skctl_using
    set -l cmd (commandline -opc)
    test (count $cmd) -ge 2; and contains -- $cmd[2] $argv
end

complete -c skctl -f

complete -c skctl -n __skctl_needs_command -a add -d 'Add a skill to a preset, project, or globally'
complete -c skctl -n __skctl_needs_command -a apply -d "Link a preset's skills into this project"
complete -c skctl -n __skctl_needs_command -a unapply -d "Remove a preset's skills from this project"
complete -c skctl -n __skctl_needs_command -a presets -d 'List, create, or delete presets'
complete -c skctl -n __skctl_needs_command -a status -d 'Show applied preset status'
complete -c skctl -n __skctl_needs_command -a list -d 'List installed skills'
complete -c skctl -n __skctl_needs_command -a remove -d 'Remove an installed skill'
complete -c skctl -n __skctl_needs_command -a update -d 'Update skills to their latest versions'
complete -c skctl -n __skctl_needs_command -a self-update -d 'Update skctl to its latest version'
complete -c skctl -n __skctl_needs_command -a search -d 'Search skills locally and on skills.sh'
complete -c skctl -n __skctl_needs_command -a config -d 'Manage ~/.skill-ctl/config.yaml'
complete -c skctl -n __skctl_needs_command -a theme -d 'View or change the color theme'
complete -c skctl -n __skctl_needs_command -a backup -d 'Back presets up to GitHub'
complete -c skctl -n __skctl_needs_command -a restore -d 'Restore presets from the backup'
complete -c skctl -n __skctl_needs_command -a doctor -d 'Find skill links that lead nowhere'
complete -c skctl -n __skctl_needs_command -a completion -d 'Print a shell completion script'
complete -c skctl -n __skctl_needs_command -a version -d 'Print the installed skctl version'

# Preset names as the first argument, and after -P/--preset anywhere.
complete -c skctl -n '__skctl_using apply unapply' -a '(__skctl_presets)'
complete -c skctl -s P -l preset -x -a '(__skctl_presets)' -d Preset
complete -c skctl -n '__skctl_using presets' -a 'list applied create edit clone combine history rename delete export import' -d Action
complete -c skctl -n '__skctl_using backup' -a 'push pull init status' -d Action
complete -c skctl -n '__skctl_using config' -a 'show path edit reset' -d Action
complete -c skctl -n '__skctl_using theme' -a 'default dark light mono catppuccin-mocha tokyo-night gruvbox' -d Theme
complete -c skctl -n '__skctl_using completion' -a 'fish bash zsh powershell' -d Shell

complete -c skctl -n '__skctl_using apply unapply' -s g -l global -d 'Apply or unapply skills globally'
complete -c skctl -s s -l skill -x -d 'Skills to apply'
complete -c skctl -s p -l project -r -d 'Project directory'
complete -c skctl -s a -l agent -x -a 'universal claude claude-code cursor windsurf codex all' -d Agent
complete -c skctl -s c -l copy -d 'Copy instead of symlinking'
complete -c skctl -n '__skctl_using apply' -l dry-run -d 'Preview changes without writing files'
complete -c skctl -s y -l yes -d 'Skip prompts'
complete -c skctl -n '__skctl_using search' -l npx -d "Use npx skills' own finder instead of skctl's fzf picker"
complete -c skctl -n '__skctl_using search' -l owner -x -d 'Search one GitHub owner'
complete -c skctl -n '__skctl_using search' -s r -l remote -d 'Search remote catalogs (skills.sh, skillsmp, all)'
complete -c skctl -n '__skctl_using update' -l all -d 'Update every preset'
complete -c skctl -n '__skctl_using self-update' -l check -d 'Check for an available update'
complete -c skctl -n '__skctl_using search' -s g -l global -d 'Search only global skill folders'
complete -c skctl -n '__skctl_using apply' -s A -l all-presets -d 'Choose from every preset at once'
complete -c skctl -n '__skctl_using status' -s g -l global -d 'Show global skills status'
complete -c skctl -n '__skctl_using status' -s a -l all -d 'Show status across all recorded projects'
complete -c skctl -n '__skctl_using status' -s v -l verbose -d 'Show verbose breakdown of all skills'
complete -c skctl -n '__skctl_using status' -l json -d 'Output status as JSON'
"""

BASH = f"""# skctl completion for bash.
# Install: skctl completion bash > ~/.local/share/bash-completion/completions/skctl

_skctl() {{
    local cur prev commands presets
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    commands="{COMMANDS} restore"

    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        return
    fi

    presets=""
    if [ -d "$HOME/.skill-ctl/presets" ]; then
        presets=$(cd "$HOME/.skill-ctl/presets" && ls -d */ 2>/dev/null | tr -d /)
    fi

    case "$prev" in
        -P|--preset) COMPREPLY=( $(compgen -W "$presets" -- "$cur") ); return ;;
        -a|--agent) COMPREPLY=( $(compgen -W "universal claude claude-code cursor windsurf codex all" -- "$cur") ); return ;;
        -p|--project) COMPREPLY=( $(compgen -d -- "$cur") ); return ;;
        -r|--remote) COMPREPLY=( $(compgen -W "skills.sh skillsmp all" -- "$cur") ); return ;;
    esac

    case "${{COMP_WORDS[1]}}" in
        apply) COMPREPLY=( $(compgen -W "$presets --pick --all-presets --global --force --resync --dry-run" -- "$cur") ) ;;
        status) COMPREPLY=( $(compgen -W "--project --global --all --verbose --json --help" -- "$cur") ) ;;
        update) COMPREPLY=( $(compgen -W "$presets --preset --global --all --yes" -- "$cur") ) ;;
        self-update) COMPREPLY=( $(compgen -W "--check --yes" -- "$cur") ) ;;
        unapply) COMPREPLY=( $(compgen -W "$presets --global --force --yes" -- "$cur") ) ;;
        search) COMPREPLY=( $(compgen -W "--preset --global --local --remote --owner --applied --json --npx skills.sh skillsmp all" -- "$cur") ) ;;
        presets|preset) COMPREPLY=( $(compgen -W "list applied create edit clone combine history rename delete export import" -- "$cur") ) ;;
        backup) COMPREPLY=( $(compgen -W "push pull init status --dry-run" -- "$cur") ) ;;
        config) COMPREPLY=( $(compgen -W "show path edit reset" -- "$cur") ) ;;
        theme) COMPREPLY=( $(compgen -W "default dark light mono catppuccin-mocha tokyo-night gruvbox" -- "$cur") ) ;;
        completion) COMPREPLY=( $(compgen -W "fish bash zsh powershell" -- "$cur") ) ;;
    esac
}}
complete -F _skctl skctl
"""

ZSH = f"""# skctl completion for zsh.
# Install: skctl completion zsh > ~/.zfunc/_skctl   (with ~/.zfunc on $fpath)
#compdef skctl

_skctl_presets() {{
    local -a presets
    [[ -d ~/.skill-ctl/presets ]] || return
    presets=(~/.skill-ctl/presets/*(/N:t))
    _describe 'preset' presets
}}

_skctl() {{
    local -a commands
    commands=({COMMANDS} restore)

    if (( CURRENT == 2 )); then
        _describe 'command' commands
        return
    fi

    if [[ "$words[CURRENT-1]" == -P || "$words[CURRENT-1]" == --preset ]]; then
        _skctl_presets
        return
    fi

    case "$words[2]" in
        apply) _arguments '--dry-run[Preview changes without writing files]' '--pick[Choose skills]' '--all-presets[Choose from all presets]' '--global[Apply skills globally]' '--force[Replace existing skills]' '--resync[Reapply recorded skills]' '*:preset:_skctl_presets' ;;
        status) _values 'option' --project --global --all --verbose --json ;;
        update) _values 'option' --preset --global --all --yes ;;
        self-update) _values 'option' --check --yes ;;
        unapply) _arguments '--global[Remove preset skills globally]' '--force[Delete real directories]' '--yes[Skip confirmation]' '*:preset:_skctl_presets' ;;
        search) _values 'option' --preset --global --local --remote --owner --applied --json --npx ;;
        presets|preset) _values 'action' list applied create edit clone combine history rename delete export import ;;
        backup) _values 'action' push pull init status --dry-run ;;
        config) _values 'action' show path edit reset ;;
        theme) _values 'action' default dark light mono catppuccin-mocha tokyo-night gruvbox ;;
        completion) _values 'shell' fish bash zsh powershell ;;
        *) _files ;;
    esac
}}
_skctl "$@"
"""

POWERSHELL = f"""# skctl completion for PowerShell.
# Install: skctl completion powershell >> $PROFILE

$SkctlCommands = @('{"', '".join((COMMANDS + " restore").split())}')

Register-ArgumentCompleter -Native -CommandName skctl, skill-ctl -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $words = @($commandAst.CommandElements | ForEach-Object {{ $_.Value }})
    $candidates = @()

    if ($words.Count -gt 1 -and $words[1] -eq 'completion') {{
        $candidates = @('fish', 'bash', 'zsh', 'powershell')
    }} elseif ($words.Count -gt 1 -and ($words[1] -in 'apply', 'unapply' -or '-P' -in $words -or '--preset' -in $words)) {{
        $presetDir = Join-Path $HOME '.skill-ctl/presets'
        if (Test-Path $presetDir) {{
            $candidates = Get-ChildItem $presetDir -Directory | Select-Object -ExpandProperty Name
        }}
    }} elseif ($words.Count -le 2) {{
        $candidates = $SkctlCommands
    }}

    $candidates | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }}
}}
"""


def completion(
    shell: Annotated[Optional[Literal["fish", "bash", "zsh", "powershell"]], typer.Argument()] = None,
) -> None:
    """Print a completion script for your shell. Omit the shell to detect it.

    Fish: `skctl completion fish > ~/.config/fish/completions/skctl.fish`

    Preset names complete after `apply`, `unapply`, `-P`, and `--preset`, read
    from ~/.skill-ctl/presets as you type, so a new preset needs no regeneration.
    """
    if shell is None:
        detected = os.path.basename(os.environ.get("SHELL", "")).lower()
        shell = {
            "fish": "fish", "bash": "bash", "zsh": "zsh",
            "pwsh": "powershell", "powershell": "powershell",
        }.get(detected)
        if shell is None and os.environ.get("PSModulePath"):
            shell = "powershell"
        if shell is None:
            raise SystemExit("Could not detect a shell. Specify fish, bash, zsh, or powershell.")
    print({"fish": FISH, "bash": BASH, "zsh": ZSH, "powershell": POWERSHELL}[shell])
