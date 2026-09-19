#!/usr/bin/env python3
"""Dieu phoi 28 lan ghi cua buoi thu K2, chay ngay tren xe.

Module nay khong doc cong serial. No nhan tung mau sensor_fusion_result da giai
ma (dung dang cua test_position.decode) qua feed(), va lo ba viec ma
VAN_HANH_BUOI_THU_1.md dang bat lam bang tay:

  1. Bam theo ke hoach 28 dong trong SO_PHIEN_K2_PHIEN.csv: dong hien tai la gi,
     con bao nhieu giay, ghi xong thi sang dong ke tiep.
  2. Canh bao TAI CHO khi mot moc im qua 10 giay hoac khong du 4 moc (§5).
     Hien nay loi nay chi lo ra sau buoi khi chay trust_check_session.py - luc
     do lan ghi da hong.
  3. Ghi CSV rieng cho tung lan ghi, va tom tat khi dung: trung vi x/y tren cua
     so da cat 10 giay moi dau (Phu luc A), do phu cua tung moc.

KHONG ghi de len SO_PHIEN_K2_PHIEN.csv goc. So phien duoc chep ra mot ban rieng
"SO_PHIEN_K2_PHIEN_orin.csv" trong thu muc ket qua; cot file_fusion de trong vi
do la ten file cua Studio, khong phai cua Orin.
"""
from __future__ import annotations

import csv
import json
import statistics
import time
from pathlib import Path

MAX_ANCHOR_GAP_S = 10.0     # §5: "khong mat moc qua 10 giay"
TRIM_S = 10.0               # Phu luc A: bo 10 giay moi dau
MIN_KEEP_S = 60.0           # Phu luc A: con it nhat 60 giay
DEFAULT_REC_S = 90.0        # §3.1: moi lan ghi 90 giay
EXPECTED_ANCHORS = 4

REC_COLUMNS = [
    "host_time", "timestamp_ms", "ukf_step",
    "ukf_x_m", "ukf_y_m", "ukf_yaw_deg",
    "tril_x_m", "tril_y_m", "zone_id",
    "anchor_mask", "n_anchors",
    "ranging_error_count", "prefilter_reject_count",
    "cov_xx_m2", "cov_yy_m2", "anchors_json",
]


def _row_label(row: dict) -> str:
    bits = [row.get("thi_nghiem", ""), row.get("diem_hoac_luot", "")]
    if row.get("vat_can"):
        bits.append(f"vat can {row['vat_can']}")
    if row.get("moc_bi_che"):
        bits.append(f"che {row['moc_bi_che']}")
    return " · ".join(b for b in bits if b)


