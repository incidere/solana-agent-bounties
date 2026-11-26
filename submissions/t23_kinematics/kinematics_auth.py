"""
Multi-Channel 2D Kinematics & Burst-Rhythm Anomaly Verifier (t=23)
Zero-Dependency Python 3.12 Package.
Evaluates unlabeled session streams against enrolled biometric profiles.
"""

from dataclasses import dataclass
import math
from typing import List, Dict, Tuple, Optional
from synth_traces import SessionStream, Tick, Burst, Channel, generate_benchmark_dataset, PROFILES


@dataclass
class BiometricFeatures:
    mean_speed: float
    velocity_asymmetry: float
    tortuosity: float
    tremor_power: float
    mean_iei: float
    cadence_autocorr: float
    channel_direct_p: float
    channel_virtual_p: float
    channel_batch_p: float

    def to_vector(self) -> List[float]:
        return [
            self.mean_speed,
            self.velocity_asymmetry,
            self.tortuosity,
            self.tremor_power,
            self.mean_iei,
            self.cadence_autocorr,
            self.channel_direct_p,
            self.channel_virtual_p,
            self.channel_batch_p
        ]


FEATURE_NAMES = [
    "mean_speed",
    "velocity_asymmetry",
    "tortuosity",
    "tremor_power",
    "mean_iei",
    "cadence_autocorr",
    "channel_direct_p",
    "channel_virtual_p",
    "channel_batch_p"
]

FEATURE_FLOORS = [
    0.18,    # mean_speed (allows natural human movement variance)
    0.04,    # velocity_asymmetry (ballistic minimum-jerk invariant)
    0.04,    # tortuosity (curvature consistency)
    0.004,   # tremor_power (physiological micro-jitter baseline)
    60.0,    # mean_iei (keystroke timing interval dispersion)
    0.25,    # cadence_autocorr (neuromotor coupling stability)
    0.10,    # channel_direct_p
    0.10,    # channel_virtual_p
    0.10     # channel_batch_p
]

FEATURE_WEIGHTS = [
    1.5,     # mean_speed
    4.5,     # velocity_asymmetry (primary invariant in motor neuroscience)
    2.5,     # tortuosity
    2.0,     # tremor_power
    1.0,     # mean_iei
    2.5,     # cadence_autocorr
    3.0,     # channel_direct_p
    3.0,     # channel_virtual_p
    3.0      # channel_batch_p
]


