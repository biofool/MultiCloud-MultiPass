# Component: Path Resolver

## Location
`paths.py` (78 lines)

## Responsibility
Project-root path resolution. All config and data file paths resolve relative
to the project root (not CWD), so the service works from any invocation
directory (local dev, Cloud Run, tests, scripts).

## Interfaces
- `PROJECT_ROOT: Path` — auto-detected by walking up to `pyproject.toml`
- `resolve(*parts) -> str` — resolve relative to PROJECT_ROOT
- `resolve_path(*parts) -> Path` — like `resolve()` but returns `Path`

## Dependencies
- `os.environ` — `CLOUDMANAGEMENT_ROOT` override
- `pathlib.Path`

## Dependents
- `registry.py` — `ACCOUNTS_FILE` path
- `intent.py` — YAML file paths (`_INTENTS_FILE`, `_ACTUALS_FILE`, etc.)
- `inventory.py` — terraform state path

## Boundaries
- `CLOUDMANAGEMENT_ROOT` env var overrides auto-detection (for Docker/Cloud Run)
- Absolute paths passed to `resolve()` are returned unchanged
- Warning logged if `CLOUDMANAGEMENT_ROOT` doesn't contain `paths.py`

## Security sensitivity
LOW — but `CLOUDMANAGEMENT_ROOT` can redirect all file reads. Only set in
trusted environments (Docker, Cloud Run, CI). OBSERVED in docstring.

## Test map target
Indirectly tested via `tests/test_registry.py`, `tests/test_intent.py`
