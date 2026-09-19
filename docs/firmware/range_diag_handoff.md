# Ranging Diagnostics Stream (`range_diag_t`)

Research data-collection stream that sends raw DW1000 RX diagnostics and an optional CIR window from the Tag to the
host, one packet per Tag–Anchor link per ranging cycle. Disabled by default.

| | |
|---|---|
| Status | HOST_TESTED · BUILD_VERIFIED · **HARDWARE_UNVERIFIED** (2026-09-14) |
| Scope | Tag firmware, protocol, host analysis script. No Anchor, air-frame, fusion or Studio change |
| Operator guide (Vietnamese) | `BAN_GIAO_K1_RANGE_DIAG.docx` — build, real-time verification plan H0–H7, merge checklist |

---

## 1. Why

The trust model needs stronger LOS/NLOS features than `fp_amp_norm`/`fp_snr`. The Tag already read the RESP RX
diagnostics but discarded them (`event_tag_ingest_resp_payload`: `(void) quality`). CIR_PWR, RXPACC_NOSAT and the
accumulator (CIR) were never read. With them the host can compute FP_POWER, RX_POWER and RX−FP (DW1000 User Manual
§4.7) and CIR-shape features.

## 2. Data flow (flags on)

```
DW1000 RESP RX (Tag) ─ uwb_rx_cb [UwbRanging task, SPI mutex held]
   capture_rx_quality()          + CIR_PWR (0x12:06), RXPACC_NOSAT (0x27:2C)
   capture_cir_window()          only if armed and payload prefix {RESP, seq, target anchor} matches;
                                 reads ACC_MEM before dwt_rxenable(), drops the dummy octet, times the read (DWT)
sys_ranging
   tag_cycle_diag_begin()        per transaction: clear per-link quality, arm CIR for peer_ids[cycle_id % N]
   event_tag_ingest_resp_payload store Tag-side quality per link
   tag_cycle_diag_publish()      on cycle completion: snapshot under osKernelLock (links + RESULT q8 + CIR by rx_ts)
SensorFusion task (freertos.c)
   app_tag_range_diag_stream()   copy newest snapshot, send <= SYS_RANGING_DIAG_MAX_PKTS_PER_LOOP packets per loop
   network_send_range_diag()     gates: ranging enabled, BLE host active; own pacing (not the 20 ms fusion limiter)
packet_t{range_diag=90} ─ HDLC ─ USART2 1 Mbps ─ nRF52832 (raw forward) ─ BLE ─ dongle ─ RTLS Studio
Studio SessionMessageRecorder ─ <session>/messages/session_messages.csv  (message=range_diag, JSON, CIR base64)
```

Design decisions:
- **RESP at the Tag**: its RX timestamp (t4) enters DS-TWR, so the CIR describes the measured channel; no extra airtime.
- **Send from SensorFusion**: the host UART write is blocking and not mutex-protected; the fusion stream already owns it.
  Sending from `UwbRanging` would block ranging between 3 ms TDMA slots and collide with fusion packets.
- **Payload-prefix match + `rx_ts` check**: capturing "the k-th frame" would mislabel the anchor after a lost RESP.
- **One CIR per cycle, rotating anchor**: bounds SPI time per cycle and link bandwidth.
- **Newest cycle wins**: links of an older cycle not yet sent are counted in `link_drop_count`.

Not captured: CIR for RESPs taken by the polled fallback path (`event_tag_poll_ready_resp`); cycles aborted by the
cycle watchdog (visible as `cycle_id` gaps); Anchor-side raw registers (RESULT stays 15 B).

## 3. Flags (`firmware/uwb/sys/positioning_config.h`, section *RANGING DIAGNOSTICS STREAM*)

| Flag | Default | Meaning |
|---|---|---|
| `SYS_RANGING_DIAG_STREAM_ENABLE` | 0 | Per-link `range_diag_t` stream + two extra register reads per RX |
| `SYS_RANGING_DIAG_CIR_ENABLE` | 0 | Adds the CIR window (requires the stream flag, `#error` otherwise) |
| `SYS_RANGING_DIAG_CIR_PRE_SAMPLES` | 8 | Samples before the integer first-path index |
| `SYS_RANGING_DIAG_CIR_SAMPLES` | 28 | Window length (≤ 32, ×4 B ≤ `cir` max_size 128). 28 keeps CIR packets < 230 B |
| `SYS_RANGING_DIAG_MAX_PKTS_PER_LOOP` | 2 | Packets per SensorFusion loop iteration |

Build configurations (flags via `DEFS`, no file edit):

