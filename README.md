# Measuring the Cost of Talking to the Kernel
### A systems performance study of system call overhead on Linux

**Author:** Md.Shaiful Alam
**Repository:** https://github.com/shishir1321x/Measuring-the-Cost-of-Talking-to-the-Kernel
**Video demo:** https://drive.google.com/file/d/1PVyIIEBp89WloVi0kOD9YV37R-D8ZhqU/view?usp=sharing

---

## 1. Introduction

A program running on Linux is not allowed to touch hardware. To read a file or
send a packet it must ask the kernel, and that request — a **system call** —
carries a fixed cost: the CPU switches privilege levels, saves and restores
state, and runs protected entry/exit code before returning.

I measured that cost on my machine at **335 nanoseconds per call.** Negligible
once; ruinous a million times over.

This project quantifies that overhead and evaluates ten techniques for reducing
it, from everyday buffering to `io_uring`, the modern batched-submission
interface. Every figure comes from executing real system calls on a real kernel
and timing them — nothing is simulated.

The thesis I set out to test was simple:

> **Fewer system calls means faster code.**

The thesis I finished with was more interesting:

> **Fewer system calls means faster code — but only when system calls were
> actually the bottleneck.** I measured two cases where cutting calls by 15x and
> 128x produced almost no gain and a net *slowdown* respectively.

## 2. What I built

| Component | Description |
|---|---|
| `bench.py` | Benchmark harness: warm-up passes, repeated trials, median reporting, exact syscall accounting. |
| `experiments.py` | Six baseline experiments (syscall cost, buffering, `writev`, `mmap`, `sendfile`, vDSO). |
| `iouring.py` | **A from-scratch `io_uring` binding written directly against the raw kernel ABI using `ctypes`** — Python has no standard binding. Implements ring setup, batched submission, and completion reaping. |
| `advanced.py` | Four advanced experiments including batched I/O and cost decomposition. |
| `visualize.py` | Generates comparison charts. |

**Headline results** (Linux 6.18, x86-64):

| Technique | Syscalls before → after | Speedup |
|---|---|---|
| Buffered writes | 262,146 → 3 | **1282x** |
| `writev`, small buffers | 8,002 → 502 | 4.3x |
| `writev`, large buffers | 8,002 → 502 | **1.3x only** |
| `mmap` vs `read()` loop | 2,051 → 4 | 2.2x |
| vDSO vs kernel trap | 50,000 → 0 | 7.4x |
| `io_uring` batching | 2,048 → 16 | **0.3x (slower)** |

The last two rows are the ones I'd defend in interview.

---

## 3. Challenges (STAR)

### Challenge 1 — My benchmark was measuring the wrong thing

**Situation.** My `mmap` experiment reported a 9x speedup over `read()`. The
number looked great, which is exactly why I distrusted it.

**Task.** I had been comparing an `mmap` loop that touched one byte per 4 KB page
against a `read()` loop that copied every byte into a user buffer. Both "read the
file," so I had treated them as equivalent.

**Action.** They were not equivalent — the `mmap` version moved a fraction of the
data. I rewrote it to copy the same chunks as the `read()` loop, so the *only*
difference was the syscall. I kept the page-touch variant as a separate row,
explicitly labelled an upper bound rather than a fair race.

**Result.** The honest speedup was **2.2x**, not 9x. I lost a flattering number
and gained a defensible one. This changed how I built every later experiment:
before trusting a result, I now ask what *else* differs between the two sides.

---

### Challenge 2 — A 15x syscall reduction that bought almost nothing

**Situation.** `writev` bundles multiple buffers into one syscall. I expected a
large win and measured only **1.3x**, despite cutting syscalls 15x.

**Task.** I had been treating "syscall count" as a proxy for "time" — assuming
that removing calls would proportionally remove cost.

**Action.** Rather than discard the result, I made buffer size the independent
variable and swept it from 16 bytes to 64 KB, plotting speedup against size.

**Result.** A clean decay curve from **5x down to below 1x**. With small buffers
the boundary crossing dominates, so removing it wins big; with large buffers the
data copy dominates and syscall reduction is irrelevant. Past ~32 KB, `writev` is
marginally *slower*. This became the central finding of the project — and
reframed it from a list of tricks into a study of when each applies.

---

### Challenge 3 — Writing an `io_uring` binding with no library to lean on

**Situation.** `io_uring` is the natural climax of a syscall-overhead project,
but Python has no standard binding and I had no `liburing` available.

**Task.** My earlier experiments used ordinary `os` module wrappers. This
required going a level lower than anything I had written before.

**Action.** I implemented the interface directly against the kernel ABI with
`ctypes`: invoking `io_uring_setup` by raw syscall number, `mmap`-ing the
submission and completion rings at their documented offsets, laying out the
64-byte submission queue entry with `struct.pack_into`, and handling the
`SINGLE_MMAP` feature flag. I validated correctness by comparing every byte
returned against `os.pread`.

