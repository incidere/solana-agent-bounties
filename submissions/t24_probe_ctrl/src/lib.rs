//! # Non-Blocking Goodput Probe & Hysteresis Tier Controller (`probe_ctrl`)
//!
//! A clean-room, zero-external-dependency (tokio + std only) Rust library for
//! adaptive capture quality tier selection driven by background uplink goodput probes.
//!
//! ## Core Invariants
//! 1. **Strict Non-Reentrancy**: Probes never execute while `active_capture` is held.
//! 2. **In-Flight Coalescing**: Concurrent probe triggers coalesce into the single
//!    in-flight probe task without spawning duplicate network work.
//! 3. **Bounded Chunk Deadlines & Cancellation Safety**: Each probe chunk has a strict
//!    cancellation deadline. Controller slots are freed cleanly even under panic or task cancellation.
//! 4. **Asymmetric Hysteresis & Dwell Timer**: Dual upward/downward thresholds and a minimum
//!    dwell duration prevent rapid tier flapping near boundaries.
//! 5. **Failure Taxonomy**: Distinguishes DNS/TCP unreachable failures (`Offline`) from slow or
//!    timed-out transfers (`Degraded(Low)`).

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::sync::{watch, Mutex};
use tokio::time::{timeout, Duration, Instant};

/// Adaptive capture quality tiers ordered from lowest to highest fidelity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Tier {
    Low,
    Medium,
    High,
    Ultra,
}

impl Tier {
    pub const ALL: [Tier; 4] = [Tier::Low, Tier::Medium, Tier::High, Tier::Ultra];

    pub fn as_str(&self) -> &'static str {
        match self {
            Tier::Low => "Low",
            Tier::Medium => "Medium",
            Tier::High => "High",
            Tier::Ultra => "Ultra",
        }
    }
}

/// Link & probe controller lifecycle state machine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeState {
    Idle,
    Probing,
    Healthy(Tier),
    Degraded(Tier),
    Offline,
}

impl ProbeState {
    pub fn current_tier(&self) -> Option<Tier> {
        match self {
            ProbeState::Healthy(t) | ProbeState::Degraded(t) => Some(*t),
            _ => None,
        }
    }

    pub fn is_online(&self) -> bool {
        !matches!(self, ProbeState::Offline)
    }
}

/// Failure classification isolating DNS/TCP connection drops from slow transfers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportError {
    /// DNS lookup failure, connection refused, or connect timeout.
    DnsOrTcpFailure,
    /// Transfer of a single chunk exceeded the bounded deadline.
    ChunkTimeout,
    /// Read/Write I/O error during chunk payload transmission.
    IoError(String),
    /// Probe aborted because high-priority active capture commenced.
    AbortedByCapture,
}

impl std::fmt::Display for TransportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TransportError::DnsOrTcpFailure => write!(f, "DNS resolution or TCP connection failure"),
            TransportError::ChunkTimeout => write!(f, "Chunk transfer deadline exceeded"),
            TransportError::IoError(msg) => write!(f, "I/O error: {}", msg),
            TransportError::AbortedByCapture => write!(f, "Probe aborted due to active capture preemption"),
        }
    }
}

impl std::error::Error for TransportError {}

/// Result returned to callers triggering a goodput probe.
#[derive(Debug, Clone, PartialEq)]
pub enum TriggerOutcome {
    /// The caller initiated and executed the probe to completion.
    Executed(ProbeState, Option<f64>),
    /// The caller attached to an already in-flight probe and received the coalesced outcome.
    Coalesced(ProbeState, Option<f64>),
    /// The probe was suppressed because an active capture is currently running.
    SuppressedActiveCapture(ProbeState),
}

impl TriggerOutcome {
    pub fn state(&self) -> ProbeState {
        match self {
            TriggerOutcome::Executed(s, _) => *s,
            TriggerOutcome::Coalesced(s, _) => *s,
            TriggerOutcome::SuppressedActiveCapture(s) => *s,
        }
    }

    pub fn kbps(&self) -> Option<f64> {
        match self {
            TriggerOutcome::Executed(_, k) => *k,
            TriggerOutcome::Coalesced(_, k) => *k,
            TriggerOutcome::SuppressedActiveCapture(_) => None,
        }
    }
}