| Cfg | Flags | Command (from repo root) |
|---|---|---|
| A | none | `make -C firmware/uwb -j8 GCC_PATH=<gcc bin>` |
| B | stream | `... BUILD_DIR=build_diag_scalar DEFS="-DDEBUG -DUSE_HAL_DRIVER -DSTM32F411xE -DSYS_RANGING_DIAG_STREAM_ENABLE=1"` |
| C | stream + CIR | `... BUILD_DIR=build_diag DEFS="-DDEBUG -DUSE_HAL_DRIVER -DSTM32F411xE -DSYS_RANGING_DIAG_STREAM_ENABLE=1 -DSYS_RANGING_DIAG_CIR_ENABLE=1"` |

Only the Tag needs B/C. Anchors can keep their current firmware (air frames unchanged). The Studio PC needs the
regenerated `software/common/protocol_pb2.py`.

## 4. Message

`protocol.proto` → `range_diag_t`, `packet_t.range_diag = 90`, `PROTOCOL_REV = 126`, options `range_diag_t.cir max_size:128`.

| Tags | Fields |
|---|---|
| 1–7 | `cycle_id`, `seq` (air sequence 0–255), `timestamp_ms` (Tag HAL tick at cycle completion), `anchor_id`, `distance_mm` (sint32, 3D RESULT distance), `result_valid`, `resp_valid` |
| 8–10 | Anchor-side RESULT summaries `a_fp_amp_norm_q8`, `a_fp_snr_q8`, `a_fp_confidence_q8` |
| 11–21 | Tag-side RESP registers `fp_amp1..3`, `std_noise`, `rxpacc`, `rxpacc_nosat`, `cir_pwr`, `fp_index_q6`, `peak_path_index`, `peak_path_amp`, `lde_threshold` |
| 22–24 | `cir_start_index`, `cir_read_us` (0 = no capture), `cir` (int16 LE real/imag pairs) |
| 25–28 | `link_count`, `pkt_seq` (Tag send attempts), `link_drop_count`, `uart_tx_fail_count` (cumulative) |

## 5. Host tools

`software/simulation/trust_range_diag_check.py`

- `--self-test`: packet sizes vs nanopb/HDLC limits, Studio CSV round trip, CIR decoding, FP/RX power, loss analysis.
- `--session <session_messages.csv> [--out-json f]`: end-to-end loss (`pkt_seq` gaps), Tag-side drops and UART
  failures, cycle completeness and effective period, `cir_read_us` percentiles, CIR argmax vs `peak_path_index`
  alignment (validates the dummy-octet handling), throughput, and — if present in the CSV — `ranging_period_ms`
  from `sys_config_resp`/`ranging_status_resp` and task stacks from `rtos_task_stats_resp`. Exit 0 = all checks pass.

## 6. Verified (branch `danh/develop`, GNU Arm 14.3.rel1, grpcio-tools 1.74.0 / libprotoc 31.1, nanopb `6eeabfa`)

| ID | Check | Result |
|---|---|---|
| G0 | Regenerate unchanged `.proto` | `protocol.pb.c/.h`, `protocol_pb2.py` identical to HEAD (LF-normalized) |
| G1 | nanopb sizes | `range_diag_t` 298 B (≤ 310 B largest existing param); `packet_t` stays 334 B |
| G2 | `--self-test` | PASS: worst packet 316 B; typical scalar 90 B, CIR 214 B, CIR after ~3 h 217 B (< 230 B) |
| B0 | HEAD `a1faa5f` | FLASH 171 220 B, RAM 77 472 B, 56 existing warnings |
| A | flags off | FLASH +88 B, RAM +16 B, no new warning |
| B | stream | FLASH 172 628 B (+1 408), RAM 79 040 B (+1 568), no new warning |
| C | stream + CIR | FLASH 172 692 B (+1 472), RAM 79 336 B (+1 864), no new warning |
| S | `-fstack-usage` | `app_tag_range_diag_stream` 64 B, `network_send_range_diag` 288 B (fusion sender 296 B), `tag_cycle_diag_publish` 48 B, `uwb_rx_cb` 64 B |

## 7. Still to verify on hardware

H0 flags-off regression · H1 real CIR read time and RESP loss · H2 BLE throughput · H3 Studio load · H4 unchanged nRF
firmware forwarding tag 90 · H5 nanopb sizes after merge · H6 runtime stack/heap · H7 `ranging_period_ms` stored on the
board. Procedures and pass criteria: `BAN_GIAO_K1_RANGE_DIAG.docx` §6.

## 8. Merge notes

- Never hand-merge `protocol.pb.c`, `protocol.pb.h`, `protocol_pb2.py`: resolve `.proto/.options`, then `make -C protocol`.
- Check tag 90 / name `range_diag` are unique after merge; if another branch bumped `PROTOCOL_REV`, take max + 1.
- `positioning_config.h`: the flag section sits right before *ERROR HANDLING*, away from the anchor layout.
- `app_tag.c` additions are at the end of the file; `sys_ranging.c`/`bsp_uwb.c` additions are inside `#if SYS_RANGING_DIAG_*`.
- After merge: build A, B, C with no new warnings, run `--self-test`, update `protocol_pb2.py` on the Studio PC.
