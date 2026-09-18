#!/usr/bin/env python3
"""Diagnose why the Tag's serial port does not show up, on the Orin.

`No serial port found or device not responding` from a vv_testings script is
almost always the board, the cable, the udev rule or group membership -- not the
script. This checks each of those in turn and ends with a verdict naming the one
command that fixes it.

Checks, in order:
    1. device nodes under /dev
    2. what pyserial enumerates
    3. the USB bus (lsusb), including DFU mode
    4. dialout membership, and processes holding the port open
    5. recent kernel USB messages

Usage
-----
    python3 check_serial.py              # run every check, print a verdict
    python3 check_serial.py --log        # always include the kernel log
    python3 check_serial.py --watch      # live kernel log; plug the board in now

Needs only the headless dependency set:
    python3 software/install.py --profile orin
"""

from __future__ import annotations

import argparse
import getpass
import glob
import os
import shutil
import subprocess
import sys

# ST's USB ids: the application enumerates as a CDC ACM port, the ROM/bootloader
# as DFU. Seeing DFU here explains an absent ttyACM -- the board is not running
# the application.
VID_ST = "0483"
PID_DFU = "df11"

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"


def _color(text: str, code: str) -> str:
    return text if not sys.stdout.isatty() else f"{code}{text}{RESET}"


def ok(text: str) -> str:
    return f"  {_color('[ ok ]', GREEN)} {text}"


def warn(text: str) -> str:
    return f"  {_color('[warn]', YELLOW)} {text}"


def bad(text: str) -> str:
    return f"  {_color('[fail]', RED)} {text}"


def heading(text: str) -> None:
    print(f"\n{_color(text, BOLD)}")