/// Asymmetric hysteresis threshold bounds between adjacent tiers.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TierThresholds {
    /// kbps required to upgrade from Low -> Medium.
    pub low_to_medium: f64,
    /// kbps below which Medium downgrades to Low.
    pub medium_to_low: f64,
    /// kbps required to upgrade from Medium -> High.
    pub medium_to_high: f64,
    /// kbps below which High downgrades to Medium.
    pub high_to_medium: f64,
    /// kbps required to upgrade from High -> Ultra.
    pub high_to_ultra: f64,
    /// kbps below which Ultra downgrades to High.
    pub ultra_to_high: f64,
}

impl Default for TierThresholds {
    fn default() -> Self {
        Self {
            low_to_medium: 1200.0,
            medium_to_low: 800.0,
            medium_to_high: 3200.0,
            high_to_medium: 2200.0,
            high_to_ultra: 6800.0,
            ultra_to_high: 4800.0,
        }
    }
}

impl TierThresholds {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.low_to_medium <= self.medium_to_low {
            return Err("low_to_medium upgrade threshold must be strictly greater than medium_to_low downgrade threshold");
        }
        if self.medium_to_high <= self.high_to_medium {
            return Err("medium_to_high upgrade threshold must be strictly greater than high_to_medium downgrade threshold");
        }
        if self.high_to_ultra <= self.ultra_to_high {
            return Err("high_to_ultra upgrade threshold must be strictly greater than ultra_to_high downgrade threshold");
        }
        if self.high_to_medium <= self.low_to_medium {
            return Err("adjacent tier hysteresis bands must be monotonic");
        }
        if self.ultra_to_high <= self.medium_to_high {
            return Err("adjacent tier hysteresis bands must be monotonic");
        }
        Ok(())
    }
}

/// Comprehensive probe controller configuration.
#[derive(Debug, Clone)]
pub struct ProbeConfig {
    /// Maximum time allowed to establish connection.
    pub connect_timeout: Duration,
    /// Deadline per individual chunk transfer.
    pub chunk_deadline: Duration,
    /// Number of chunks fetched per probe session.
    pub num_chunks: usize,
    /// Target byte size per chunk.
    pub chunk_target_bytes: usize,
    /// Exponential Weighted Moving Average smoothing coefficient (0.0 < alpha <= 1.0).
    pub ewma_alpha: f64,
    /// Minimum duration to dwell in a tier before another tier switch is permitted.
    pub min_dwell: Duration,
    /// Hysteresis bandwidth thresholds.
    pub thresholds: TierThresholds,
    /// Initial default tier before any measurements.
    pub initial_tier: Tier,
}

impl Default for ProbeConfig {
    fn default() -> Self {
        Self {
            connect_timeout: Duration::from_millis(1500),
            chunk_deadline: Duration::from_millis(1000),
            num_chunks: 4,
            chunk_target_bytes: 65536,
            ewma_alpha: 0.35,
            min_dwell: Duration::from_secs(3),
            thresholds: TierThresholds::default(),
            initial_tier: Tier::Medium,
        }
    }
}

impl ProbeConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.connect_timeout.is_zero() {
            return Err("connect_timeout must be positive");
        }
        if self.chunk_deadline.is_zero() {
            return Err("chunk_deadline must be positive");
        }
        if self.num_chunks == 0 {
            return Err("num_chunks must be at least 1");
        }
        if self.chunk_target_bytes == 0 {
            return Err("chunk_target_bytes must be positive");
        }
        if !(0.0 < self.ewma_alpha && self.ewma_alpha <= 1.0) {
            return Err("ewma_alpha must be in range (0.0, 1.0]");
        }
        self.thresholds.validate()?;
        Ok(())
    }
}

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Abstraction for goodput probe network transports (supports mocks and real sockets).
pub trait ProbeTransport: Send + Sync {
    /// Establish probe connection to uplink peer.
    fn connect(&self) -> BoxFuture<'_, Result<(), TransportError>>;

    /// Fetch a single chunk of target size, returning actual bytes transferred.
    fn fetch_chunk(&self, chunk_idx: usize, target_bytes: usize) -> BoxFuture<'_, Result<usize, TransportError>>;
}

/// RAII Guard that suppresses background probes while active capture is held.
#[derive(Debug)]
pub struct CaptureGuard {
    active_counter: Arc<AtomicUsize>,
}

impl CaptureGuard {
    fn new(counter: Arc<AtomicUsize>) -> Self {
        counter.fetch_add(1, Ordering::SeqCst);
        Self { active_counter: counter }
    }
}

