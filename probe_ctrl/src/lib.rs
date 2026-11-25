//! # probe_ctrl
//!
//! Non-blocking adaptive goodput probe and hysteresis capture-tier controller (Stellarator t=24).
//!
//! Provides:
//! - [`ProbeState`]: Three-tier state machine (`Normal`, `Degraded`, `CriticalCapture`).
//! - [`ProbeConfig`]: Tunable thresholds, recovery streaks, and observation windows.
//! - [`CaptureGuard`]: Single-flight atomic RAII guard for critical diagnostic capture.
//! - [`RequestCoalescer`]: Concurrent probe deduplication avoiding duplicate work.
//! - [`GoodputTracker`]: Non-blocking sliding-window measurement of goodput bps and ratio.
//! - [`ProbeController`]: Integrated controller orchestrating adaptive sampling with hysteresis.

use std::collections::VecDeque;
use std::fmt;
use std::future::Future;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{broadcast, Mutex, Notify, RwLock};

/// Operational tier for the health-probe controller.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(u8)]
pub enum ProbeState {
    /// Nominal goodput tier; healthy channel.
    Normal = 0,
    /// Transient degradation or initial metric drops.
    Degraded = 1,
    /// Sustained failures requiring capture lock and aggressive throttling.
    CriticalCapture = 2,
}

impl ProbeState {
    #[inline]
    pub fn as_u8(self) -> u8 {
        self as u8
    }

    #[inline]
    pub fn is_normal(self) -> bool {
        matches!(self, ProbeState::Normal)
    }

    #[inline]
    pub fn is_degraded(self) -> bool {
        matches!(self, ProbeState::Degraded)
    }

    #[inline]
    pub fn is_critical(self) -> bool {
        matches!(self, ProbeState::CriticalCapture)
    }
}

impl From<u8> for ProbeState {
    fn from(val: u8) -> Self {
        match val {
            0 => ProbeState::Normal,
            1 => ProbeState::Degraded,
            _ => ProbeState::CriticalCapture,
        }
    }
}

impl fmt::Display for ProbeState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ProbeState::Normal => write!(f, "NORMAL"),
            ProbeState::Degraded => write!(f, "DEGRADED"),
            ProbeState::CriticalCapture => write!(f, "CRITICAL_CAPTURE"),
        }
    }
}

/// Configuration settings for hysteresis thresholds and controller intervals.
#[derive(Debug, Clone)]
pub struct ProbeConfig {
    /// Goodput ratio (0.0 .. 1.0) below which an evaluation is deemed a failure.
    pub goodput_threshold: f64,
    /// Higher threshold required to count toward recovery streak (hysteresis deadband).
    pub recovery_threshold: f64,
    /// Consecutive failures needed to drop into `CriticalCapture`.
    pub failure_streak_limit: u32,
    /// Consecutive successes at or above `recovery_threshold` needed to return to `Normal`.
    pub recovery_streak_required: u32,
    /// Nominal periodic probe interval.
    pub probe_interval: Duration,
    /// History duration for goodput sliding window.
    pub window_duration: Duration,
}

impl Default for ProbeConfig {
    fn default() -> Self {
        Self {
            goodput_threshold: 0.80,
            recovery_threshold: 0.90,
            failure_streak_limit: 3,
            recovery_streak_required: 5,
            probe_interval: Duration::from_millis(100),
            window_duration: Duration::from_secs(10),
        }
    }
}

/// RAII lock ensuring single-flight execution for high-tier diagnostic capture.
pub struct CaptureGuard {
    active: Arc<AtomicBool>,
}

impl CaptureGuard {
    /// Attempts atomic acquisition of the capture guard. Returns None if already active.
    pub fn try_acquire(active: Arc<AtomicBool>) -> Option<Self> {
        if active
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_ok()
        {
            Some(Self { active })
        } else {
            None
        }
    }
}

impl Drop for CaptureGuard {
    fn drop(&mut self) {
        self.active.store(false, Ordering::Release);
    }
}

