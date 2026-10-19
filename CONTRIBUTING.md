# Contributing to skill-ctl

Thank you for contributing to `skill-ctl`!

## Getting Started

1. **Clone the repository and install dependencies:**
   ```bash
   uv sync --extra dev
   ```

2. **Verify installation:**
   ```bash
   make check
   ```

## Development Workflow

We use:
- **Python 3.10+**
- **[uv](https://github.com/astral-sh/uv)** for fast virtualenv and dependency management
- **[ruff](https://github.com/astral-sh/ruff)** for linting and code formatting
- **[pytest](https://pytest.org)** with **[pytest-xdist](https://github.com/pytest-dev/pytest-xdist)** for parallel testing

### Common Commands

| Command | Description |
| :--- | :--- |
| `make test` | Run full test suite in parallel across all available CPU cores |
| `make test-fast` | Run test suite and fail immediately on first error |
| `make lint` | Run ruff code and import checks |
| `make format` | Automatically format code and fix lint issues |
| `make check` | Run linter and tests before committing |
| `make clean` | Clean up build and cache directories |

## Pull Requests

Before submitting a pull request:
1. Ensure all tests pass: `make check`.
2. Keep commits atomic and descriptive following [Conventional Commits](https://www.conventionalcommits.org/).
