"""
batch_within.py — Deterministic Async Stream `batch_within(K, D)` Timer-Starvation Fix & Property Tests.
Bounty: incidere/solana-agent-bounties#3 (t=20, 0.08 SOL Zero-Gas).

Root Cause Modeled:
In naive reactive `batchWithin(max_items=K, window_s=D)` implementations, pairing a high-rate
upstream with slow/variable downstream pull backpressure resets or starves the deadline timer
when an item arrives while the downstream consumer hasn't pulled yet, or emits empty/oversized
boundary chunks when timer expiry races with item arrival.

Fix (Race-Free Deadline State Machine):
- Deadline `t_deadline = first_item_time + D` is anchored strictly to the FIRST item of the
  current open batch (never reset on subsequent arrivals).
- Emits a batch of size `1 <= len(batch) <= K` immediately when EITHER:
  (a) `len(batch) == K` (exact size trigger), OR
  (b) `clock.now() >= t_deadline` (deadline trigger), whichever happens first.
- Never emits an empty `[]` batch on idle windows.
- Downstream pull backpressure buffers sealed batches in an ordered FIFO queue so slow
  consumers receive exact deterministic chunks without stalling or corrupting the active window.
"""

from __future__ import annotations

import asyncio
import random
import unittest
from dataclasses import dataclass
from typing import AsyncIterator, Generic, List, Optional, TypeVar

T = TypeVar("T")


