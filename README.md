# System Call Optimization: Measuring the User/Kernel Boundary

A benchmark suite that measures **real system call overhead on a real Linux
kernel** and demonstrates six ways to reduce it. Unlike a simulation, every
number here comes from actually executing syscalls and timing them.

## The one idea behind everything

Your program runs in **user mode** and cannot touch hardware directly. To read a
file or send a packet it must ask the **kernel**. That request is a **system
call**, and crossing the boundary is not free — the CPU switches privilege
levels, saves and restores registers, runs kernel entry/exit code (including
Spectre/Meltdown mitigations), and pollutes your caches on the way back.

Measured on this machine: **roughly 265–330 ns per syscall.** Tiny — until you
make a million of them, which is then a third of a second of pure overhead.

> **Every optimization in this project is the same idea in different clothes:
> do more work per boundary crossing, or don't cross at all.**

## Quick start

```bash
python3 run_all.py            # run all six experiments
python3 run_all.py --quick    # smaller workloads, faster
python3 run_all.py --only 2   # run just experiment 2
python3 visualize.py          # write charts to ./charts/  (needs matplotlib)
```

Requires **Linux** (uses `writev`, `sendfile`, `mmap`, and the vDSO). Pure
standard library except for the optional charts.

## Files

| File | Role |
|------|------|
| `bench.py` | Timing harness: warmup, repeats, medians, syscall accounting. |
| `experiments.py` | The six experiments, each heavily commented with the theory. |
| `run_all.py` | Runs everything and prints tables plus interpretation. |
| `visualize.py` | Three charts (see below). |

## The six experiments

**1. What does one syscall cost?** Times a genuine kernel trap (`getpid` via
`libc.syscall`, which can't be cached away) against an empty user-space call.
The difference *is* the boundary cost.

**2. Buffering.** Write 1 MB one byte at a time (262,146 syscalls) versus
buffered versus a single `write()` (3 syscalls). Same bytes, wildly different
cost. **This is the biggest and most practical win in the whole project.**

**3. Scatter-gather (`writev`).** Writing several buffers with one syscall
instead of one per buffer. Tested at both small and large buffer sizes — which
turns out to matter enormously (see the key finding below).

**4. `mmap`.** Map a file into memory so data arrives via page faults instead of
`read()` calls. Compared **like-for-like** (both copy the same chunks) so the
comparison is fair.

**5. `sendfile` (zero-copy).** Copy a file without the bytes ever entering your
process. `read()+write()` drags every byte into user space and back out;
`sendfile()` moves it inside the kernel.

**6. The vDSO.** Linux maps a page into every process holding the current time,
so `clock_gettime()` is answered in **user space with no trap at all**. The
purest demonstration of the thesis: the fastest syscall is the one you never make.

## Results (Linux 6.18, x86-64, Python 3.12 — re-run on your own machine)

```
EXPERIMENT 1 — cost of one boundary crossing
  empty user-space call      24.6 ns/call        0 syscalls
  real getpid() syscall     289.9 ns/call   50,000 syscalls
  -> a kernel trap costs ~265 ns more.  1M syscalls = ~265 ms wasted.

EXPERIMENT 2 — buffering (writing the same 1 MB)
  write() 1 byte at a time   88.66 ms    262,146 syscalls     1.0x
  buffered, 4KB buffer       12.94 ms         66 syscalls     6.8x
  buffered, 64KB buffer      12.84 ms          6 syscalls     6.9x
  single write() of all       0.25 ms          3 syscalls   348.2x

EXPERIMENT 3 — writev, 64-byte buffers
  16 separate write() calls   3.30 ms      8,002 syscalls     1.0x
  one writev()                0.87 ms        502 syscalls     3.8x
EXPERIMENT 3 — writev, 4096-byte buffers
  16 separate write() calls  34.77 ms      8,002 syscalls     1.0x
  one writev()               22.44 ms        502 syscalls     1.5x

EXPERIMENT 4 — mmap (32 MB file)
  read() loop, 4KB buffer     1.20 ms      2,051 syscalls     1.0x
  mmap + copy same chunks     0.61 ms          4 syscalls     2.0x
  mmap, touch pages only*     0.13 ms          4 syscalls     9.3x

EXPERIMENT 5 — sendfile (32 MB copy)
  read()+write() loop, 4KB    9.70 ms      4,100 syscalls     1.0x
  read()+write() loop, 64KB   5.32 ms        260 syscalls     1.8x
  sendfile() zero-copy        4.77 ms          6 syscalls     2.0x

EXPERIMENT 6 — vDSO
  clock_gettime() via vDSO    79.7 ns/call      0 syscalls     3.8x faster
  getpid() real kernel trap  299.7 ns/call 50,000 syscalls
```

`*` does strictly less work than the other rows — shown as an upper bound, not a
fair race.

## The key finding (the part worth writing up)

**Cutting syscalls is not automatically a speedup.** In Experiment 3, `writev`
reduces syscalls by **15x in both cases** — but delivers **3.8x** with 64-byte
buffers and only **1.5x** with 4 KB buffers.

The reason: with small buffers, the *boundary crossing* is the dominant cost, so
removing it wins big. With large buffers, the *data copy* dominates, and no
amount of syscall reduction touches that.

`charts/cost_vs_size.png` sweeps buffer size from 16 bytes to 64 KB and shows
the speedup decaying smoothly from ~5x to **below 1x** — past a certain size,
`writev` is actually slightly *slower*.

**The lesson: measure where the time actually goes before optimizing.** Same
change, same syscall reduction, radically different payoff depending on context.
That nuance is what separates a real performance study from a list of tricks.

## Charts

- `buffering.png` — syscall count vs time on log-log axes; a near-straight line
  showing the direct relationship.
- `speedups.png` — what each optimization actually buys (log scale, since
  buffering dwarfs everything).
- `cost_vs_size.png` — **the most interesting one**: when syscall optimization
  stops being worth it.

## Measurement caveats (state these in your report)

- These are **Python-level** measurements and include interpreter overhead.
  Experiment 1 subtracts a baseline to isolate the trap, but absolute numbers
  would be lower in C. **The relative comparisons and the syscall counts are the
  trustworthy parts.**
- Results depend on machine, kernel version, and whether Spectre/Meltdown
  mitigations are active (they meaningfully increased syscall cost after ~2018).
  A VM or container will differ from bare metal — this run was in a VM.
- File I/O here hits the **page cache**, not the disk. These measure syscall and
  copy overhead, not storage speed. Add `fsync()` or `O_DIRECT` to involve real
  hardware.
- Every figure is a median of repeated runs, but a 1-CPU machine is noisier than
  a quiet multi-core box. Re-run before quoting any absolute number.

## Extension ideas

- **Port the hot loops to C** and re-measure — quantify how much of the cost was
  Python. This is the single best next step for rigor.
- **Verify syscall counts empirically** with `strace -c -f python3 run_all.py`
  instead of counting analytically. (`strace` wasn't available here.)
- **Add `io_uring`** — the modern Linux interface where you submit *batches* of
  I/O through shared ring buffers, often with zero syscalls per operation. This
  is the state of the art and the natural climax of the project.
- **Measure network syscalls**: `send()` per message vs `sendmmsg()` batching;
  this is where syscall overhead hurts most in practice.
- **Show the cache pollution effect**: measure how much slower your *user-space*
  code runs immediately after a syscall, versus in a tight loop with none.
- **Sweep against CPU mitigations** (`mitigations=off` at boot, if you control
  the machine) to isolate how much of the trap cost is Spectre/Meltdown defense.