class Recording:
    """Mot lan ghi: mo file CSV, gom mau, tom tat khi dung."""

    def __init__(self, row: dict, index: int, out_dir: Path, seconds: float):
        self.row = row
        self.index = index
        self.seconds = seconds
        self.started = time.time()
        self.stopped: float | None = None
        self.samples = 0
        self.issues: list[str] = []
        # Chi giu mau co moc (buoc UPDATE) de tinh trung vi - buoc PREDICT khong
        # mang thong tin do moi.
        self._fix: list[tuple[float, float, float]] = []
        self._anchor_hits: dict[int, int] = {}
        self._anchor_used: dict[int, int] = {}   # weight > 0 = thuc su vao ban fix
        self._update_samples = 0

        stt = row.get("stt", str(index + 1))
        tn = (row.get("thi_nghiem") or "x").replace("/", "-")
        stamp = time.strftime("%H%M%S", time.localtime(self.started))
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / f"rec_{int(stt):02d}_{tn}_{stamp}.csv"
        self._fh = self.path.open("w", newline="")
        self._w = csv.DictWriter(self._fh, fieldnames=REC_COLUMNS)
        self._w.writeheader()

    @property
    def elapsed(self) -> float:
        return (self.stopped or time.time()) - self.started

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    def feed(self, s: dict) -> None:
        row = {
            "host_time": s.get("host_time", time.time()),
            "timestamp_ms": s.get("timestamp_ms", 0),
            "ukf_step": s.get("ukf_step", 0),
            "ukf_x_m": s.get("ukf_x_m"), "ukf_y_m": s.get("ukf_y_m"),
            "ukf_yaw_deg": s.get("ukf_yaw_deg"),
            "tril_x_m": s.get("tril_x_m"), "tril_y_m": s.get("tril_y_m"),
            "zone_id": s.get("zone_id"), "anchor_mask": s.get("anchor_mask"),
            "n_anchors": s.get("n_anchors"),
            "ranging_error_count": s.get("ranging_error_count"),
            "prefilter_reject_count": s.get("prefilter_reject_count"),
            "cov_xx_m2": s.get("cov_xx_m2"), "cov_yy_m2": s.get("cov_yy_m2"),
            "anchors_json": json.dumps(s.get("anchors", []), separators=(",", ":")),
        }
        self._w.writerow(row)
        self.samples += 1

        anchors = s.get("anchors") or []
        if anchors:
            self._update_samples += 1
            for a in anchors:
                self._anchor_hits[a["id"]] = self._anchor_hits.get(a["id"], 0) + 1
                if a.get("weight", 0) > 0:
                    self._anchor_used[a["id"]] = self._anchor_used.get(a["id"], 0) + 1
            self._fix.append((row["host_time"], s.get("ukf_x_m", 0.0), s.get("ukf_y_m", 0.0)))

    def note(self, text: str) -> None:
        if text not in self.issues:
            self.issues.append(text)

    def close(self) -> dict:
        self.stopped = time.time()
        self._fh.close()

        keep = [p for p in self._fix
                if self.started + TRIM_S <= p[0] <= self.stopped - TRIM_S]
        kept_s = (keep[-1][0] - keep[0][0]) if len(keep) > 1 else 0.0
        summary = {
            "file": self.path.name,
            "stt": self.row.get("stt"),
            "thi_nghiem": self.row.get("thi_nghiem"),
            "diem": self.row.get("diem_hoac_luot"),
            "bat_dau": time.strftime("%H:%M:%S", time.localtime(self.started)),
            "ket_thuc": time.strftime("%H:%M:%S", time.localtime(self.stopped)),
            "giay": round(self.elapsed, 1),
            "so_mau": self.samples,
            "so_mau_co_moc": self._update_samples,
            "hz": round(self.samples / self.elapsed, 1) if self.elapsed > 0 else 0.0,
            "cua_so_con_lai_giay": round(kept_s, 1),
            "du_60_giay": kept_s >= MIN_KEEP_S,
            "van_de": list(self.issues),
        }
        if keep:
            summary["trung_vi_x_m"] = round(statistics.median(p[1] for p in keep), 3)
            summary["trung_vi_y_m"] = round(statistics.median(p[2] for p in keep), 3)
        if self._update_samples:
            summary["do_phu_moc_pct"] = {
                str(k): round(100.0 * v / self._update_samples, 1)
                for k, v in sorted(self._anchor_hits.items())
            }
            # Nhan duoc khong co nghia la duoc dung: tien loc co the cho weight = 0,
            # luc do moc van "song" nhung khong vao ban fix. Luat 10 giay khong
            # bat duoc ca nay, nen tinh rieng.
            summary["do_dung_moc_pct"] = {
                str(k): round(100.0 * self._anchor_used.get(k, 0) / self._update_samples, 1)
                for k in sorted(self._anchor_hits)
            }
            bo_qua = [f"A{k}" for k, v in summary["do_dung_moc_pct"].items() if v < 50.0]
            if bo_qua:
                self.note("moc bi tien loc bo qua phan lon thoi gian: "
                          + ", ".join(bo_qua))
                summary["van_de"] = list(self.issues)
        return summary