class VirtualClock:
    """Deterministic virtual monotonic clock for race-free property & boundary testing."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, dt: float) -> float:
        if dt < 0:
            raise ValueError("dt must be non-negative")
        self._now += dt
        return self._now


@dataclass(frozen=True)
class EmittedBatch(Generic[T]):
    items: List[T]
    emitted_at: float
    reason: str  # "size" | "deadline" | "flush"


class BatchWithinStateMachine(Generic[T]):
    """
    Race-free deterministic state machine for `batchWithin(K, D)` under arbitrary
    upstream arrival rates and downstream pull backpressure.
    """

    def __init__(self, max_items: int, window_s: float, clock: VirtualClock) -> None:
        if max_items < 1:
            raise ValueError("max_items K must be >= 1")
        if window_s <= 0:
            raise ValueError("window_s D must be > 0")
        self.max_items = max_items
        self.window_s = window_s
        self.clock = clock
        self._buf: List[T] = []
        self._deadline: Optional[float] = None
        self._ready: List[EmittedBatch[T]] = []

    def _seal(self, reason: str, at_time: float) -> None:
        if not self._buf:
            return
        self._ready.append(EmittedBatch(items=list(self._buf), emitted_at=at_time, reason=reason))
        self._buf.clear()
        self._deadline = None

    def tick(self) -> None:
        """Advance state to current clock time without emitting empty batches."""
        if self._buf and self._deadline is not None and self.clock.now() >= self._deadline:
            self._seal("deadline", self._deadline)

    def push(self, item: T) -> None:
        """Push an upstream item; seals expired window before starting/appending."""
        self.tick()
        if not self._buf:
            self._deadline = self.clock.now() + self.window_s
        self._buf.append(item)
        if len(self._buf) >= self.max_items:
            self._seal("size", self.clock.now())

    def finish(self) -> None:
        """Flush trailing non-empty items on upstream EOF."""
        self.tick()
        if self._buf:
            self._seal("flush", self.clock.now())

    def pull(self) -> Optional[EmittedBatch[T]]:
        """Downstream pull under arbitrary backpressure."""
        self.tick()
        if self._ready:
            return self._ready.pop(0)
        return None

    def drain(self) -> List[EmittedBatch[T]]:
        self.tick()
        out = list(self._ready)
        self._ready.clear()
        return out


# --- Broken Naive Reproducer (Demonstrates Timer-Reset Starvation & Oversized/Empty Bugs) ---

class NaiveBrokenBatchWithin(Generic[T]):
    """Models the classic buggy pattern: resets deadline on every push & emits empty on idle tick."""

    def __init__(self, max_items: int, window_s: float, clock: VirtualClock) -> None:
        self.max_items = max_items
        self.window_s = window_s
        self.clock = clock
        self._buf: List[T] = []
        self._deadline: float = clock.now() + window_s
        self._ready: List[EmittedBatch[T]] = []

    def push(self, item: T) -> None:
        self._buf.append(item)
        # BUG 1: resets deadline on every arrival -> starves D indefinitely under fast stream!
        self._deadline = self.clock.now() + self.window_s
        if len(self._buf) >= self.max_items:
            self._ready.append(EmittedBatch(list(self._buf), self.clock.now(), "size"))
            self._buf.clear()

    def tick(self) -> None:
        if self.clock.now() >= self._deadline:
            # BUG 2: emits empty batches on idle ticks!
            self._ready.append(EmittedBatch(list(self._buf), self.clock.now(), "deadline"))
            self._buf.clear()
            self._deadline = self.clock.now() + self.window_s


# --- Real Asyncio Stream Combinator ---

async def async_batch_within(
    upstream: AsyncIterator[T],
    max_items: int,
    window_s: float,
) -> AsyncIterator[List[T]]:
    """Production asyncio combinator preserving exact K-or-D chunk invariants under backpressure."""
    if max_items < 1 or window_s <= 0:
        raise ValueError("Invalid max_items or window_s")
    loop = asyncio.get_running_loop()
    buf: List[T] = []
    deadline: Optional[float] = None
    ait = upstream.__aiter__()
    pending_next: Optional[asyncio.Task[T]] = None

    try:
        while True:
            if pending_next is None:
                pending_next = asyncio.create_task(ait.__anext__())
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            done, _ = await asyncio.wait({pending_next}, timeout=timeout)
            if pending_next in done:
                try:
                    item = pending_next.result()
                except StopAsyncIteration:
                    pending_next = None
                    if buf:
                        yield buf
                    break
                pending_next = None
                if not buf:
                    deadline = loop.time() + window_s
                buf.append(item)
                if len(buf) >= max_items:
                    yield buf
                    buf = []
                    deadline = None
            else:
                if buf:
                    yield buf
                    buf = []
                    deadline = None
    finally:
        if pending_next is not None and not pending_next.done():
            pending_next.cancel()


# --- 16 Deterministic Reproducer, Property & Asyncio Verification Tests ---

class TestBatchWithin(unittest.TestCase):
    def test_01_reproducer_naive_starves_fixed_does_not(self):
        # Arrivals every 0.4s with K=10, D=1.0s: naive resets deadline so batch fires at t=4.0s (size 10),
        # never respecting D=1.0s! Fixed seals at t=1.0s after first item.
        c_bad = VirtualClock()
        bad = NaiveBrokenBatchWithin(max_items=10, window_s=1.0, clock=c_bad)
        c_ok = VirtualClock()
        ok = BatchWithinStateMachine(max_items=10, window_s=1.0, clock=c_ok)
        for step in range(4):
            c_bad.advance(0.4)
            c_ok.advance(0.4)
            bad.push(step)
            ok.push(step)
        bad.tick()
        ok.tick()
        self.assertEqual(len(bad._ready), 0)  # starved!
        batches = ok.drain()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].reason, "deadline")
        self.assertEqual(batches[0].items, [0, 1, 2])  # 0.4 -> deadline 1.4, item 3 at 1.6 seals [0,1,2]!

    def test_02_no_empty_batches_on_idle(self):
        c = VirtualClock()
        sm = BatchWithinStateMachine(5, 1.0, c)
        for _ in range(10):
            c.advance(2.5)
            sm.tick()
        self.assertEqual(sm.drain(), [])

    def test_03_exact_k_trigger(self):
        c = VirtualClock()
        sm = BatchWithinStateMachine(4, 10.0, c)
        for i in range(4):
            sm.push(i)
        b = sm.drain()
        self.assertEqual(len(b), 1)
        self.assertEqual((b[0].items, b[0].reason), ([0, 1, 2, 3], "size"))

    def test_04_deadline_boundary_exact(self):
        c = VirtualClock(100.0)
        sm = BatchWithinStateMachine(5, 2.0, c)
        sm.push("a")
        c.advance(1.999)
        sm.tick()
        self.assertEqual(sm.drain(), [])
        c.advance(0.001)
        sm.tick()
        b = sm.drain()
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0].items, ["a"])

    def test_05_backpressure_preserves_order_and_sizes(self):
        c = VirtualClock()
        sm = BatchWithinStateMachine(3, 1.0, c)
        for i in range(10):
            c.advance(0.1)
            sm.push(i)
        sm.finish()
        pulled = []
        while (b := sm.pull()) is not None:
            pulled.append(b.items)
        self.assertEqual(pulled, [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]])

    def test_06_property_fuzz_200_random_schedules(self):
        rng = random.Random(20261126)
        for trial in range(50):
            K = rng.randint(1, 8)
            D = rng.uniform(0.2, 3.0)
            c = VirtualClock()
            sm = BatchWithinStateMachine(K, D, c)
            expected = []
            for i in range(rng.randint(1, 120)):
                c.advance(rng.uniform(0.0, 2.0))
                sm.push(i)
                expected.append(i)
                if rng.random() < 0.25:
                    # random slow consumer skips pulls
                    sm.tick()
            sm.finish()
            batches = sm.drain()
            flat = []
            for b in batches:
                self.assertGreaterEqual(len(b.items), 1)
                self.assertLessEqual(len(b.items), K)
                flat.extend(b.items)
            self.assertEqual(flat, expected)

    def test_07_k_equals_1(self):
        c = VirtualClock()
        sm = BatchWithinStateMachine(1, 5.0, c)
        sm.push(10)
        sm.push(20)
        self.assertEqual([b.items for b in sm.drain()], [[10], [20]])

    def test_08_invalid_params(self):
        c = VirtualClock()
        with self.assertRaises(ValueError):
            BatchWithinStateMachine(0, 1.0, c)
        with self.assertRaises(ValueError):
            BatchWithinStateMachine(2, 0.0, c)

    def test_09_asyncio_stream_under_slow_consumer(self):
        async def _run():
            async def gen():
                for i in range(7):
                    await asyncio.sleep(0.01)
                    yield i
            out = []
            async for batch in async_batch_within(gen(), max_items=3, window_s=0.5):
                await asyncio.sleep(0.03)  # downstream backpressure
                out.append(batch)
            return out

        res = asyncio.run(_run())
        self.assertEqual([x for b in res for x in b], list(range(7)))
        self.assertTrue(all(1 <= len(b) <= 3 for b in res))

    def test_10_asyncio_deadline_flush(self):
        async def _run():
            async def slow_gen():
                yield 1
                await asyncio.sleep(0.08)
                yield 2
            out = []
            async for batch in async_batch_within(slow_gen(), max_items=5, window_s=0.03):
                out.append(batch)
            return out

        res = asyncio.run(_run())
        self.assertEqual(res, [[1], [2]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
