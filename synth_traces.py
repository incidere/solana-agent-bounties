import random
from typing import List, Tuple
from kinematics_auth import Tick, Burst

def generate_session(style: str, profile_id: str, state: str) -> Tuple[List[Tick], List[Burst]]:
    """
    Generates a synthetic session based on interaction style and user state.
    
    Styles:
    - rapid_continuous: High velocity, frequent ticks, direct key bursts.
    - low_velocity_batch: Low velocity, sparse ticks, batch insert bursts.
    
    States:
    - enrolled_normal: Baseline behavior.
    - enrolled_fatigued: Slower, more erratic.
    - stranger: Different baseline.
    - habit_mimic: Attempts to mimic enrolled but with subtle deviations.
    """
    ticks = []
    bursts = []
    
    if style == 'rapid_continuous':
        base_interval = 50
        base_burst_interval = 300
        channel = 'DIRECT_KEY'
    elif style == 'low_velocity_batch':
        base_interval = 200
        base_burst_interval = 1000
        channel = 'BATCH_INSERT'
    else:
        base_interval = 100
        base_burst_interval = 500
        channel = 'VIRTUAL_GRID'

    # Adjust for state
    if state == 'enrolled_fatigued':
        base_interval *= 1.5
        base_burst_interval *= 1.5
    elif state == 'stranger':
        base_interval *= 0.8
        base_burst_interval *= 0.8
    elif state == 'habit_mimic':
        base_interval *= 1.1
        base_burst_interval *= 1.1

    # Generate Ticks
    ts = 0
    x, y = 0.5, 0.5
    for i in range(50):
        ts += base_interval + random.gauss(0, base_interval * 0.2)
        x += random.gauss(0, 0.05)
        y += random.gauss(0, 0.05)
        x = max(0, min(1, x))
        y = max(0, min(1, y))
        ticks.append(Tick(ts_ms=ts, x_norm=x, y_norm=y, act_kind='move'))

    # Generate Bursts
    ts = 0
    for i in range(10):
        ts += base_burst_interval + random.gauss(0, base_burst_interval * 0.2)
        is_edit = random.random() < 0.1
        symbols = ['a', 'b', 'c'] if channel == 'BATCH_INSERT' else ['a']
        bursts.append(Burst(ts_ms=ts, channel=channel, symbols=symbols, is_edit=is_edit))

    return ticks, bursts

def generate_benchmark_data(num_profiles: int = 2, num_sessions_per_state: int = 10):
    """
    Generates a full benchmark dataset.
    """
    data = {}
    styles = ['rapid_continuous', 'low_velocity_batch']
    states = ['enrolled_normal', 'enrolled_fatigued', 'stranger', 'habit_mimic']
    
    for i in range(num_profiles):
        profile_id = f"Profile_{i}"
        data[profile_id] = {}
        for style in styles:
            data[profile_id][style] = {}
            for state in states:
                sessions = []
                for _ in range(num_sessions_per_state):
                    ticks, bursts = generate_session(style, profile_id, state)
                    sessions.append((ticks, bursts))
                data[profile_id][style][state] = sessions
    
    return data