impl Drop for CaptureGuard {
    fn drop(&mut self) {
        self.active_counter.fetch_sub(1, Ordering::SeqCst);
    }
}

struct InnerState {
    state: ProbeState,
    current_tier: Tier,
    ewma_kbps: Option<f64>,
    last_tier_change: Option<Instant>,
    last_probe_time: Option<Instant>,
}

/// In-flight probe slot guard ensuring state cleanup under cancellation or panic.
struct InFlightGuard {
    flag: Arc<AtomicBool>,
}

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        self.flag.store(false, Ordering::SeqCst);
    }
}

/// Non-blocking, coalescing link goodput probe and adaptive capture tier controller.
pub struct ProbeController<T: ProbeTransport> {
    transport: Arc<T>,
    config: ProbeConfig,
    active_captures: Arc<AtomicUsize>,
    is_probing: Arc<AtomicBool>,
    inner: Mutex<InnerState>,
    coalesce_tx: watch::Sender<Option<(ProbeState, Option<f64>)>>,
    coalesce_rx: watch::Receiver<Option<(ProbeState, Option<f64>)>>,
}

impl<T: ProbeTransport> ProbeController<T> {
    /// Create a new controller instance with validated configuration.
    pub fn new(transport: Arc<T>, config: ProbeConfig) -> Result<Self, &'static str> {
        config.validate()?;
        let initial_tier = config.initial_tier;
        let (tx, rx) = watch::channel(None);
        
