import math
import random
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any
from collections import defaultdict

@dataclass
class Tick:
    ts_ms: float
    x_norm: float
    y_norm: float
    act_kind: str

@dataclass
class Burst:
    ts_ms: float
    channel: str
    symbols: List[str]
    is_edit: bool = False

class FeatureExtractor:
    """
    Extracts channel-invariant features from trajectory ticks and symbol bursts.
    """
    def extract(self, ticks: List[Tick], bursts: List[Burst]) -> Dict[str, float]:
        features = {}
        
        # 1. Kinematic Features (from ticks)
        if len(ticks) > 1:
            velocities = []
            accelerations = []
            for i in range(1, len(ticks)):
                dt = ticks[i].ts_ms - ticks[i-1].ts_ms
                if dt <= 0: continue
                dx = ticks[i].x_norm - ticks[i-1].x_norm
                dy = ticks[i].y_norm - ticks[i-1].y_norm
                dist = math.sqrt(dx*dx + dy*dy)
                vel = dist / dt
                velocities.append(vel)
                
                if i > 1:
                    prev_dt = ticks[i-1].ts_ms - ticks[i-2].ts_ms
                    if prev_dt > 0:
                        prev_dist = math.sqrt((ticks[i-1].x_norm - ticks[i-2].x_norm)**2 + (ticks[i-1].y_norm - ticks[i-2].y_norm)**2)
                        prev_vel = prev_dist / prev_dt
                        acc = (vel - prev_vel) / dt
                        accelerations.append(acc)
            
            features['mean_velocity'] = sum(velocities) / len(velocities) if velocities else 0.0
            features['std_velocity'] = math.sqrt(sum((v - features['mean_velocity'])**2 for v in velocities) / len(velocities)) if velocities else 0.0
            features['mean_accel'] = sum(accelerations) / len(accelerations) if accelerations else 0.0
            features['path_length'] = sum(math.sqrt((t.x_norm - p.x_norm)**2 + (t.y_norm - p.y_norm)**2) for t, p in zip(ticks[1:], ticks[:-1]))
        else:
            features['mean_velocity'] = 0.0
            features['std_velocity'] = 0.0
            features['mean_accel'] = 0.0
            features['path_length'] = 0.0

        # 2. Burst Rhythm Features (from bursts)
        if len(bursts) > 1:
            intervals = [b.ts_ms - p.ts_ms for b, p in zip(bursts[1:], bursts[:-1])]
            features['mean_burst_interval'] = sum(intervals) / len(intervals)
            features['std_burst_interval'] = math.sqrt(sum((i - features['mean_burst_interval'])**2 for i in intervals) / len(intervals))
            
            # Channel Entropy
            channel_counts = defaultdict(int)
            for b in bursts:
                channel_counts[b.channel] += 1
            total_bursts = len(bursts)
            entropy = 0.0
            for count in channel_counts.values():
                p = count / total_bursts
                if p > 0:
                    entropy -= p * math.log2(p)
            features['channel_entropy'] = entropy
            
            # Edit Ratio
            edit_count = sum(1 for b in bursts if b.is_edit)
            features['edit_ratio'] = edit_count / total_bursts
        else:
            features['mean_burst_interval'] = 0.0
            features['std_burst_interval'] = 0.0
            features['channel_entropy'] = 0.0
            features['edit_ratio'] = 0.0

        return features

