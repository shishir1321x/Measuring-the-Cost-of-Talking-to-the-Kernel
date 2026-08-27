"""
run_advanced.py — LEVEL 2 optimizations, beyond the basics.

    python3 run_advanced.py
    python3 run_advanced.py --quick
    python3 run_advanced.py --only A

Requires Linux. io_uring needs kernel 5.1+ (2019) and must not be blocked by
seccomp; the script reports whether it's usable before running.
"""

import argparse
import platform
import os

from bench import table
import advanced as A


def banner(tag, title, body):
    print("\n" + "=" * 78)
    print(f"[{tag}] {title}")
    print("=" * 78)
    print(body + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", type=str, default=None, help="A, B, C or D")
    args = ap.parse_args()
    q = args.quick

    print("=" * 78)
    print("ADVANCED SYSTEM CALL OPTIMIZATION  (level 2)")
    print("=" * 78)
    print(f"kernel : {platform.system()} {platform.release()}")
    print(f"python : {platform.python_version()}   cpus: {os.cpu_count()}")
    caps = A.capabilities()
    print("\navailable here:")
    for k, v in caps.items():
        print(f"  {'YES' if v else 'no '}  {k}")

    def want(t):
        return args.only is None or args.only.upper() == t

    if want("A"):
        banner("A", "io_uring \u2014 N operations, ONE syscall",
               "Earlier experiments reduced HOW OFTEN we cross into the kernel.\n"
               "io_uring breaks the link between operations and crossings entirely:\n"
               "requests go into a shared ring buffer in memory, and a single\n"
               "io_uring_enter() submits the whole batch.")
        r = A.expA_iouring(n_ops=256 if q else 2048,
                           batch=64 if q else 128)
        if not r:
            print("  io_uring not available on this system \u2014 skipped.")
        else:
            print(table(r, unit="read"))
            print(f"\n  -> MECHANISM CONFIRMED: {r[0].syscalls:,} syscalls became "
                  f"{r[1].syscalls} \u2014 {r[0].syscalls // r[1].syscalls}x fewer")
            print("     boundary crossings for byte-identical work (verified against pread).")
            sp = r[0].total_ms / r[1].total_ms
            print(f"\n  -> AND YET it is {sp:.2f}x the speed of the simple pread() loop.")
            print("     The third row explains why. Filling ring slots from Python costs")
            prep_ns = r[2].ns_per_op
            kern_ns = r[1].ns_per_op - r[2].ns_per_op
            print(f"     {prep_ns:.0f} ns per request \u2014 before ANY I/O happens. That is more")
            print(f"     than the ~300 ns syscall it saves, so Python eats the entire gain.")
            print(f"\n  -> Strip that away and io_uring's own path costs {kern_ns:.0f} ns/op")
            print(f"     versus {r[0].ns_per_op:.0f} ns/op for pread().")
            if kern_ns < r[0].ns_per_op:
                print("     So the kernel side really IS faster \u2014 the overhead that")
                print("     erased the win is the LANGUAGE, not the interface.")
            else:
                print("     Here it is still not ahead: with few, small batches the fixed")
                print("     submit/reap cost isn't amortised. Try a larger --only A run")
                print("     (bigger n_ops and batch) and watch this figure fall.")
            print("\n  -> THE LESSON: an optimization only pays if the thing it removes was")
            print("     actually your bottleneck. In C, filling an SQE is a few stores")
            print("     (~5 ns) and this becomes a large win; from Python it cannot.")
            print("     io_uring's full power needs C, high concurrency, and SQPOLL mode,")
            print("     where a kernel thread polls the ring and steady-state syscalls")
            print("     drop to ZERO.")

    if want("B"):
        banner("B", "copy_file_range \u2014 let the kernel do the copy",
               "sendfile() kept data out of user space. copy_file_range() goes\n"
               "further: on some filesystems the kernel can share blocks instead of\n"
               "copying them (reflink), making the copy nearly free.")
        r = A.expB_copy_file_range(size=(8 << 20) if q else (32 << 20))
        print(table(r, unit="byte"))
        if len(r) >= 3:
            sp = r[0].total_ms / r[-1].total_ms
            print(f"\n  -> copy_file_range was {sp:.1f}x faster than the read/write loop.")
            if sp > 20:
                print("  -> A speedup this large means the filesystem supports REFLINK:")
                print("     no bytes were copied at all, just block references shared.")
            else:
                print("  -> A modest speedup means this filesystem still copies the")
                print("     bytes, just without a user-space round trip.")

    if want("C"):
        banner("C", "Readahead hints \u2014 telling the kernel your plans",
               "posix_fadvise() and madvise() let you say 'I'm about to read this'.\n"
               "The kernel then fetches data WHILE you work, instead of stalling you\n"
               "on a page fault later. This costs 2 extra syscalls to save thousands\n"
               "of stalls.")
        r = A.expC_hints(size=(8 << 20) if q else (32 << 20))
        print(table(r, unit="byte"))
        print("\n  -> IMPORTANT: these files were just written, so they're already in")
        print("     the page cache and the hints have little to do. A small or zero")
        print("     effect here means 'no effect UNDER THESE CONDITIONS', not that")
        print("     hints are useless. To see the real benefit you must drop the")
        print("     cache first (needs root):")
        print("       sync && echo 3 | sudo tee /proc/sys/vm/drop_caches")

    if want("D"):
        banner("D", "Where does the time ACTUALLY go?",
               "In the base project, buffering cut syscalls 4000x but ran only ~5x\n"
               "faster, while a single write() ran ~1000x faster. That gap was never\n"
               "the kernel \u2014 it was the Python loop. Same syscall count below;\n"
               "only the interpreter work differs.")
        r = A.expD_where_time_goes(total=(1 << 18) if q else (1 << 20))
        print(table(r, unit="byte"))
        print(f"\n  -> All three make the SAME 3 syscalls, yet differ by "
              f"{r[0].total_ms / min(x.total_ms for x in r[1:]):.0f}x.")
        print("  -> That entire difference is interpreter overhead, not kernel cost.")
        print("  -> THE LESSON: 'reduce syscalls' is only the right answer once")
        print("     syscalls are actually your bottleneck. Measure first, then")
        print("     optimize the layer that's genuinely slow.")

    print("\n" + "=" * 78)
    print("CAVEATS")
    print("=" * 78)
    print("""
  * Python-level measurements: interpreter overhead is included, and for
    io_uring it is substantial (filling ring slots costs real time). Syscall
    COUNTS are exact; timings are indicative.
  * copy_file_range and reflink behaviour depend entirely on the filesystem.
  * Readahead hints do nothing on a warm page cache. Drop caches to test fairly.
  * WSL2 and VMs tax syscalls more than bare metal, which flatters every
    optimization here. Re-run natively before quoting numbers.
""")


if __name__ == "__main__":
    main()
