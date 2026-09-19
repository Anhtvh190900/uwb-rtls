#!/usr/bin/env python3
"""Read the board's identity over USB, on the Orin. Read-only.

Sends device_information_get and prints what comes back: device type, role,
firmware version and git SHA, hardware version, serial number, UID.

Use it to record what is on a board *before* flashing it, and to confirm role
and serial *after* - the check Danh asked for on 19/09 ("sau nap phai kiem lai
role va Anchor ID"), without needing RTLS Studio.

Packets are addressed with src = VEHICLE, so the board sees the vehicle
controller asking, not a desktop host.

Usage
-----
    python3 device_info.py                      # autodetect port, print
    python3 device_info.py --port /dev/ttyACM0
    python3 device_info.py --json before.json   # also write a record file
    python3 device_info.py --sys-config         # also read sys_config_resp
                                                # (role, device_id, TX/RX antenna delay)

Needs only the headless dependency set:
    software/.venv/bin/python device_info.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path


def _find_software_dir(start: Path) -> Path:
    """Locate software/ so this file keeps working if it is moved."""
    for candidate in (start, *start.parents):
        if (candidate / "common" / "protocol_pb2.py").is_file():
            return candidate
        if (candidate / "software" / "common" / "protocol_pb2.py").is_file():
            return candidate / "software"
    raise SystemExit(
        f"ERROR: could not find software/common/protocol_pb2.py above {start}.\n"
        "       Keep this script inside the repository."
    )


SOFTWARE_DIR = _find_software_dir(Path(__file__).resolve().parent)
sys.path.insert(0, str(SOFTWARE_DIR))

import serial                                              # noqa: E402
from serial.tools import list_ports                        # noqa: E402

from common import protocol_pb2 as pb                      # noqa: E402
from common.commands import CommandFactory                 # noqa: E402
from common.transport import VvAddress, VvProtocol         # noqa: E402

VCP_VID, VCP_PID = 0x0483, 0x5740

DEVICE_TYPE = {0: "UNSPECIFIED", 1: "TAG", 2: "ANCHOR", 3: "GATEWAY", 4: "DEBUG_TOOL"}
DEVICE_ROLE = {0: "UNSPECIFIED", 1: "TAG", 2: "ANCHOR"}

# Every field of uwb_cfg_t, in .proto order. Recorded whole: sys_config_set on
# the board does `cfg->uwb = *new_cfg`, a wholesale overwrite, so a full record
# is what makes a later restore possible.
UWB_CFG_FIELDS = (
    "role", "device_id", "ranging_period_ms", "rx_timeout_ms", "uwb_channel",
    "uwb_prf", "uwb_data_rate", "uwb_preamble_code", "tx_antenna_delay",
    "rx_antenna_delay", "tx_power", "anchor_list", "power_mode",
    "uwb_preamble_len", "uwb_rx_pac", "uwb_ns_sfd", "uwb_phr_mode",
    "smart_tx_power", "pg_delay",
)

# The four Danh asked to see before and after a role change (19/09).
UWB_CFG_KEY_FIELDS = ("role", "device_id", "tx_antenna_delay", "rx_antenna_delay")


def find_port() -> str | None:
    for p in list_ports.comports():
        if (p.vid, p.pid) == (VCP_VID, VCP_PID):
            return p.device
    return None


def decode(resp) -> dict:
    version = resp.fw_version
    return {
        "device_type": DEVICE_TYPE.get(resp.device_type, str(resp.device_type)),
        "device_type_value": int(resp.device_type),
        "role": DEVICE_ROLE.get(resp.role, str(resp.role)),
        "role_value": int(resp.role),
        "fw_version": f"{version.major}.{version.minor}.{version.patch}+{version.build}",
        "fw_gitsha": f"{version.gitsha:016x}",
        "hw_version": int(resp.hw_version),
        "serial_number": int(resp.serial_number),
        "uid": resp.uid.hex() if resp.uid else "",
    }


def decode_uwb_cfg(cfg) -> dict:
    out = {}
    for name in UWB_CFG_FIELDS:
        value = getattr(cfg, name)
        if isinstance(value, bytes):
            value = value.hex()
        out[name] = value
    out["role_name"] = DEVICE_ROLE.get(cfg.role, str(cfg.role))
    return out


def read_device_info(link, proto, factory, src, dst, timeout) -> dict | None:
    """Send device_information_get and return the decoded reply, or None."""
    link.write(proto.wrap_packet(factory.device_information_get(src, dst, proto.next_seq())))
    link.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = link.read(4096)
        if not chunk:
            continue
        for packet in proto.decode_from_frames(chunk):
            if packet.WhichOneof("params") == "device_information_resp":
                return decode(packet.device_information_resp)
    return None


def read_sys_config(link, proto, factory, src, dst, timeout) -> dict | None:
    link.write(proto.wrap_packet(factory.sys_config_get(src, dst, proto.next_seq())))
    link.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = link.read(4096)
        if not chunk:
            continue
        for packet in proto.decode_from_frames(chunk):
            if packet.WhichOneof("params") == "sys_config_resp":
                return decode_uwb_cfg(packet.sys_config_resp.config)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read device type, role, firmware version and serial from the board.",
    )
    parser.add_argument("--port", help="serial port (default: autodetect 0483:5740)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="seconds to wait for the reply (default: 3)")
    parser.add_argument("--json", type=Path, help="write the reply to this JSON file")
    parser.add_argument("--label", default="",
                        help="free-text note stored in the JSON record")
    parser.add_argument("--sys-config", action="store_true",
                        help="also read sys_config_resp (role, device_id, antenna delays)")
    args = parser.parse_args()

    port = args.port or find_port()
    if port is None:
        print("No board serial port found (0483:5740).")
        print("If the board is in DFU mode it has no serial port - that is expected.")
        print("Diagnose with:  python3 check_serial.py")
        return 1

    proto = VvProtocol()
    factory = CommandFactory()
    src, dst = int(VvAddress.VEHICLE), int(VvAddress.MCU)

    try:
        link = serial.Serial(port, args.baud, timeout=0.2)
    except (serial.SerialException, OSError) as exc:
        print(f"ERROR: could not open {port}: {exc}")
        print("Close anything else using the port, and check 'dialout' membership.")
        return 1

    with link:
        print(f"Port {port} @ {args.baud}   src=VEHICLE dst=MCU")
        info = read_device_info(link, proto, factory, src, dst, args.timeout)
        if info is not None:
                print()
                for key in ("device_type", "role", "fw_version", "fw_gitsha",
                            "hw_version", "serial_number", "uid"):
                    print(f"  {key:<14} {info[key]}")

                if args.sys_config:
                    cfg = read_sys_config(link, proto, factory, src, dst, args.timeout)
                    if cfg is None:
                        print(f"\n  WARNING: no sys_config_resp within {args.timeout:.0f}s")
                    else:
                        info["sys_config"] = cfg
                        print("\n  sys_config_resp:")
                        for key in UWB_CFG_KEY_FIELDS:
                            shown = cfg["role_name"] if key == "role" else cfg[key]
                            print(f"    {key:<20} {shown}")
                        rest = [k for k in UWB_CFG_FIELDS if k not in UWB_CFG_KEY_FIELDS]
                        print("    " + "  ".join(f"{k}={cfg[k]}" for k in rest))

                if args.json:
                    record = dict(info)
                    record["read_at"] = datetime.now().isoformat(timespec="seconds")
                    record["port"] = port
                    if args.label:
                        record["label"] = args.label
                    args.json.write_text(json.dumps(record, indent=2) + "\n")
                    print(f"\n  recorded -> {args.json}")
                return 0

    print(f"\nNo device_information_resp within {args.timeout:.0f}s.")
    print("The board may be busy streaming, or running firmware that does not answer.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
