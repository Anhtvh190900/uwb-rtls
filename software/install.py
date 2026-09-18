#!/usr/bin/env python3
"""Set up the Python environment for the UWB-RTLS host software.

Replaces install_requirements.bat, which was Windows-only and hunted for a
hardcoded Python 3.12 path. This runs on Windows and Linux alike.

Two profiles:

  desktop  RTLS Studio, Programmer, simulation tools - the full GUI stack.
           Requires a display. This is requirements.txt.

  orin     Headless profile for the vehicle embedded platform (Jetson Orin,
           aarch64): protocol layer plus serial only, no Qt. This is
           requirements-orin.txt.

The profile defaults to "orin" on aarch64 and "desktop" everywhere else.

Examples
--------
    python3 install.py                      # autodetect profile, create .venv
    python3 install.py --profile orin
    python3 install.py --check              # verify imports, install nothing
    python3 install.py --no-venv            # install into the active env
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_DIR = HERE / ".venv"
MIN_PYTHON = (3, 10)

PROFILES = {
    "desktop": HERE / "requirements.txt",
    "orin": HERE / "requirements-orin.txt",
}

# Modules each profile must be able to import once installed.
# Kept small on purpose: these are the ones that actually break in practice.
CHECKS = {
    "orin": [("google.protobuf", "protobuf"), ("serial", "pyserial")],
    "desktop": [
        ("google.protobuf", "protobuf"),
        ("serial", "pyserial"),
        ("PyQt6.QtCore", "PyQt6"),
        ("PySide6.QtCore", "PySide6"),
    ],
}


def default_profile() -> str:
    return "orin" if platform.machine().lower() in ("aarch64", "arm64") else "desktop"


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def run(cmd: list[str]) -> int:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run([str(c) for c in cmd]).returncode


def check_imports(python: Path, profile: str) -> int:
    """Import each required module in `python` and report what is missing."""
    print(f"\nChecking imports for profile '{profile}' using {python}")
    failures = []
    for module, package in CHECKS[profile]:
        probe = (
            f"import importlib; "
            f"m = importlib.import_module('{module}'); "
            f"v = getattr(m, '__version__', None) "
            f"or getattr(importlib.import_module('{module.split('.')[0]}'), '__version__', '?'); "
            f"print('  ok      {package:<12}', v)"
        )
        result = subprocess.run(
            [str(python), "-c", probe],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            print(result.stdout.rstrip())
        else:
            failures.append(package)
            last = (result.stderr or "").strip().splitlines()
            reason = last[-1] if last else "import failed"
            print(f"  MISSING {package:<12} {reason}")

    # The generated protobuf bindings are the thing most likely to break on a
    # version mismatch, so exercise them rather than just importing protobuf.
    if not failures:
        result = subprocess.run(
            [str(python), "-c", "from common import protocol_pb2; print('  ok      protocol_pb2')"],
            cwd=str(HERE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            print(result.stdout.rstrip())
        else:
            print("  FAILED  protocol_pb2")
            print((result.stderr or "").rstrip())
            return 1

    if failures:
        print(f"\nMissing: {', '.join(failures)}")
        print("Run without --check to install them.")
        return 1

    print("\nAll good.")
    return 0


def print_activation(venv: Path) -> None:
    print("\nActivate the environment with:")
    if os.name == "nt":
        print(f"  cmd:        {venv}\\Scripts\\activate.bat")
        print(f"  PowerShell: {venv}\\Scripts\\Activate.ps1")
        print(f"  Git Bash:   source {venv.as_posix()}/Scripts/activate")
    else:
        print(f"  source {venv}/bin/activate")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install Python dependencies for the UWB-RTLS host software.",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        help=f"dependency set to install (default: {default_profile()} on this machine)",
    )
    parser.add_argument("--check", action="store_true", help="verify imports only, install nothing")
    parser.add_argument("--no-venv", action="store_true", help="install into the active environment")
    parser.add_argument("--venv", type=Path, default=VENV_DIR, help=f"venv location (default: {VENV_DIR})")
    parser.add_argument("--recreate", action="store_true", help="delete and recreate the venv first")
    args = parser.parse_args()

    profile = args.profile or default_profile()
    req_file = PROFILES[profile]

    print(f"Platform: {platform.system()} {platform.machine()}, Python {platform.python_version()}")
    print(f"Profile:  {profile}  ->  {req_file.name}")

    if not req_file.is_file():
        print(f"\nERROR: {req_file} not found.", file=sys.stderr)
        return 1

    # --- check-only mode -------------------------------------------------
    if args.check:
        target = sys.executable if args.no_venv else venv_python(args.venv)
        if not args.no_venv and not Path(target).exists():
            print(f"\nERROR: no environment at {args.venv}.", file=sys.stderr)
            print("Run without --check to create it, or pass --no-venv.", file=sys.stderr)
            return 1
        return check_imports(Path(target), profile)

    # --- pick the interpreter -------------------------------------------
    if sys.version_info < MIN_PYTHON:
        need = ".".join(str(n) for n in MIN_PYTHON)
        print(f"\nERROR: Python {need}+ required, running {platform.python_version()}.", file=sys.stderr)
        print(f"Re-run with a newer interpreter, e.g. python3.12 {Path(__file__).name}", file=sys.stderr)
        return 1

    if args.no_venv:
        python = Path(sys.executable)
        print(f"\nInstalling into the active environment: {python}")
    else:
        if args.recreate and args.venv.exists():
            import shutil

            print(f"\nRemoving {args.venv}")
            shutil.rmtree(args.venv)

        python = venv_python(args.venv)
        if python.exists():
            print(f"\nReusing existing environment: {args.venv}")
        else:
            print(f"\nCreating environment: {args.venv}")
            if run([sys.executable, "-m", "venv", args.venv]) != 0:
                print("\nERROR: could not create the virtual environment.", file=sys.stderr)
                if os.name != "nt":
                    print("On Debian/Ubuntu you may need: sudo apt install python3-venv", file=sys.stderr)
                return 1

    # --- install ---------------------------------------------------------
    print("\nUpgrading pip")
    if run([python, "-m", "pip", "install", "--upgrade", "pip"]) != 0:
        return 1

    print(f"\nInstalling {req_file.name}")
    if run([python, "-m", "pip", "install", "-r", req_file]) != 0:
        print("\nERROR: dependency installation failed.", file=sys.stderr)
        if profile == "desktop" and platform.machine().lower() in ("aarch64", "arm64"):
            print(
                "\nNote: the 'desktop' profile is not expected to install on ARM64.\n"
                "      PyOpenGL_accelerate has no aarch64 wheel. Use --profile orin.",
                file=sys.stderr,
            )
        return 1

    rc = check_imports(python, profile)
    if not args.no_venv:
        print_activation(args.venv)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