        Ok(Self {
            transport,
            config,
            active_captures: Arc::new(AtomicUsize::new(0)),
            is_probing: Arc::new(AtomicBool::new(false)),
            inner: Mutex::new(InnerState {
                state: ProbeState::Idle,
                current_tier: initial_tier,
                ewma_kbps: None,
                last_tier_change: None,
                last_probe_time: None,
            }),
            coalesce_tx: tx,
            coalesce_rx: rx,
        })
    }

    /// Acquire an active capture guard. While any guard is alive, probes are strictly suppressed.
    pub fn acquire_capture(&self) -> CaptureGuard {
        CaptureGuard::new(self.active_captures.clone())
    }

    /// Check if active capture is currently running.
    pub fn is_capture_active(&self) -> bool {
        self.active_captures.load(Ordering::Relaxed) > 0
    }

    /// Read current snapshot of controller state.
    pub async fn current_state(&self) -> ProbeState {
        self.inner.lock().await.state
    }

    /// Read current capture quality tier.
    pub async fn current_tier(&self) -> Tier {
        self.inner.lock().await.current_tier
    }

    /// Read smoothed EWMA goodput in kbps, if available.
    pub async fn smoothed_kbps(&self) -> Option<f64> {
        self.inner.lock().await.ewma_kbps
    }

    /// Trigger a link-goodput probe. If capture is active, suppressed immediately.
    /// If another probe is already running, coalesces into the in-flight probe.
    pub async fn trigger_probe(&self) -> TriggerOutcome {
        // Invariant 1: Non-reentrancy against active capture
        if self.is_capture_active() {
            let state = self.inner.lock().await.state;
            return TriggerOutcome::SuppressedActiveCapture(state);
        }

        // Invariant 2: In-flight probe coalescing
        // Try to become the probe leader
        if self.is_probing.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
            // Already probing: coalesce caller into the current in-flight execution
            let mut rx = self.coalesce_rx.clone();
            loop {
                if let Err(_) = rx.changed().await {
                    let state = self.inner.lock().await.state;
                    return TriggerOutcome::Coalesced(state, None);
                }
                if let Some((st, kbps)) = *rx.borrow() {
                    return TriggerOutcome::Coalesced(st, kbps);
                }
            }
        }

        // We are the probe leader; RAII guard ensures flag is released even on cancel/panic
        let _guard = InFlightGuard { flag: self.is_probing.clone() };

        // Double check active capture before starting network operations
        if self.is_capture_active() {
            let state = self.inner.lock().await.state;
            let _ = self.coalesce_tx.send(Some((state, None)));
            return TriggerOutcome::SuppressedActiveCapture(state);
        }

        // Update state to Probing
        {
            let mut st = self.inner.lock().await;
            st.state = ProbeState::Probing;
        }

        // Execute bounded probe session
        let (new_state, measured_kbps) = self.execute_probe_session().await;

        // Apply state transition & asymmetric hysteresis
        let final_state = self.apply_measurement(new_state, measured_kbps).await;

        // Broadcast to all coalesced waiting callers
        let _ = self.coalesce_tx.send(Some((final_state, measured_kbps)));

        TriggerOutcome::Executed(final_state, measured_kbps)
    }

    async fn execute_probe_session(&self) -> (Result<(), TransportError>, Option<f64>) {
        // Step 1: Connect with bounded connect_timeout
        let connect_fut = self.transport.connect();
        let connect_res = match timeout(self.config.connect_timeout, connect_fut).await {
            Ok(res) => res,
            Err(_) => Err(TransportError::DnsOrTcpFailure),
        };

        if let Err(e) = connect_res {
            return (Err(e), None);
        }

        // Step 2: Fetch chunks with per-chunk deadline & mid-probe capture cancellation check
        let mut total_bytes: usize = 0;
        let mut total_duration = Duration::ZERO;

        for chunk_idx in 0..self.config.num_chunks {
            // Mid-probe preemption: if capture started, abort probe immediately
            if self.is_capture_active() {
                return (Err(TransportError::AbortedByCapture), None);
            }

            let start = Instant::now();
            let chunk_fut = self.transport.fetch_chunk(chunk_idx, self.config.chunk_target_bytes);
            
            let chunk_res = match timeout(self.config.chunk_deadline, chunk_fut).await {
                Ok(res) => res,
                Err(_) => Err(TransportError::ChunkTimeout),
            };

            match chunk_res {
                Ok(bytes) => {
                    total_bytes += bytes;
                    total_duration += start.elapsed();
                }
                Err(err) => {
                    return (Err(err), None);
                }
            }
        }

        let elapsed_secs = total_duration.as_secs_f64();
        if elapsed_secs > 0.0 && total_bytes > 0 {
            let bits = (total_bytes as f64) * 8.0;
            let kbps = (bits / elapsed_secs) / 1000.0;
            (Ok(()), Some(kbps))
        } else {
            (Err(TransportError::ChunkTimeout), None)
        }
    }

    async fn apply_measurement(&self, res: Result<(), TransportError>, sample_kbps: Option<f64>) -> ProbeState {
        let mut inner = self.inner.lock().await;
        let now = Instant::now();
        inner.last_probe_time = Some(now);

        match res {
            Err(TransportError::DnsOrTcpFailure) => {
                inner.state = ProbeState::Offline;
                ProbeState::Offline
            }
            Err(TransportError::ChunkTimeout) | Err(TransportError::IoError(_)) => {
                // Chunk deadline exceeded or slow transmission: degrades link, does not claim complete offline
                inner.current_tier = Tier::Low;
                inner.last_tier_change = Some(now);
                inner.state = ProbeState::Degraded(Tier::Low);
                ProbeState::Degraded(Tier::Low)
            }
            Err(TransportError::AbortedByCapture) => {
                // Return previous state without updating EWMA
                inner.state
            }
            Ok(()) => {
                if let Some(sample) = sample_kbps {
                    if sample.is_finite() && sample >= 0.0 {
                        let is_first = inner.ewma_kbps.is_none();
                        let new_ewma = match inner.ewma_kbps {
                            Some(prev) => self.config.ewma_alpha * sample + (1.0 - self.config.ewma_alpha) * prev,
                            None => sample,
                        };
                        inner.ewma_kbps = Some(new_ewma);

                        // Asymmetric Hysteresis Tier Selection
                        let current = inner.current_tier;
                        let dwell_elapsed = match inner.last_tier_change {
                            Some(last) => now.duration_since(last) >= self.config.min_dwell,
                            None => true, // First measurement always selects proper tier
                        };

                        if is_first || dwell_elapsed {
                            let next_tier = self.evaluate_tier_hysteresis(current, new_ewma);
                            if next_tier != current || is_first {
                                inner.current_tier = next_tier;
                                inner.last_tier_change = Some(now);
                            }
                        }

                        let target_tier = inner.current_tier;
                        let state = ProbeState::Healthy(target_tier);
                        inner.state = state;
                        state
                    } else {
                        inner.state
                    }
                } else {
                    inner.state
                }
            }
        }
    }

    pub fn evaluate_tier_hysteresis(&self, current: Tier, kbps: f64) -> Tier {
        let th = &self.config.thresholds;
        match current {
            Tier::Low => {
                if kbps >= th.high_to_ultra {
                    Tier::Ultra
                } else if kbps >= th.medium_to_high {
                    Tier::High
                } else if kbps >= th.low_to_medium {
                    Tier::Medium
                } else {
                    Tier::Low
                }
            }
            Tier::Medium => {
                if kbps <= th.medium_to_low {
                    Tier::Low
                } else if kbps >= th.high_to_ultra {
                    Tier::Ultra
                } else if kbps >= th.medium_to_high {
                    Tier::High
                } else {
                    Tier::Medium
                }
            }
            Tier::High => {
                if kbps <= th.medium_to_low {
                    Tier::Low
                } else if kbps <= th.high_to_medium {
                    Tier::Medium
                } else if kbps >= th.high_to_ultra {
                    Tier::Ultra
                } else {
                    Tier::High
                }
            }
            Tier::Ultra => {
                if kbps <= th.medium_to_low {
                    Tier::Low
                } else if kbps <= th.high_to_medium {
                    Tier::Medium
                } else if kbps <= th.ultra_to_high {
                    Tier::High
                } else {
                    Tier::Ultra
                }
            }
        }
    }
}

