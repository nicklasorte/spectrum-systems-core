"""Schema loader + validator for harness/ artifacts."""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import jsonschema

_REPO_ROOT_MARKER = "pyproject.toml"


def _find_contracts_dir(start: Path) -> Path:
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / _REPO_ROOT_MARKER).is_file() and (parent / "contracts").is_dir():
            return parent / "contracts"
    raise FileNotFoundError("contracts/ directory not found from " + str(start))


@cache
def _load_schema_cached(schema_path_str: str) -> dict:
    return json.loads(Path(schema_path_str).read_text(encoding="utf-8"))


def load_harness_schema(schema_name: str, repo_root: str | Path | None = None) -> dict:
    """Load contracts/schemas/harness/<schema_name>.schema.json."""
    if repo_root is not None:
        contracts_dir = Path(repo_root).resolve() / "contracts"
    else:
        contracts_dir = _find_contracts_dir(Path(__file__).parent)
    schema_path = (
        contracts_dir / "schemas" / "harness" / f"{schema_name}.schema.json"
    )
    return _load_schema_cached(str(schema_path))


def validate_harness_artifact(
    artifact: dict,
    schema_name: str,
    repo_root: str | Path | None = None,
) -> tuple[bool, str]:
    """Return (ok, error_message). Never raises."""
    try:
        schema = load_harness_schema(schema_name, repo_root)
        jsonschema.Draft202012Validator(schema).validate(artifact)
        return True, ""
    except jsonschema.ValidationError as exc:
        return False, exc.message
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        return False, str(exc)
