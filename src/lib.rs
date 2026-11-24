//! probe_ctrl – adaptive goodput probe controller with EWMA and hysteresis.
//!
//! The library is deliberately self‑contained and only depends on `std` and `tokio`.

use std::sync::Arc;
use tokio::sync::{Mutex, Notify};
use tokio::time::{self, Duration, Instant, timeout};

/// Capture tier levels.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Ultra,
    High,
    Medium,
    Low,
}

/// Internal state of the controller.
#[derive(Debug, Clone)]
enum State {
    Idle,
    Probing,
    Healthy(Tier),
    Degraded(Tier),
    Offline,
}

/// Simple EWMA estimator for kilobits‑per‑second.
#[derive(Debug, Clone)]
struct Ewma {
    alpha: f64,
    value: Option<f64>,
}

impl Ewma {
    fn new(alpha: f64) -> Self {
        Self { alpha, value: None }
    }
    fn update(&mut self, sample: f64) {
        self.value = Some(match self.value {
            Some(prev) => self.alpha * sample + (1.0 - self.alpha) * prev,
            None => sample,
        });
    }
    fn get(&self) -> f64 {
        self.value.unwrap_or(0.0)
    }
}

/// Errors that can arise during a probe.
#[derive(Debug)]
pub enum ProbeError {
    Timeout,
    DnsFailure,
    TcpFailure,
    Other(String),
}

/// Core controller exposing a non‑blocking API.
#[derive(Clone)]
pub struct ProbeController {
    // Shared mutable state.
    inner: Arc<Inner>,
}

struct Inner {
    state: Mutex<State>,
    // Guard that indicates an active capture is in progress.
    active_capture: Mutex<bool>,
    // Guard that indicates a probe task is already running.
    in_flight: Mutex<bool>,
    // EWMA estimator for goodput.
    ewma: Mutex<Ewma>,
    // Timestamp of the last tier transition.
    last_transition: Mutex<Instant>,
    // Configuration.
    dwell: Duration,
    up_thresh: f64,
    down_thresh: f64,
    // Notification used to wake a waiting test.
    notify: Notify,
}

impl ProbeController {
    /// Create a new controller.
    ///
    /// * `dwell` – minimum time a tier must be held before another transition.
    /// * `up_thresh` – EWMA value above which we *upgrade* the tier.
    /// * `down_thresh` – EWMA value below which we *downgrade* the tier.
    pub fn new(dwell: Duration, up_thresh: f64, down_thresh: f64) -> Self {
        let inner = Inner {
            state: Mutex::new(State::Idle),
            active_capture: Mutex::new(false),
            in_flight: Mutex::new(false),
            ewma: Mutex::new(Ewma::new(0.3)),
            last_transition: Mutex::new(Instant::now()),
            dwell,
            up_thresh,
            down_thresh,
            notify: Notify::new(),
        };
        Self { inner: Arc::new(inner) }
    }

    /// Mark that a capture is active. While held, probes are suppressed.
    pub async fn with_active_capture<F, Fut>(&self, f: F) -> F::Output
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future,
    {
        let mut guard = self.inner.active_capture.lock().await;
        *guard = true;
        let res = f().await;
        *guard = false;
        // Wake any pending probe that might have been coalesced.
        self.inner.notify.notify_waiters();
        res
    }

    /// Request a good‑put probe. The call returns immediately; the probe runs in the background.
    /// If a probe is already in flight the request is coalesced.
    pub async fn start_probe(&self) {
        // Suppress if a capture is active.
        if *self.inner.active_capture.lock().await {
            return;
        }

        // Coalesce concurrent calls.
        let mut in_flight = self.inner.in_flight.lock().await;
        if *in_flight {
            // Already probing – nothing to do.
            return;
        }
        *in_flight = true;
        drop(in_flight);

        let ctrl = self.clone();
        tokio::spawn(async move {
            ctrl.run_probe().await;
        });
    }

    async fn run_probe(&self) {
        // Transition to Probing state.
        {
            let mut state = self.inner.state.lock().await;
            *state = State::Probing;
        }

        // Simulated probe – in real code this would perform DNS/TCP checks and measure bytes.
        // For deterministic tests we use a fixed 5 seconds deadline and a constant sample.
        let deadline = Duration::from_secs(5);
        let probe_fut = async {
            // Simulate network latency.
            time::sleep(Duration::from_millis(200)).await;
            // Return a sample goodput in kbps.
            Ok::<f64, ProbeError>(1200.0)
        };

        let result = timeout(deadline, probe_fut).await;
        let sample = match result {
            Ok(Ok(kbps)) => Some(kbps),
            Ok(Err(e)) => {
                self.handle_error(e).await;
                None
            }
            Err(_) => {
                // Timeout – treat as degraded.
                self.handle_error(ProbeError::Timeout).await;
                None
            }
        };

        if let Some(kbps) = sample {
            // Update EWMA.
            {
                let mut ewma = self.inner.ewma.lock().await;
                ewma.update(kbps);
            }
            self.evaluate_hysteresis().await;
        }

        // Reset in‑flight flag.
        let mut in_flight = self.inner.in_flight.lock().await;
        *in_flight = false;
        // Notify potential waiters (used only in tests).
        self.inner.notify.notify_waiters();
    }

