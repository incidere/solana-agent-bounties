"""Comprehensive offline test suite for Compact Bytecode Stack VM with Fiber Interrupt Checkpointing."""
from __future__ import annotations
import os
import time
import unittest
from compact_vm import CompactVM, Fiber, FiberState, Op


class TestCompactVM(unittest.TestCase):
    def setUp(self):
        self.vm = CompactVM()

    def test_basic_arithmetic(self):
        # 10 + 20 * 2 - 15 // 3 = 10 + 40 - 5 = 45
        code = [
            (Op.LOAD_CONST, 10),
            (Op.LOAD_CONST, 20),
            (Op.LOAD_CONST, 2),
            (Op.MUL,),
            (Op.ADD,),
            (Op.LOAD_CONST, 15),
            (Op.LOAD_CONST, 3),
            (Op.DIV,),
            (Op.SUB,),
            (Op.HALT,),
        ]
        fb = self.vm.spawn(code)
        st = self.vm.step_fiber(fb)
        self.assertEqual(st, FiberState.HALTED)
        self.assertEqual(fb.stack, [45])

    def test_stack_manipulation(self):
        # DUP, SWAP, POP
        code = [
            (Op.LOAD_CONST, 42),
            (Op.DUP,),
            (Op.LOAD_CONST, 99),
            (Op.SWAP,),
            (Op.POP,),
            (Op.HALT,),
        ]
        fb = self.vm.spawn(code)
        st = self.vm.step_fiber(fb)
        self.assertEqual(st, FiberState.HALTED)
        self.assertEqual(fb.stack, [42, 99])

    def test_comparisons_and_not(self):
        code = [
            (Op.LOAD_CONST, 10),
            (Op.LOAD_CONST, 20),
            (Op.LT,),  # True
            (Op.LOAD_CONST, 5),
            (Op.LOAD_CONST, 5),
            (Op.EQ,),  # True
            (Op.LOAD_CONST, 1),
            (Op.LOAD_CONST, 2),
            (Op.GT,),  # False
            (Op.NOT,), # True
            (Op.HALT,),
        ]
        fb = self.vm.spawn(code)
        self.vm.step_fiber(fb)
        self.assertEqual(fb.stack, [True, True, True])

    def test_factorial_loop(self):
        # n = 5, fact = 1
        # while n > 1: fact *= n; n -= 1
        code = [
            (Op.LOAD_CONST, 5),
            (Op.STORE_VAR, "n"),
            (Op.LOAD_CONST, 1),
            (Op.STORE_VAR, "fact"),
            # loop head at pc=4
            (Op.LOAD_VAR, "n"),
            (Op.LOAD_CONST, 1),
            (Op.GT,),
            (Op.JUMP_IF_FALSE, 17), # break to pc=17
            (Op.LOAD_VAR, "fact"),
            (Op.LOAD_VAR, "n"),
            (Op.MUL,),
            (Op.STORE_VAR, "fact"),
            (Op.LOAD_VAR, "n"),
            (Op.LOAD_CONST, 1),
            (Op.SUB,),
            (Op.STORE_VAR, "n"),
            (Op.JUMP, 4),           # jump back
            # done at pc=17
            (Op.LOAD_VAR, "fact"),
            (Op.HALT,),
        ]
        fb = self.vm.spawn(code)
        st = self.vm.step_fiber(fb)
        self.assertEqual(st, FiberState.HALTED)
        self.assertEqual(fb.locals["fact"], 120)
        self.assertEqual(fb.stack, [120])

    def test_cooperative_multitasking_fibers(self):
        # Fiber 1 increments a up to 3 with yields
        code1 = [
            (Op.LOAD_CONST, 0), (Op.STORE_VAR, "a"),
            # loop:
            (Op.LOAD_VAR, "a"), (Op.LOAD_CONST, 1), (Op.ADD,), (Op.STORE_VAR, "a"),
            (Op.YIELD,),
            (Op.LOAD_VAR, "a"), (Op.LOAD_CONST, 3), (Op.LT,), (Op.JUMP_IF_TRUE, 2),
            (Op.HALT,),
        ]
        # Fiber 2 increments b up to 3 with yields
        code2 = [
            (Op.LOAD_CONST, 0), (Op.STORE_VAR, "b"),
            # loop:
            (Op.LOAD_VAR, "b"), (Op.LOAD_CONST, 10), (Op.ADD,), (Op.STORE_VAR, "b"),
            (Op.YIELD,),
            (Op.LOAD_VAR, "b"), (Op.LOAD_CONST, 30), (Op.LT,), (Op.JUMP_IF_TRUE, 2),
            (Op.HALT,),
        ]
        fb1 = self.vm.spawn(code1)
        fb2 = self.vm.spawn(code2)
        res = self.vm.run(slice_steps=1)
        self.assertEqual(res[fb1.fid], FiberState.HALTED)
        self.assertEqual(res[fb2.fid], FiberState.HALTED)
        self.assertEqual(fb1.locals["a"], 3)
        self.assertEqual(fb2.locals["b"], 30)

    def test_fiber_interrupt_checkpoint_and_restore(self):
        # Accumulator loop: sum = sum + i, for i in 1..200 (expected sum = 20100)
        code = [
            (Op.LOAD_CONST, 0), (Op.STORE_VAR, "i"),
            (Op.LOAD_CONST, 0), (Op.STORE_VAR, "sum"),
            # loop at pc=4:
            (Op.LOAD_VAR, "i"), (Op.LOAD_CONST, 200), (Op.LT,),
            (Op.JUMP_IF_FALSE, 17),
            (Op.LOAD_VAR, "i"), (Op.LOAD_CONST, 1), (Op.ADD,), (Op.STORE_VAR, "i"),
            (Op.LOAD_VAR, "sum"), (Op.LOAD_VAR, "i"), (Op.ADD,), (Op.STORE_VAR, "sum"),
            (Op.JUMP, 4),
            # end at pc=17:
            (Op.LOAD_VAR, "sum"),
            (Op.HALT,),
        ]
        # 1. Run uninterrupted on baseline to get exact reference
        baseline_vm = CompactVM()
        base_fb = baseline_vm.spawn(code)
        baseline_vm.step_fiber(base_fb)
        self.assertEqual(base_fb.state, FiberState.HALTED)
        expected_sum = base_fb.locals["sum"]
        expected_steps = base_fb.steps
        self.assertEqual(expected_sum, 20100)

        # 2. Run partially with interruption
        fb = self.vm.spawn(code)
        # Run 50 steps
        self.vm.step_fiber(fb, max_steps=50)
        self.assertEqual(fb.state, FiberState.SUSPENDED)
        mid_steps = fb.steps
        self.assertGreater(mid_steps, 0)
        self.assertLess(mid_steps, expected_steps)

        # Signal an external interrupt
        self.vm.interrupt(fb.fid)
        self.assertTrue(fb.interrupt_flag)

        # Step VM: interrupt must be caught immediately at instruction boundary
        st = self.vm.step_fiber(fb)
        self.assertEqual(st, FiberState.INTERRUPTED)

        # Checkpoint serialization
        snapshot_dict = fb.checkpoint()
        snapshot_json = fb.checkpoint_json()
        self.assertIn("pc", snapshot_dict)
        self.assertIn("locals", snapshot_dict)
        self.assertIn("stack", snapshot_dict)

        # 3. Simulate total node/process migration: instantiate brand new VM
        new_vm = CompactVM()
        restored_fb = Fiber.restore(snapshot_json)
        self.assertEqual(restored_fb.fid, fb.fid)
        self.assertEqual(restored_fb.pc, fb.pc)
        self.assertEqual(restored_fb.steps, mid_steps)
        self.assertEqual(restored_fb.locals, fb.locals)

        # Clear interrupt and resume execution to completion
        restored_fb.interrupt_flag = False
        restored_fb.state = FiberState.READY
        new_vm.fibers[restored_fb.fid] = restored_fb

        final_st = new_vm.step_fiber(restored_fb)
        self.assertEqual(final_st, FiberState.HALTED)
        self.assertEqual(restored_fb.locals["sum"], expected_sum)
        self.assertEqual(restored_fb.steps, expected_steps)
        self.assertEqual(restored_fb.stack, [expected_sum])

    def test_gas_exhaustion_interrupt_and_resume(self):
        # Infinite loop: i += 1
        code = [
            (Op.LOAD_CONST, 0), (Op.STORE_VAR, "i"),
            (Op.LOAD_VAR, "i"), (Op.LOAD_CONST, 1), (Op.ADD,), (Op.STORE_VAR, "i"),
            (Op.JUMP, 2),
        ]
        # Allocate small gas quota of 15 instructions
        fb = self.vm.spawn(code, gas=15)
        st = self.vm.step_fiber(fb)
        self.assertEqual(st, FiberState.INTERRUPTED)
        self.assertEqual(fb.gas, 0)
        self.assertEqual(fb.steps, 15)

        # Checkpoint, refuel with 30 gas, and resume
        snap = fb.checkpoint_json()
        restored = Fiber.restore(snap)
        restored.gas = 30
        restored.state = FiberState.READY
        self.vm.fibers[restored.fid] = restored

        st2 = self.vm.step_fiber(restored)
        self.assertEqual(st2, FiberState.INTERRUPTED)
        self.assertEqual(restored.gas, 0)
        self.assertEqual(restored.steps, 45)

    def test_compact_source_code_size_under_4kb(self):
        import compact_vm
        src_path = compact_vm.__file__
        file_bytes = os.path.getsize(src_path)
        print(f"\n[SIZE VERIFICATION] compact_vm.py size = {file_bytes} bytes (budget <= 4096 bytes)")
        self.assertLessEqual(file_bytes, 4096, f"File exceeds 4096 bytes: {file_bytes}")

    def test_ops_per_second_throughput(self):
        # 10,000 loop cycles -> ~60,000 instructions executed
        code = [
            (Op.LOAD_CONST, 0), (Op.STORE_VAR, "i"),
            # loop:
            (Op.LOAD_VAR, "i"), (Op.LOAD_CONST, 10000), (Op.LT,),
            (Op.JUMP_IF_FALSE, 12),
            (Op.LOAD_VAR, "i"), (Op.LOAD_CONST, 1), (Op.ADD,), (Op.STORE_VAR, "i"),
            (Op.JUMP, 2),
            (Op.HALT,),
        ]
        fb = self.vm.spawn(code)
        t0 = time.perf_counter()
        self.vm.step_fiber(fb)
        elapsed = time.perf_counter() - t0
        ops_per_sec = fb.steps / max(elapsed, 1e-9)
        print(f"\n[BENCHMARK] Executed {fb.steps} ops in {elapsed:.4f}s ({ops_per_sec:,.0f} ops/sec)")
        self.assertEqual(fb.state, FiberState.HALTED)
        self.assertGreater(ops_per_sec, 500_000, "VM throughput too low")


if __name__ == "__main__":
    unittest.main()
