# Vehicle Embedded Platform Setup

[Documentation Home](README.md) · [Getting Started (desktop)](getting_started.md) · [Firmware Architecture](firmware/architecture.md)

Setup guide for the **embedded computer carried on the vehicle** — an NVIDIA Jetson Orin
running Ubuntu 22.04 on `aarch64`. This machine builds and flashes the STM32 firmware and
runs headless data-collection scripts. It does not run any GUI application.

[Getting Started](getting_started.md) covers the Windows desktop workflow. This page covers
everything that differs on the vehicle platform, and is self-contained: you can follow it
start to finish on a fresh board.

---

## 1. What changes on ARM64, and what does not

**STMicroelectronics does not ship STM32CubeMX or STM32CubeIDE for Linux ARM64.** Only
Windows, macOS and Linux x86_64 builds exist. Nothing you can install on the Orin will
change that.

This turns out not to matter, because the firmware does not need CubeIDE to build:

| Concern | How it is handled here |
| --- | --- |
| Compiler | `arm-none-eabi-gcc` from Ubuntu's own repository, not the CubeIDE plugin |
| Build system | `firmware/uwb/Makefile`, a standalone GNU Make build |
| Generated peripheral code | Already committed under `Core/` — regenerate only when the pinout changes |
| CMSIS + HAL drivers | Vendored under `Drivers/`, refreshed by [`tools/sync_st_drivers.py`](#6-keeping-the-st-drivers-in-sync) straight from ST's git repositories |

The one thing you cannot do here is **edit `.ioc` files**. See
[section 11](#11-what-still-needs-a-windows-or-x86-machine).

---

## 2. Install prerequisites

```bash
sudo apt update
sudo apt install -y \
    gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi \
    make git python3 python3-venv dfu-util
```

`libnewlib-arm-none-eabi` is not optional: the build links with `--specs=nano.specs`, which
fails without it.

Verify the toolchain:

```bash
arm-none-eabi-gcc --version     # expect 10.3.1 on Ubuntu 22.04
```

Version 10.3.1 is one of the two versions listed as verified in
[Getting Started](getting_started.md#1-environment--version-matrix), so no separate download
is needed. If you later want to match a CubeIDE-produced binary exactly, ARM publishes
`aarch64` Linux builds of the 13.3 toolchain; point `GCC_PATH` at that instead.

### Set `GCC_PATH`

The Makefile requires `GCC_PATH` to be set explicitly — the same on Linux as on Windows.
Ubuntu's toolchain lives in `/usr/bin`:

```bash
echo 'export GCC_PATH=/usr/bin' >> ~/.bashrc
source ~/.bashrc
```

### Let DFU flashing work without `sudo`

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0483", ATTR{idProduct}=="df11", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/49-stm32-dfu.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Add yourself to `dialout` for serial access, then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

---

## 3. Clone the repository

`protocol/nanopb` is a git submodule and the firmware **will not link without it**:

```bash
git clone --recursive https://github.com/phuongmt08/uwb-rtls.git
cd uwb-rtls
```

Already cloned without `--recursive`? Run:

```bash
git submodule update --init --recursive
git submodule status        # expect no '-' prefix on protocol/nanopb
```

> **You do not need to run `make -C protocol`.** The generated bindings
> (`protocol/protos/protocol.pb.c`, `.pb.h` and `software/common/protocol_pb2.py`) are
> committed. Regenerate them only after editing a `.proto` file — that needs `protobuf` and
> `grpcio-tools`, which are otherwise unnecessary on this machine.

---

## 4. Build the firmware

```bash
make -C firmware/uwb -j"$(nproc)"
```

A clean build takes roughly 8 seconds on an Orin. Compare the memory report against this
known-good baseline — a large deviation means something is wrong:

```
Memory region         Used Size  Region Size  %age Used
      APP_HEADER:         584 B         1 KB     57.03%
           FLASH:      179776 B       207 KB     84.81%
    DATA_STORAGE:          0 GB       256 KB      0.00%
             RAM:       77120 B       120 KB     62.76%
         LOG_RAM:          8 KB         8 KB    100.00%
```

`LOG_RAM` at 100% is by design: it is a fixed 8 KB shared log region, not a section that
grows with your code.

Artifacts land in `firmware/uwb/build/`: `uwb-rtls.elf`, `.hex`, `.bin` and `.map`.

Other targets:

```bash
make -C firmware/uwb size       # section sizes of the current ELF
make -C firmware/uwb clean
make -C firmware/uwb rebuild
```

---

## 5. Flash the firmware

Put the board into bootloader mode, confirm it enumerates, then flash:

```bash
lsusb | grep -i 0483:df11      # ST DFU device should appear
make -C firmware/uwb flash
```

`flash` invokes `dfu-util` against alt setting 0 (`@App Flash`) at `0x0800C000`, the
application region defined in `firmware/common/memorylayout.h`, and resets the board
afterwards. Override any of it if your setup differs:

```bash
make -C firmware/uwb flash DFU_VID_PID=0483:df11 DFU_ADDRESS=0x0800C000
```

---

## 6. Keeping the ST drivers in sync

`firmware/uwb/Drivers` and `firmware/bootloader1/Drivers` hold vendored copies of CMSIS
Core, the CMSIS STM32F4xx device headers, and the STM32F4xx HAL driver. On a desktop these
are written by STM32CubeMX. Here, `tools/sync_st_drivers.py` refreshes them directly from
ST's upstream git repositories, pinned by `tools/st_drivers.lock.json` to exactly the
commits that **STM32Cube FW_F4 V1.28.3** ships — the firmware package the `.ioc` files
declare.

```bash
python3 tools/sync_st_drivers.py            # report drift, write nothing (exit 1 if any)
python3 tools/sync_st_drivers.py --apply    # overwrite the drifted files
```

The first run downloads about 45 MB into `.st-cache/` (git-ignored) and reuses it afterwards.

### Mirror semantics — read this before using `--apply`

**The script only refreshes files that already exist. It never adds and never deletes.**

That is deliberate. CubeMX was configured with `LibraryCopy=1`, so each project vendors only
the drivers it uses: the UWB application has 45 headers and 28 sources, the bootloader 32 and
19. Copying the full upstream tree in would defeat that and inflate the image.

Files it reports as having *no upstream counterpart* are expected and are left untouched —
they are the short `LICENSE.txt` stubs CubeMX writes, whereas the upstream repositories ship
`LICENSE.md`.

### When to run it

- After changing `tools/st_drivers.lock.json`.
- Before a release, as an audit that nothing drifted.
- Whenever a build behaves oddly and you suspect a hand-edited driver file.

Running `--check` in CI keeps the vendored tree honest.

### Upgrading to a newer STM32CubeF4 release

```bash
python3 tools/sync_st_drivers.py --resolve v1.28.4   # re-pin the lock file
python3 tools/sync_st_drivers.py --check             # inspect what would change
python3 tools/sync_st_drivers.py --apply
make -C firmware/uwb rebuild
```

`--resolve` reads the submodule commits that the given tag pins, so the three components
stay mutually consistent instead of being guessed individually. Update
`expected_hal_version` in the lock file to match the new release; the script warns if the
HAL version it finds upstream disagrees.

> **Do not reformat anything under `Drivers/`.** Those files must stay byte-identical to
> upstream for the sync to be meaningful. A `.clang-format` with `DisableFormat: true` sits
> in each `Drivers/` directory to stop editors from reformatting on save — this tree drifted
> from upstream exactly that way once already.

---

## 7. Python environment (headless)

The Orin runs no GUI. RTLS Studio, the Programmer and the simulation tools all stay on a
desktop machine. What remains is the protocol layer plus a serial link — enough for data
collection and debugging.

Run it from the repository root:

```bash
cd ~/Desktop/hoang_anh/uwb-rtls
python3 software/install.py
```

No `--profile` needed: it defaults to `orin` on `aarch64`. Expected output:

```
Platform: Linux aarch64, Python 3.13.9
Profile:  orin  ->  requirements-orin.txt
...
  ok      protobuf     7.36.1
  ok      pyserial     3.5
  ok      protocol_pb2
All good.
```

This creates `software/.venv` and installs `software/requirements-orin.txt`, which is just
`protobuf` and `pyserial`. That list was derived from the actual imports of
`software/common/` and `software/vv_testings/`, not by trimming the desktop list by hand.
Activate it before running your own scripts:

```bash
source software/.venv/bin/activate
```

### Virtualenv or conda — pick one

By default `install.py` builds an isolated venv at `software/.venv`, separate from any conda
environment. If your data-collection scripts live in a conda environment and need these
packages alongside what is already installed there (ROS, for instance), install into the
active environment instead:

```bash
python3 software/install.py --no-venv
```

Use one or the other, not both, or you will lose track of which interpreter has what.

### Options

| Command | Use when |
| --- | --- |
| `python3 software/install.py` | First-time setup, or after `requirements-orin.txt` changes |
| `python3 software/install.py --check` | Verify the imports work; installs nothing |
| `python3 software/install.py --no-venv` | Install into the active environment (conda) |
| `python3 software/install.py --recreate` | The venv is broken; delete and rebuild it |
| `python3 software/install.py --venv PATH` | Put the venv somewhere else |
| `python3 software/install.py --profile desktop` | **Fails on ARM64** — the GUI profile needs `PyOpenGL_accelerate`, which has no aarch64 wheel. The script says so and points you back to `orin`. |

You do not need to re-run this on every build — only when the requirements file changes or
the environment breaks.

`install.py` replaces the old Windows-only `install_requirements.bat`, which remains as a
thin shim so existing Windows instructions keep working.

### Adding packages for your own scripts

If your analysis code needs `numpy` or `matplotlib`, uncomment those two lines in
[`software/requirements-orin.txt`](../software/requirements-orin.txt) and re-run `install.py`.
Both have working aarch64 wheels. On a headless board, select the non-interactive backend
before importing pyplot:

```python
import matplotlib
matplotlib.use("Agg")
```

> Deliberately excluded: `PyOpenGL_accelerate` has no `aarch64` wheel at all (source-only,
> needs a C toolchain), and `PySide6` has exactly one `aarch64` version, 6.8.0.2. Neither is
> needed headless. `requirements-orin.txt` documents this inline.

### A minimal data-collection starting point

Everything you need is in `software/common/`: `protocol_pb2` for the message definitions and
`transport.VvProtocol` for HDLC framing on top of a serial port. The snippet below builds a
request, frames it, and decodes it back — no hardware required, so you can confirm your
environment works before wiring anything up.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))   # run from software/

from common import protocol_pb2 as pb
from common.transport import VvProtocol, VvAddress

proto = VvProtocol()

req = pb.packet_t()
req.hdr.addr.src = pb.PACKET_ADDR_HOST
req.hdr.addr.dst = pb.PACKET_ADDR_MCU
req.hdr.seq = proto.next_seq()
req.device_information_get.dummy = 0

frame = proto.wrap_packet(req)
print("TX frame:", frame.hex(" "), f"({len(frame)} bytes)")

for pkt in proto.decode_from_frames(frame):        # loopback
    print("RX packet:", pkt.WhichOneof("params"), "seq =", pkt.hdr.seq,
          "dst =", VvAddress(pkt.hdr.addr.dst).name)
```

```
TX frame: 55 00 0c 00 0a 08 0a 04 08 05 10 01 10 01 22 00 d2 (17 bytes)
RX packet: device_information_get seq = 1 dst = MCU
```

Against real hardware, replace the loopback with a serial port: write `frame` to it, and feed
whatever you read back into `proto.decode_from_frames()`, which buffers partial frames across
reads.

`software/vv_testings/` holds working headless examples of this pattern — device queries,
configuration, calibration, FOTA — and is the best reference for a custom logger. Note that
a few of those scripts import `msvcrt` (Windows-only) or `tkinter`; skip those.

### Receiving the fused position stream

[`software/vv_testings/vehicle_testings/test_position.py`](../software/vv_testings/vehicle_testings/test_position.py)
is the reference consumer: it opens the Tag's virtual COM port, sends `ranging_start`, and
prints the UKF pose out of every `sensor_fusion_result` that arrives.

```bash
python3 software/vv_testings/vehicle_testings/test_position.py
python3 software/vv_testings/vehicle_testings/test_position.py --csv run1.csv --seconds 30
```

What makes the stream reach the vehicle is the **source address**, not the destination. The
script addresses everything it sends as `src = VEHICLE`, and the Tag treats the first such
packet as the start of a wired session:

| Source address seen by the Tag | Effect |
| --- | --- |
| `PACKET_ADDR_DEBUG` | marks the serial link live (desktop tooling) |
| `PACKET_ADDR_VEHICLE` | marks the serial link live (vehicle controller) |
| `PACKET_ADDR_HOST` | marks the BLE link live |

Until one of those arrives the Tag has no live session, and the fusion sender returns early
without transmitting anything — so a listener that never sends (`--no-start`) on a freshly
booted Tag will wait forever. Send `ranging_start` at least once, or run another `src =
VEHICLE` command first.

Each fused sample is then published twice, once per destination:

| Destination | Carried over | Reaches |
| --- | --- | --- |
| `PACKET_ADDR_HOST` | BLE bridge | the desktop RTLS Studio |
| `PACKET_ADDR_VEHICLE` | serial link, once a wired session is live | this script, on `/dev/ttyACM*` |

Both copies are paced independently at `SENSOR_FUSION_STREAM_PERIOD_MS` (20 ms, so ~50 Hz
each). The pacing budget is tracked per destination — a shared budget would let the first
send of a pair consume the whole period and starve the second one permanently.

Two configuration details decide whether the bytes actually land on `/dev/ttyACM*`:

- **Ranging must be enabled.** The sender is gated on it; `ranging_start` is what turns it on.
- **`host_transport` must be `USB`.** The Tag routes the serial link either to USB CDC or to
  USART1. `USB` is the default for an unconfigured device, but a Tag previously set to `UART`
  will emit on the UART pins instead, and the COM port will stay silent.

Values arrive as fixed-point hundredths — `123` means `1.23 m` or `1.23°`. The script divides
by 100 for you; a custom consumer must do the same.

---

## 8. Build & flash helper

[`software/vv_testings/vehicle_testings/build&flash.py`](../software/vv_testings/vehicle_testings/build&flash.py)
wraps the day-to-day loop — rebuild the Tag firmware, put the Tag into DFU mode, flash it —
into a menu, so you do not have to remember the sequence or press the boot button.

```bash
source software/.venv/bin/activate
export GCC_PATH=/usr/bin
python3 'software/vv_testings/vehicle_testings/build&flash.py'
```

> Quote the filename. `&` is a shell metacharacter, so bare
> `python3 software/vv_testings/vehicle_testings/build&flash.py` will not work — use quotes as above, or
> escape it as `build\&flash.py`.

```
========================================
  UWB Tag - build & flash from the Orin
========================================
  1) Build firmware
  2) Flash firmware  (auto-enters DFU as VEHICLE)
  3) Build, then flash
  q) Quit

  GCC_PATH=/usr/bin   |   image: built 4 min ago   |   Tag: running (/dev/ttyACM0)
