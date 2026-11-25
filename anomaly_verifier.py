"""Multi-Channel 2D Kinematics & Burst-Rhythm Anomaly Verifier.

Biomechanical 2D trajectory kinematics and multi-channel symbol-arrival burst rhythm
anomaly detection for behavioral authentication. Robust against user fatigue while
detecting impostors, foreign strangers, and cadence-mimic adversaries.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import synth_traces
from synth_traces import BurstEvent, SessionStream, TrajectoryTick


@dataclass
class VerificationResult:
    is_genuine: bool
    anomaly_score: float
    threshold: float
    profile_id: str
    session_id: str
    actual_cohort: str
    predicted_cohort: str
    penalties: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_genuine": self.is_genuine,
            "anomaly_score": round(self.anomaly_score, 4),
            "threshold": round(self.threshold, 4),
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "actual_cohort": self.actual_cohort,
            "predicted_cohort": self.predicted_cohort,
            "penalties": {k: round(v, 4) for k, v in self.penalties.items()},
        }


@dataclass
class ProfileModel:
    profile_id: str
    feature_means: Dict[str, float]
    feature_stds: Dict[str, float]
    threshold: float = 14.0


class KinematicBurstAnomalyVerifier:
    """Multi-channel 2D kinematics and burst-rhythm anomaly verifier."""

    def __init__(self):
        self.models: Dict[str, ProfileModel] = {}

    @staticmethod
    def extract_features(session: SessionStream) -> Dict[str, float]:
        """Extracts biomechanical kinematic invariants and rhythm-burst dynamics."""
        ticks = session.ticks
        bursts = session.bursts

        # 1. Hardware polling rate (Hz)
        dt_ticks: List[float] = []
        for i in range(1, len(ticks)):
            dt = ticks[i].ts_ms - ticks[i - 1].ts_ms
            if 2.0 <= dt <= 100.0:
                dt_ticks.append(dt)
        tick_hz = 1000.0 / statistics.median(dt_ticks) if dt_ticks else 60.0

        # 2. Segment into strokes (DOWN -> DRAG -> UP) and dwells
        dwell_time = 0.0
        strokes: List[List[TrajectoryTick]] = []
        cur_stroke: List[TrajectoryTick] = []

        for i, t in enumerate(ticks):
            if t.act_kind == "DWELL":
                if cur_stroke:
                    if len(cur_stroke) >= 4:
                        strokes.append(cur_stroke)
                    cur_stroke = []
                if i > 0:
                    dwell_time += max(0.0, t.ts_ms - ticks[i - 1].ts_ms)
            else:
                cur_stroke.append(t)
        if len(cur_stroke) >= 4:
            strokes.append(cur_stroke)

        total_time = (ticks[-1].ts_ms - ticks[0].ts_ms) if len(ticks) > 1 else 1.0
        dwell_fraction = dwell_time / max(1.0, total_time)

        peak_ratios: List[float] = []
        front_loads: List[float] = []
        tortuosities: List[float] = []
        lateral_arcs: List[float] = []
        overshoots: List[float] = []
        tremor_list: List[float] = []

        for stroke in strokes:
            x0, y0 = stroke[0].x_norm, stroke[0].y_norm
            x1, y1 = stroke[-1].x_norm, stroke[-1].y_norm
            dx, dy = x1 - x0, y1 - y0
            chord = math.hypot(dx, dy)
            if chord < 0.01:
                continue
            ux, uy = dx / chord, dy / chord
            nx, ny = -uy, ux  # Normal vector perpendicular to chord

            path_len = 0.0
            speeds: List[float] = []
            durs: List[float] = []
            t0 = stroke[0].ts_ms
            total_dur = stroke[-1].ts_ms - t0
            if total_dur < 10.0:
                continue

            dist_first_half = 0.0
            lat_disps: List[float] = []

            for k in range(1, len(stroke)):
                ds = math.hypot(stroke[k].x_norm - stroke[k - 1].x_norm, stroke[k].y_norm - stroke[k - 1].y_norm)
                dt = max(1.0, stroke[k].ts_ms - stroke[k - 1].ts_ms)
                path_len += ds
                speeds.append(ds / (dt / 1000.0))
                durs.append(stroke[k].ts_ms - t0)

                if (stroke[k].ts_ms - t0) <= (total_dur * 0.5):
                    dist_first_half += ds

                # Perpendicular displacement relative to chord
                px, py = stroke[k].x_norm - x0, stroke[k].y_norm - y0
                lat = px * nx + py * ny
                lat_disps.append(lat / chord)

            if not speeds:
                continue

            max_speed = max(speeds)
            max_idx = speeds.index(max_speed)
            peak_ratios.append(durs[max_idx] / total_dur)
            front_loads.append(dist_first_half / max(1e-5, path_len))
            tortuosities.append(path_len / chord - 1.0)
            lateral_arcs.append(statistics.mean(lat_disps))

            proj_overshoot = max(
                0.0,
                max((stroke[k].x_norm - x0) * ux + (stroke[k].y_norm - y0) * uy for k in range(len(stroke))) / chord - 1.0,
            )
            overshoots.append(proj_overshoot)

            if len(lat_disps) >= 3:
                tremor_list.append(statistics.stdev(lat_disps))

        # 3. Multi-channel symbol arrival & burst-rhythm features
        c_counts = {"DIRECT_KEY": 0, "VIRTUAL_GRID": 0, "BATCH_INSERT": 0}
        c_symbols = {"DIRECT_KEY": 0, "VIRTUAL_GRID": 0, "BATCH_INSERT": 0}
        ieis: List[float] = []
        log_ieis: List[float] = []
        edit_flags: List[bool] = []

        for i, b in enumerate(bursts):
            # Normalize channel alias BATCH_CHUNK -> BATCH_INSERT
            c = "BATCH_INSERT" if b.channel in ("BATCH_INSERT", "BATCH_CHUNK") else b.channel
            if c in c_counts:
                c_counts[c] += 1
                c_symbols[c] += b.symbol_count
            edit_flags.append(b.is_edit)
            if i > 0:
                iei = max(5.0, b.ts_ms - bursts[i - 1].ts_ms)
                ieis.append(iei)
                log_ieis.append(math.log(iei))

        total_bursts = len(bursts) or 1
        pref_channel = max(c_counts, key=c_counts.get)
        channel_code = 0.0 if pref_channel == "DIRECT_KEY" else (1.0 if pref_channel == "VIRTUAL_GRID" else 2.0)

        log_iei_mean = statistics.mean(log_ieis) if log_ieis else 5.0
        log_iei_std = statistics.stdev(log_ieis) if len(log_ieis) > 1 else 0.5

        # Rhythmic cadence lag-1 autocorrelation
        if len(log_ieis) >= 4:
            mean_y = statistics.mean(log_ieis)
            num = sum((log_ieis[i] - mean_y) * (log_ieis[i - 1] - mean_y) for i in range(1, len(log_ieis)))
            den = sum((y - mean_y) ** 2 for y in log_ieis)
            autocorr_lag1 = num / max(1e-6, den)
        else:
            autocorr_lag1 = 0.0

        edit_rate = sum(1 for e in edit_flags if e) / total_bursts
        pre_edit_ratios: List[float] = []
        post_edit_ratios: List[float] = []
        med_iei = statistics.median(ieis) if ieis else 200.0

        for i in range(len(bursts)):
            if bursts[i].is_edit:
                if i > 0:
                    pre_edit_ratios.append((bursts[i].ts_ms - bursts[i - 1].ts_ms) / max(1.0, med_iei))
                if i + 1 < len(bursts):
                    post_edit_ratios.append((bursts[i + 1].ts_ms - bursts[i].ts_ms) / max(1.0, med_iei))

        pre_edit_hes = statistics.mean(pre_edit_ratios) if pre_edit_ratios else 1.0
        post_edit_rec = statistics.mean(post_edit_ratios) if post_edit_ratios else 1.0
        primary_chunk_size = c_symbols[pref_channel] / max(1, c_counts[pref_channel])

        return {
            "tick_hz": tick_hz,
            "dwell_fraction": dwell_fraction,
            "peak_ratio": statistics.mean(peak_ratios) if peak_ratios else 0.4,
            "front_load": statistics.mean(front_loads) if front_loads else 0.5,
            "tortuosity": statistics.mean(tortuosities) if tortuosities else 0.1,
            "lateral_arc": statistics.mean(lateral_arcs) if lateral_arcs else 0.0,
            "overshoot": statistics.mean(overshoots) if overshoots else 0.05,
            "tremor": statistics.mean(tremor_list) if tremor_list else 0.02,
            "channel_code": channel_code,
            "log_iei_mean": log_iei_mean,
            "log_iei_std": log_iei_std,
            "autocorr_lag1": autocorr_lag1,
            "edit_rate": edit_rate,
            "num_edits": sum(1 for e in edit_flags if e),
            "pre_edit_hes": pre_edit_hes,
            "post_edit_rec": post_edit_rec,
            "burst_chunk_size": primary_chunk_size,
        }

    def enroll(self, profile_id: str, enroll_sessions: List[SessionStream]) -> ProfileModel:
        """Enrolls user baseline statistics from genuine normal interaction streams."""
        all_feats = [self.extract_features(s) for s in enroll_sessions]
        keys = all_feats[0].keys()
        means = {k: statistics.mean(f[k] for f in all_feats) for k in keys}
        stds = {}
        for k in keys:
            v = statistics.stdev(f[k] for f in all_feats) if len(all_feats) > 1 else 0.1
            min_std = 0.02
            if k == "lateral_arc":
                min_std = 0.008
            elif k == "autocorr_lag1":
                min_std = 0.05
            elif k == "tick_hz":
                min_std = 5.0
            stds[k] = max(min_std, v)

        model = ProfileModel(profile_id=profile_id, feature_means=means, feature_stds=stds)
        self.models[profile_id] = model
        return model

    def compute_anomaly_score(self, profile_id: str, session: SessionStream) -> Tuple[float, Dict[str, float]]:
        """Computes asymmetric feature penalties and composite anomaly score."""
        if profile_id not in self.models:
            raise KeyError(f"Profile '{profile_id}' has not been enrolled.")

        model = self.models[profile_id]
        f = self.extract_features(session)
        mu = model.feature_means
        std = model.feature_stds

        penalties: Dict[str, float] = {}

        # 1. Hardware polling rate: strict hardware invariant
        hz_diff = abs(f["tick_hz"] - mu["tick_hz"])
        penalties["tick_hz"] = 5.0 * min(6.0, (hz_diff / std["tick_hz"]) ** 2)

        # 2. Preferred input channel (compare rounded discrete channel code)
        penalties["channel"] = 20.0 if round(f["channel_code"]) != round(mu["channel_code"]) else 0.0

        # 3. Lateral arc: invariant wrist/finger curvature direction
        # Fatigue increases magnitude with identical sign; mimic/stranger inverts curvature sign
        if abs(mu["lateral_arc"]) > 0.01:
            if (f["lateral_arc"] * mu["lateral_arc"]) < -0.0002:
                penalties["lateral_arc"] = 25.0  # Inverted wrist arc (hallmark of mimic)
            else:
                ratio = f["lateral_arc"] / mu["lateral_arc"]
                if ratio < 0.3 or ratio > 2.8:
                    penalties["lateral_arc"] = 4.0 * min(5.0, abs(ratio - 1.0))
                else:
                    penalties["lateral_arc"] = 0.0
        else:
            penalties["lateral_arc"] = 2.0 * abs(f["lateral_arc"] - mu["lateral_arc"]) / std["lateral_arc"]

        # 4. Cadence Autocorrelation lag-1: biological rhythm vs uniform random timing
        # If wrist arc matches, low autocorrelation is penalized mildly (due to finite session sampling variance)
        if mu["autocorr_lag1"] >= 0.25:
            if f["autocorr_lag1"] < -0.12:
                penalties["autocorr"] = 10.0
            elif f["autocorr_lag1"] < 0.0:
                penalties["autocorr"] = 4.0
            else:
                penalties["autocorr"] = 0.0
        else:
            if f["autocorr_lag1"] < -0.15:
                penalties["autocorr"] = 8.0
            else:
                penalties["autocorr"] = 0.0

        # 5. Peak velocity ratio: asymmetric bell curve invariant
        peak_diff = abs(f["peak_ratio"] - mu["peak_ratio"])
        penalties["peak_ratio"] = 2.0 * min(5.0, peak_diff / std["peak_ratio"])

        # 6. Front load bias
        penalties["front_load"] = 1.5 * min(4.0, abs(f["front_load"] - mu["front_load"]) / std["front_load"])

        # 7. Tortuosity: fatigue increases tortuosity naturally due to tremor and longer duration
        tort_ratio = f["tortuosity"] / max(1e-4, mu["tortuosity"])
        if tort_ratio > 4.5:
            penalties["tortuosity"] = 12.0
        elif tort_ratio < 0.15:
            penalties["tortuosity"] = 8.0
        else:
            penalties["tortuosity"] = 0.0

        # 8. Log IEI mean: allow fatigue slowdown (+1.25 in log space for high-variance profiles)
        iei_diff = f["log_iei_mean"] - mu["log_iei_mean"]
        if iei_diff < -0.45:  # Unnaturally fast
            penalties["iei_tempo"] = 8.0 * min(4.0, abs(iei_diff + 0.45) / std["log_iei_mean"])
        elif iei_diff > 1.25:  # Far too slow
            penalties["iei_tempo"] = 8.0 * min(4.0, (iei_diff - 1.25) / std["log_iei_mean"])
        else:
            penalties["iei_tempo"] = 0.0

        # 9. Edit hesitation: evaluate if session had sufficient edit events
        if f.get("num_edits", 0) >= 4 and mu["pre_edit_hes"] > 1.4:
            if f["pre_edit_hes"] < 1.10:
                penalties["edit_dynamics"] = 15.0
            else:
                penalties["edit_dynamics"] = 0.0
        else:
            penalties["edit_dynamics"] = 0.0

        total_score = sum(penalties.values())
        return total_score, penalties

    def calibrate(self, profile_id: str, cal_sessions: List[SessionStream], default_threshold: float = 14.0):
        """Calibrates threshold on disjoint calibration set to maximize discrimination margin."""
        scores = [self.compute_anomaly_score(profile_id, s)[0] for s in cal_sessions]
        sorted_scores = sorted(scores)
        mid_idx = len(sorted_scores) // 2
        cand_threshold = (sorted_scores[mid_idx - 1] + sorted_scores[mid_idx]) / 2.0
        self.models[profile_id].threshold = max(8.0, min(25.0, cand_threshold))

    def verify(self, profile_id: str, session: SessionStream) -> VerificationResult:
        """Verifies a test interaction session stream against enrolled profile."""
        score, penalties = self.compute_anomaly_score(profile_id, session)
        threshold = self.models[profile_id].threshold
        is_genuine = score < threshold

        # Cohort heuristic classification for auditability
        if is_genuine:
            f = self.extract_features(session)
            mu = self.models[profile_id].feature_means
            is_fatigued = (f["log_iei_mean"] - mu["log_iei_mean"]) > 0.20 or (f["tortuosity"] > 1.4 * mu["tortuosity"])
            predicted = "genuine_fatigued" if is_fatigued else "genuine_normal"
        else:
            if penalties.get("tick_hz", 0) > 4.0 or penalties.get("channel", 0) > 10.0:
                predicted = "impostor_stranger"
            else:
                predicted = "impostor_mimic"

        return VerificationResult(
            is_genuine=is_genuine,
            anomaly_score=score,
            threshold=threshold,
            profile_id=profile_id,
            session_id=session.session_id,
            actual_cohort=session.cohort,
            predicted_cohort=predicted,
            penalties=penalties,
        )

    def evaluate_suite(
        self, suite: Dict[str, Dict[str, List[SessionStream]]]
    ) -> Dict[str, Dict[str, float]]:
        """Evaluates verifier across disjoint benchmark splits."""
        scorecard: Dict[str, Dict[str, float]] = {}

        for profile_id, splits in suite.items():
            self.enroll(profile_id, splits["enroll"])
            self.calibrate(profile_id, splits["calibration"])

            normal_results = [self.verify(profile_id, s) for s in splits["holdout_normal"]]
            fatigued_results = [self.verify(profile_id, s) for s in splits["holdout_fatigued"]]
            stranger_results = [self.verify(profile_id, s) for s in splits["holdout_stranger"]]
            mimic_results = [self.verify(profile_id, s) for s in splits["holdout_mimic"]]

            normal_acc = sum(1 for r in normal_results if r.is_genuine) / len(normal_results)
            fatigued_acc = sum(1 for r in fatigued_results if r.is_genuine) / len(fatigued_results)
            stranger_rej = sum(1 for r in stranger_results if not r.is_genuine) / len(stranger_results)
            mimic_rej = sum(1 for r in mimic_results if not r.is_genuine) / len(mimic_results)

            scorecard[profile_id] = {
                "threshold": self.models[profile_id].threshold,
                "normal_accept_rate": normal_acc,
                "fatigued_accept_rate": fatigued_acc,
                "stranger_reject_rate": stranger_rej,
                "mimic_reject_rate": mimic_rej,
                "total_balanced_accuracy": (normal_acc + fatigued_acc + stranger_rej + mimic_rej) / 4.0,
            }

        return scorecard


def main():
    parser = argparse.ArgumentParser(description="Multi-Channel 2D Kinematics & Burst-Rhythm Anomaly Verifier")
    parser.add_argument("--benchmark", action="store_true", help="Run full synthetic benchmark suite")
    parser.add_argument("--seed", type=int, default=42, help="PRNG seed for benchmark generation")
    parser.add_argument("--verify-json", type=str, default="", help="Path to SessionStream JSON to verify")
    parser.add_argument("--profile", type=str, default="fast_desktop", help="Target profile for verification")
    args = parser.parse_args()

    gen = synth_traces.SyntheticTraceGenerator(args.seed)
    verifier = KinematicBurstAnomalyVerifier()

    if args.benchmark or not args.verify_json:
        print(f"Generating synthetic interaction benchmark suite (seed={args.seed})...")
        suite = gen.generate_benchmark_suite(
            n_enroll=25,
            n_calibration_genuine=20,
            n_calibration_impostor=20,
            n_holdout_per_cohort=60,
        )
        print("Evaluating Kinematic & Burst-Rhythm Anomaly Verifier across cohorts...\n")
        scorecard = verifier.evaluate_suite(suite)

        all_passed = True
        for pid, stats in scorecard.items():
            print(f"=== Profile: {pid} (calibrated threshold: {stats['threshold']:.2f}) ===")
            print(f"  Holdout Normal Accept Rate:   {stats['normal_accept_rate']*100:.1f}% (target >= 95%)")
            print(f"  Holdout Fatigued Accept Rate: {stats['fatigued_accept_rate']*100:.1f}% (target >= 95%)")
            print(f"  Holdout Stranger Reject Rate: {stats['stranger_reject_rate']*100:.1f}% (target = 100%)")
            print(f"  Holdout Mimic Reject Rate:    {stats['mimic_reject_rate']*100:.1f}% (target >= 95%)")
            print(f"  Composite Balanced Accuracy:  {stats['total_balanced_accuracy']*100:.2f}%\n")

            if stats["normal_accept_rate"] < 0.95 or stats["fatigued_accept_rate"] < 0.95:
                all_passed = False
            if stats["stranger_reject_rate"] < 0.99 or stats["mimic_reject_rate"] < 0.95:
                all_passed = False

        if all_passed:
            print("[PASS] Multi-Channel 2D Kinematics & Burst-Rhythm Verifier passed all benchmark criteria.")
            sys.exit(0)
        else:
            print("[FAIL] Verification criteria not met on some profiles.")
            sys.exit(1)
    else:
        # Single file verification
        suite = gen.generate_benchmark_suite(profiles=[args.profile], n_enroll=25, n_calibration_genuine=20, n_calibration_impostor=20, n_holdout_per_cohort=5)
        verifier.enroll(args.profile, suite[args.profile]["enroll"])
        verifier.calibrate(args.profile, suite[args.profile]["calibration"])

        with open(args.verify_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        session = SessionStream.from_dict(data)
        res = verifier.verify(args.profile, session)
        print(json.dumps(res.to_dict(), indent=2))
        sys.exit(0 if res.is_genuine else 1)


if __name__ == "__main__":
    main()