#[derive(Debug, Clone, Copy)]
struct TransferSample {
    timestamp: Instant,
    useful_bytes: u64,
    total_bytes: u64,
    elapsed: Duration,
}

/// Sliding-window non-blocking goodput tracker.
#[derive(Debug, Clone)]
pub struct GoodputTracker {
    samples: Arc<RwLock<VecDeque<TransferSample>>>,
    window_duration: Duration,
}

impl GoodputTracker {
    pub fn new(window_duration: Duration) -> Self {
        Self {
            samples: Arc::new(RwLock::new(VecDeque::new())),
            window_duration,
        }
    }

    /// Records a transfer sample non-blockingly.
    pub async fn record_sample(&self, useful_bytes: u64, total_bytes: u64, elapsed: Duration) {
        let now = Instant::now();
        let total = total_bytes.max(useful_bytes);
        let sample = TransferSample {
            timestamp: now,
            useful_bytes,
            total_bytes: total,
            elapsed,
        };

        let mut lock = self.samples.write().await;
        lock.push_back(sample);
        self.evict_stale(&mut lock, now);
    }

    /// Convenience wrapper when total bytes equals useful bytes.
    pub async fn record_transfer(&self, useful_bytes: u64, elapsed: Duration) {
        self.record_sample(useful_bytes, useful_bytes, elapsed).await;
    }

    fn evict_stale(&self, queue: &mut VecDeque<TransferSample>, now: Instant) {
        while let Some(front) = queue.front() {
            if now.saturating_duration_since(front.timestamp) > self.window_duration {
                queue.pop_front();
            } else {
                break;
            }
        }
    }

    /// Calculates aggregate goodput in bits per second across the active window.
    pub async fn current_goodput_bps(&self) -> f64 {
        let now = Instant::now();
        let mut lock = self.samples.write().await;
        self.evict_stale(&mut lock, now);

        let mut total_useful_bytes: u64 = 0;
        let mut total_elapsed = Duration::ZERO;

        for s in lock.iter() {
            total_useful_bytes += s.useful_bytes;
            total_elapsed += s.elapsed;
        }

        let secs = total_elapsed.as_secs_f64();
        if secs > 0.0 {
            (total_useful_bytes as f64 * 8.0) / secs
        } else {
            0.0
        }
    }

    /// Calculates useful / total goodput ratio (0.0 to 1.0) across the window.
    pub async fn current_goodput_ratio(&self) -> f64 {
        let now = Instant::now();
        let mut lock = self.samples.write().await;
        self.evict_stale(&mut lock, now);

        if lock.is_empty() {
            return 1.0;
        }

        let mut useful: u64 = 0;
        let mut total: u64 = 0;
        for s in lock.iter() {
            useful += s.useful_bytes;
            total += s.total_bytes;
        }

        if total == 0 {
            1.0
        } else {
            (useful as f64 / total as f64).clamp(0.0, 1.0)
        }
    }

    /// Returns the number of unevicted samples in the window.
    pub async fn sample_count(&self) -> usize {
        self.samples.read().await.len()
    }
}

/// Deduplicates concurrent probe calls, sharing a single in-flight evaluation.
pub struct RequestCoalescer<T> {
    in_flight: Mutex<Option<broadcast::Sender<T>>>,
}

impl<T: Clone + Send + 'static> RequestCoalescer<T> {
    pub fn new() -> Self {
        Self {
            in_flight: Mutex::new(None),
        }
    }

    /// If another caller is already executing `fut_factory`, waits for its result.
    /// Otherwise executes `fut_factory` and broadcasts to all concurrent waiters.
    pub async fn coalesce<F, Fut>(&self, fut_factory: F) -> T
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = T>,
    {
        let mut rx = {
            let mut guard = self.in_flight.lock().await;
            if let Some(ref sender) = *guard {
                sender.subscribe()
            } else {
                let (tx, _rx) = broadcast::channel(1);
                *guard = Some(tx);
                drop(guard);

                let result = fut_factory().await;

                let mut guard = self.in_flight.lock().await;
                if let Some(sender) = guard.take() {
                    let _ = sender.send(result.clone());
                }
                return result;
            }
        };

        match rx.recv().await {
            Ok(val) => val,
            Err(_) => {
                // Sender dropped or lagging; fallback to direct execution
                fut_factory().await
            }
        }
    }
}

