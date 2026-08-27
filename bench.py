"""
bench.py — a small, careful benchmarking harness.

Benchmarking real systems is noisy. This harness reduces the main sources of
error so your numbers are defensible:

  * WARMUP runs first (page faults, caches, CPU frequency scaling settle down).
  * Each measurement is REPEATED and we report the MEDIAN, not the mean —
    medians ignore the occasional huge outlier caused by the OS scheduling
    something else on our CPU.
  * We report ns/operation so results are comparable across different N.
  * We also track SYSCALL COUNT analytically (we know exactly how many syscalls
    each strategy issues), because the whole thesis of this project is
    "fewer syscalls = faster", and we want to show the cause, not just the effect.
"""

import time
import statistics
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class Result:
    name: str
    ns_per_op: float          # median time per logical operation
    total_ms: float           # median wall time for one full repetition
    ops: int                  # logical operations performed (e.g. bytes, records)
    syscalls: Optional[int]   # how many syscalls this strategy issues (None = unknown)

    @property
    def syscalls_per_op(self) -> float:
        return (self.syscalls / self.ops) if (self.syscalls and self.ops) else 0.0


def measure(name: str, fn: Callable[[], None], ops: int,
            syscalls: Optional[int] = None,
            repeats: int = 5, warmup: int = 1) -> Result:
    """
    Run `fn` (which performs `ops` logical operations) `repeats` times and
    report the median. `fn` must be self-contained and repeatable.
    """
    for _ in range(warmup):
        fn()

    times_ns: List[int] = []
    for _ in range(repeats):
        t0 = time.perf_counter_ns()
        fn()
        times_ns.append(time.perf_counter_ns() - t0)

    med = statistics.median(times_ns)
    return Result(name=name,
                  ns_per_op=med / ops if ops else 0.0,
                  total_ms=med / 1e6,
                  ops=ops,
                  syscalls=syscalls)


def table(results: List[Result], unit: str = "op", baseline_idx: int = 0) -> str:
    """Render results as a comparison table with speedup vs a chosen baseline."""
    if not results:
        return ""
    base = results[baseline_idx].total_ms
    w = max(len(r.name) for r in results) + 2
    lines = []
    head = (f"{'Strategy':<{w}}{'time (ms)':>11}{f'ns/{unit}':>12}"
            f"{'syscalls':>11}{'speedup':>10}")
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        sc = f"{r.syscalls:,}" if r.syscalls is not None else "?"
        speed = base / r.total_ms if r.total_ms else float("inf")
        # small ns/op values (e.g. per-byte) need more decimals to be meaningful
        nspo = f"{r.ns_per_op:.3f}" if r.ns_per_op < 10 else f"{r.ns_per_op:.1f}"
        lines.append(f"{r.name:<{w}}{r.total_ms:>11.2f}{nspo:>12}"
                     f"{sc:>11}{speed:>9.1f}x")
    return "\n".join(lines)