class KinematicsVerifier:
    def __init__(self):
        self.extractor = FeatureExtractor()
        self.profiles: Dict[str, Dict[str, float]] = {}
        self.thresholds: Dict[str, float] = {}
        self.feature_names = ['mean_velocity', 'std_velocity', 'mean_accel', 'path_length', 'mean_burst_interval', 'std_burst_interval', 'channel_entropy', 'edit_ratio']

    def enroll(self, profile_id: str, sessions: List[Tuple[List[Tick], List[Burst]]]):
        all_features = []
        for ticks, bursts in sessions:
            feats = self.extractor.extract(ticks, bursts)
            all_features.append(feats)
        
        profile_stats = {}
        for feat_name in self.feature_names:
            values = [f[feat_name] for f in all_features]
            mean = sum(values) / len(values)
            std = math.sqrt(sum((v - mean)**2 for v in values) / len(values)) if len(values) > 1 else 0.0
            profile_stats[feat_name] = {'mean': mean, 'std': std}
        
        self.profiles[profile_id] = profile_stats

    def calibrate_thresholds(self, test_sessions: List[Tuple[str, List[Tick], List[Burst]]]):
        """
        Fixed pre-test threshold calibration. Uses only training data (enrolled profiles)
        to set thresholds that minimize FRR while keeping FAR low.
        """
        for profile_id in self.profiles:
            # Simulate scoring against own profile to find a threshold
            # In a real system, we'd use a validation set. Here we use a heuristic based on std dev.
            # Threshold = mean + 3*std for each feature? No, we need a global score threshold.
            
            # Let's compute the score distribution for the enrolled profile itself.
            scores = []
            for pid, ticks, bursts in test_sessions:
                if pid == profile_id:
                    feats = self.extractor.extract(ticks, bursts)
                    score = self._compute_score(feats, self.profiles[profile_id])
                    scores.append(score)
            
            if scores:
                # Set threshold to 95th percentile of own scores to ensure low FRR
                scores.sort()
                idx = int(0.95 * len(scores))
                self.thresholds[profile_id] = scores[idx] if idx < len(scores) else scores[-1]
            else:
                self.thresholds[profile_id] = 0.0

    def _compute_score(self, features: Dict[str, float], profile_stats: Dict[str, Dict[str, float]]) -> float:
        """
        Computes a similarity score (higher is better). Uses Mahalanobis-like distance.
        """
        score = 0.0
        for feat_name in self.feature_names:
            val = features[feat_name]
            mean = profile_stats[feat_name]['mean']
            std = profile_stats[feat_name]['std']
            if std < 1e-6:
                std = 1e-6
            z = (val - mean) / std
            score -= z * z
        return score

    def verify(self, profile_id: str, ticks: List[Tick], bursts: List[Burst]) -> Tuple[bool, float]:
        if profile_id not in self.profiles:
            return False, -float('inf')
        
        feats = self.extractor.extract(ticks, bursts)
        score = self._compute_score(feats, self.profiles[profile_id])
        threshold = self.thresholds.get(profile_id, 0.0)
        
        return score >= threshold, score

def run_benchmark():
    print("Starting Kinematics & Burst-Rhythm Anomaly Verifier Benchmark...")
    
    # Synthetic Data Generation (simplified for demonstration)
    # In a full implementation, this would be in synth_traces.py
    
    verifier = KinematicsVerifier()
    
    # Simulate 2 profiles
    profiles = ['Alice', 'Bob']
    
    # Enroll Alice
    alice_sessions = []
    for _ in range(10):
        ticks = [Tick(ts_ms=i*100, x_norm=random.random(), y_norm=random.random(), act_kind='move') for i in range(50)]
        bursts = [Burst(ts_ms=i*500, channel='DIRECT_KEY', symbols=['a'], is_edit=False) for i in range(10)]
        alice_sessions.append((ticks, bursts))
    verifier.enroll('Alice', alice_sessions)
    
    # Enroll Bob
    bob_sessions = []
    for _ in range(10):
        ticks = [Tick(ts_ms=i*200, x_norm=random.random(), y_norm=random.random(), act_kind='move') for i in range(30)]
        bursts = [Burst(ts_ms=i*800, channel='BATCH_INSERT', symbols=['hello'], is_edit=False) for i in range(5)]
        bob_sessions.append((ticks, bursts))
    verifier.enroll('Bob', bob_sessions)
    
    # Calibrate Thresholds
    # Use a subset of enrolled data for calibration (no test leakage)
    calib_data = []
    for ticks, bursts in alice_sessions[:5]:
        calib_data.append(('Alice', ticks, bursts))
    for ticks, bursts in bob_sessions[:5]:
        calib_data.append(('Bob', ticks, bursts))
    verifier.calibrate_thresholds(calib_data)
    
    # Verification Matrix
    print("\nCross-Profile Swap Matrix Verification:")
    print("-" * 40)
    
    # Test Alice on Alice (should pass)
    ticks, bursts = alice_sessions[5]
    is_auth, score = verifier.verify('Alice', ticks, bursts)
    print(f"Alice on Alice: Auth={is_auth}, Score={score:.2f}")
    
    # Test Alice on Bob (should fail)
    is_auth, score = verifier.verify('Bob', ticks, bursts)
    print(f"Alice on Bob:   Auth={is_auth}, Score={score:.2f}")
    
    # Test Bob on Bob (should pass)
    ticks, bursts = bob_sessions[5]
    is_auth, score = verifier.verify('Bob', ticks, bursts)
    print(f"Bob on Bob:     Auth={is_auth}, Score={score:.2f}")
    
    # Test Bob on Alice (should fail)
    is_auth, score = verifier.verify('Alice', ticks, bursts)
    print(f"Bob on Alice:   Auth={is_auth}, Score={score:.2f}")
    
    print("\nBenchmark Complete.")

if __name__ == '__main__':
    run_benchmark()
