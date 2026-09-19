"""Check and analyse the range_diag_t research stream (roadmap K1).

Modes
-----
--self-test
    Host-only checks, no hardware: packet sizes against the nanopb/HDLC limits,
    Studio recorder round trip (session_messages.csv format), CIR decoding,
    DW1000 power formulas and the loss analysis itself.

--session PATH
    Analyse a real ``<session>/messages/session_messages.csv`` recorded by
    RTLS Studio while the Tag ran firmware built with
    SYS_RANGING_DIAG_STREAM_ENABLE (and optionally SYS_RANGING_DIAG_CIR_ENABLE):
    end-to-end packet loss, cycle completeness, CIR read time, CIR/peak index
    alignment, stream throughput, effective cycle period, and — when present in
    the same CSV — ranging_period_ms and RTOS task stack/CPU responses.

Exit code 0 when every check passes, 1 otherwise.

Examples
--------
    python trust_range_diag_check.py --self-test
    python trust_range_diag_check.py --session D:/sessions/2026xxxx/messages/session_messages.csv --out-json report.json
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import os
import re
import statistics
import struct
import sys
import tempfile
from dataclasses import dataclass, field

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(SOFTWARE_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)

from google.protobuf.json_format import MessageToDict  # noqa: E402

from common import protocol_pb2 as pb  # noqa: E402

# Limits from the protocol/firmware (checked against protocol.pb.h when available).
HDLC_MAX_DATA_LEN = 512                 # firmware/common/serial/hdlc.h
PACKET_WARN_BYTES = 230                 # protocol.proto PROTOBUF_PACKET_WARN_BYTES
DW1000_A_PRF64_DB = 121.74              # DW1000 User Manual 4.7
DW1000_A_PRF16_DB = 113.77
STUDIO_FIELDNAMES = [                   # services/session_message_recorder.py
    "time_iso", "timestamp", "direction", "message", "seq", "src", "dst",
    "payload_summary", "payload_json", "packet_hex",
]

# Default acceptance thresholds for --session (override on the command line).
DEFAULT_MAX_LOSS_PCT = 5.0
DEFAULT_MAX_CIR_READ_P99_US = 700
DEFAULT_MAX_ALIGN_MEDIAN = 1.0


# --------------------------------------------------------------------------- helpers
def nanopb_size(name: str) -> int | None:
    """Read '#define protobuf_<name>_size N' from protocol.pb.h, if present."""
    path = os.path.join(REPO_DIR, "protocol", "protos", "protocol.pb.h")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(rf"#define\s+protobuf_{name}_size\s+(\d+)", text)
    return int(m.group(1)) if m else None


def firmware_define(name: str, default: int) -> int:
    """Read an integer '#define NAME value' from firmware/uwb/sys/positioning_config.h."""
    path = os.path.join(REPO_DIR, "firmware", "uwb", "sys", "positioning_config.h")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return default
    m = re.search(rf"#define\s+{name}\s+(\d+)U?\b", text)
    return int(m.group(1)) if m else default


def decode_cir(raw: bytes) -> list[complex]:
    """int16 little-endian (real, imaginary) pairs -> complex samples."""
    n = len(raw) // 4
    vals = struct.unpack("<" + "h" * (2 * n), raw[: 4 * n])
    return [complex(vals[2 * i], vals[2 * i + 1]) for i in range(n)]


def fp_power_dbm(f1: int, f2: int, f3: int, n: float, a_db: float = DW1000_A_PRF64_DB) -> float:
    return 10.0 * math.log10((f1 * f1 + f2 * f2 + f3 * f3) / (n * n)) - a_db


def rx_power_dbm(cir_pwr: int, n: float, a_db: float = DW1000_A_PRF64_DB) -> float:
    return 10.0 * math.log10((cir_pwr * (2 ** 17)) / (n * n)) - a_db


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] if lo == hi else ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def build_packet(diag: "pb.range_diag_t", seq: int = 1) -> "pb.packet_t":
    pkt = pb.packet_t()
    pkt.hdr.addr.src = pb.PACKET_ADDR_MCU
    pkt.hdr.addr.dst = pb.PACKET_ADDR_HOST
    pkt.hdr.seq = seq
    pkt.range_diag.CopyFrom(diag)
    return pkt


def studio_row(pkt: "pb.packet_t", host_ts: float) -> dict:
    """Reproduce SessionMessageRecorder._write_packet for one received packet."""
    try:
        payload = MessageToDict(pkt.range_diag, preserving_proto_field_name=True,
                                always_print_fields_with_no_presence=True)
    except TypeError:  # older protobuf runtime
        payload = MessageToDict(pkt.range_diag, preserving_proto_field_name=True,
                                including_default_value_fields=True)
    return {
        "time_iso": "", "timestamp": f"{host_ts:.6f}", "direction": "rx", "message": "range_diag",
        "seq": str(pkt.hdr.seq), "src": str(pkt.hdr.addr.src), "dst": str(pkt.hdr.addr.dst),
        "payload_summary": "", "payload_json": json.dumps(payload, sort_keys=True),
        "packet_hex": pkt.SerializeToString().hex(),
    }


# --------------------------------------------------------------------------- session analysis
@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def load_session_rows(path: str) -> tuple[list[dict], list[dict]]:
    """Return (range_diag payloads with host timestamp/packet size, other messages)."""
    diag, other = [], []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                continue
            if row.get("message") == "range_diag" and row.get("direction", "rx") == "rx":
                payload["_host_ts"] = float(row.get("timestamp") or "nan")
                payload["_packet_bytes"] = len(row.get("packet_hex") or "") // 2
                diag.append(payload)
            else:
                payload["_message"] = row.get("message")
                payload["_host_ts"] = float(row.get("timestamp") or "nan")
                other.append(payload)
    return diag, other


def analyse_session(diag: list[dict], other: list[dict], max_loss_pct: float,
                    max_cir_p99_us: float, max_align_median: float) -> Report:
    rep = Report()
    m = rep.metrics
    m["range_diag_rows"] = len(diag)
    if not diag:
        rep.check("range_diag present", False, "no message=range_diag rows (stream disabled, old protocol_pb2, or not recorded)")
        return rep
    rep.check("range_diag present", True, f"{len(diag)} rows")

    # 1) End-to-end loss (Tag send attempt -> Studio CSV) from pkt_seq gaps.
    seqs = sorted({int(d.get("pkt_seq", 0)) for d in diag})
    expected = seqs[-1] - seqs[0] + 1
    loss_pct = 100.0 * (expected - len(seqs)) / expected if expected > 0 else 0.0
    m.update(pkt_seq_first=seqs[0], pkt_seq_last=seqs[-1], pkt_expected=expected,
             pkt_received=len(seqs), loss_pct=round(loss_pct, 3))
    rep.check("end-to-end loss", loss_pct <= max_loss_pct, f"{loss_pct:.2f}% (limit {max_loss_pct}%)")

    # 2) Tag-side counters (cumulative): stream pacing drops and host-UART failures.
    first, last = min(diag, key=lambda d: int(d.get("pkt_seq", 0))), max(diag, key=lambda d: int(d.get("pkt_seq", 0)))
    drop_delta = int(last.get("link_drop_count", 0)) - int(first.get("link_drop_count", 0))
    uart_delta = int(last.get("uart_tx_fail_count", 0)) - int(first.get("uart_tx_fail_count", 0))
    m.update(link_drop_delta=drop_delta, uart_tx_fail_delta=uart_delta)
    rep.check("tag-side link drops", drop_delta == 0, f"{drop_delta} links skipped by pacing during session")
    rep.check("host UART write failures", uart_delta == 0, f"{uart_delta} failed writes (all streams)")

    # 3) Cycle completeness and effective period.
    cycles: dict[int, list[dict]] = {}
    for d in diag:
        cycles.setdefault(int(d.get("cycle_id", 0)), []).append(d)
    ids = sorted(cycles)
    complete = sum(1 for c in ids if len({int(x.get("anchor_id", 0)) for x in cycles[c]}) >= int(cycles[c][0].get("link_count", 0)))
    span_ids = ids[-1] - ids[0] + 1
    tag_ts = [int(cycles[c][0].get("timestamp_ms", 0)) for c in ids]
    periods = [(tag_ts[i + 1] - tag_ts[i]) / (ids[i + 1] - ids[i]) for i in range(len(ids) - 1) if ids[i + 1] > ids[i]]
    m.update(cycles_seen=len(ids), cycles_span=span_ids, cycles_all_links=complete,
             cycles_not_published=span_ids - len(ids),
             cycle_period_ms_median=round(statistics.median(periods), 2) if periods else None)
    rep.check("cycles published", len(ids) > 0, f"{len(ids)} of {span_ids} cycle_ids seen, {complete} with all links")

    # 4) Per-anchor RESP/RESULT validity.
    per_anchor: dict[int, dict] = {}
    for d in diag:
        a = per_anchor.setdefault(int(d.get("anchor_id", 0)), {"rows": 0, "resp": 0, "result": 0, "cir": 0})
        a["rows"] += 1
        a["resp"] += bool(d.get("resp_valid"))
        a["result"] += bool(d.get("result_valid"))
        a["cir"] += bool(d.get("cir"))
    m["per_anchor"] = per_anchor

    # 5) CIR read time and alignment (validates the ACC_MEM dummy-octet handling).
    cir_rows = [d for d in diag if d.get("cir")]
    m["cir_rows"] = len(cir_rows)
    if cir_rows:
        read_us = [float(d.get("cir_read_us", 0)) for d in cir_rows]
        p99 = percentile(read_us, 99)
        m.update(cir_read_us_min=min(read_us), cir_read_us_median=statistics.median(read_us),
                 cir_read_us_p95=round(percentile(read_us, 95), 1), cir_read_us_p99=round(p99, 1),
                 cir_read_us_max=max(read_us))
        rep.check("CIR read time p99", p99 <= max_cir_p99_us, f"{p99:.0f} us (limit {max_cir_p99_us} us)")

        diffs = []
        for d in cir_rows:
            samples = decode_cir(base64.b64decode(d["cir"]))
            if not samples:
                continue
            peak = int(d.get("peak_path_index", 0))
            start = int(d.get("cir_start_index", 0))
            if start <= peak < start + len(samples):
                arg = max(range(len(samples)), key=lambda i: abs(samples[i]))
                diffs.append(abs((start + arg) - peak))
        if diffs:
            med = statistics.median(diffs)
            m.update(cir_peak_align_rows=len(diffs), cir_peak_align_median=med)
            rep.check("CIR/peak index alignment", med <= max_align_median,
                      f"median |argmax|CIR| - peak_path_index| = {med} samples over {len(diffs)} rows")
        else:
            rep.check("CIR/peak index alignment", False, "no CIR row with peak_path_index inside the window")
    else:
        m["cir_rows"] = 0

    # 6) Throughput and host arrival jitter (Studio side).
    host_ts = sorted(d["_host_ts"] for d in diag if not math.isnan(d["_host_ts"]))
    if len(host_ts) >= 2 and host_ts[-1] > host_ts[0]:
        span_s = host_ts[-1] - host_ts[0]
        total_bytes = sum(d["_packet_bytes"] for d in diag)
        gaps = [host_ts[i + 1] - host_ts[i] for i in range(len(host_ts) - 1)]
        m.update(session_span_s=round(span_s, 2), range_diag_pkts_per_s=round(len(diag) / span_s, 2),
                 range_diag_bytes_per_s=round(total_bytes / span_s, 1),
                 max_packet_bytes=max(d["_packet_bytes"] for d in diag),
                 packets_over_warn_230=sum(1 for d in diag if d["_packet_bytes"] > PACKET_WARN_BYTES),
                 host_arrival_gap_max_s=round(max(gaps), 3))

    # 7) Board configuration and RTOS health, when Studio recorded the responses.
    periods_cfg = [o.get("config", {}).get("ranging_period_ms") for o in other if o.get("_message") == "sys_config_resp"]
    periods_status = [o.get("ranging_period_ms") for o in other if o.get("_message") == "ranging_status_resp"]
    m["ranging_period_ms_sys_config_resp"] = periods_cfg[-1] if periods_cfg else "MISSING"
    m["ranging_period_ms_ranging_status_resp"] = periods_status[-1] if periods_status else "MISSING"
    stats = [o for o in other if o.get("_message") == "rtos_task_stats_resp"]
    if stats:
        tasks = {}
        for o in stats:
            for t in o.get("tasks", []):
                cur = tasks.setdefault(t.get("name", "?"), {"stack_min_free_bytes": 1 << 30, "cpu_permille_max": 0})
                cur["stack_min_free_bytes"] = min(cur["stack_min_free_bytes"], int(t.get("stack_min_free_bytes", 0)))
                cur["cpu_permille_max"] = max(cur["cpu_permille_max"], int(t.get("cpu_permille", 0)))
        m["rtos_tasks"] = tasks
    else:
        m["rtos_tasks"] = "MISSING"
    return rep


def print_report(rep: Report) -> None:
    print("== metrics ==")
    for key, value in rep.metrics.items():
        print(f"  {key}: {value}")
    print("== checks ==")
    for name, ok, detail in rep.checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print("RESULT:", "PASS" if rep.passed else "FAIL")


# --------------------------------------------------------------------------- self-test
def sample_diag(cycle_id: int, anchor_id: int, with_cir: bool, pkt_seq: int) -> "pb.range_diag_t":
    d = pb.range_diag_t(
        cycle_id=cycle_id, seq=cycle_id % 256, timestamp_ms=900000 + 100 * cycle_id, anchor_id=anchor_id,
        distance_mm=5412, result_valid=True, resp_valid=True,
        a_fp_amp_norm_q8=8561, a_fp_snr_q8=45346, a_fp_confidence_q8=243,
        fp_amp1=5210, fp_amp2=6030, fp_amp3=4880, std_noise=90, rxpacc=482, rxpacc_nosat=489,
        cir_pwr=1326, fp_index_q6=47808, peak_path_index=748, peak_path_amp=7011, lde_threshold=1920,
        link_count=4, pkt_seq=pkt_seq, link_drop_count=0, uart_tx_fail_count=0,
    )
    if with_cir:
        pre = firmware_define("SYS_RANGING_DIAG_CIR_PRE_SAMPLES", 8)
        count = firmware_define("SYS_RANGING_DIAG_CIR_SAMPLES", 28)
        start = (47808 >> 6) - pre                     # firmware: fp_index - PRE_SAMPLES
        samples = []
        for i in range(count):
            amp = 7000 if start + i == 748 else (300 if i < pre else 1500 // (1 + abs(start + i - 748)))
            samples += [amp, -amp // 4]
        d.cir = struct.pack("<" + "h" * len(samples), *samples)
        d.cir_start_index = start
        d.cir_read_us = 310
    return d


def self_test() -> bool:
    ok = True

    def expect(name: str, cond: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")

    print("== self-test ==")
    nb_diag, nb_packet = nanopb_size("range_diag_t"), nanopb_size("packet_t")
    nb_largest = nanopb_size("rtos_task_stats_resp_t")

    # T1 worst-case message size vs nanopb and packet capacity.
    worst = pb.range_diag_t()
    for f in worst.DESCRIPTOR.fields:
        if f.name == "cir":
            worst.cir = b"\xff" * 128
        elif f.type == f.TYPE_BOOL:
            setattr(worst, f.name, True)
        elif f.type == f.TYPE_SINT32:
            setattr(worst, f.name, -(2 ** 31))
        else:
            setattr(worst, f.name, 0xFFFFFFFF)
    worst_size = worst.ByteSize()
    expect("T1 worst range_diag_t size", nb_diag is not None and worst_size <= nb_diag <= (nb_largest or 310),
           f"python {worst_size} B, nanopb {nb_diag} B, largest existing param {nb_largest} B")
    worst_pkt = len(build_packet(worst, seq=0xFFFFFFFF).SerializeToString())
    expect("T2 worst packet_t size", nb_packet is not None and worst_pkt <= nb_packet and worst_pkt <= HDLC_MAX_DATA_LEN,
           f"{worst_pkt} B (packet_t_size {nb_packet} B, HDLC data {HDLC_MAX_DATA_LEN} B)")

    # T3 realistic packets must stay under the 230 B firmware log warning, also
    # late in a multi-hour session when the counters need more varint bytes.
    cir_samples = firmware_define("SYS_RANGING_DIAG_CIR_SAMPLES", 28)
    typ_scalar = len(build_packet(sample_diag(1532, 2, False, 6128), seq=18422).SerializeToString())
    typ_cir = len(build_packet(sample_diag(1532, 2, True, 6129), seq=18423).SerializeToString())
    long_cir = len(build_packet(sample_diag(108000, 2, True, 540000), seq=2_000_000).SerializeToString())
    expect("T3 realistic packet sizes", long_cir <= PACKET_WARN_BYTES,
           f"scalar {typ_scalar} B, {cir_samples}-sample CIR {typ_cir} B, "
           f"CIR after ~3 h {long_cir} B (warn {PACKET_WARN_BYTES} B)")

    # T4 Studio recorder round trip + CIR decode.
    diag_msgs = [sample_diag(c, a, a == ((c % 4) + 1), pkt_seq=(c - 10) * 4 + a) for c in range(10, 13) for a in (1, 2, 3, 4)]
    rows = [studio_row(build_packet(d, seq=i + 1), host_ts=1000.0 + 0.025 * i) for i, d in enumerate(diag_msgs)]
    dropped = rows.pop(5)                                   # simulate one packet lost on the link
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session_messages.csv")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=STUDIO_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        parsed, other = load_session_rows(path)
    first = parsed[0]
    cir_back = base64.b64decode(next(p for p in parsed if p.get("cir"))["cir"])
    expect("T4 recorder round trip", len(parsed) == 11 and int(first["fp_amp1"]) == 5210
           and len(cir_back) == 4 * cir_samples and first["distance_mm"] == 5412,
           f"{len(parsed)} rows parsed, CIR {len(cir_back)} B, dropped pkt_seq {json.loads(dropped['payload_json'])['pkt_seq']}")

    # T5 DW1000 power formulas (hand values from DE_XUAT_K1_RANGE_DIAG.md section 6).
    fp = fp_power_dbm(5210, 6030, 4880, 482)
    rx = rx_power_dbm(1326, 482)
    expect("T5 FP/RX power", abs(fp - (-95.99)) < 0.05 and abs(rx - (-93.00)) < 0.05,
           f"FP {fp:.2f} dBm, RX {rx:.2f} dBm, RX-FP {rx - fp:.2f} dB")

    # T6 analysis on the synthetic session (1 lost packet out of 12).
    rep = analyse_session(parsed, other, DEFAULT_MAX_LOSS_PCT, DEFAULT_MAX_CIR_READ_P99_US, DEFAULT_MAX_ALIGN_MEDIAN)
    expect("T6 loss analysis", rep.metrics.get("pkt_expected") == 12 and rep.metrics.get("pkt_received") == 11,
           f"expected {rep.metrics.get('pkt_expected')}, received {rep.metrics.get('pkt_received')}, "
           f"loss {rep.metrics.get('loss_pct')}%")
    expect("T7 CIR/peak alignment", rep.metrics.get("cir_peak_align_median") == 0,
           f"median offset {rep.metrics.get('cir_peak_align_median')} samples")
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true", help="host-only checks")
    mode.add_argument("--session", help="path to <session>/messages/session_messages.csv")
    parser.add_argument("--max-loss-pct", type=float, default=DEFAULT_MAX_LOSS_PCT)
    parser.add_argument("--max-cir-read-p99-us", type=float, default=DEFAULT_MAX_CIR_READ_P99_US)
    parser.add_argument("--max-align-median", type=float, default=DEFAULT_MAX_ALIGN_MEDIAN)
    parser.add_argument("--out-json", help="write metrics and checks to this JSON file")
    args = parser.parse_args()

    if args.self_test:
        return 0 if self_test() else 1

    diag, other = load_session_rows(args.session)
    rep = analyse_session(diag, other, args.max_loss_pct, args.max_cir_read_p99_us, args.max_align_median)
    print_report(rep)
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as handle:
            json.dump({"metrics": rep.metrics, "checks": rep.checks, "passed": rep.passed}, handle,
                      indent=2, ensure_ascii=False, default=str)
    return 0 if rep.passed else 1


if __name__ == "__main__":
    sys.exit(main())
