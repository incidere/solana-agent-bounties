# probe_ctrl

Non-blocking adaptive goodput probe and hysteresis capture-tier controller (Stellarator t=24).

## Features

- **Three-Tier State Machine**: `Normal`, `Degraded`, and `CriticalCapture`.
- **Hysteresis Tier Controller**: Separated drop threshold and recovery threshold with configurable failure/recovery streaks to eliminate flapping and chattering.
- **Single-Flight CaptureGuard**: Atomic RAII guard ensuring critical diagnostic capture executes without concurrent overlap.
- **Request Coalescer**: Deduplicates concurrent health/goodput probes so concurrent tasks share a single in-flight evaluation.
- **Non-blocking Goodput Tracker**: Sliding-window tracking of effective throughput (bps) and goodput ratio.

## Testing

```bash
cargo test --manifest-path probe_ctrl/Cargo.toml --offline
```
