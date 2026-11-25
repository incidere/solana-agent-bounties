"""Unit and integration tests for Multi-Channel 2D Kinematics & Burst-Rhythm Verifier."""
import unittest

import synth_traces
from anomaly_verifier import KinematicBurstAnomalyVerifier, ProfileModel, VerificationResult
from synth_traces import BurstEvent, SessionStream, SyntheticTraceGenerator, TrajectoryTick


class TestKinematicBurstAnomalyVerifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rng_seed = 42
        cls.gen = SyntheticTraceGenerator(cls.rng_seed)
        cls.suite = cls.gen.generate_benchmark_suite(
            n_enroll=25,
            n_calibration_genuine=20,
            n_calibration_impostor=20,
            n_holdout_per_cohort=30,
        )
        cls.verifier = KinematicBurstAnomalyVerifier()
        for pid in cls.suite:
            cls.verifier.enroll(pid, cls.suite[pid]["enroll"])
            cls.verifier.calibrate(pid, cls.suite[pid]["calibration"])

    def test_feature_extraction_keys(self):
        session = self.suite["fast_desktop"]["enroll"][0]
        feats = KinematicBurstAnomalyVerifier.extract_features(session)
        expected_keys = {
            "tick_hz", "dwell_fraction", "peak_ratio", "front_load",
            "tortuosity", "lateral_arc", "overshoot", "tremor",
            "channel_code", "log_iei_mean", "log_iei_std", "autocorr_lag1",
            "edit_rate", "num_edits", "pre_edit_hes", "post_edit_rec",
            "burst_chunk_size"
        }
        self.assertTrue(expected_keys.issubset(set(feats.keys())))
        for k in expected_keys:
            self.assertIsInstance(feats[k], (int, float))

    def test_channel_alias_normalization(self):
        # BATCH_CHUNK alias should map to BATCH_INSERT (channel_code = 2.0)
        ticks = [TrajectoryTick(ts_ms=float(i * 10), x_norm=0.5, y_norm=0.5, act_kind="DWELL") for i in range(10)]
        bursts = [
            BurstEvent(ts_ms=100.0, channel="BATCH_CHUNK", symbol_count=4, is_edit=False),
            BurstEvent(ts_ms=250.0, channel="BATCH_CHUNK", symbol_count=3, is_edit=False),
            BurstEvent(ts_ms=400.0, channel="BATCH_INSERT", symbol_count=4, is_edit=False),
        ]
        session = SessionStream(
            session_id="alias_test",
            profile_id="hybrid_stylus",
            cohort="enrolled_normal",
            ticks=ticks,
            bursts=bursts,
        )
        feats = KinematicBurstAnomalyVerifier.extract_features(session)
        self.assertEqual(feats["channel_code"], 2.0)

    def test_session_roundtrip_serialization(self):
        session = self.suite["slow_assistive"]["enroll"][0]
        d = session.to_dict()
        reconstructed = SessionStream.from_dict(d)
        self.assertEqual(session.session_id, reconstructed.session_id)
        self.assertEqual(session.profile_id, reconstructed.profile_id)
        self.assertEqual(session.cohort, reconstructed.cohort)
        self.assertEqual(len(session.ticks), len(reconstructed.ticks))
        self.assertEqual(len(session.bursts), len(reconstructed.bursts))

    def test_benchmark_performance_fast_desktop(self):
        pid = "fast_desktop"
        scorecard = self.verifier.evaluate_suite({pid: self.suite[pid]})
        stats = scorecard[pid]
        self.assertGreaterEqual(stats["normal_accept_rate"], 0.95)
        self.assertGreaterEqual(stats["fatigued_accept_rate"], 0.95)
        self.assertEqual(stats["stranger_reject_rate"], 1.0)
        self.assertGreaterEqual(stats["mimic_reject_rate"], 0.95)

    def test_benchmark_performance_slow_assistive(self):
        pid = "slow_assistive"
        scorecard = self.verifier.evaluate_suite({pid: self.suite[pid]})
        stats = scorecard[pid]
        self.assertGreaterEqual(stats["normal_accept_rate"], 0.95)
        self.assertGreaterEqual(stats["fatigued_accept_rate"], 0.95)
        self.assertEqual(stats["stranger_reject_rate"], 1.0)
        self.assertGreaterEqual(stats["mimic_reject_rate"], 0.95)

    def test_benchmark_performance_hybrid_stylus(self):
        pid = "hybrid_stylus"
        scorecard = self.verifier.evaluate_suite({pid: self.suite[pid]})
        stats = scorecard[pid]
        self.assertGreaterEqual(stats["normal_accept_rate"], 0.95)
        self.assertGreaterEqual(stats["fatigued_accept_rate"], 0.95)
        self.assertEqual(stats["stranger_reject_rate"], 1.0)
        self.assertGreaterEqual(stats["mimic_reject_rate"], 0.95)

    def test_verification_result_structure(self):
        session = self.suite["fast_desktop"]["holdout_normal"][0]
        res = self.verifier.verify("fast_desktop", session)
        self.assertIsInstance(res, VerificationResult)
        self.assertTrue(res.is_genuine)
        self.assertLess(res.anomaly_score, res.threshold)
        d = res.to_dict()
        self.assertIn("is_genuine", d)
        self.assertIn("anomaly_score", d)
        self.assertIn("penalties", d)


if __name__ == "__main__":
    unittest.main()
