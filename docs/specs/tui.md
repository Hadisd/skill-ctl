# `skill-ctl` TUI Architecture & Design Specification (`tui.md`)

## 1. Vision & Core Philosophy

The `skill-ctl` TUI is designed as **"LazyGit for AI Agent Skills"**.

While the `skctl` CLI provides fast one-liners and scriptable execution (`skctl apply writing`, `skctl add owner/repo -g`), the TUI provides a visual, keyboard-driven workbench for:
- Browsing, previewing, and discovering skills with rich rendered Markdown.
- Managing presets as visual collections (clone, combine, diff, and re-order).
- Inspecting project workspaces and seeing at a glance which agent folders (`.agents`, `.claude`, `.cursor`, etc.) have active links or copies.
- Health auditing broken links and orphaned skills with one-keystroke repairs.
- Full parity between CLI and TUI: every action taken in the TUI uses the same battle-tested engine (`skill_ctl.presets`, `skill_ctl.linker`, `skill_ctl.catalog`, etc.).

---

## 2. Technology Stack

| Layer | Technology | Rationale |
| :--- | :--- | :--- |
| **Framework** | **[Textual](https://textual.textualize.io/)** | Developed by Textualize (authors of `rich`). Native reactive architecture, async event loop, CSS-like styling (`tcss`), and seamless compatibility with `rich` renderables already in `skill-ctl`. |
| **Markdown Engine**| **Textual `MarkdownViewer` / `Markdown`** | Live syntax-highlighted rendering of `SKILL.md` documents, frontmatter badges, and table parsing. |
| **Styling** | **Textual TCSS + Custom Themes** | Extends `skill-ctl`'s theme system (`tokyo-night`, `catppuccin-mocha`, `gruvbox`, `dark`, `light`). |
| **Backend Core** | Existing `skill-ctl` modules | Direct calls to `skill_ctl.presets`, `skill_ctl.linker`, `skill_ctl.catalog`, `skill_ctl.doctor`, and `skill_ctl.config`. |

---

## 3. UI Layout & ASCII Wireframe

The interface employs a responsive multi-pane layout adapted from modern devtools (LazyGit, k9s, Spotify TUI).

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ skctl v0.1.2 ── Project: ~/MyOS/skill-ctl [Clean] ── Agents: [universal, claude, cursor]  ── [?] Help  │
├───────────────┬───────────────────────────────────┬────────────────────────────────────────────────────┤
│  [1] Workspace│  [2] Presets  │  [3] Catalog      │  [4] Global Agents  │  [5] Doctor  │  [6] Config   │
├───────────────┴───────────────────────────────────┴────────────────────────────────────────────────────┤
│  PRESETS / DIRECTORIES   │  SKILLS IN SELECTION         │  SKILL DETAILS & PREVIEW                      │
│                          │                              │                                                │
│  ▼ Local Presets (5)     │  ● tdd             🔗 symlink │  tdd                                           │
│    ▶ default (3)         │  ● review          🔗 symlink │  ============================================  │
│    ▼ writing (4)         │  ● caveman         📋 copy    │  Test-Driven Development skill for AI agents. │
│        tdd               │  ○ humanizer       (unlinked) │                                                │
│        review            │                              │  Source: ~/.skill-ctl/presets/writing/tdd       │
│        caveman           │                              │  Mode:   🔗 Symlinked into 2 targets            │
│        academic-slides   │                              │  Targets:                                      │
│    ▶ coding (12)         │                              │    • .agents/skills/tdd                        │
│    ▶ science (8)         │                              │    • .claude/skills/tdd                        │
│                          │                              │                                                │
│  ▼ Applied Projects (3)  │                              │  ## Instructions                               │
│    ● current (.)         │                              │  Always write tests first before writing       │
│    ○ ~/work/web-app      │                              │  implementation code. Ensure red-green-cycle.  │
│    ○ ~/research/genomics │                              │                                                │
├──────────────────────────┴──────────────────────────────┴────────────────────────────────────────────────┤
│  [Space] Select  [a] Apply  [u] Unapply  [+] Add Skill  [c] Combine  [r] Resync  [/] Search  [q] Quit     │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Main Screens & Modes

### Screen 1: Workspace Dashboard (`Tab 1`)
- **Left Pane (Context Explorer):**
  - Shows current project directory, detected agent directories (`.agents`, `.claude`, `.cursor`), and registered projects from `~/.skill-ctl/applied.json`.
- **Center Pane (Applied Skills):**
  - Lists skills applied to the project.
  - Badges indicate link status:
    - `🔗 link` (Symlinked into preset)
    - `📋 copy` (Copied file tree)
    - `⚠️ broken` (Target exists but symlink points to nonexistent preset)
- **Right Pane (Live Preview):**
  - Rendered `SKILL.md` of the highlighted skill with frontmatter (metadata, author, allowed tools).
- **Actions:**
  - `a`: Open preset selector modal to apply skills to this project.
  - `u`: Unapply selected skill(s) or preset.
  - `r`: Resync project against preset changes.
  - `f`: Fix broken links / run doctor on current project.

---

### Screen 2: Presets Studio (`Tab 2`)
- **Left Pane (Preset List):**
  - Displays all local presets with skill counts and Git backup sync badges.
  - Quick filtering by typing.
- **Center Pane (Preset Skills):**
  - Skills contained in the selected preset.
  - Multi-select mode (`Space` / `v` / `Ctrl+A`) for batch operations.
- **Right Pane (Inspector & Diff):**
  - Full metadata: Path on disk, origin package/repo, upstream lock status.
  - Skill contents viewer.
- **Actions:**
  - `n`: Create new preset (prompts for name).
  - `+` / `a`: Add skills to this preset (from Catalog or other Presets).
  - `c`: Clone preset into an independent new preset.
  - `m`: Combine multiple presets with conflict resolution wizard.
  - `d` / `x`: Delete preset or remove selected skill.
  - `e`: Export preset to `.zip` archive.
  - `i`: Import preset from local `.zip` archive or GitHub repo URL.

---

### Screen 3: Remote Catalog & Discovery (`Tab 3`)
- **Top Input:** Live, debounced search bar querying `skills.sh` via the cached candidate engine.
- **Left Pane (Search Results):**
  - Displays search hits with GitHub star count (⭐), install metrics (⬇️), owner/repo name, and skill tags.
- **Right Pane (README & SKILL.md Inspector):**
  - Formatted preview of the candidate's `SKILL.md` fetched asynchronously.
  - Shows source repository and install arguments.
- **Actions:**
  - `Enter`: Quick Install modal:
    1. Target: Current Project (`.`) / Preset / Global (`-g`).
    2. Agents: Universal / Claude / Cursor / All.
    3. Mode: Symlink vs Copy.
  - `p`: Add directly to a selected preset.
  - `g`: Add directly to global agents.

---

### Screen 4: Global Agents Hub (`Tab 4`)
- **Overview:** Inspects and manages skills residing in user-wide agent folders:
  - `~/.agents/skills` (Universal: Antigravity, Cursor, Codex, Cline)
  - `~/.claude/skills` (Claude Code)
  - `~/.codeium/windsurf/skills` (Windsurf)
  - Custom agent paths configured in `~/.skill-ctl/config.yaml`.
- **Features:**
  - Discover which tools are installed globally.
  - Cross-install or link global skills into presets or projects.
  - Update all global skills through upstream `npx skills update -g`.

---

### Screen 5: Doctor & Health Diagnostics (`Tab 5`)
- **Visual Health Table:**
  - Scans all projects, presets, and global directories for:
    - Dead symlinks pointing to deleted presets.
    - Orphaned skill folders without a valid `SKILL.md`.
    - Real directories shadowing preset links.
    - Stale lockfiles (`skills-lock.json`).
- **Interactive Healing:**
  - Select any issue and press `[F]` to automatically clean, relink, or prune.
  - Batch "Fix All" button for one-keystroke repairs.

---

## 5. Keyboard Navigation & Keymap

The keybindings follow standard terminal conventions (LazyGit & Vim):

### Global Keybindings
| Key | Action |
| :--- | :--- |
| `1` - `6` | Switch top-level tabs (Workspace, Presets, Catalog, Global, Doctor, Config) |
| `Tab` / `Shift+Tab` | Cycle focus between panes |
| `h` / `j` / `k` / `l` or Arrows | Navigate lists and scroll preview panes |
| `/` | Filter / search in active pane |
| `Space` | Toggle multi-select checkbox on highlighted item |
| `Ctrl+A` | Select all items in current pane |
| `Ctrl+D` | Deselect all items |
| `?` | Toggle keybinding overlay / help modal |
| `Esc` | Close modal / clear filter / cancel action |
| `q` | Quit TUI |

### Action Keybindings (Context-Sensitive)
| Key | Context | Action |
| :--- | :--- | :--- |
| `a` | Workspace | Apply a preset or skill to project |
| `a` / `+` | Presets | Add skill to preset (from catalog or other presets) |
| `u` | Workspace | Unapply selected skill(s) |
| `r` | Workspace | Resync project against preset changes |
| `c` | Presets | Clone or Combine presets |
| `d` | Presets / Workspace | Delete / remove skill or preset |
| `Enter` | Catalog | Install selected skill(s) |
| `f` | Doctor | Fix highlighted diagnostic issue |
| `p` | Global / Presets | Push Git backup to remote repository |

---

## 6. Architecture & Code Structure

The TUI implementation lives inside a dedicated `skill_ctl/tui/` package:

```
skill_ctl/
├── tui/
│   ├── __init__.py          # Entry point (launch_tui())
│   ├── app.py              # Main Textual App (SkillCtlApp)
│   ├── theme.tcss          # Textual CSS styles & color themes
│   ├── screens/
│   │   ├── workspace.py    # Screen 1: Project & applied links
│   │   ├── presets.py      # Screen 2: Preset studio & combiner
│   │   ├── catalog.py      # Screen 3: skills.sh search & discovery
│   │   ├── global_hub.py   # Screen 4: Global agent skills
│   │   ├── doctor.py       # Screen 5: Health checks & repair
│   │   └── help.py         # Modal: Interactive keybindings cheat-sheet
│   ├── widgets/
│   │   ├── header.py       # Top context bar with agent indicators
│   │   ├── preset_tree.py  # Hierarchical preset tree view
│   │   ├── skill_table.py  # DataTable with status icons (🔗, 📋, ⚠️)
│   │   ├── preview.py      # Markdown viewer with syntax highlighting
│   │   └── modals.py       # Reusable confirm, picker, and text input modals
│   └── state.py            # Reactive state management (active project, selection)
```

### Integration with CLI
Add a first-class `tui` subcommand to `cli.py`, and allow running bare `skctl` with no args (when interactive) to launch the TUI:

```python
@app.command(name="tui")
def tui_command(
    project: Optional[str] = typer.Option(
        None, "--project", "-p", help="Initial project directory."
    ),
) -> None:
    """Launch the interactive Terminal User Interface."""
    from skill_ctl.tui import launch_tui

    launch_tui(project=project)
```

---

## 7. Implementation Roadmap

### Phase 1: Core Shell & Workspace Viewer
- [ ] Set up `textual` dependency and base `SkillCtlApp`.
- [ ] Implement layout: Top context header, dual-pane list + Markdown preview, footer keymap.
- [ ] Connect `Workspace` screen to display current project's applied presets and skill links.
- [ ] Live preview for `SKILL.md`.

### Phase 2: Preset Studio & Actions
- [ ] Build the `Presets` screen: list presets, inspect skills, add new preset.
- [ ] Connect actions: Apply to project (`apply`), remove skill (`remove`), clone preset (`clone`).
- [ ] Implement multi-select skill operations.

### Phase 3: Catalog & Remote Discovery
- [ ] Integrate `skill_ctl.catalog` search with async debounced input.
- [ ] Live preview of candidate `SKILL.md` from GitHub.
- [ ] One-click install modal (install to project, preset, or global).

### Phase 4: Doctor, Global Hub & Polish
- [ ] Global agents tab (`~/.agents`, `~/.claude`, `~/.cursor`).
- [ ] Doctor screen with interactive repair button.
- [ ] Custom TCSS theming matching `skill-ctl` configuration (`tokyo-night`, `catppuccin`, etc.).
- [ ] Add `skctl tui` command and documentation.
