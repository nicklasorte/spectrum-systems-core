"""Phase 3P CLI shell exercising the ``--enable-few-shot`` flag.

Mirrors ``scripts/cli_glossary.py``. Reads the canonical prompt file,
either keeps or strips the Few-Shot Examples section, and prints the
resulting prompt to stdout. The disabled-by-default behaviour test
calls this script with ``ENABLE_FEW_SHOT=true`` in the env and asserts
the section does NOT appear; the enabled behaviour test passes
``--enable-few-shot`` and asserts the section DOES appear.

The script is intentionally CLI-only — the parser deliberately does
NOT consult environment variables or config files. The corresponding
production wiring in ``cli.py meeting-minutes-llm`` follows the same
contract.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from spectrum_systems_core.few_shot import (  # noqa: E402
    FewShotError,
    inject_or_strip_few_shot,
    load_few_shot_registry,
)

_DEFAULT_PROMPT = (
    _REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
    / "meeting_minutes_llm.md"
)
_DEFAULT_FEW_SHOT_DIR = _REPO_ROOT / "data" / "few_shot"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3P few-shot CLI shell. Prints the production prompt "
            "with the Few-Shot Examples section either present or "
            "stripped. CLI-only: --enable-few-shot is intentionally "
            "NOT read from env vars or config files."
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--enable-few-shot",
        dest="enable_few_shot",
        action="store_true",
        default=None,
        help=(
            "Inject the Few-Shot Examples section. DO NOT enable in "
            "production until the operator has confirmed real-corpus "
            "provenance."
        ),
    )
    group.add_argument(
        "--disable-few-shot",
        dest="enable_few_shot",
        action="store_false",
        default=None,
        help=(
            "Strip the Few-Shot Examples section. Mutually exclusive "
            "with --enable-few-shot."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        type=pathlib.Path,
        default=_DEFAULT_PROMPT,
        help="Canonical prompt file to load.",
    )
    parser.add_argument(
        "--few-shot-dir",
        type=pathlib.Path,
        default=_DEFAULT_FEW_SHOT_DIR,
        help=(
            "Directory containing examples_v1.jsonl + MANIFEST.json. "
            "Used only to verify the manifest hash; the section text "
            "comes from the prompt file."
        ),
    )
    args = parser.parse_args(argv)

    enable = args.enable_few_shot
    if enable is None:
        # Match the production CLI's default-OFF resolution.
        enable = False

    # When enabling, verify the manifest hash so the canonical section
    # in the prompt file matches what's registered on disk. The
    # disabled path skips the verification (the section is stripped
    # anyway).
    if enable:
        try:
            load_few_shot_registry(
                examples_path=args.few_shot_dir / "examples_v1.jsonl",
                manifest_path=args.few_shot_dir / "MANIFEST.json",
            )
        except FewShotError as exc:
            print(f"FAIL {exc.reason}: {exc.detail}", file=sys.stderr)
            return 1

    if not args.prompt_file.is_file():
        print(f"FAIL prompt file missing: {args.prompt_file}", file=sys.stderr)
        return 1

    prompt_text = args.prompt_file.read_text(encoding="utf-8")
    out_text = inject_or_strip_few_shot(prompt_text, enable=enable)
    sys.stdout.write(out_text)
    if not out_text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