def run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output).

    Returns (127, "") when the binary is absent so callers can treat a missing
    tool as a skipped check rather than a failure.
    """
    if shutil.which(cmd[0]) is None:
        return 127, ""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --- 1. device nodes -------------------------------------------------------

def check_device_nodes() -> list[str]:
    heading("1. Device nodes under /dev")
    nodes = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not nodes:
        print(bad("no /dev/ttyACM* or /dev/ttyUSB* present"))
        return []
    for node in nodes:
        try:
            st = os.stat(node)
            import grp

            group = grp.getgrgid(st.st_gid).gr_name
            mode = oct(st.st_mode & 0o777)[2:]
            readable = os.access(node, os.R_OK | os.W_OK)
            line = f"{node}  group={group} mode={mode}"
            print(ok(line) if readable else bad(f"{line}  (no read/write permission)"))
        except OSError as exc:
            print(bad(f"{node}  cannot stat: {exc}"))
    return nodes


# --- 2. pyserial -----------------------------------------------------------

def check_pyserial() -> list[str]:
    heading("2. What pyserial sees")
    try:
        from serial.tools import list_ports
    except ImportError:
        print(bad("pyserial not installed in this environment"))
        print("         python3 software/install.py --profile orin")
        return []

    ports = list(list_ports.comports())
    if not ports:
        print(bad("pyserial enumerates no ports"))
        return []
    for port in ports:
        detail = f"{port.device}  {port.description}"
        if port.vid is not None:
            detail += f"  [{port.vid:04x}:{port.pid:04x}]"
        print(ok(detail))
    return [p.device for p in ports]


# --- 3. USB bus ------------------------------------------------------------

def check_usb_bus() -> tuple[bool, bool]:
    """Return (any ST device present, device is in DFU mode)."""
    heading("3. USB bus")
    code, out = run(["lsusb"])
    if code == 127:
        print(warn("lsusb not installed -- skipped (sudo apt install usbutils)"))
        return False, False

    st_lines = [ln for ln in out.splitlines() if f"ID {VID_ST}:" in ln]
    if not st_lines:
        print(bad(f"no STMicroelectronics device (VID {VID_ST}) on the bus"))
        return False, False

    in_dfu = False
    for line in st_lines:
        print(ok(line.strip()))
        if f"{VID_ST}:{PID_DFU}" in line:
            in_dfu = True
    if in_dfu:
        print(warn("board is in DFU/bootloader mode -- it exposes no serial port there"))
    return True, in_dfu


# --- 4. groups and interference -------------------------------------------

def check_groups_and_holders(nodes: list[str]) -> bool:
    """Return True when the user is in dialout."""
    heading("4. Group membership and interference")
    user = getpass.getuser()
    code, out = run(["id", "-nG"])
    groups = out.split() if code == 0 else []
    in_dialout = "dialout" in groups
    if in_dialout:
        print(ok(f"{user} is in dialout"))
    else:
        print(bad(f"{user} is NOT in dialout"))

    # A port already held open by ModemManager or a stray script looks exactly
    # like a missing port to the caller, so name the holder explicitly.
    if nodes:
        code, out = run(["fuser"] + nodes)
        if code == 127:
            print(warn("fuser not installed -- cannot check for holders"))
        elif out.strip():
            print(bad(f"port is held open by another process: {out.strip()}"))
        else:
            print(ok("no other process holds the port"))

    code, out = run(["systemctl", "is-active", "ModemManager"])
    if code == 0 and out.strip() == "active":
        print(warn("ModemManager is running; it can grab ttyACM for a few seconds"))
    return in_dialout


# --- 5. kernel log ---------------------------------------------------------

KERNEL_FILTER = "usb [0-9]|cdc_acm|ttyACM|disconnect"


def show_kernel_log(lines: int = 40) -> None:
    heading("5. Recent kernel USB messages")
    code, out = run(["journalctl", "-k", "-n", str(lines), "--no-pager"])
    if code == 127:
        print(warn("journalctl not available -- skipped"))
        return
    if code != 0 or not out.strip():
        # Ubuntu sets kernel.dmesg_restrict=1, so plain dmesg fails unprivileged.
        print(warn("could not read the kernel log (try: sudo journalctl -k -n 40)"))
        return
    import re

    pattern = re.compile(KERNEL_FILTER, re.IGNORECASE)
    hits = [ln for ln in out.splitlines() if pattern.search(ln)]
    if not hits:
        print(warn("no USB/ttyACM messages in the last %d kernel lines" % lines))
        return
    for line in hits[-15:]:
        print(f"    {line}")


def watch_kernel_log() -> int:
    heading("Live kernel log -- plug the board in now (Ctrl-C to stop)")
    if shutil.which("journalctl") is None:
        print(bad("journalctl not available"))
        return 1
    print("    Nothing at all on plug-in means the kernel never saw the device:")
    print("    that is a dead cable or a dead board, not a software problem.\n")
    try:
        proc = subprocess.Popen(
            ["journalctl", "-k", "-f", "--no-pager"],
            stdout=subprocess.PIPE,
            text=True,
        )
        import re

        pattern = re.compile(KERNEL_FILTER, re.IGNORECASE)
        assert proc.stdout is not None
        for line in proc.stdout:
            if pattern.search(line):
                print(f"    {line.rstrip()}")
    except KeyboardInterrupt:
        print("\n    stopped")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    return 0


# --- verdict ---------------------------------------------------------------

def verdict(nodes, ports, st_present, in_dfu, in_dialout) -> int:
    heading("Verdict")
    if in_dfu:
        print(bad("The board is in DFU mode, so there is no serial port to open."))
        print("      Reset it into the application, or flash with:")
        print("          cd firmware/uwb && make flash")
        return 1
    if not st_present and not nodes:
        print(bad("The kernel never saw an ST device."))
        print("      Cable or board, in that order. Try a different (short, data-capable)")
        print("      cable, then confirm with:")
        print("          python3 check_serial.py --watch")
        return 1
    if nodes and not in_dialout:
        print(bad("The port exists but you cannot open it: not in dialout."))
        print("          sudo usermod -aG dialout \"$USER\"")
        print("      Then log out and back in -- a new shell alone is not enough.")
        return 1
    if nodes and not ports:
        print(bad("The node exists but pyserial does not enumerate it."))
        print("      Install the headless dependency set into the active environment:")
        print("          python3 software/install.py --profile orin")
        return 1
    if not nodes and st_present:
        print(bad("ST device on the bus, but no tty node: the CDC ACM driver did not bind."))
        print("      Inspect the kernel log:")
        print("          python3 check_serial.py --log")
        return 1
    print(ok("Serial access looks healthy."))
    print(f"      Ports: {', '.join(nodes) or 'none'}")
    print("      If a script still fails here, the problem is on the Tag side:")
    print("      confirm the firmware is running and streaming.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose a missing or unopenable Tag serial port."
    )
    parser.add_argument(
        "--log", action="store_true", help="always include the kernel log"
    )
    parser.add_argument(
        "--watch", action="store_true", help="follow the kernel log live"
    )
    args = parser.parse_args()

    if args.watch:
        return watch_kernel_log()

    nodes = check_device_nodes()
    ports = check_pyserial()
    st_present, in_dfu = check_usb_bus()
    in_dialout = check_groups_and_holders(nodes)

    # The log is noise when everything works, and the first thing you want when
    # it does not.
    if args.log or not nodes:
        show_kernel_log()

    return verdict(nodes, ports, st_present, in_dfu, in_dialout)


if __name__ == "__main__":
    sys.exit(main())