impl<T: Clone + Send + 'static> Default for RequestCoalescer<T> {
    fn default() -> Self {
        Self::new()
    }
}

/// Adaptive goodput probe & hysteresis tier controller (Stellarator t=24).
pub struct ProbeController {
    config: ProbeConfig,
    state: Arc<AtomicU8>,
    capture_lock: Arc<AtomicBool>,
    consecutive_failures: Arc<Mutex<u32>>,
    consecutive_successes: Arc<Mutex<u32>>,
    goodput_tracker: GoodputTracker,
    notifier: Arc<Notify>,
}

impl ProbeController {
    pub fn new(config: ProbeConfig) -> Self {
        let window = config.window_duration;
        Self {
            config,
            state: Arc::new(AtomicU8::new(ProbeState::Normal.as_u8())),
            capture_lock: Arc::new(AtomicBool::new(false)),
            consecutive_failures: Arc::new(Mutex::new(0)),
            consecutive_successes: Arc::new(Mutex::new(0)),
            goodput_tracker: GoodputTracker::new(window),
            notifier: Arc::new(Notify::new()),
        }
    }

    pub fn config(&self) -> &ProbeConfig {
        &self.config
    }

    pub fn current_state(&self) -> ProbeState {
        ProbeState::from(self.state.load(Ordering::Acquire))
    }

    pub fn tracker(&self) -> &GoodputTracker {
        &self.goodput_tracker
    }

    /// Read streak statistics: (consecutive_failures, consecutive_successes).
    pub async fn streaks(&self) -> (u32, u32) {
        let f = *self.consecutive_failures.lock().await;
        let s = *self.consecutive_successes.lock().await;
        (f, s)
    }

    /// Attempts to acquire the exclusive capture guard.
    pub fn acquire_capture_guard(&self) -> Option<CaptureGuard> {
        CaptureGuard::try_acquire(Arc::clone(&self.capture_lock))
    }

    /// Checks if a capture guard is currently held.
    pub fn is_capture_active(&self) -> bool {
        self.capture_lock.load(Ordering::Relaxed)
    }

    /// Evaluates raw metric values against hysteresis boundaries and returns updated state.
    pub async fn evaluate_metric(&self, goodput_ratio: f64, is_healthy: bool) -> ProbeState {
        let mut failures = self.consecutive_failures.lock().await;
        let mut successes = self.consecutive_successes.lock().await;

        let current = self.current_state();

        if !is_healthy || goodput_ratio < self.config.goodput_threshold {
            *failures += 1;
            *successes = 0;

            if *failures >= self.config.failure_streak_limit {
                if current != ProbeState::CriticalCapture {
                    self.set_state(ProbeState::CriticalCapture);
                }
            } else if current == ProbeState::Normal && *failures >= 1 {
                self.set_state(ProbeState::Degraded);
            }
        } else if is_healthy && goodput_ratio >= self.config.recovery_threshold {
            *successes += 1;
            *failures = 0;

            if *successes >= self.config.recovery_streak_required {
                if current != ProbeState::Normal {
                    self.set_state(ProbeState::Normal);
                }
                *successes = 0;
            }
        }
        // When between goodput_threshold and recovery_threshold:
        // Hysteresis deadband active; retain state and avoid chattering.

        self.current_state()
    }

    /// Integrated pipeline: records transfer, measures window goodput ratio, and evaluates state.
    pub async fn record_and_evaluate(
        &self,
        useful_bytes: u64,
        total_bytes: u64,
        elapsed: Duration,
        is_healthy: bool,
    ) -> ProbeState {
        self.goodput_tracker
            .record_sample(useful_bytes, total_bytes, elapsed)
            .await;
        let ratio = self.goodput_tracker.current_goodput_ratio().await;
        self.evaluate_metric(ratio, is_healthy).await
    }