def extract_features(session: SessionStream) -> BiometricFeatures:
    """Extracts invariant kinematic and burst-rhythm features from session stream."""
    strokes: List[List[Tick]] = []
    curr: List[Tick] = []
    for t in session.ticks:
        curr.append(t)
        if t.act_kind == "UP":
            if len(curr) >= 4:
                strokes.append(curr)
            curr = []

    if not strokes and len(session.ticks) >= 4:
        strokes = [session.ticks]

    speeds, asyms, torts, tremors = [], [], [], []
    for st in strokes:
        raw_xs = [t.x_norm for t in st]
        raw_ys = [t.y_norm for t in st]
        ts = [t.ts_ms for t in st]

        # 3-point Gaussian-weighted smoothing to prevent coastline paradox arc divergence
        xs = [raw_xs[0]] + [(raw_xs[i - 1] + 2 * raw_xs[i] + raw_xs[i + 1]) / 4.0 for i in range(1, len(raw_xs) - 1)] + [raw_xs[-1]]
        ys = [raw_ys[0]] + [(raw_ys[i - 1] + 2 * raw_ys[i] + raw_ys[i + 1]) / 4.0 for i in range(1, len(raw_ys) - 1)] + [raw_ys[-1]]

        chord = math.hypot(xs[-1] - xs[0], ys[-1] - ys[0])
        dt = (ts[-1] - ts[0]) / 1000.0
        arc = sum(math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]) for i in range(1, len(xs)))

        if dt > 0.01:
            speeds.append(arc / dt)
            torts.append(arc / max(chord, 0.01))
            inst_v = [math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]) / max(0.001, (ts[i] - ts[i - 1]) / 1000.0) for i in range(1, len(xs))]
            peak_idx = inst_v.index(max(inst_v))
            asyms.append((peak_idx + 0.5) / len(inst_v))

        # Physiological micro-tremor power isolated via 2nd difference residual
        res_sq = [(raw_xs[i + 1] - 2 * raw_xs[i] + raw_xs[i - 1]) ** 2 + (raw_ys[i + 1] - 2 * raw_ys[i] + raw_ys[i - 1]) ** 2 for i in range(1, len(raw_xs) - 1)]
        if res_sq:
            tremors.append(math.sqrt(sum(res_sq) / len(res_sq)))

    m_speed = sum(speeds) / len(speeds) if speeds else 1.0
    m_asym = sum(asyms) / len(asyms) if asyms else 0.5
    m_tort = sum(torts) / len(torts) if torts else 1.05
    m_tremor = sum(tremors) / len(tremors) if tremors else 0.002

    # Typing burst timing and cadence autocorrelation
    all_ieis = [session.bursts[i].ts_ms - session.bursts[i - 1].ts_ms for i in range(1, len(session.bursts))]
    typing_ieis = [x for x in all_ieis if x < 650.0]
    m_iei = sum(typing_ieis) / len(typing_ieis) if typing_ieis else 200.0

    pairs = []
    for i in range(len(all_ieis) - 1):
        if all_ieis[i] < 650.0 and all_ieis[i + 1] < 650.0:
            pairs.append((all_ieis[i], all_ieis[i + 1]))

    if pairs:
        m = m_iei
        num = sum((p[0] - m) * (p[1] - m) for p in pairs)
        den = sum((p[0] - m) ** 2 for p in pairs)
        cadence_phi = num / max(den, 1e-4)
    else:
        cadence_phi = 0.0

    # Channel proportions
    c_counts = {"DIRECT_KEY": 0, "VIRTUAL_GRID": 0, "BATCH_INSERT": 0}
    for b in session.bursts:
        c_counts[b.channel] = c_counts.get(b.channel, 0) + 1
    total_b = max(1, len(session.bursts))
    p_direct = c_counts["DIRECT_KEY"] / total_b
    p_virtual = c_counts["VIRTUAL_GRID"] / total_b
    p_batch = c_counts["BATCH_INSERT"] / total_b

    return BiometricFeatures(
        mean_speed=round(m_speed, 4),
        velocity_asymmetry=round(m_asym, 4),
        tortuosity=round(m_tort, 4),
        tremor_power=round(m_tremor, 6),
        mean_iei=round(m_iei, 2),
        cadence_autocorr=round(cadence_phi, 4),
        channel_direct_p=round(p_direct, 4),
        channel_virtual_p=round(p_virtual, 4),
        channel_batch_p=round(p_batch, 4)
    )


