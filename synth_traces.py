"""Synthetic interaction trace generator for multi-channel kinematics & burst-rhythm anomaly detection.

Generates realistic 2D trajectory ticks and multi-channel symbol-arrival bursts
across distinct interaction styles and behavioral cohorts (enrolled_normal,
enrolled_fatigued, stranger, habit_mimic).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


# Burst channels
DIRECT_KEY = "DIRECT_KEY"
VIRTUAL_GRID = "VIRTUAL_GRID"
BATCH_INSERT = "BATCH_INSERT"
BATCH_CHUNK = "BATCH_CHUNK"  # Accepted alias for BATCH_INSERT


@dataclass
class TrajectoryTick:
    ts_ms: float
    x_norm: float
    y_norm: float
    act_kind: str  # "MOVE", "DOWN", "UP", "DWELL", "DRAG"


@dataclass
class BurstEvent:
    ts_ms: float
    channel: str  # DIRECT_KEY, VIRTUAL_GRID, BATCH_INSERT
    symbol_count: int
    is_edit: bool = False  # e.g., backspace, deletion, replacement


@dataclass
class SessionStream:
    session_id: str
    profile_id: str
    cohort: str  # enrolled_normal, enrolled_fatigued, stranger, habit_mimic
    ticks: List[TrajectoryTick] = field(default_factory=list)
    bursts: List[BurstEvent] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "cohort": self.cohort,
            "ticks": [
                [t.ts_ms, round(t.x_norm, 5), round(t.y_norm, 5), t.act_kind]
                for t in self.ticks
            ],
            "bursts": [
                [b.ts_ms, b.channel, b.symbol_count, b.is_edit]
                for b in self.bursts
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionStream":
        ticks = [
            TrajectoryTick(ts_ms=t[0], x_norm=t[1], y_norm=t[2], act_kind=t[3])
            for t in data.get("ticks", [])
        ]
        bursts = [
            BurstEvent(ts_ms=b[0], channel=b[1], symbol_count=b[2], is_edit=bool(b[3]))
            for b in data.get("bursts", [])
        ]
        return cls(
            session_id=data.get("session_id", ""),
            profile_id=data.get("profile_id", ""),
            cohort=data.get("cohort", ""),
            ticks=ticks,
            bursts=bursts,
        )


@dataclass
class StyleProfile:
    name: str
    # Kinematic base parameters
    peak_ratio_base: float
    front_load_bias: float
    tortuosity_base: float
    lateral_arc_bias: float
    dwell_fraction_base: float
    overshoot_tendency: float
    turning_noise: float
    tick_hz: float  # Nominal hardware polling rate (e.g., 60Hz, 120Hz)

    # Burst-rhythm base parameters
    preferred_channel: str
    log_iei_mean: float
    log_iei_std_base: float
    autocorr_lag1_base: float
    edit_rate_base: float
    pre_edit_hesitation: float
    post_edit_recovery: float
    burst_chunk_size: float


# Three distinct interaction styles (> = 2 required by specification)
PROFILES: Dict[str, StyleProfile] = {
    "fast_desktop": StyleProfile(
        name="fast_desktop",
        peak_ratio_base=0.34,
        front_load_bias=0.62,
        tortuosity_base=0.045,
        lateral_arc_bias=-0.038,
        dwell_fraction_base=0.12,
        overshoot_tendency=0.082,
        turning_noise=0.035,
        tick_hz=125.0,
        preferred_channel=DIRECT_KEY,
        log_iei_mean=4.85,  # ~127ms nominal IEI
        log_iei_std_base=0.32,
        autocorr_lag1_base=0.48,
        edit_rate_base=0.042,
        pre_edit_hesitation=1.65,
        post_edit_recovery=1.35,
        burst_chunk_size=1.2,
    ),
    "slow_assistive": StyleProfile(
        name="slow_assistive",
        peak_ratio_base=0.52,
        front_load_bias=0.48,
        tortuosity_base=0.185,
        lateral_arc_bias=0.065,
        dwell_fraction_base=0.38,
        overshoot_tendency=0.025,
        turning_noise=0.115,
        tick_hz=50.0,
        preferred_channel=VIRTUAL_GRID,
        log_iei_mean=5.95,  # ~383ms nominal IEI
        log_iei_std_base=0.68,
        autocorr_lag1_base=0.18,
        edit_rate_base=0.095,
        pre_edit_hesitation=2.45,
        post_edit_recovery=1.85,
        burst_chunk_size=1.0,
    ),
    "hybrid_stylus": StyleProfile(
        name="hybrid_stylus",
        peak_ratio_base=0.42,
        front_load_bias=0.55,
        tortuosity_base=0.095,
        lateral_arc_bias=0.112,
        dwell_fraction_base=0.22,
        overshoot_tendency=0.055,
        turning_noise=0.055,
        tick_hz=90.0,
        preferred_channel=BATCH_INSERT,
        log_iei_mean=5.35,  # ~210ms nominal IEI
        log_iei_std_base=0.46,
        autocorr_lag1_base=0.35,
        edit_rate_base=0.060,
        pre_edit_hesitation=1.90,
        post_edit_recovery=1.50,
        burst_chunk_size=3.8,
    ),
}


class SyntheticTraceGenerator:
    """Generates synthetic multi-channel kinematics and burst-rhythm traces."""

    def __init__(self, rng_seed: int = 42):
        self.rng = random.Random(rng_seed)

    def _sample_stroke(
        self,
        t_start: float,
        duration: float,
        p_start: Tuple[float, float],
        p_end: Tuple[float, float],
        profile: StyleProfile,
        fatigue_mult: float = 1.0,
        hz_override: float | None = None,
    ) -> List[TrajectoryTick]:
        hz = hz_override or profile.tick_hz
        dt_tick = 1000.0 / hz
        num_ticks = max(4, int(duration / dt_tick))
        ticks: List[TrajectoryTick] = []

        x0, y0 = p_start
        x1, y1 = p_end
        dx, dy = x1 - x0, y1 - y0
        chord = math.hypot(dx, dy)
        if chord < 1e-4:
            chord = 1e-4
            dx, dy = 1e-4, 0.0

        ux, uy = dx / chord, dy / chord
        nx, ny = -uy, ux  # Normal vector for lateral curvature

        peak_r = max(0.15, min(0.85, profile.peak_ratio_base * (1.0 + 0.1 * (fatigue_mult - 1.0))))
        arc_bias = profile.lateral_arc_bias * fatigue_mult
        tort_base = profile.tortuosity_base * fatigue_mult
        overshoot = profile.overshoot_tendency * (1.1 if fatigue_mult > 1.0 else 1.0)

        for step in range(num_ticks):
            u = step / float(num_ticks - 1)
            ts = t_start + step * dt_tick

            # Beta/asymmetric bell-curve for velocity profile
            if u < peak_r:
                prog = 0.5 * (1.0 - math.cos(math.pi * (u / peak_r))) * peak_r
            else:
                prog = peak_r + 0.5 * (1.0 - math.cos(math.pi * ((u - peak_r) / (1.0 - peak_r)))) * (1.0 - peak_r)
            s_norm = prog / max(prog if step == num_ticks - 1 else 1.0, 1e-5)
            s_norm = min(1.0 + overshoot * math.sin(math.pi * u) * (1.0 - u), max(0.0, s_norm))

            # Primary arc curvature
            lateral = arc_bias * chord * math.sin(math.pi * u)
            # High-frequency tremor / turning noise
            noise = tort_base * chord * math.sin(6.0 * math.pi * u) * (self.rng.uniform(-1.0, 1.0))

            px = x0 + s_norm * dx + (lateral + noise) * nx
            py = y0 + s_norm * dy + (lateral + noise) * ny

            # Clamp to [0, 1] normalized bounds
            px = max(0.01, min(0.99, px))
            py = max(0.01, min(0.99, py))

            act = "DOWN" if step == 0 else ("UP" if step == num_ticks - 1 else "DRAG")
            ticks.append(TrajectoryTick(ts_ms=ts, x_norm=px, y_norm=py, act_kind=act))

        return ticks

    def generate_session(
        self,
        profile_name: str,
        cohort: str,
        session_idx: int,
        session_duration_s: float = 12.0,
        hz_override: float | None = None,
        channel_alias_test: bool = False,
    ) -> SessionStream:
        profile = PROFILES.get(profile_name, PROFILES["fast_desktop"])
        session_id = f"{profile_name}_{cohort}_{session_idx:04d}"

        # Cohort behavioral parameter modulation
        if cohort == "enrolled_normal":
            fatigue_mult = 1.0
            param_jitter = 1.0
            mimic_adversary = False
        elif cohort == "enrolled_fatigued":
            fatigue_mult = 1.45  # Slower movement, higher tremor/dwell, but invariant kinematic knot structure
            param_jitter = 1.0
            mimic_adversary = False
        elif cohort == "stranger":
            # Completely foreign profile dynamics
            alt_profiles = [p for name, p in PROFILES.items() if name != profile_name]
            profile = self.rng.choice(alt_profiles)
            fatigue_mult = self.rng.uniform(0.8, 1.3)
            param_jitter = self.rng.uniform(0.7, 1.4)
            mimic_adversary = False
        elif cohort == "habit_mimic":
            # Adversary attempts to mimic average speed / tempo, but lacks genuine biomechanical invariants
            fatigue_mult = 1.0
            param_jitter = 1.0
            mimic_adversary = True
        else:
            raise ValueError(f"Unknown cohort: {cohort}")

        # Generate trajectory ticks (series of strokes and dwells)
        ticks: List[TrajectoryTick] = []
        cur_t = self.rng.uniform(10.0, 50.0)
        end_t = cur_t + session_duration_s * 1000.0
        cur_pos = (self.rng.uniform(0.2, 0.8), self.rng.uniform(0.2, 0.8))

        dwell_base = profile.dwell_fraction_base * fatigue_mult
        while cur_t < end_t:
            # Dwell phase
            dwell_ms = max(40.0, self.rng.gauss(180.0 * dwell_base, 35.0))
            for d_idx in range(max(2, int(dwell_ms / (1000.0 / (hz_override or profile.tick_hz))))):
                t_dwell = cur_t + d_idx * (1000.0 / (hz_override or profile.tick_hz))
                jitter_x = cur_pos[0] + self.rng.gauss(0.0, 0.0008)
                jitter_y = cur_pos[1] + self.rng.gauss(0.0, 0.0008)
                ticks.append(TrajectoryTick(ts_ms=t_dwell, x_norm=jitter_x, y_norm=jitter_y, act_kind="DWELL"))
            cur_t += dwell_ms

            # Stroke phase
            next_pos = (self.rng.uniform(0.1, 0.9), self.rng.uniform(0.1, 0.9))
            dist = math.hypot(next_pos[0] - cur_pos[0], next_pos[1] - cur_pos[1])
            stroke_dur_ms = max(120.0, dist * (850.0 * fatigue_mult) + self.rng.gauss(40.0, 15.0))

            if mimic_adversary:
                # Mimic uses artificial linear interpolation with unnatural constant velocity or random arc
                fake_profile = StyleProfile(
                    name="mimic",
                    peak_ratio_base=self.rng.uniform(0.2, 0.8),  # Random non-matching peak
                    front_load_bias=0.50,
                    tortuosity_base=profile.tortuosity_base * 2.8,  # Excessive jaggedness
                    lateral_arc_bias=-profile.lateral_arc_bias * 1.5,  # Inverted/wrong wrist arc
                    dwell_fraction_base=profile.dwell_fraction_base,
                    overshoot_tendency=profile.overshoot_tendency * 0.2,
                    turning_noise=0.15,
                    tick_hz=profile.tick_hz,
                    preferred_channel=profile.preferred_channel,
                    log_iei_mean=profile.log_iei_mean,
                    log_iei_std_base=profile.log_iei_std_base * 2.0,
                    autocorr_lag1_base=-0.15,  # Broken cadence correlation
                    edit_rate_base=profile.edit_rate_base,
                    pre_edit_hesitation=1.0,
                    post_edit_recovery=1.0,
                    burst_chunk_size=profile.burst_chunk_size,
                )
                stroke_ticks = self._sample_stroke(cur_t, stroke_dur_ms, cur_pos, next_pos, fake_profile, 1.0, hz_override)
            else:
                stroke_ticks = self._sample_stroke(cur_t, stroke_dur_ms, cur_pos, next_pos, profile, fatigue_mult, hz_override)

            ticks.extend(stroke_ticks)
            cur_t += stroke_dur_ms
            cur_pos = next_pos

        # Generate burst events (symbol arrival and edit streams)
        bursts: List[BurstEvent] = []
        b_t = self.rng.uniform(25.0, 80.0)
        iei_mean = profile.log_iei_mean + (0.35 if cohort == "enrolled_fatigued" else 0.0)
        iei_std = profile.log_iei_std_base * (1.2 if cohort == "enrolled_fatigued" else 1.0)
        autocorr = profile.autocorr_lag1_base

        prev_iei_log = iei_mean
        primary_channel = profile.preferred_channel
        if channel_alias_test and primary_channel == BATCH_INSERT:
            primary_channel = BATCH_CHUNK

        while b_t < end_t:
            # Autoregressive log-IEI generation for true rhythmic cadence
            white_noise = self.rng.gauss(0.0, iei_std * math.sqrt(max(0.01, 1.0 - autocorr * autocorr)))
            if mimic_adversary:
                # Mimic lacks rhythmic autocorrelation; independent uniform noise
                cur_iei_log = self.rng.gauss(iei_mean, iei_std * 1.8)
            else:
                cur_iei_log = iei_mean + autocorr * (prev_iei_log - iei_mean) + white_noise
            prev_iei_log = cur_iei_log

            iei_ms = max(18.0, math.exp(cur_iei_log))

            # Determine if this burst contains an edit/correction
            is_edit = (self.rng.random() < profile.edit_rate_base)
            if is_edit:
                # Pre-edit hesitation
                iei_ms *= (profile.pre_edit_hesitation if not mimic_adversary else 1.0)

            b_t += iei_ms
            if b_t >= end_t:
                break

            # Channel assignment: primary channel majority + occasional cross-channel bursts
            c_roll = self.rng.random()
            if c_roll < 0.78:
                channel = primary_channel
            elif c_roll < 0.92:
                channel = VIRTUAL_GRID if primary_channel != VIRTUAL_GRID else DIRECT_KEY
            else:
                channel = BATCH_INSERT

            # Symbol count per burst
            if channel in (BATCH_INSERT, BATCH_CHUNK):
                symbols = max(1, int(self.rng.gauss(profile.burst_chunk_size, 1.2)))
            else:
                symbols = 1 if not is_edit else max(1, int(self.rng.expovariate(1.0 / 1.5)))

            bursts.append(BurstEvent(ts_ms=b_t, channel=channel, symbol_count=symbols, is_edit=is_edit))

            if is_edit and not mimic_adversary:
                # Post-edit recovery interval
                b_t += max(15.0, math.exp(iei_mean) * (profile.post_edit_recovery - 1.0))

        return SessionStream(
            session_id=session_id,
            profile_id=profile_name,
            cohort=cohort,
            ticks=ticks,
            bursts=bursts,
        )

    def generate_benchmark_suite(
        self,
        profiles: List[str] | None = None,
        n_enroll: int = 25,
        n_calibration_genuine: int = 20,
        n_calibration_impostor: int = 20,
        n_holdout_per_cohort: int = 60,
    ) -> Dict[str, Dict[str, List[SessionStream]]]:
        """Generates disjoint enrollment, calibration, and holdout splits across all cohorts."""
        target_profiles = profiles or list(PROFILES.keys())
        suite: Dict[str, Dict[str, List[SessionStream]]] = {}

        for pname in target_profiles:
            # 1. Enrollment set: genuine normal sessions only
            enroll_set = [
                self.generate_session(pname, "enrolled_normal", idx)
                for idx in range(n_enroll)
            ]

            # 2. Disjoint Calibration set: genuine + stranger/mimic calibration sessions
            cal_genuine = [
                self.generate_session(pname, "enrolled_normal", 1000 + idx)
                for idx in range(n_calibration_genuine)
            ]
            cal_impostor = [
                self.generate_session(pname, "stranger", 1500 + idx)
                for idx in range(n_calibration_impostor // 2)
            ] + [
                self.generate_session(pname, "habit_mimic", 1600 + idx)
                for idx in range(n_calibration_impostor // 2)
            ]
            cal_set = cal_genuine + cal_impostor

            # 3. Disjoint Holdout set: 4 cohorts
            holdout_normal = [
                self.generate_session(pname, "enrolled_normal", 2000 + idx)
                for idx in range(n_holdout_per_cohort)
            ]
            holdout_fatigued = [
                self.generate_session(pname, "enrolled_fatigued", 3000 + idx)
                for idx in range(n_holdout_per_cohort)
            ]
            holdout_stranger = [
                self.generate_session(pname, "stranger", 4000 + idx)
                for idx in range(n_holdout_per_cohort)
            ]
            holdout_mimic = [
                self.generate_session(pname, "habit_mimic", 5000 + idx)
                for idx in range(n_holdout_per_cohort)
            ]

            suite[pname] = {
                "enroll": enroll_set,
                "calibration": cal_set,
                "holdout_normal": holdout_normal,
                "holdout_fatigued": holdout_fatigued,
                "holdout_stranger": holdout_stranger,
                "holdout_mimic": holdout_mimic,
            }

        return suite
