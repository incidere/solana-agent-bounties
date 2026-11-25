"""Compact <=4KB Bytecode VM with Fiber Interrupt Checkpointing."""
import json

class Op:
    NOP, LOAD_CONST, LOAD_VAR, STORE_VAR, ADD, SUB, MUL, DIV, MOD, EQ, LT, GT, NOT, JUMP, JUMP_IF_TRUE, JUMP_IF_FALSE, DUP, POP, SWAP, YIELD, HALT = range(21)

class FiberState:
    READY, RUNNING, SUSPENDED, INTERRUPTED, HALTED = "READY", "RUNNING", "SUSPENDED", "INTERRUPTED", "HALTED"

class Fiber:
    def __init__(self, fid, code, pc=0, stack=None, locals=None, state="READY", interrupt_flag=False, gas=None, steps=0):
        self.fid, self.code, self.pc = fid, code, pc
        self.stack, self.locals = list(stack or []), dict(locals or {})
        self.state, self.interrupt_flag, self.gas, self.steps = state, bool(interrupt_flag), gas, steps

    def checkpoint(self):
        return {"fid": self.fid, "code": self.code, "pc": self.pc, "stack": list(self.stack), "locals": dict(self.locals), "state": self.state, "interrupt_flag": self.interrupt_flag, "gas": self.gas, "steps": self.steps}

    def checkpoint_json(self):
        return json.dumps(self.checkpoint())

    @classmethod
    def restore(cls, d):
        return cls(**(json.loads(d) if isinstance(d, (str, bytes)) else d))

class CompactVM:
    def __init__(self):
        self.fibers, self.next_fid = {}, 1

    def spawn(self, code, fid=None, locals=None, gas=None):
        if fid is None:
            fid = self.next_fid; self.next_fid += 1
        fb = Fiber(fid, code, locals=locals, gas=gas)
        self.fibers[fid] = fb
        return fb

    def interrupt(self, fid):
        f = self.fibers.get(fid)
        if f: f.interrupt_flag = True; return True
        return False

    def step_fiber(self, fiber, max_steps=None):
        n, code, code_len, stack, locs, FS = 0, fiber.code, len(fiber.code), fiber.stack, fiber.locals, FiberState
        fiber.state = FS.RUNNING
        while True:
            if fiber.interrupt_flag or (fiber.gas is not None and fiber.gas <= 0):
                fiber.state = FS.INTERRUPTED; return fiber.state
            if max_steps is not None and n >= max_steps:
                fiber.state = FS.SUSPENDED; return fiber.state
            if fiber.pc >= code_len:
                fiber.state = FS.HALTED; return fiber.state
            ins = code[fiber.pc]
            op, arg = ins[0], ins[1] if len(ins) > 1 else None
            fiber.pc += 1; fiber.steps += 1; n += 1
            if fiber.gas is not None: fiber.gas -= 1

            if op == 1: stack.append(arg)
            elif op == 2: stack.append(locs[arg])
            elif op == 3: locs[arg] = stack.pop()
            elif 4 <= op <= 11:
                b = stack.pop()
                if op == 4: stack[-1] += b
                elif op == 5: stack[-1] -= b
                elif op == 6: stack[-1] *= b
                elif op == 7: stack[-1] = stack[-1] // b if type(stack[-1]) is type(b) is int else stack[-1] / b
                elif op == 8: stack[-1] %= b
                elif op == 9: stack[-1] = (stack[-1] == b)
                elif op == 10: stack[-1] = (stack[-1] < b)
                elif op == 11: stack[-1] = (stack[-1] > b)
            elif op == 12: stack[-1] = not stack[-1]
            elif op == 13: fiber.pc = arg
            elif op == 14: fiber.pc = arg if stack.pop() else fiber.pc
            elif op == 15: fiber.pc = arg if not stack.pop() else fiber.pc
            elif op == 16: stack.append(stack[-1])
            elif op == 17: stack.pop()
            elif op == 18: stack[-1], stack[-2] = stack[-2], stack[-1]
            elif op == 19: fiber.state = FS.SUSPENDED; return fiber.state
            elif op == 20: fiber.state = FS.HALTED; return fiber.state
            elif op != 0: raise ValueError(f"Bad op: {op}")

    def run(self, max_cycles=1000000, slice_steps=50):
        for _ in range(max_cycles):
            act = [self.step_fiber(f, slice_steps) for f in list(self.fibers.values()) if f.state in ("READY", "SUSPENDED")]
            if not act: break
        return {fid: f.state for fid, f in self.fibers.items()}