class K2Session:
    """Trang thai ca buoi thu: ke hoach, lan ghi dang chay, canh bao moc."""

    def __init__(self, plan_csv: Path, out_dir: Path,
                 seconds: float = DEFAULT_REC_S, auto_stop: bool = True):
        self.plan_path = Path(plan_csv)
        self.out_dir = Path(out_dir)
        self.seconds = seconds
        self.auto_stop = auto_stop

        with self.plan_path.open(encoding="utf-8-sig") as f:
            self.rows = list(csv.DictReader(f))
            self.columns = list(self.rows[0].keys()) if self.rows else []
        self.index = 0
        self.rec: Recording | None = None
        self.done: list[dict] = []

        self.last_seen: dict[int, float] = {}
        self.alarm: dict[int, bool] = {}
        self.no_data_since = time.time()
        self.sample_count = 0

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.out_dir / "SO_PHIEN_K2_PHIEN_orin.csv"

    # ---------------------------------------------------------------- ke hoach
    @property
    def row(self) -> dict:
        return self.rows[self.index] if 0 <= self.index < len(self.rows) else {}

    def goto(self, index: int) -> None:
        if self.rec is not None:
            raise RuntimeError("dang ghi, dung truoc khi doi dong")
        self.index = max(0, min(index, len(self.rows) - 1))

    # ------------------------------------------------------------------- ghi
    def start(self, seconds: float | None = None) -> dict:
        if self.rec is not None:
            raise RuntimeError("dang ghi roi")
        if not self.row:
            raise RuntimeError("het dong ke hoach")
        self.rec = Recording(self.row, self.index, self.out_dir,
                             seconds or self.seconds)
        return self.state()

    def stop(self, k3: str = "", note: str = "") -> dict:
        if self.rec is None:
            raise RuntimeError("chua ghi")
        summary = self.rec.close()
        summary["k3_ket_qua"] = k3
        summary["ghi_chu"] = note
        self.done.append(summary)
        self._write_log(self.rec.index, summary)
        self.rec = None
        if self.index < len(self.rows) - 1:
            self.index += 1
        return summary

    def _write_log(self, row_index: int, summary: dict) -> None:
        """Ghi vao BAN SAO so phien, khong dung file goc."""
        rows = [dict(r) for r in self.rows]
        for s in self.done:
            for r in rows:
                if r.get("stt") == s.get("stt"):
                    r["gio_bat_dau"] = s["bat_dau"]
                    r["gio_ket_thuc"] = s["ket_thuc"]
                    r["k3_ket_qua"] = s.get("k3_ket_qua", "")
                    extra = f"[orin] {s['file']}"
                    if s.get("van_de"):
                        extra += " | " + "; ".join(s["van_de"])
                    if s.get("ghi_chu"):
                        extra += " | " + s["ghi_chu"]
                    r["ghi_chu"] = extra
        with self.log_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=self.columns)
            w.writeheader()
            w.writerows(rows)

    # ------------------------------------------------------------------ mau
    def feed(self, s: dict) -> list[dict]:
        """Nhan mot mau; tra ve danh sach su kien can bao len trang."""
        now = s.get("host_time", time.time())
        self.sample_count += 1
        self.no_data_since = now
        events: list[dict] = []

        for a in s.get("anchors") or []:
            aid = a["id"]
            if self.alarm.get(aid):
                self.alarm[aid] = False
                events.append({"muc": "ok", "text": f"moc A{aid} da co lai"})
            self.last_seen[aid] = now

        for aid, seen in self.last_seen.items():
            gap = now - seen
            if gap > MAX_ANCHOR_GAP_S and not self.alarm.get(aid):
                self.alarm[aid] = True
                msg = f"moc A{aid} im {gap:.0f} giay (> {MAX_ANCHOR_GAP_S:.0f})"
                events.append({"muc": "loi", "text": msg})
                if self.rec is not None:
                    self.rec.note(msg)

        if self.rec is not None:
            self.rec.feed(s)
            if self.auto_stop and self.rec.remaining <= 0.0:
                summary = self.stop()
                events.append({"muc": "xong", "text":
                               f"du {summary['giay']:.0f} giay, da dung", "tom_tat": summary})
        return events

    def tick(self) -> list[dict]:
        """Goi dinh ky khi khong co mau - bat truong hop im hoan toan."""
        events: list[dict] = []
        gap = time.time() - self.no_data_since
        if gap > MAX_ANCHOR_GAP_S and not self.alarm.get(-1):
            self.alarm[-1] = True
            msg = f"khong nhan duoc mau nao {gap:.0f} giay"
            events.append({"muc": "loi", "text": msg})
            if self.rec is not None:
                self.rec.note(msg)
        elif gap <= MAX_ANCHOR_GAP_S and self.alarm.get(-1):
            self.alarm[-1] = False
            events.append({"muc": "ok", "text": "da co mau tro lai"})
        return events

    # ---------------------------------------------------------------- trang thai
    def state(self) -> dict:
        now = time.time()
        anchors = []
        for aid in sorted(set(list(self.last_seen) + list(range(1, EXPECTED_ANCHORS + 1)))):
            if aid < 1:
                continue
            seen = self.last_seen.get(aid)
            anchors.append({
                "id": aid,
                # max(0, ...): dong ho cua mau va cua may co the lech vai ms,
                # hien so am tren trang thi kho doc.
                "giay_truoc": round(max(0.0, now - seen), 1) if seen else None,
                "canh_bao": bool(self.alarm.get(aid)) or seen is None,
            })
        return {
            "dong": self.index + 1,
            "tong_dong": len(self.rows),
            "ke_hoach": _row_label(self.row),
            "chi_tiet": {k: self.row.get(k, "") for k in
                         ("stt", "thi_nghiem", "diem_hoac_luot", "huong_deg",
                          "vat_can", "tam_vat_can_xy_m", "moc_bi_che",
                          "ke_hoach_ghi_chu")},
            "dang_ghi": self.rec is not None,
            "con_lai_giay": round(self.rec.remaining, 1) if self.rec else None,
            "da_ghi_giay": round(self.rec.elapsed, 1) if self.rec else None,
            "so_mau_lan_nay": self.rec.samples if self.rec else 0,
            "da_xong": len(self.done),
            "moc": anchors,
            "so_phien_orin": str(self.log_path),
        }
