.PHONY: help install test test-fast lint format check clean

help:
	@echo "Available make commands:"
	@echo "  make install    - Install package and dev dependencies with uv"
	@echo "  make test       - Run test suite in parallel with pytest-xdist"
	@echo "  make test-fast  - Run test suite and fail on first error"
	@echo "  make lint       - Run ruff linter check"
	@echo "  make format     - Run ruff format and fix lint issues"
	@echo "  make check      - Run lint and tests together"
	@echo "  make clean      - Remove build and test artifacts"

install:
	uv sync --extra dev

test:
	uv run pytest -n auto

test-fast:
	uv run pytest -x -q

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

check: lint test

clean:
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
