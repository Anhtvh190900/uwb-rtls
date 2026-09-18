#!/usr/bin/env python3
"""Synchronise the vendored ST driver code against pinned upstream sources.

The firmware projects vendor CMSIS Core, the CMSIS STM32F4xx device headers and
the STM32F4xx HAL driver under ``firmware/<project>/Drivers``.  Normally those
files are produced by STM32CubeMX, but CubeMX is not available for Linux ARM64,
so this script keeps them in sync directly from ST's upstream git repositories.

Mirror semantics
----------------
Only files that ALREADY EXIST in a project are refreshed.  Files are never
added and never deleted.  This is deliberate: CubeMX was configured with
``LibraryCopy=1``, so each project vendors a different subset of the drivers
(the uwb app needs ADC/I2C/SPI/TIM, the bootloader does not).  Copying the full
upstream tree in would defeat that and bloat the image.

Usage
-----
    sync_st_drivers.py               # report drift, write nothing, exit 1 if any
    sync_st_drivers.py --check       # same as above, explicit
    sync_st_drivers.py --apply       # overwrite drifted files
    sync_st_drivers.py --resolve TAG # re-pin the lock file to another CubeF4 tag
"""

from __future__ import annotations

import argparse
import filecmp
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = Path(__file__).resolve().parent / "st_drivers.lock.json"
CACHE_DIR = REPO_ROOT / ".st-cache"


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def git(*args: str, cwd: Path | None = None, capture: bool = True) -> str:
    """Run a git command, raising on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        cmd = " ".join(["git", *args])
        raise RuntimeError(f"{cmd}\n{(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def ensure_checkout(name: str, spec: dict) -> Path:
    """Fetch a component at its pinned commit into the cache, reusing if present."""
    commit = spec["commit"]
    dest = CACHE_DIR / f"{name}-{commit[:12]}"

    if (dest / ".git").is_dir():
        try:
            if git("rev-parse", "HEAD", cwd=dest) == commit:
                print(f"  [cache] {name} @ {commit[:12]}")
                return dest
        except RuntimeError:
            pass
        shutil.rmtree(dest, ignore_errors=True)

    print(f"  [fetch] {name} @ {commit[:12]} from {spec['repo']}")
    dest.mkdir(parents=True, exist_ok=True)
    git("init", "-q", ".", cwd=dest)
    git("remote", "add", "origin", spec["repo"], cwd=dest)

    fetch_args = ["fetch", "-q", "--depth", "1"]
    sparse = spec.get("sparse")
    if sparse:
        # The STM32CubeF4 superproject is ~1.5 GB; a blobless sparse fetch of
        # just the CMSIS Core headers keeps this under 10 MB.
        git("config", "core.sparseCheckout", "true", cwd=dest)
        git("sparse-checkout", "set", "--no-cone", *sparse, cwd=dest)
        fetch_args.append("--filter=blob:none")

    git(*fetch_args, "origin", commit, cwd=dest)
    git("checkout", "-q", "FETCH_HEAD", cwd=dest)
    return dest


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

class Drift:
    """Files that differ between a project directory and upstream."""

    def __init__(self) -> None:
        self.differs: list[tuple[Path, Path]] = []   # (local, upstream)
        self.same = 0
        self.orphans: list[Path] = []                # local file, no upstream match


def compare_dir(upstream_dir: Path, local_dir: Path, drift: Drift) -> None:
    """Compare files directly inside local_dir against upstream_dir (non-recursive)."""
    if not local_dir.is_dir():
        return
    for local_file in sorted(local_dir.iterdir()):
        if not local_file.is_file():
            continue
        upstream_file = upstream_dir / local_file.name
        if not upstream_file.is_file():
            drift.orphans.append(local_file)
        elif filecmp.cmp(local_file, upstream_file, shallow=False):
            drift.same += 1
        else:
            drift.differs.append((local_file, upstream_file))


def check_hal_version(hal_root: Path, expected: str) -> str | None:
    """Read the HAL version macros from upstream and compare with the lock file."""
    hal_c = hal_root / "Src" / "stm32f4xx_hal.c"
    if not hal_c.is_file():
        return None
    text = hal_c.read_text(encoding="utf-8", errors="replace")
    parts = []
    for field in ("MAIN", "SUB1", "SUB2"):
        match = re.search(rf"__STM32F4xx_HAL_VERSION_{field}\s+\(0x([0-9A-Fa-f]+)U?\)", text)
        if not match:
            return None
        parts.append(str(int(match.group(1), 16)))
    found = ".".join(parts)
    if found != expected:
        print(
            f"\nWARNING: upstream HAL version is {found}, lock file expects {expected}.\n"
            f"         The pinned commits may not match the CubeF4 tag in the lock file.",
            file=sys.stderr,
        )
    return found


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_sync(lock: dict, apply_changes: bool) -> int:
    components = lock["components"]
    checkouts = {name: ensure_checkout(name, spec) for name, spec in components.items()}

    if "hal_driver" in checkouts:
        check_hal_version(checkouts["hal_driver"], lock["expected_hal_version"])

    total_differs = 0
    total_same = 0
    total_orphans = 0

    for project in lock["projects"]:
        project_dir = REPO_ROOT / project
        if not project_dir.is_dir():
            print(f"\n{project}: SKIPPED (directory not found)")
            continue

        drift = Drift()
        for mapping in lock["mappings"]:
            checkout = checkouts[mapping["component"]]
            upstream = checkout if mapping["upstream"] == "." else checkout / mapping["upstream"]
            compare_dir(upstream, project_dir / mapping["local"], drift)

        total_same += drift.same
        total_differs += len(drift.differs)
        total_orphans += len(drift.orphans)

        status = "in sync" if not drift.differs else f"{len(drift.differs)} file(s) drifted"
        print(f"\n{project}: {drift.same} in sync, {status}")

        for local_file, upstream_file in drift.differs:
            rel = local_file.relative_to(REPO_ROOT)
            if apply_changes:
                shutil.copyfile(upstream_file, local_file)
                print(f"  updated  {rel}")
            else:
                print(f"  DRIFTED  {rel}")

        for local_file in drift.orphans:
            print(f"  no upstream counterpart: {local_file.relative_to(REPO_ROOT)}")

    print(
        f"\nTotal: {total_same} in sync, {total_differs} drifted, "
        f"{total_orphans} without upstream counterpart"
    )

    if total_differs == 0:
        print("Everything matches the pinned upstream sources.")
        return 0
    if apply_changes:
        print("Applied. Review with 'git diff' before committing.")
        return 0
    print("Run with --apply to overwrite the drifted files.")
    return 1


def cmd_resolve(lock: dict, tag: str) -> int:
    """Re-pin the lock file to a different STM32CubeF4 tag."""
    cube = lock["components"]["cube_f4"]
    print(f"Resolving {cube['repo']} at {tag} ...")

    work = CACHE_DIR / f"resolve-{tag}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    git("init", "-q", ".", cwd=work)
    git("remote", "add", "origin", cube["repo"], cwd=work)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", f"refs/tags/{tag}", cwd=work)

    cube_commit = git("rev-parse", "FETCH_HEAD", cwd=work)
    print(f"  cube_f4      {cube_commit}")

    cube["tag"] = tag
    cube["commit"] = cube_commit

    # The HAL driver and CMSIS device headers are submodules of the superproject;
    # read the gitlink each release pins rather than guessing a tag.
    for name, spec in lock["components"].items():
        path = spec.get("submodule_path")
        if not path:
            continue
        entry = git("ls-tree", "FETCH_HEAD", path, cwd=work)
        match = re.match(r"160000 commit ([0-9a-f]{40})", entry)
        if not match:
            print(f"ERROR: {path} is not a submodule at {tag}", file=sys.stderr)
            return 1
        spec["commit"] = match.group(1)
        print(f"  {name:<12} {match.group(1)}")

    LOCK_FILE.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"\nUpdated {LOCK_FILE.relative_to(REPO_ROOT)}.")
    print("Now run --check to see what would change, then --apply.")
    print("Note: expected_hal_version may need updating for the new release.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync vendored ST drivers against pinned upstream sources.",
        epilog="Only files already present in a project are refreshed; none are added or deleted.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="report drift only (default)")
    group.add_argument("--apply", action="store_true", help="overwrite drifted files")
    group.add_argument("--resolve", metavar="TAG", help="re-pin the lock file to a CubeF4 tag")
    args = parser.parse_args()

    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if args.resolve:
            return cmd_resolve(lock, args.resolve)
        return cmd_sync(lock, apply_changes=args.apply)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