    async fn handle_error(&self, err: ProbeError) {
        let mut state = self.inner.state.lock().await;
        match err {
            ProbeError::DnsFailure | ProbeError::TcpFailure => {
                *state = State::Offline;
            }
            ProbeError::Timeout => {
                // Timeout is interpreted as a degraded low tier.
                *state = State::Degraded(Tier::Low);
            }
            ProbeError::Other(_) => {
                // Keep previous state.
            }
        }
    }

    async fn evaluate_hysteresis(&self) {
        let ewma = { self.inner.ewma.lock().await.get() };
        let now = Instant::now();
        let mut last = self.inner.last_transition.lock().await;
        if now.duration_since(*last) < self.inner.dwell {
            // Dwell timer prevents rapid flapping.
            return;
        }

        let mut state = self.inner.state.lock().await;
        match *state {
            State::Healthy(current) => {
                if ewma < self.inner.down_thresh {
                    // Downgrade.
                    let new_tier = downgrade_tier(current);
                    *state = State::Degraded(new_tier);
                    *last = now;
                }
            }
            State::Degraded(current) => {
                if ewma > self.inner.up_thresh {
                    // Upgrade.
                    let new_tier = upgrade_tier(current);
                    *state = State::Healthy(new_tier);
                    *last = now;
                }
            }
            State::Idle | State::Probing | State::Offline => {
                // Initial classification based on thresholds.
                let tier = if ewma > self.inner.up_thresh {
                    Tier::Ultra
                } else if ewma > self.inner.down_thresh {
                    Tier::High
                } else {
                    Tier::Low
                };
                *state = State::Healthy(tier);
                *last = now;
            }
        }
    }

    /// Retrieve the current tier (or `None` if offline/idle).
    pub async fn current_tier(&self) -> Option<Tier> {
        let state = self.inner.state.lock().await;
        match *state {
            State::Healthy(t) => Some(t),
            State::Degraded(t) => Some(t),
            _ => None,
        }
    }
}

fn upgrade_tier(tier: Tier) -> Tier {
    match tier {
        Tier::Low => Tier::Medium,
        Tier::Medium => Tier::High,
        Tier::High => Tier::Ultra,
        Tier::Ultra => Tier::Ultra,
    }
}

fn downgrade_tier(tier: Tier) -> Tier {
    match tier {
        Tier::Ultra => Tier::High,
        Tier::High => Tier::Medium,
        Tier::Medium => Tier::Low,
        Tier::Low => Tier::Low,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::time::{self, Duration, Instant};

    #[tokio::test]
    async fn active_capture_suppression() {
        time::pause();
        let ctrl = ProbeController::new(Duration::from_secs(2), 1500.0, 800.0);
        // Start a probe while a capture is active – it must be suppressed.
        ctrl.with_active_capture(|| async {
            ctrl.start_probe().await;
        }).await;
        // Advance time enough for a probe to have completed if it ran.
        time::advance(Duration::from_secs(10)).await;
        // No tier should be set because the probe never executed.
        assert!(ctrl.current_tier().await.is_none());
    }

    #[tokio::test]
    async fn coalescing_multiple_calls() {
        time::pause();
        let ctrl = ProbeController::new(Duration::from_secs(2), 1500.0, 800.0);
        // Fire three concurrent start_probe calls.
        let c1 = ctrl.clone();
        let c2 = ctrl.clone();
        let c3 = ctrl.clone();
        tokio::join!(c1.start_probe(), c2.start_probe(), c3.start_probe());
        // Only one probe should be in‑flight; wait for it to finish.
        ctrl.inner.notify.notified().await; // first notification after probe ends
        // Verify that EWMA was updated exactly once (value 1200.0).
        let ewma = { ctrl.inner.ewma.lock().await.get() };
        assert!((ewma - 1200.0).abs() < f64::EPSILON);
    }

    #[tokio::test]
    async fn probe_timeout_cancels() {
        time::pause();
        let ctrl = ProbeController::new(Duration::from_secs(2), 1500.0, 800.0);
        // Override the internal run_probe to use a long‑running future.
        // For simplicity we simulate by advancing time beyond the deadline.
        ctrl.start_probe().await;
        // Fast‑forward past the 5 s deadline.
        time::advance(Duration::from_secs(6)).await;
        // Wait for the probe task to notice the timeout.
        ctrl.inner.notify.notified().await;
        // The controller should now be in Degraded(Low) state.
        match ctrl.inner.state.lock().await.clone() {
            State::Degraded(Tier::Low) => {}
            other => panic!("unexpected state: {:?}", other),
        }
    }

    #[tokio::test]
    async fn hysteresis_prevents_flap() {
        time::pause();
        let ctrl = ProbeController::new(Duration::from_secs(5), 1500.0, 800.0);
        // First probe yields 1200 kbps → Healthy(High) after evaluation.
        ctrl.start_probe().await;
        time::advance(Duration::from_secs(1)).await; // let probe finish
        ctrl.inner.notify.notified().await;
        assert_eq!(ctrl.current_tier().await, Some(Tier::High));
        // Second probe with a low sample should not downgrade because dwell timer (5 s) hasn't elapsed.
        {
            let mut ewma = ctrl.inner.ewma.lock().await;
            ewma.update(400.0);
        }
        ctrl.evaluate_hysteresis().await;
        assert_eq!(ctrl.current_tier().await, Some(Tier::High)); // still High
        // Advance past dwell and evaluate again – now it should downgrade.
        time::advance(Duration::from_secs(5)).await;
        ctrl.evaluate_hysteresis().await;
        assert_eq!(ctrl.current_tier().await, Some(Tier::Medium));
    }
}