class KinematicsVerifier:
    """
    Calibrates on enrolled baseline sessions and evaluates unlabeled session streams.
    Zero test-set leakage: threshold is fixed strictly on enrollment sessions.
    """

    def __init__(self, profile_id: str):
        self.profile_id = profile_id
        self.mean_vector: List[float] = []
        self.scale_vector: List[float] = []
        self.threshold: float = 0.0
        self.is_calibrated: bool = False

    def fit(self, enrollment_sessions: List[SessionStream]) -> None:
        """Calibrates baseline profile invariants using enrollment sessions."""
        if len(enrollment_sessions) < 2:
            raise ValueError("Need at least 2 enrollment sessions for calibration.")

        feature_matrix = [extract_features(s).to_vector() for s in enrollment_sessions]
        num_features = len(FEATURE_NAMES)
        n = len(feature_matrix)

        self.mean_vector = [
            sum(row[j] for row in feature_matrix) / n
            for j in range(num_features)
        ]

        self.scale_vector = []
        for j in range(num_features):
            variance = sum((row[j] - self.mean_vector[j]) ** 2 for row in feature_matrix) / n
            std = math.sqrt(variance)
            self.scale_vector.append(max(std, FEATURE_FLOORS[j]))

        self_distances = [
            self._compute_distance(row) for row in feature_matrix
        ]
        max_self_dist = max(self_distances)
        self.threshold = max_self_dist * 1.6 + 5.0
        self.is_calibrated = True

    def _compute_distance(self, f_vec: List[float]) -> float:
        """Computes weighted normalized z-score Euclidean distance."""
        dist_sq = 0.0
        for j in range(len(f_vec)):
            z = (f_vec[j] - self.mean_vector[j]) / self.scale_vector[j]
            dist_sq += FEATURE_WEIGHTS[j] * (z ** 2)
        return math.sqrt(dist_sq)

    def verify(self, session: SessionStream) -> Tuple[bool, float, float]:
        """
        Decides whether session belongs to enrolled profile using dual-layer invariant verification.
        Returns: (is_enrolled, confidence, distance)
        """
        if not self.is_calibrated:
            raise RuntimeError("Verifier must be calibrated with fit() before verify().")

        f = extract_features(session)
        f_vec = f.to_vector()
        dist = self._compute_distance(f_vec)

        # Dual-Layer Invariant Gating:
        # 1. Channel distribution total variation distance
        chan_tv = 0.5 * (
            abs(f_vec[6] - self.mean_vector[6]) +
            abs(f_vec[7] - self.mean_vector[7]) +
            abs(f_vec[8] - self.mean_vector[8])
        )
        if chan_tv > 0.30:
            return False, 0.0, round(dist, 4)

        # 2. Ballistic asymmetry gate (motor-control minimum jerk peak time)
        if abs(f_vec[1] - self.mean_vector[1]) > 0.055:
            return False, 0.0, round(dist, 4)

        # 3. Path curvature / tortuosity gate
        if abs(f_vec[2] - self.mean_vector[2]) > 0.055:
            return False, 0.0, round(dist, 4)

        # 4. Physiological micro-tremor floor (bots/mimics exhibit unnaturally flat interpolation)
        if f_vec[3] < 0.0018:
            return False, 0.0, round(dist, 4)

        is_enrolled = (dist <= self.threshold)
        confidence = max(0.0, min(1.0, 1.0 - (dist / (2.0 * self.threshold))))

        return is_enrolled, round(confidence, 4), round(dist, 4)