**Result.** Working batched I/O: **2,048 reads submitted in 16 syscalls**, output
byte-identical to `pread`. I learned to read kernel documentation as a primary
source and to verify low-level code against a known-good reference rather than
trusting that it "looked right."

---

### Challenge 4 — The advanced technique made things *slower*

**Situation.** `io_uring` cut syscalls by 128x and ran at **0.3x the speed** of
the naive loop it replaced.

**Task.** My instinct was to assume a bug in my ring implementation, since the
correctness check had passed but the performance was backwards.

**Action.** Instead of guessing, I decomposed the cost: I measured a variant that
filled ring slots but never submitted them, isolating pure interpreter work from
kernel work. I also found and fixed a genuine benchmark flaw along the way —
ring creation (`io_uring_setup` plus several `mmap` calls) was inside the timed
region, which no real program would do.

**Result.** Filling one ring slot from Python cost **362 ns before any I/O
occurred** — more than the ~335 ns syscall it eliminated. **Python was spending
more to avoid the syscall than the syscall cost.** The mechanism was correct;
the language was the bottleneck. In C, that step is a few stores. This is the
result I am most pleased with, because the "failure" produced a sharper insight
than a success would have.

---

### Challenge 5 — My report claimed things my data didn't support

**Situation.** My output printed a conclusion — that `io_uring`'s kernel path was
faster once interpreter cost was removed — that was true on one machine and
false on another, where the printed numbers directly contradicted the sentence
beside them.

**Task.** I had hard-coded interpretations written while looking at one
particular run.

**Action.** I made the commentary conditional on the measured values, so the
program compares the figures at runtime and selects claims accordingly. Where a
result was inconclusive, it now says so and explains what conditions would change
it.

**Result.** The tool cannot overstate its own findings. I applied the same
discipline elsewhere: the readahead-hints experiment reports **no measurable
effect**, states that this means "no effect under these conditions" rather than
"hints don't work," and gives the command to drop the page cache and retest
properly. A negative result reported clearly is more valuable than a flattering
one.

---

### Challenge 6 — It crashed instantly on Windows

**Situation.** `FileNotFoundError: Could not find module 'libc.so.6'` — the code
died on import for anyone not on Linux.

**Task.** I had hard-coded Linux assumptions throughout: the C library name,
`writev`, `sendfile`, `clock_gettime`.

**Action.** I added capability detection at startup, with graceful fallbacks and
a report of what can and cannot be measured on the host. While auditing, I caught
a second latent bug: `os.open` defaults to *text mode* on Windows, which would
have silently corrupted binary test data and invalidated every byte count.

**Result.** The suite runs on Windows and macOS with adapted experiments,
degrading transparently rather than crashing or — worse — reporting corrupted
numbers as valid. I then ran the full six experiments under WSL2 to obtain the
complete Linux results.

---

## 4. Skills demonstrated

- **Systems programming:** direct kernel ABI use via `ctypes`; `mmap`, `writev`,
  `sendfile`, `copy_file_range`, `posix_fadvise`, and a hand-written `io_uring`
  ring-buffer implementation.
- **Performance engineering:** warm-up handling, median-of-repeats to reject
  outliers, cost decomposition to attribute time across layers, and parameter
  sweeps to locate crossover points.
- **Experimental rigour:** identifying and correcting unfair comparisons,
  validating low-level output against known-good references, and reporting
  negative results.
- **Scientific honesty:** runtime-conditional conclusions, explicit
  documentation of confounds (page cache, virtualization, interpreter overhead),
  and cross-validation between independent methods.

**Cross-validation worth highlighting.** I measured syscall cost two independent
ways: directly, by timing a `getpid` loop (**335 ns**), and indirectly, by
dividing time saved by syscalls eliminated in the buffering experiment
(**303 ns**). Two unrelated methods agreeing within 10% is what convinced me the
measurements were real rather than noise.

---

## 5. Limitations

- Measurements include Python interpreter overhead. Relative comparisons and
  syscall counts are exact; absolute timings would be lower in C.
- Run under WSL2, which taxes syscalls more heavily than bare metal and
  therefore flatters every optimization here.
- File I/O is served from the page cache, so these measure syscall and copy
  overhead rather than storage performance.
- The `io_uring` binding implements only the operations this benchmark needs and
  is a teaching implementation, not a production library.

## 6. Reproducing

```bash
git clone https://github.com/YOUR-USERNAME/YOUR-REPO
cd YOUR-REPO
python3 run_all.py --quick        # six baseline experiments
python3 run_advanced.py --quick   # io_uring and advanced techniques
pip install matplotlib && python3 visualize.py
```

Linux required for the full suite; WSL2 works. The scripts report their own
platform capabilities at startup.
