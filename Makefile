# the checks CI runs (.github/workflows/ci.yml), in the same order — run before every push
check:
	uv run --extra dev ruff check src tests
	uv run --extra dev ruff format --check src tests
	uv run --extra dev mypy src
	uv run --extra dev pytest -q

.PHONY: check