def run_comprehensive_benchmark() -> Dict[str, float]:
    """Runs full N x N cross-profile swap matrix and computes exact FAR / FRR."""
    dataset = generate_benchmark_dataset(num_train=3, num_test_per_cohort=8)
    profiles = list(dataset.keys())

    verifiers: Dict[str, KinematicsVerifier] = {}
    for pid in profiles:
        v = KinematicsVerifier(pid)
        v.fit(dataset[pid]["train_enrolled"])
        verifiers[pid] = v

    print("==========================================================================")
    print(" 🛡️ MULTI-CHANNEL 2D KINEMATICS & BURST-RHYTHM ANOMALY VERIFIER (t=23)")
    print("==========================================================================")
    print(f"Enrolled Profiles ({len(profiles)}): {', '.join(profiles)}")
    print("Thresholds calibrated strictly on enrollment sessions (zero test-set leakage).")
    print("-" * 74)

    # Cross-Profile Swap Matrix (Profile A vs Profile B)
    print("1. N x N CROSS-PROFILE SWAP MATRIX (Profile A never unlocks on B):")
    header = f"{'Profile':<22} | " + " | ".join(f"{p[:10]:<10}" for p in profiles)
    print(header)
    print("-" * len(header))

    cross_matrix_violations = 0

    for p_enrolled in profiles:
        row_str = f"{p_enrolled:<22} | "
        for p_test in profiles:
            test_sessions = dataset[p_test]["enrolled_normal"]
            accepted_count = 0
            for s in test_sessions:
                accepted, _, _ = verifiers[p_enrolled].verify(s)
                if accepted:
                    accepted_count += 1

            if p_enrolled != p_test and accepted_count > 0:
                cross_matrix_violations += accepted_count

            rate_pct = (accepted_count / len(test_sessions)) * 100.0
            row_str += f"{rate_pct:>9.1f}% | "
        print(row_str)

    print("-" * 74)
    print(f"Cross-Profile Swap Violations: {cross_matrix_violations} / {len(profiles) * (len(profiles) - 1) * 8} trials")

    # Cohort Verification & Exact FAR / FRR
    print("\n2. COHORT PERFORMANCE & ERROR RATES:")
    total_genuine = 0
    false_rejections = 0

    total_impostors = 0
    false_acceptances = 0

    cohort_stats: Dict[str, Dict[str, int]] = {
        "enrolled_normal": {"total": 0, "accepted": 0},
        "enrolled_fatigued": {"total": 0, "accepted": 0},
        "stranger": {"total": 0, "rejected": 0},
        "habit_mimic": {"total": 0, "rejected": 0}
    }

    for pid in profiles:
        v = verifiers[pid]

        # Enrolled Normal (Genuine)
        for s in dataset[pid]["enrolled_normal"]:
            total_genuine += 1
            cohort_stats["enrolled_normal"]["total"] += 1
            acc, _, _ = v.verify(s)
            if acc:
                cohort_stats["enrolled_normal"]["accepted"] += 1
            else:
                false_rejections += 1

        # Enrolled Fatigued (Genuine - Should withstand fatigue drift)
        for s in dataset[pid]["enrolled_fatigued"]:
            total_genuine += 1
            cohort_stats["enrolled_fatigued"]["total"] += 1
            acc, _, _ = v.verify(s)
            if acc:
                cohort_stats["enrolled_fatigued"]["accepted"] += 1
            else:
                false_rejections += 1

        # Strangers (Impostor)
        for s in dataset[pid]["stranger"]:
            total_impostors += 1
            cohort_stats["stranger"]["total"] += 1
            acc, _, _ = v.verify(s)
            if acc:
                false_acceptances += 1
            else:
                cohort_stats["stranger"]["rejected"] += 1

        # Habit Mimic (Adversary - Matches macro speed/channels, fails micro-dynamics)
        for s in dataset[pid]["habit_mimic"]:
            total_impostors += 1
            cohort_stats["habit_mimic"]["total"] += 1
            acc, _, _ = v.verify(s)
            if acc:
                false_acceptances += 1
            else:
                cohort_stats["habit_mimic"]["rejected"] += 1

    far = (false_acceptances / max(total_impostors, 1)) * 100.0
    frr = (false_rejections / max(total_genuine, 1)) * 100.0

    print(f"- Enrolled Normal Acceptance Rate:   {cohort_stats['enrolled_normal']['accepted']}/{cohort_stats['enrolled_normal']['total']} ({(cohort_stats['enrolled_normal']['accepted']/cohort_stats['enrolled_normal']['total'])*100.0:.1f}%)")
    print(f"- Enrolled Fatigued Acceptance Rate: {cohort_stats['enrolled_fatigued']['accepted']}/{cohort_stats['enrolled_fatigued']['total']} ({(cohort_stats['enrolled_fatigued']['accepted']/cohort_stats['enrolled_fatigued']['total'])*100.0:.1f}%)")
    print(f"- Stranger Rejection Rate:           {cohort_stats['stranger']['rejected']}/{cohort_stats['stranger']['total']} ({(cohort_stats['stranger']['rejected']/cohort_stats['stranger']['total'])*100.0:.1f}%)")
    print(f"- Habit Mimic Rejection Rate:        {cohort_stats['habit_mimic']['rejected']}/{cohort_stats['habit_mimic']['total']} ({(cohort_stats['habit_mimic']['rejected']/cohort_stats['habit_mimic']['total'])*100.0:.1f}%)")
    print("-" * 74)
    print(f"🎯 EXACT False Acceptance Rate (FAR): {far:.2f}% (Target: 0.00%)")
    print(f"🎯 EXACT False Rejection Rate  (FRR): {frr:.2f}% (Target: <= 5.00%)")
    print("==========================================================================")

    assert far == 0.0, f"FAR must be 0.0%, got {far}%"
    assert frr <= 5.0, f"FRR must be <= 5.0%, got {frr}%"
    assert cross_matrix_violations == 0, f"Cross-profile swap violations must be 0, got {cross_matrix_violations}"

    return {"FAR": far, "FRR": frr, "cross_violations": float(cross_matrix_violations)}


if __name__ == "__main__":
    run_comprehensive_benchmark()