// ============================================================================
// UNIT & DETERMINISTIC TOKIO INTEGRATION TESTS
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpStream};
    use tokio::sync::Notify;

    struct MockTransport {
        connect_err: Option<TransportError>,
        chunk_bytes: usize,
        chunk_delay: Duration,
        connect_calls: AtomicUsize,
        gate: Option<Arc<Notify>>,
    }

    impl MockTransport {
        fn healthy(kbps: f64, chunk_delay: Duration) -> Self {
            let secs = chunk_delay.as_secs_f64().max(0.0001);
            let bytes = ((kbps * 1000.0 * secs) / 8.0).round() as usize;
            Self {
                connect_err: None,
                chunk_bytes: bytes.max(1),
                chunk_delay,
                connect_calls: AtomicUsize::new(0),
                gate: None,
            }
        }

        fn with_connect_error(err: TransportError) -> Self {
            Self {
                connect_err: Some(err),
                chunk_bytes: 1024,
                chunk_delay: Duration::from_millis(10),
                connect_calls: AtomicUsize::new(0),
                gate: None,
            }
        }
    }

    impl ProbeTransport for MockTransport {
        fn connect(&self) -> BoxFuture<'_, Result<(), TransportError>> {
            self.connect_calls.fetch_add(1, Ordering::SeqCst);
            Box::pin(async move {
                if let Some(ref g) = self.gate {
                    g.notified().await;
                }
                match &self.connect_err {
                    Some(e) => Err(e.clone()),
                    None => Ok(()),
                }
            })
        }

        fn fetch_chunk(&self, _chunk_idx: usize, _target_bytes: usize) -> BoxFuture<'_, Result<usize, TransportError>> {
            Box::pin(async move {
                tokio::time::sleep(self.chunk_delay).await;
                Ok(self.chunk_bytes)
            })
        }
    }

    #[test]
    fn test_01_config_validation_rejects_inverted_thresholds() {
        let mut cfg = ProbeConfig::default();
        cfg.thresholds.low_to_medium = 800.0;
        cfg.thresholds.medium_to_low = 1000.0; // Inverted!
        assert!(cfg.validate().is_err());

        let mut cfg_alpha = ProbeConfig::default();
        cfg_alpha.ewma_alpha = 1.5; // Invalid alpha!
        assert!(cfg_alpha.validate().is_err());
    }

    #[tokio::test]
    async fn test_02_active_capture_suppresses_probes() {
        tokio::time::pause();
        let transport = Arc::new(MockTransport::healthy(5000.0, Duration::from_millis(50)));
        let ctrl = ProbeController::new(transport.clone(), ProbeConfig::default()).unwrap();

        // Without capture, probe runs
        let res1 = ctrl.trigger_probe().await;
        assert!(matches!(res1, TriggerOutcome::Executed(..)));
        assert_eq!(transport.connect_calls.load(Ordering::SeqCst), 1);

        // Acquire active capture
        let guard = ctrl.acquire_capture();
        assert!(ctrl.is_capture_active());

        // Probe must be suppressed with zero network activity
        let res2 = ctrl.trigger_probe().await;
        assert!(matches!(res2, TriggerOutcome::SuppressedActiveCapture(..)));
        assert_eq!(transport.connect_calls.load(Ordering::SeqCst), 1); // No new network call

        // Nested capture guard: dropping 1 doesn't clear if another is held
        let guard2 = ctrl.acquire_capture();
        drop(guard);
        assert!(ctrl.is_capture_active());
        let res_nested = ctrl.trigger_probe().await;
        assert!(matches!(res_nested, TriggerOutcome::SuppressedActiveCapture(..)));

        // Drop final guard
        drop(guard2);
        assert!(!ctrl.is_capture_active());

        // Probing resumes
        let res3 = ctrl.trigger_probe().await;
        assert!(matches!(res3, TriggerOutcome::Executed(..)));
        assert_eq!(transport.connect_calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn test_03_concurrent_triggers_coalesce_into_single_inflight_probe() {
        tokio::time::pause();
        let gate = Arc::new(Notify::new());
        let mut transport = MockTransport::healthy(5000.0, Duration::from_millis(50));
        transport.gate = Some(gate.clone());
        let transport = Arc::new(transport);
        let ctrl = Arc::new(ProbeController::new(transport.clone(), ProbeConfig::default()).unwrap());

        // Spawn 4 concurrent trigger tasks
        let c1 = ctrl.clone();
        let c2 = ctrl.clone();
        let c3 = ctrl.clone();
        let c4 = ctrl.clone();

        let t1 = tokio::spawn(async move { c1.trigger_probe().await });
        let t2 = tokio::spawn(async move { c2.trigger_probe().await });
        let t3 = tokio::spawn(async move { c3.trigger_probe().await });
        let t4 = tokio::spawn(async move { c4.trigger_probe().await });

        // Let them all queue up
        tokio::task::yield_now().await;
        // Release gate
        gate.notify_waiters();

        // Advance time for chunk sleep
        tokio::time::advance(Duration::from_millis(500)).await;

        let r1 = t1.await.unwrap();
        let r2 = t2.await.unwrap();
        let r3 = t3.await.unwrap();
        let r4 = t4.await.unwrap();

        // Exactly 1 caller executed, 3 coalesced
        let executed_count = [&r1, &r2, &r3, &r4].iter().filter(|r| matches!(r, TriggerOutcome::Executed(..))).count();
        let coalesced_count = [&r1, &r2, &r3, &r4].iter().filter(|r| matches!(r, TriggerOutcome::Coalesced(..))).count();

        assert_eq!(executed_count, 1);
        assert_eq!(coalesced_count, 3);
        // Transport connect was called exactly once!
        assert_eq!(transport.connect_calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn test_04_separates_dns_tcp_offline_from_slow_uplink() {
        tokio::time::pause();
        
        // 1. Connection failure -> Offline
        let offline_transport = Arc::new(MockTransport::with_connect_error(TransportError::DnsOrTcpFailure));
        let ctrl_offline = ProbeController::new(offline_transport, ProbeConfig::default()).unwrap();
        let res_off = ctrl_offline.trigger_probe().await;
        assert_eq!(res_off.state(), ProbeState::Offline);

        // 2. Slow chunk / Chunk timeout -> Degraded(Low), NOT Offline
        let mut slow_cfg = ProbeConfig::default();
        slow_cfg.chunk_deadline = Duration::from_millis(50);
        let slow_transport = Arc::new(MockTransport::healthy(100.0, Duration::from_millis(200))); // Delay > deadline
        let ctrl_slow = ProbeController::new(slow_transport, slow_cfg).unwrap();
        
        let probe_fut = ctrl_slow.trigger_probe();
        tokio::pin!(probe_fut);
        
        tokio::time::advance(Duration::from_millis(100)).await;
        let res_slow = probe_fut.await;
        assert_eq!(res_slow.state(), ProbeState::Degraded(Tier::Low));
    }

    #[tokio::test]
    async fn test_05_cancellation_safety_releases_probe_slot() {
        tokio::time::pause();
        let gate = Arc::new(Notify::new());
        let mut transport = MockTransport::healthy(5000.0, Duration::from_millis(50));
        transport.gate = Some(gate.clone());
        let transport = Arc::new(transport);
        let ctrl = Arc::new(ProbeController::new(transport.clone(), ProbeConfig::default()).unwrap());

        // Spawn a probe that waits on gate
        let c = ctrl.clone();
        let handle = tokio::spawn(async move {
            c.trigger_probe().await
        });

        tokio::task::yield_now().await;
        // Cancel the task mid-flight
        handle.abort();
        tokio::task::yield_now().await;

        // In-flight slot must be freed: next trigger can run without wedging!
        assert!(!ctrl.is_probing.load(Ordering::SeqCst));
        
        // Release gate for clean mock
        gate.notify_waiters();
        
        let res = ctrl.trigger_probe().await;
        assert!(matches!(res, TriggerOutcome::Executed(..)));
    }

    #[tokio::test]
    async fn test_06_asymmetric_hysteresis_and_min_dwell_prevent_flapping() {
        tokio::time::pause();
        let mut cfg = ProbeConfig::default();
        cfg.min_dwell = Duration::from_secs(4);
        cfg.initial_tier = Tier::Low;

        // High bandwidth transport (8500 kbps -> Ultra)
        let transport = Arc::new(MockTransport::healthy(8500.0, Duration::from_millis(10)));
        let ctrl = ProbeController::new(transport, cfg).unwrap();

        // Probe 1: low -> ultra upgrade (initial probe sets tier based on measured bandwidth)
        let res1 = ctrl.trigger_probe().await;
        assert_eq!(res1.state(), ProbeState::Healthy(Tier::Ultra));
        assert_eq!(ctrl.current_tier().await, Tier::Ultra);

        // Immediate lower sample (4500 kbps): would downgrade to High, but min_dwell (4s) hasn't passed!
        let ctrl_ref = Arc::new(ctrl);
        tokio::time::advance(Duration::from_secs(1)).await; // only 1s passed
        
        // Evaluate hysteresis directly
        assert_eq!(ctrl_ref.evaluate_tier_hysteresis(Tier::Ultra, 4500.0), Tier::High);
        
        // After 4.5 seconds (> min_dwell), tier switch is permitted
        tokio::time::advance(Duration::from_secs(4)).await;
        assert_eq!(ctrl_ref.evaluate_tier_hysteresis(Tier::Ultra, 4500.0), Tier::High);
        // Hysteresis boundary: 4900 kbps (above ultra_to_high 4800) stays Ultra
        assert_eq!(ctrl_ref.evaluate_tier_hysteresis(Tier::Ultra, 4900.0), Tier::Ultra);
    }

    #[tokio::test]
    async fn test_07_mid_probe_capture_preemption() {
        tokio::time::pause();
        let transport = Arc::new(MockTransport::healthy(5000.0, Duration::from_millis(50)));
        let ctrl = Arc::new(ProbeController::new(transport, ProbeConfig::default()).unwrap());

        let c = ctrl.clone();
        let probe_task = tokio::spawn(async move {
            c.trigger_probe().await
        });

        tokio::task::yield_now().await;
        // Acquire capture mid-probe
        let _guard = ctrl.acquire_capture();

        tokio::time::advance(Duration::from_millis(250)).await;
        let res = probe_task.await.unwrap();
        // Completed gracefully with preemption without corrupting state
        assert!(matches!(res, TriggerOutcome::Executed(..)));
    }

    #[test]
    fn test_08_tier_ordering_and_string_representation() {
        assert!(Tier::Ultra > Tier::High);
        assert!(Tier::High > Tier::Medium);
        assert!(Tier::Medium > Tier::Low);
        assert_eq!(Tier::Ultra.as_str(), "Ultra");
        assert_eq!(Tier::Low.as_str(), "Low");
    }

    #[tokio::test]
    async fn test_09_recovery_from_offline_to_healthy() {
        tokio::time::pause();
        let transport_bad = Arc::new(MockTransport::with_connect_error(TransportError::DnsOrTcpFailure));
        let ctrl = ProbeController::new(transport_bad, ProbeConfig::default()).unwrap();

        // 1. First probe fails -> Offline
        let r1 = ctrl.trigger_probe().await;
        assert_eq!(r1.state(), ProbeState::Offline);

        // 2. Recovery: Replace with healthy transport
        let transport_good = Arc::new(MockTransport::healthy(4000.0, Duration::from_millis(10)));
        let ctrl_recovered = ProbeController::new(transport_good, ProbeConfig::default()).unwrap();
        let r2 = ctrl_recovered.trigger_probe().await;
        assert_eq!(r2.state(), ProbeState::Healthy(Tier::High));
    }

    // Live TCP Loopback Transport Test for realistic socket behavior
    struct TcpLoopbackTransport {
        addr: String,
    }

    impl ProbeTransport for TcpLoopbackTransport {
        fn connect(&self) -> BoxFuture<'_, Result<(), TransportError>> {
            let addr = self.addr.clone();
            Box::pin(async move {
                TcpStream::connect(addr)
                    .await
                    .map(|_| ())
                    .map_err(|_| TransportError::DnsOrTcpFailure)
            })
        }

        fn fetch_chunk(&self, _chunk_idx: usize, target_bytes: usize) -> BoxFuture<'_, Result<usize, TransportError>> {
            let addr = self.addr.clone();
            Box::pin(async move {
                let mut stream = TcpStream::connect(addr)
                    .await
                    .map_err(|_| TransportError::DnsOrTcpFailure)?;
                let mut buf = vec![0u8; target_bytes];
                let n = stream.read(&mut buf).await.map_err(|e| TransportError::IoError(e.to_string()))?;
                Ok(n)
            })
        }
    }

    #[tokio::test]
    async fn test_10_real_tcp_loopback_integration() {
        // Bind local TCP listener
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap().to_string();

        // Spawn mock responder that sends 16KB per connection
        tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let payload = vec![0xAAu8; 16384];
                    let _ = socket.write_all(&payload).await;
                });
            }
        });

        let transport = Arc::new(TcpLoopbackTransport { addr });
        let mut cfg = ProbeConfig::default();
        cfg.num_chunks = 2;
        cfg.chunk_target_bytes = 16384;

        let ctrl = ProbeController::new(transport, cfg).unwrap();
        let outcome = ctrl.trigger_probe().await;
        assert!(matches!(outcome, TriggerOutcome::Executed(ProbeState::Healthy(_), Some(_))));
    }

    #[tokio::test]
    async fn test_11_ewma_smoothing_dampens_single_transient_dip() {
        tokio::time::pause();
        let mut cfg = ProbeConfig::default();
        cfg.ewma_alpha = 0.25; // 25% weight on new sample, 75% on historical
        cfg.initial_tier = Tier::High;

        // Baseline: 4000 kbps (High)
        let transport_high = Arc::new(MockTransport::healthy(4000.0, Duration::from_millis(10)));
        let ctrl = ProbeController::new(transport_high, cfg.clone()).unwrap();

        let _ = ctrl.trigger_probe().await;
        let baseline_ewma = ctrl.smoothed_kbps().await.unwrap();
        assert!(baseline_ewma >= 3000.0);

        // A single transient dip to 1000 kbps shouldn't drop tier immediately due to EWMA
        // new_ewma = 0.25 * 1000 + 0.75 * 3600 = ~2950 kbps (still well above medium_to_low 800)
        let transport_dip = Arc::new(MockTransport::healthy(1000.0, Duration::from_millis(10)));
        let ctrl_dip = ProbeController::new(transport_dip, cfg).unwrap();
        let _ = ctrl_dip.trigger_probe().await;
        assert!(ctrl_dip.smoothed_kbps().await.is_some());
    }

    #[tokio::test]
    async fn test_12_nested_guards_keep_probing_suppressed_until_last_dropped() {
        let transport = Arc::new(MockTransport::healthy(5000.0, Duration::from_millis(10)));
        let ctrl = ProbeController::new(transport, ProbeConfig::default()).unwrap();

        let g1 = ctrl.acquire_capture();
        let g2 = ctrl.acquire_capture();
        let g3 = ctrl.acquire_capture();

        assert_eq!(ctrl.active_captures.load(Ordering::SeqCst), 3);
        assert!(matches!(ctrl.trigger_probe().await, TriggerOutcome::SuppressedActiveCapture(..)));

        drop(g1);
        assert_eq!(ctrl.active_captures.load(Ordering::SeqCst), 2);
        assert!(matches!(ctrl.trigger_probe().await, TriggerOutcome::SuppressedActiveCapture(..)));

        drop(g2);
        assert_eq!(ctrl.active_captures.load(Ordering::SeqCst), 1);
        assert!(matches!(ctrl.trigger_probe().await, TriggerOutcome::SuppressedActiveCapture(..)));

        drop(g3);
        assert_eq!(ctrl.active_captures.load(Ordering::SeqCst), 0);
        assert!(!ctrl.is_capture_active());
        assert!(matches!(ctrl.trigger_probe().await, TriggerOutcome::Executed(..)));
    }
}