```

The status line above the prompt shows whether `GCC_PATH` is set, how fresh the built image
is, and whether the Tag is currently running, already in DFU mode, or not detected at all.

**Option 1** runs `make -C firmware/uwb -j$(nproc)`.

**Option 2** does the part that is otherwise fiddly:

1. If a DFU device is already present, it goes straight to flashing.
2. Otherwise it finds the Tag's virtual COM port (`0483:5740`) and sends
   `enter_to_bootloader` with **`src = VEHICLE`** and `dst = MCU` — the Tag sees the reboot
   request as coming from the vehicle controller, not from a desktop host.
3. It waits for the board to re-enumerate as a DFU device (`0483:df11`).
4. It calls `make -C firmware/uwb flash`, so the DFU VID/PID and the application flash
   address stay defined in one place — the Makefile — rather than being duplicated here.

**Option 3** chains the two.

For scripting or a CI job, skip the menu:

```bash
python3 'software/vv_testings/vehicle_testings/build&flash.py' --build
python3 'software/vv_testings/vehicle_testings/build&flash.py' --flash
python3 'software/vv_testings/vehicle_testings/build&flash.py' --all
```

These exit non-zero on failure.

> **`make flash` and option 2 write to the board immediately** — there is no confirmation
> prompt. Both act on whatever DFU device is currently attached. Check the status line, or
> `dfu-util -l`, before flashing if more than one board may be connected.

---

## 9. When the serial port does not appear

`No serial port found or device not responding` from a `vv_testings` script almost never
means the script is broken. Run the diagnostic instead of guessing:

```bash
source software/.venv/bin/activate
python3 software/vv_testings/vehicle_testings/check_serial.py
```

It reports, in order: device nodes under `/dev`, what pyserial sees, the USB bus, group
membership and interference, then recent kernel USB messages — and ends with a verdict and
the command to fix it.

```bash
python3 software/vv_testings/vehicle_testings/check_serial.py --log     # always include the kernel log
python3 software/vv_testings/vehicle_testings/check_serial.py --watch   # live log; plug the board in now
```

`--watch` is the fastest way to tell a dead cable from a dead board: if nothing at all
appears when you plug in, the kernel never saw the device.

### Reading the kernel log without sudo

Ubuntu sets `kernel.dmesg_restrict=1`, so plain `dmesg` fails with
`read kernel buffer failed: Operation not permitted`. Use the journal instead — no sudo
needed:

```bash
journalctl -k -n 40 --no-pager | grep -Ei 'usb [0-9]|cdc_acm|ttyACM'
journalctl -k -f --no-pager                 # live
sudo dmesg | tail -40                       # if you prefer dmesg, it needs sudo
```

`check_serial.py` already does this for you and falls back automatically.

### Reading the result

| What the kernel log shows | Meaning |
| --- | --- |
| `usb 1-2.3: new full-speed USB device` followed by `cdc_acm 1-2.3:1.0: ttyACM0: USB ACM device` | Healthy. The application is running and `/dev/ttyACM0` exists. |
| `new full-speed USB device` with **no** `cdc_acm` line | The board enumerated in DFU mode. A DFU device exposes no serial port. Flash a good application (§8) and let it boot. |
| Nothing at all on plug-in | The kernel never saw it: charge-only cable, no power, or a bad port. Try a direct port on the Orin rather than a hub. |
| `new full-speed USB device` then `USB disconnect` seconds later, repeatedly | The board is browning out or the cable is marginal. This also corrupts a flash in progress. |

> A device in DFU mode and a device running the application are **different USB devices**:
> `0483:df11` versus `0483:5740`. Only the second one gives you `/dev/ttyACM*`. If a script
> cannot find a port, check which of the two is attached before anything else.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `GCC_PATH is not set. Please set GCC_PATH to your GNU Arm toolchain bin directory` | Variable not exported | `export GCC_PATH=/usr/bin` (§2) |
| `Cannot find arm-none-eabi-gcc under GCC_PATH='...'` | Wrong directory | Must contain `arm-none-eabi-gcc` directly, or a `bin/` that does |
| `No rule to make target 'build/protocol/nanopb/pb_common.o'` | `protocol/nanopb` submodule empty | `git submodule update --init --recursive` (§3) |
| `cannot find -lc` / missing `nano.specs` at link time | `libnewlib-arm-none-eabi` not installed | `sudo apt install libnewlib-arm-none-eabi` |
| `make: python3: Command not found` | No `python3` on PATH | `sudo apt install python3`, or `make PYTHON=/path/to/python` |
| `ERROR: 'dfu-util' not found.` | Package missing | `sudo apt install dfu-util` |
| `dfu-util: No DFU capable USB device available` | Board not in bootloader mode, or udev rule missing | Re-enter bootloader mode; check `lsusb`; apply the udev rule in §2 |
| `dfu-util: Error during special command "ERASE_PAGE" get_status`, transfer stops part-way | The board dropped off USB mid-transfer — loose cable, marginal power, or it was unplugged | Reseat the cable (a short, powered one), re-enter DFU and flash again. The bootloader lives at `0x08000000`–`0x0800BFFF` and is never written by this command, so a half-finished application flash is always recoverable this way. |
| Tag does not enumerate after a failed flash | Application flash is incomplete, so there is nothing valid to boot | Expected. Enter DFU mode and flash again — the bootloader is intact. |
| `ERROR: could not create the virtual environment.` | `python3-venv` missing | `sudo apt install python3-venv` |
| Serial port permission denied | User not in `dialout` | `sudo usermod -aG dialout "$USER"`, then log out and back in |
| `sync_st_drivers.py` reports drift you did not make | An editor reformatted a file under `Drivers/` | `--apply` to restore, and check §6 on formatting |

---

## 11. What still needs a Windows or x86 machine

| Task | Why |
| --- | --- |
| Editing `.ioc` / regenerating peripheral code | STM32CubeMX has no Linux ARM64 build |
| Building the Nordic BLE firmware | Needs nRF5 SDK 17.1.0 — see [Getting Started §3.4](getting_started.md#34-build-nordic-ble-firmware) |
| Running RTLS Studio, the Programmer, or the simulation tools | GUI applications; use the `desktop` profile there |

Regenerating peripheral code elsewhere costs nothing here: the Makefile discovers sources
with `$(wildcard ...)`, so files CubeMX adds under `Core/` or `Drivers/` are picked up
automatically on the next build. Only a brand-new *directory* — enabling a new middleware,
say — needs a line added to `LOCAL_SOURCES` and `INCLUDE_DIRS` in `firmware/uwb/Makefile`.
