#!/usr/bin/env python3
"""Build and flash the Tag firmware from the vehicle's Orin.

Interactive menu:

    1  Build     ->  make -C firmware/uwb
    2  Flash     ->  put the Tag into DFU mode, then make -C firmware/uwb flash
    3  Build + flash
    q  Quit

The bootloader-entry command is sent with **src = VEHICLE**, so the Tag sees the
request as coming from the vehicle controller rather than from a desktop host.

Only needs the headless dependency set (protobuf + pyserial):

    python3 software/install.py --profile orin

Non-interactive use, for scripting:

    python3 'build&flash.py' --build
    python3 'build&flash.py' --flash
    python3 'build&flash.py' --all

Note the quotes: '&' is a shell metacharacter, so the filename must be quoted or
escaped when running it from bash.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_repo_root(start: Path) -> Path:
    """Walk up until the repo layout is recognisable, so this file can be moved."""
    for candidate in (start, *start.parents):
        if (candidate / "firmware" / "uwb" / "Makefile").is_file():
            return candidate
    raise SystemExit(
        f"ERROR: could not locate the uwb-rtls repository above {start}.\n"
        "       Keep this script somewhere inside the repository."
    )


REPO_ROOT = _find_repo_root(HERE)
SOFTWARE_DIR = REPO_ROOT / "software"
FIRMWARE_DIR = REPO_ROOT / "firmware" / "uwb"
FIRMWARE_BIN = FIRMWARE_DIR / "build" / "uwb-rtls.bin"

# The Tag enumerates as an ST virtual COM port while running the application,
# and as an ST DFU device once the bootloader takes over.
VCP_VID, VCP_PID = 0x0483, 0x5740
DFU_VID_PID = "0483:df11"

BOOTLOADER_SETTLE_S = 1.5
DFU_WAIT_TIMEOUT_S = 10.0

sys.path.insert(0, str(SOFTWARE_DIR))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], **kwargs) -> int:
    print(f"\n$ {' '.join(str(c) for c in cmd)}\n")
    return subprocess.run([str(c) for c in cmd], **kwargs).returncode


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def gcc_path() -> str | None:
    """The Makefile requires GCC_PATH explicitly; it never guesses."""
    value = os.environ.get("GCC_PATH", "").strip()
    return value or None


def require_gcc_path() -> str | None:
    value = gcc_path()
    if value:
        return value
    print("\nERROR: GCC_PATH is not set - the firmware Makefile requires it.")
    print("\n  On this board the Ubuntu toolchain lives in /usr/bin:")
    print("      export GCC_PATH=/usr/bin")
    print("\n  To make it permanent:")
    print("      echo 'export GCC_PATH=/usr/bin' >> ~/.bashrc && source ~/.bashrc")
    return None


def find_vcp() -> str | None:
    """Return the device path of the Tag's virtual COM port, if present."""
    try:
        from serial.tools import list_ports
    except ImportError:
        print("ERROR: pyserial is not installed.")
        print("       python3 software/install.py --profile orin")
        return None

    for port in list_ports.comports():
        if port.vid == VCP_VID and port.pid == VCP_PID:
            return port.device
    return None


