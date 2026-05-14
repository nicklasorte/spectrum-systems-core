"""One-shot migration: copy committed data-lake/ artifacts from
spectrum-systems-core into the nicklasorte/data-lake repository.

Background
==========

Until now, ``spectrum-systems-core`` carried a committed ``data-lake/``
directory that held every pipeline artifact (extractions, evals,
ground-truth pairs, glossary, processed source records, ...). The
target architecture splits responsibilities:

  * ``spectrum-systems-core`` — code only, zero data artifacts.
  * ``nicklasorte/data-lake`` — every data artifact lives here.

This script is the one-shot bridge. It clones ``nicklasorte/data-lake``,
copies the artifacts the spectrum-systems-core checkout still carries
into the data-lake clone, commits, and pushes.

It is intentionally idempotent: a file that already exists in the
data-lake repo with byte-identical content is a no-op. Existing
artifacts in the data-lake repo are NEVER overwritten with a smaller
or older copy — the script writes the spectrum-systems-core copy only
when the destination file is missing.

Usage
=====

  DATA_LAKE_TOKEN=<pat> python scripts/_migrate_artifacts_to_data_lake_repo.py

Or via the ``migrate-data-lake-artifacts.yml`` workflow_dispatch (the
intended path, because the PAT only lives in repo secrets).

Exit codes
==========

  0 — migration completed (push succeeded, or no changes to push).
  1 — at least one precondition failed.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, List

# Subdirectories of the source data-lake/ that contain artifacts the
# pipeline still depends on. Anything outside this list is intentionally
# NOT migrated — those paths are either runtime-only (caches, debug
# outputs) or already gitignored.
ARTIFACT_SUBPATHS: tuple[str, ...] = (
    "store/artifacts",
    "store/processed",
)

DATA_LAKE_REMOTE = "https://x-access-token:{token}@github.com/nicklasorte/data-lake.git"


def _run(args: List[str], cwd: pathlib.Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), check=check, text=True, capture_output=True)


def _git(args: List[str], cwd: pathlib.Path, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, check=check)


def _copy_tree(src: pathlib.Path, dst: pathlib.Path) -> List[pathlib.Path]:
    """Copy every file under ``src`` to the matching path under ``dst``.

    Returns the list of destination paths that were written (i.e.
    files that were missing in ``dst`` before this call). Existing
    destination files are left untouched so we never clobber a
    later state in the data-lake repo with a stale copy from
    spectrum-systems-core.
    """
    written: List[pathlib.Path] = []
    if not src.is_dir():
        return written
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        target = dst / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written.append(target)
    return written


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default="data-lake",
        help="Path to the spectrum-systems-core data-lake/ tree to migrate (default: data-lake)",
    )
    parser.add_argument(
        "--token-env-var",
        default="DATA_LAKE_TOKEN",
        help="Environment variable holding the PAT for nicklasorte/data-lake (default: DATA_LAKE_TOKEN)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Clone and copy locally; do not commit or push.",
    )
    args = parser.parse_args(argv)

    src = pathlib.Path(args.source).resolve()
    if not src.is_dir():
        print(
            f"ERROR: source data-lake/ not found at {src}. "
            "Has the migration already run? Nothing to do.",
            file=sys.stderr,
        )
        return 1

    token = os.environ.get(args.token_env_var)
    if not token:
        print(
            f"ERROR: env var {args.token_env_var} is not set. "
            "Migration cannot push to nicklasorte/data-lake without a PAT.",
            file=sys.stderr,
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="dl-migrate-") as tmpdir:
        clone_root = pathlib.Path(tmpdir) / "data-lake"
        clone_url = DATA_LAKE_REMOTE.format(token=token)
        print(f"Cloning nicklasorte/data-lake into {clone_root} ...")
        # Avoid printing the token by passing the URL through the
        # arg list (subprocess does not echo args by default).
        _run(["git", "clone", clone_url, str(clone_root)], cwd=pathlib.Path.cwd())

        _git(["config", "user.name", "github-actions[bot]"], cwd=clone_root)
        _git(
            [
                "config",
                "user.email",
                "github-actions[bot]@users.noreply.github.com",
            ],
            cwd=clone_root,
        )

        all_written: List[pathlib.Path] = []
        for sub in ARTIFACT_SUBPATHS:
            src_sub = src / sub
            dst_sub = clone_root / sub
            written = _copy_tree(src_sub, dst_sub)
            print(f"  {sub}: {len(written)} new file(s) copied")
            all_written.extend(written)

        if not all_written:
            print("OK: no new artifacts to migrate; data-lake already up to date.")
            return 0

        print(f"\nMigrating {len(all_written)} new artifact file(s) to nicklasorte/data-lake")

        if args.dry_run:
            print("--dry-run: skipping commit + push")
            return 0

        _git(["add", "store/"], cwd=clone_root)
        status = _git(["diff", "--staged", "--quiet"], cwd=clone_root, check=False)
        if status.returncode == 0:
            print("OK: nothing to commit after add (idempotent re-run).")
            return 0

        _git(
            [
                "commit",
                "-m",
                "migrate: import artifacts from spectrum-systems-core",
            ],
            cwd=clone_root,
        )

        attempt = 0
        delay = 2
        push_url = DATA_LAKE_REMOTE.format(token=token)
        while attempt < 5:
            attempt += 1
            try:
                _git(["fetch", "origin", "main"], cwd=clone_root)
                _git(["pull", "--rebase", "origin", "main"], cwd=clone_root)
                _run(
                    ["git", "push", push_url, "HEAD:main"],
                    cwd=clone_root,
                )
                print(f"Push succeeded on attempt {attempt}")
                return 0
            except subprocess.CalledProcessError as exc:
                print(
                    f"Push attempt {attempt} failed (rc={exc.returncode}); "
                    f"sleeping {delay}s ...",
                    file=sys.stderr,
                )
                # Best-effort cleanup before the next attempt.
                _git(["rebase", "--abort"], cwd=clone_root, check=False)
                import time

                time.sleep(delay)
                delay *= 2

        print("ERROR: push to nicklasorte/data-lake failed after 5 attempts", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
