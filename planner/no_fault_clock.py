"""Resource-first, single-activation Clock for native no-fault only."""
import time
from .common import Clock


class NativeSolverClock(Clock):
    def __init__(self, out):
        self.start = None
        self.deadline = None
        self.margin = 1
        self.out = out
        self.stages = []
        self.run_start = None
        self.logfile = (out / 'run.log').open('w', encoding='utf-8', buffering=1)

    def activate(self):
        if self.start is not None:
            raise RuntimeError('native solver Clock may only activate once')
        self.start = time.monotonic()
        self.deadline = self.start + 30
        self.run_start = self.start