def dfu_present() -> bool:
    """True if a DFU device is currently enumerated."""
    if not have("dfu-util"):
        return False
    result = subprocess.run(
        ["dfu-util", "-l"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    return DFU_VID_PID in result.stdout


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def do_build() -> bool:
    path = require_gcc_path()
    if path is None:
        return False

    jobs = str(os.cpu_count() or 1)
    print(f"Building firmware  (GCC_PATH={path}, -j{jobs})")
    rc = run(["make", "-C", str(FIRMWARE_DIR), f"-j{jobs}"])
    if rc != 0:
        print("\nBuild FAILED.")
        return False

    print("\nBuild OK.")
    if FIRMWARE_BIN.is_file():
        size_kb = FIRMWARE_BIN.stat().st_size / 1024
        print(f"  {FIRMWARE_BIN.relative_to(REPO_ROOT)}  ({size_kb:.1f} KiB)")
    return True


def enter_bootloader() -> bool:
    """Ask the Tag to reboot into its DFU bootloader, as the VEHICLE."""
    from common.commands import CommandFactory
    from common.transport import VvAddress, VvProtocol
    import serial

    port = find_vcp()
    if port is None:
        print(f"\nNo Tag virtual COM port found ({VCP_VID:04X}:{VCP_PID:04X}).")
        print("The Tag may already be in DFU mode, or it is not connected.")
        return False

    proto = VvProtocol()
    packet = CommandFactory().enter_to_bootloader(
        int(VvAddress.VEHICLE),   # src: this vehicle controller
        int(VvAddress.MCU),       # dst: the Tag's MCU
        proto.next_seq(),
    )
    frame = proto.wrap_packet(packet)

    print(f"\nSending enter_to_bootloader on {port}")
    print(f"  src = VEHICLE (0x{int(VvAddress.VEHICLE):02X})   "
          f"dst = MCU (0x{int(VvAddress.MCU):02X})   seq = {packet.hdr.seq}")

    try:
        with serial.Serial(port, 115200, timeout=1.0) as link:
            link.write(frame)
            link.flush()
            # The device reboots immediately; an ACK may or may not arrive first.
            for pkt in proto.decode_from_frames(link.read(64)):
                if pkt.WhichOneof("params") == "ack":
                    print("  ACK received.")
                    break
    except serial.SerialException as exc:
        print(f"\nERROR: could not open {port}: {exc}")
        print("Close anything else using the port (RTLS Studio, another script),")
        print("and check you are in the 'dialout' group.")
        return False

    print(f"Waiting for the DFU device to enumerate (up to {DFU_WAIT_TIMEOUT_S:.0f}s)")
    deadline = time.time() + DFU_WAIT_TIMEOUT_S
    while time.time() < deadline:
        if dfu_present():
            print("  DFU device is up.")
            time.sleep(0.3)   # let the interface settle before dfu-util claims it
            return True
        time.sleep(0.3)

    print("\nThe DFU device did not appear.")
    return False


def do_flash() -> bool:
    if not have("dfu-util"):
        print("\nERROR: dfu-util not found.")
        print("       sudo apt install dfu-util")
        return False

    if not FIRMWARE_BIN.is_file():
        print(f"\nNo firmware image at {FIRMWARE_BIN.relative_to(REPO_ROOT)}.")
        print("Build it first (option 1).")
        return False

    if dfu_present():
        print("\nA DFU device is already connected - skipping bootloader entry.")
    elif not enter_bootloader():
        print("\nCannot flash: the Tag is not in DFU mode.")
        print("Enter bootloader mode manually, then choose option 2 again.")
        return False

    # Delegate the actual transfer to the Makefile so the DFU VID/PID and the
    # application flash address stay defined in exactly one place.
    path = require_gcc_path()
    if path is None:
        return False

    rc = run(["make", "-C", str(FIRMWARE_DIR), "flash"])
    if rc != 0:
        print("\nFlash FAILED.")
        return False

    print("\nFlash OK - the Tag has been reset into the new firmware.")
    return True


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------

MENU = """
========================================
  UWB Tag - build & flash from the Orin
========================================
  1) Build firmware
  2) Flash firmware  (auto-enters DFU as VEHICLE)
  3) Build, then flash
  q) Quit
"""


def status_line() -> str:
    bits = []
    bits.append(f"GCC_PATH={gcc_path() or 'NOT SET'}")
    if FIRMWARE_BIN.is_file():
        age_min = (time.time() - FIRMWARE_BIN.stat().st_mtime) / 60
        bits.append(f"image: built {age_min:.0f} min ago")
    else:
        bits.append("image: none")
    port = find_vcp()
    if dfu_present():
        bits.append("Tag: DFU mode")
    elif port:
        bits.append(f"Tag: running ({port})")
    else:
        bits.append("Tag: not detected")
    return "  " + "   |   ".join(bits)


def menu_loop() -> int:
    while True:
        print(MENU)
        print(status_line())
        try:
            choice = input("\nSelect: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0

        if choice == "1":
            do_build()
        elif choice == "2":
            do_flash()
        elif choice == "3":
            if do_build():
                do_flash()
        elif choice in ("q", "quit", "exit"):
            print("Bye.")
            return 0
        else:
            print(f"Unknown option: {choice!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and flash the UWB Tag firmware from the vehicle's Orin.",
    )
    parser.add_argument("--build", action="store_true", help="build, then exit")
    parser.add_argument("--flash", action="store_true", help="flash, then exit")
    parser.add_argument("--all", action="store_true", help="build then flash, then exit")
    args = parser.parse_args()

    if args.all:
        return 0 if (do_build() and do_flash()) else 1
    if args.build:
        return 0 if do_build() else 1
    if args.flash:
        return 0 if do_flash() else 1

    return menu_loop()


if __name__ == "__main__":
    raise SystemExit(main())