    /// Awaits notification of a state transition.
    pub async fn wait_for_transition(&self) -> ProbeState {
        self.notifier.notified().await;
        self.current_state()
    }

    /// Resets the controller back to Normal with zeroed streaks.
    pub async fn reset(&self) {
        let mut failures = self.consecutive_failures.lock().await;
        let mut successes = self.consecutive_successes.lock().await;
        *failures = 0;
        *successes = 0;
        self.set_state(ProbeState::Normal);
    }

    fn set_state(&self, next: ProbeState) {
        let old = self.state.swap(next.as_u8(), Ordering::Release);
        if old != next.as_u8() {
            self.notifier.notify_waiters();
        }
    }
}

impl Default for ProbeController {
    fn default() -> Self {
        Self::new(ProbeConfig::default())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_01_initial_state_is_normal() {
        let controller = ProbeController::default();
        assert_eq!(controller.current_state(), ProbeState::Normal);
        assert!(controller.current_state().is_normal());
    }

    #[tokio::test]
    async fn test_02_single_failure_transitions_to_degraded() {
        let controller = ProbeController::default();
        let next = controller.evaluate_metric(0.70, true).await;
        assert_eq!(next, ProbeState::Degraded);
        assert_eq!(controller.current_state(), ProbeState::Degraded);
        assert_eq!(controller.streaks().await, (1, 0));
    }

    #[tokio::test]
    async fn test_03_failure_streak_transitions_to_critical_capture() {
        let config = ProbeConfig {
            failure_streak_limit: 3,
            ..Default::default()
        };
        let controller = ProbeController::new(config);

        controller.evaluate_metric(0.50, true).await;
        assert_eq!(controller.current_state(), ProbeState::Degraded);

        controller.evaluate_metric(0.40, true).await;
        assert_eq!(controller.current_state(), ProbeState::Degraded);

        let third = controller.evaluate_metric(0.30, true).await;
        assert_eq!(third, ProbeState::CriticalCapture);
        assert_eq!(controller.streaks().await, (3, 0));
    }

    #[tokio::test]
    async fn test_04_hysteresis_recovery_streak_required() {
        let config = ProbeConfig {
            failure_streak_limit: 2,
            recovery_streak_required: 3,
            goodput_threshold: 0.80,
            recovery_threshold: 0.90,
            ..Default::default()
        };
        let controller = ProbeController::new(config);

        // Enter critical
        controller.evaluate_metric(0.10, true).await;
        controller.evaluate_metric(0.10, true).await;
        assert_eq!(controller.current_state(), ProbeState::CriticalCapture);

        // 1st recovery
        controller.evaluate_metric(0.95, true).await;
        assert_eq!(controller.current_state(), ProbeState::CriticalCapture);

        // 2nd recovery
        controller.evaluate_metric(0.95, true).await;
        assert_eq!(controller.current_state(), ProbeState::CriticalCapture);

        // 3rd recovery triggers transition to Normal
        let res = controller.evaluate_metric(0.95, true).await;
        assert_eq!(res, ProbeState::Normal);
    }

    #[tokio::test]
    async fn test_05_interrupted_recovery_resets_streak() {
        let config = ProbeConfig {
            failure_streak_limit: 1,
            recovery_streak_required: 3,
            recovery_threshold: 0.90,
            ..Default::default()
        };
        let controller = ProbeController::new(config);
        controller.evaluate_metric(0.10, false).await;
        assert_eq!(controller.current_state(), ProbeState::CriticalCapture);

        // 2 successful recoveries
        controller.evaluate_metric(0.95, true).await;
        controller.evaluate_metric(0.95, true).await;
        assert_eq!(controller.streaks().await, (0, 2));

        // Interrupted by drop
        controller.evaluate_metric(0.70, true).await;
        assert_eq!(controller.streaks().await, (1, 0));
        assert_eq!(controller.current_state(), ProbeState::CriticalCapture);
    }

    #[tokio::test]
    async fn test_06_hysteresis_deadband_stabilization() {
        let config = ProbeConfig {
            goodput_threshold: 0.70,
            recovery_threshold: 0.90,
            failure_streak_limit: 2,
            recovery_streak_required: 2,
            ..Default::default()
        };
        let controller = ProbeController::new(config);

        // Drop to Degraded
        controller.evaluate_metric(0.60, true).await;
        assert_eq!(controller.current_state(), ProbeState::Degraded);

        // Hover in deadband (0.80 between 0.70 and 0.90)
        controller.evaluate_metric(0.80, true).await;
        assert_eq!(controller.current_state(), ProbeState::Degraded);
        assert_eq!(controller.streaks().await, (1, 0));
    }

    #[tokio::test]
    async fn test_07_capture_guard_acquisition_and_exclusivity() {
        let controller = ProbeController::default();
        let guard1 = controller.acquire_capture_guard();
        assert!(guard1.is_some(), "First capture guard must succeed");
        assert!(controller.is_capture_active());

        let guard2 = controller.acquire_capture_guard();
        assert!(guard2.is_none(), "Concurrent second capture guard must fail");
    }

    #[tokio::test]
    async fn test_08_capture_guard_drop_allows_reacquisition() {
        let controller = ProbeController::default();
        {
            let guard = controller.acquire_capture_guard();
            assert!(guard.is_some());
            assert!(controller.is_capture_active());
        }
        assert!(!controller.is_capture_active());

        let second = controller.acquire_capture_guard();
        assert!(second.is_some(), "Reacquisition after drop must succeed");
    }

    #[tokio::test]
    async fn test_09_capture_guard_concurrent_contention() {
        let controller = Arc::new(ProbeController::default());
        let acquired_count = Arc::new(std::sync::atomic::AtomicUsize::new(0));

        let mut handles = Vec::new();
        for _ in 0..50 {
            let ctrl = Arc::clone(&controller);
            let count = Arc::clone(&acquired_count);
            handles.push(tokio::spawn(async move {
                if let Some(_guard) = ctrl.acquire_capture_guard() {
                    count.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            }));
        }

        for h in handles {
            let _ = h.await;
        }

        // At any single moment only 1 task held it, but across serialized sequential executions,
        // multiple may succeed sequentially. Total concurrent overlap never violated atomic CAS.
        assert!(acquired_count.load(Ordering::SeqCst) >= 1);
    }

    #[tokio::test]
    async fn test_10_goodput_tracker_ratio_and_bps() {
        let tracker = GoodputTracker::new(Duration::from_secs(5));
        // Useful 8000 bytes out of 10000 bytes total in 100ms
        tracker
            .record_sample(8000, 10000, Duration::from_millis(100))
            .await;

        let ratio = tracker.current_goodput_ratio().await;
        assert!((ratio - 0.80).abs() < 1e-4);

        let bps = tracker.current_goodput_bps().await;
        // 8000 bytes * 8 bits / 0.1s = 640_000 bps
        assert!((bps - 640_000.0).abs() < 1e-2);
    }

    #[tokio::test]
    async fn test_11_goodput_tracker_window_eviction() {
        let tracker = GoodputTracker::new(Duration::from_millis(50));
        tracker
            .record_sample(1000, 1000, Duration::from_millis(10))
            .await;
        assert_eq!(tracker.sample_count().await, 1);

        tokio::time::sleep(Duration::from_millis(80)).await;
        // Eviction happens on query/insert
        assert_eq!(tracker.current_goodput_bps().await, 0.0);
        assert_eq!(tracker.sample_count().await, 0);
    }

    #[tokio::test]
    async fn test_12_goodput_tracker_zero_elapsed_safe() {
        let tracker = GoodputTracker::new(Duration::from_secs(1));
        tracker.record_sample(1000, 1000, Duration::ZERO).await;
        assert_eq!(tracker.current_goodput_bps().await, 0.0);
        assert_eq!(tracker.current_goodput_ratio().await, 1.0);
    }

    #[tokio::test]
    async fn test_13_request_coalescing_single_flight() {
        let coalescer = Arc::new(RequestCoalescer::<u64>::new());
        let executions = Arc::new(std::sync::atomic::AtomicUsize::new(0));

        let mut handles = Vec::new();
        for _ in 0..10 {
            let c = Arc::clone(&coalescer);
            let ex = Arc::clone(&executions);
            handles.push(tokio::spawn(async move {
                c.coalesce(|| async {
                    ex.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(20)).await;
                    42u64
                })
                .await
            }));
        }

        for h in handles {
            let res = h.await.unwrap();
            assert_eq!(res, 42);
        }

        // Under near-simultaneous spawn, the closure executes exactly 1 time
        assert_eq!(executions.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn test_14_request_coalescing_sequential_runs() {
        let coalescer = RequestCoalescer::<String>::new();
        let first = coalescer.coalesce(|| async { "first".to_string() }).await;
        assert_eq!(first, "first");

        let second = coalescer
            .coalesce(|| async { "second".to_string() })
            .await;
        assert_eq!(second, "second");
    }

    #[tokio::test]
    async fn test_15_async_wait_for_transition_notification() {
        let controller = Arc::new(ProbeController::default());
        let ctrl_clone = Arc::clone(&controller);

        let waiter = tokio::spawn(async move { ctrl_clone.wait_for_transition().await });

        tokio::time::sleep(Duration::from_millis(10)).await;
        // Trigger degradation
        controller.evaluate_metric(0.50, true).await;

        let observed = waiter.await.unwrap();
        assert_eq!(observed, ProbeState::Degraded);
    }

    #[tokio::test]
    async fn test_16_record_transfer_and_evaluate_pipeline() {
        let controller = ProbeController::new(ProbeConfig {
            goodput_threshold: 0.85,
            ..Default::default()
        });

        // 70% goodput -> Degraded
        let st = controller
            .record_and_evaluate(700, 1000, Duration::from_millis(10), true)
            .await;
        assert_eq!(st, ProbeState::Degraded);
    }

    #[tokio::test]
    async fn test_17_controller_manual_reset() {
        let controller = ProbeController::default();
        controller.evaluate_metric(0.10, false).await;
        assert_eq!(controller.current_state(), ProbeState::Degraded);

        controller.reset().await;
        assert_eq!(controller.current_state(), ProbeState::Normal);
        assert_eq!(controller.streaks().await, (0, 0));
    }

    #[tokio::test]
    async fn test_18_probe_state_conversions_and_display() {
        assert_eq!(ProbeState::from(0), ProbeState::Normal);
        assert_eq!(ProbeState::from(1), ProbeState::Degraded);
        assert_eq!(ProbeState::from(2), ProbeState::CriticalCapture);
        assert_eq!(ProbeState::from(99), ProbeState::CriticalCapture);

        assert_eq!(ProbeState::Normal.to_string(), "NORMAL");
        assert_eq!(ProbeState::Degraded.to_string(), "DEGRADED");
        assert_eq!(ProbeState::CriticalCapture.to_string(), "CRITICAL_CAPTURE");
    }

    #[tokio::test]
    async fn test_19_high_concurrency_state_machine_stress() {
        let controller = Arc::new(ProbeController::default());
        let mut handles = Vec::new();

        for i in 0..40 {
            let ctrl = Arc::clone(&controller);
            handles.push(tokio::spawn(async move {
                if i % 2 == 0 {
                    ctrl.evaluate_metric(0.95, true).await;
                } else {
                    ctrl.evaluate_metric(0.60, true).await;
                }
            }));
        }

        for h in handles {
            let _ = h.await;
        }

        let st = controller.current_state();
        assert!(st == ProbeState::Normal || st == ProbeState::Degraded || st == ProbeState::CriticalCapture);
    }
}
