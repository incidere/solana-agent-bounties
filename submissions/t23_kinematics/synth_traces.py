"""
Synthetic Interaction Trace Generator for Multi-Channel 2D Kinematics & Burst-Rhythm Anomaly Verifier
Clean-Room Zero-Dependency Python 3.12 Implementation.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import random
from typing import List, Dict, Tuple


class Channel(Enum):
    DIRECT_KEY = "DIRECT_KEY"
    VIRTUAL_GRID = "VIRTUAL_GRID"
    BATCH_INSERT = "BATCH_INSERT"


class ActKind(Enum):
    DOWN = "DOWN"
    MOVE = "MOVE"
    UP = "UP"
    HOVER = "HOVER"


@dataclass(frozen=True)
class Tick:
    ts_ms: float
    x_norm: float
    y_norm: float
    act_kind: str


@dataclass(frozen=True)
class Burst:
    ts_ms: float
    duration_ms: float
    channel: str
    symbol_count: int
    edit_count: int


@dataclass
class SessionStream:
    profile_id: str
    cohort: str  # enrolled_normal, enrolled_fatigued, stranger, habit_mimic
    ticks: List[Tick] = field(default_factory=list)
    bursts: List[Burst] = field(default_factory=list)


@dataclass
class ProfileConfig:
    profile_id: str
    name: str
    mean_speed: float         # normalized units/s
    speed_var: float
    ballistic_skew: float     # velocity profile peak time fraction (0.28..0.62)
    tortuosity_bias: float    # path curvature factor
    tremor_amp: float         # high-frequency physiological jitter amplitude
    mean_iei_ms: float        # inter-event interval for typing (ms)
    iei_std_ms: float
    cadence_autocorr: float   # lag-1 correlation in keystroke timing (0.15..0.65)
    channel_weights: Dict[Channel, float]
    edit_rate: float          # backspace / edit fraction


PROFILES: Dict[str, ProfileConfig] = {
    "rapid_thumb": ProfileConfig(
        profile_id="rapid_thumb",
        name="Rapid Single-Thumb Mobile Flicks",
        mean_speed=1.50,
        speed_var=0.08,
        ballistic_skew=0.28,
        tortuosity_bias=0.08,
        tremor_amp=0.003,
        mean_iei_ms=130.0,
        iei_std_ms=18.0,
        cadence_autocorr=0.55,
        channel_weights={Channel.DIRECT_KEY: 0.85, Channel.VIRTUAL_GRID: 0.10, Channel.BATCH_INSERT: 0.05},
        edit_rate=0.04
    ),
    "fluid_trackpad": ProfileConfig(
        profile_id="fluid_trackpad",
        name="Fluid Trackpad Continuous Swiper",
        mean_speed=0.75,
        speed_var=0.06,
        ballistic_skew=0.58,
        tortuosity_bias=0.28,
        tremor_amp=0.002,
        mean_iei_ms=210.0,
        iei_std_ms=25.0,
        cadence_autocorr=0.35,
        channel_weights={Channel.DIRECT_KEY: 0.45, Channel.VIRTUAL_GRID: 0.45, Channel.BATCH_INSERT: 0.10},
        edit_rate=0.06
    ),
    "assistive_deliberate": ProfileConfig(
        profile_id="assistive_deliberate",
        name="Assistive Low-Velocity & Batch Insert",
        mean_speed=0.35,
        speed_var=0.04,
        ballistic_skew=0.62,
        tortuosity_bias=0.04,
        tremor_amp=0.004,
        mean_iei_ms=360.0,
        iei_std_ms=45.0,
        cadence_autocorr=0.18,
        channel_weights={Channel.DIRECT_KEY: 0.10, Channel.VIRTUAL_GRID: 0.15, Channel.BATCH_INSERT: 0.75},
        edit_rate=0.02
    ),
    "precision_stylus": ProfileConfig(
        profile_id="precision_stylus",
        name="Stylus Precision & Multi-Grid Targeter",
        mean_speed=1.35,
        speed_var=0.06,
        ballistic_skew=0.34,
        tortuosity_bias=0.06,
        tremor_amp=0.002,
        mean_iei_ms=175.0,
        iei_std_ms=22.0,
        cadence_autocorr=0.65,
        channel_weights={Channel.DIRECT_KEY: 0.15, Channel.VIRTUAL_GRID: 0.80, Channel.BATCH_INSERT: 0.05},
        edit_rate=0.05
    )
}


def _compute_ballistic_progression(steps: int, skew: float) -> List[float]:
    """
    Computes smooth asymmetric ballistic path progression tau(s) based on Flash & Hogan minimum-jerk profile.
    Guarantees unique peak velocity at exactly s = skew with zero derivative singularities.
    """
    sig_left = max(0.08, skew * 0.45)
    sig_right = max(0.08, (1.0 - skew) * 0.45)

    weights = []
    for i in range(steps + 1):
        s = i / steps
        sig = sig_left if s < skew else sig_right
        w = math.exp(-0.5 * ((s - skew) / sig) ** 2)
        weights.append(w)

    cum = [0.0]
    for w in weights[1:]:
        cum.append(cum[-1] + w)
    total = cum[-1] if cum[-1] > 1e-6 else 1.0
    return [c / total for c in cum]


def generate_stroke_ticks(
    start_pt: Tuple[float, float],
    end_pt: Tuple[float, float],
    start_t: float,
    cfg: ProfileConfig,
    fatigued: bool = False,
    mimic: bool = False
) -> Tuple[List[Tick], float]:
    """Generates 2D trajectory ticks along an asymmetric ballistic curvilinear stroke."""
    dx = end_pt[0] - start_pt[0]
    dy = end_pt[1] - start_pt[1]
    chord_len = max(math.hypot(dx, dy), 0.05)

    speed = cfg.mean_speed * (0.86 if fatigued else (1.0 + (random.random() - 0.5) * cfg.speed_var))
    if mimic:
        speed *= (1.0 + (random.random() - 0.5) * 0.03)

    duration = chord_len / max(speed, 0.05)
    steps = max(int(duration * 60.0), 12)  # 60 Hz hardware ticks
    dt = duration / steps

    # Control point for quadratic Bezier arc
    normal_x = -dy / chord_len
    normal_y = dx / chord_len
    arc_mag = cfg.tortuosity_bias * chord_len * (-1.0 if random.random() < 0.5 else 1.0)
    if mimic:
        arc_mag = -arc_mag * 1.5

    mid_x = (start_pt[0] + end_pt[0]) / 2.0 + normal_x * arc_mag
    mid_y = (start_pt[1] + end_pt[1]) / 2.0 + normal_y * arc_mag

    skew = 0.50 if mimic else cfg.ballistic_skew
    tau_curve = _compute_ballistic_progression(steps, skew)

    ticks = []
    t_curr = start_t
    ticks.append(Tick(round(t_curr, 2), start_pt[0], start_pt[1], ActKind.DOWN.value))

    tremor_scale = cfg.tremor_amp * (1.35 if fatigued else 1.0)
    if mimic:
        tremor_scale *= 0.15

    for i in range(1, steps):
        t_curr += dt * 1000.0
        tau = tau_curve[i]

        # Quadratic Bezier
        bx = (1 - tau)**2 * start_pt[0] + 2 * (1 - tau) * tau * mid_x + tau**2 * end_pt[0]
        by = (1 - tau)**2 * start_pt[1] + 2 * (1 - tau) * tau * mid_y + tau**2 * end_pt[1]

        # Physiological tremor
        noise_x = math.sin(i * 2.2) * tremor_scale
        noise_y = math.cos(i * 2.2) * tremor_scale

        px = max(0.0, min(1.0, bx + noise_x))
        py = max(0.0, min(1.0, by + noise_y))

        ticks.append(Tick(round(t_curr, 2), round(px, 5), round(py, 5), ActKind.MOVE.value))

    t_curr += dt * 1000.0
    ticks.append(Tick(round(t_curr, 2), end_pt[0], end_pt[1], ActKind.UP.value))
    return ticks, t_curr


def generate_bursts(
    start_t: float,
    count: int,
    cfg: ProfileConfig,
    prev_iei_dev: float = 0.0,
    fatigued: bool = False,
    mimic: bool = False
) -> Tuple[List[Burst], float, float]:
    """Generates symbol arrival bursts with channel tags and continuous lag-1 cadence autocorrelation."""
    bursts = []
    t_curr = start_t
    cur_dev = prev_iei_dev

    channels = list(cfg.channel_weights.keys())
    weights = [cfg.channel_weights[c] for c in channels]

    for _ in range(count):
        phi = 0.02 if mimic else cfg.cadence_autocorr
        noise = random.gauss(0.0, cfg.iei_std_ms)
        iei_dev = phi * cur_dev + math.sqrt(max(0.0, 1.0 - phi**2)) * noise
        cur_dev = iei_dev

        base_iei = cfg.mean_iei_ms * (1.14 if fatigued else 1.0)
        iei = max(35.0, base_iei + iei_dev)
        t_curr += iei

        chan = random.choices(channels, weights=weights, k=1)[0]

        if chan == Channel.BATCH_INSERT:
            symbols = random.randint(5, 25)
            duration = random.uniform(15.0, 45.0)
            edits = 0
        elif chan == Channel.VIRTUAL_GRID:
            symbols = random.randint(1, 3)
            duration = iei * 0.75
            edits = 1 if random.random() < (cfg.edit_rate * 1.3) else 0
        else:  # DIRECT_KEY
            symbols = 1
            duration = max(20.0, random.gauss(75.0, 12.0))
            edits = 1 if random.random() < cfg.edit_rate else 0

        bursts.append(Burst(
            ts_ms=round(t_curr, 2),
            duration_ms=round(duration, 2),
            channel=chan.value,
            symbol_count=symbols,
            edit_count=edits
        ))

    return bursts, t_curr, cur_dev


def generate_session(
    cfg: ProfileConfig,
    cohort: str,
    seed: int,
    num_strokes: int = 5,
    burst_count: int = 25
) -> SessionStream:
    """Generates a complete session stream with ticks and bursts."""
    random.seed(seed)
    fatigued = (cohort == "enrolled_fatigued")
    mimic = (cohort == "habit_mimic")

    session = SessionStream(profile_id=cfg.profile_id, cohort=cohort)
    t_global = 1000.0

    cur_pt = (random.uniform(0.15, 0.85), random.uniform(0.15, 0.85))
    running_iei_dev = 0.0

    for _ in range(num_strokes):
        next_pt = (random.uniform(0.15, 0.85), random.uniform(0.15, 0.85))
        ticks, t_end = generate_stroke_ticks(cur_pt, next_pt, t_global, cfg, fatigued=fatigued, mimic=mimic)
        session.ticks.extend(ticks)
        t_global = t_end + random.uniform(60.0, 150.0)
        cur_pt = next_pt

        # Interleave typing bursts
        sub_count = burst_count // num_strokes
        sub_bursts, t_burst_end, running_iei_dev = generate_bursts(
            t_global, sub_count, cfg, prev_iei_dev=running_iei_dev, fatigued=fatigued, mimic=mimic
        )
        session.bursts.extend(sub_bursts)
        t_global = t_burst_end + random.uniform(80.0, 180.0)

    return session


def generate_benchmark_dataset(num_train: int = 3, num_test_per_cohort: int = 8) -> Dict[str, Dict[str, List[SessionStream]]]:
    """
    Generates dataset for 4 distinct interaction profiles across 4 cohorts:
    - enrolled_normal
    - enrolled_fatigued
    - stranger
    - habit_mimic
    """
    dataset: Dict[str, Dict[str, List[SessionStream]]] = {}
    base_seed = 42

    for pid, cfg in PROFILES.items():
        dataset[pid] = {
            "train_enrolled": [
                generate_session(cfg, "enrolled_normal", seed=base_seed + i * 17)
                for i in range(num_train)
            ],
            "enrolled_normal": [
                generate_session(cfg, "enrolled_normal", seed=base_seed + 100 + i * 19)
                for i in range(num_test_per_cohort)
            ],
            "enrolled_fatigued": [
                generate_session(cfg, "enrolled_fatigued", seed=base_seed + 200 + i * 23)
                for i in range(num_test_per_cohort)
            ],
            "stranger": [
                generate_session(
                    PROFILES[random.choice([p for p in PROFILES if p != pid])],
                    "stranger",
                    seed=base_seed + 300 + i * 29
                )
                for i in range(num_test_per_cohort)
            ],
            "habit_mimic": [
                generate_session(cfg, "habit_mimic", seed=base_seed + 400 + i * 31)
                for i in range(num_test_per_cohort)
            ]
        }

    return dataset
