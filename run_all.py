"""
run_all.py — run every system-call experiment and print the results.

    python3 run_all.py            # run everything
    python3 run_all.py --quick    # smaller sizes, faster
    python3 run_all.py --only 2   # run just experiment 2
"""

import argparse
import platform
import os
import sys

from bench import table
import experiments as E


def banner(n, title, thesis):
    print("\n" + "=" * 78)
    print(f"EXPERIMENT {n}: {title}")
    print("=" * 78)
    print(thesis + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="smaller workloads")
    ap.add_argument("--only", type=int, default=None, help="run one experiment (1-6)")
    args = ap.parse_args()

    q = args.quick
    print("=" * 78)
    print("SYSTEM CALL OPTIMIZATION BENCHMARK")
    print("=" * 78)
    print(f"kernel : {platform.system()} {platform.release()}")
    print(f"machine: {platform.machine()}   python: {platform.python_version()}")
    print(f"cpus   : {os.cpu_count()}")
    print("\nCore thesis: a system call crosses the user/kernel boundary, and that")
    print("crossing costs real time. Every optimization here does the same thing:")
    print("do more work per crossing, or avoid crossing at all.")

    def want(n):
        return args.only is None or args.only == n

    if want(1):
        banner(1, "What does ONE system call cost?",
               "Compare an empty user-space call against a genuine kernel trap.\n"
               "The difference is the price of crossing the boundary.")
        r = E.exp1_syscall_cost(n=50_000 if q else 200_000)
        print(table(r, unit="call"))
        delta = r[1].ns_per_op - r[0].ns_per_op
        print(f"\n  -> A kernel trap costs about {delta:.0f} ns more than a plain "
              f"user-space call.")
        print(f"  -> At that price, 1 million syscalls \u2248 {delta:.0f} ms of pure overhead")
        print(f"     (1e6 calls \u00d7 {delta:.0f} ns = {delta:.0f} ms) \u2014 wasted crossing the boundary.")

    if want(2):
        banner(2, "BUFFERING \u2014 the biggest everyday win",
               "Write 1 MB to a file. Same bytes every time; only the number of\n"
               "write() syscalls changes.")
        r = E.exp2_buffering(total_bytes=(1 << 18) if q else (1 << 20))
        print(table(r, unit="byte"))
        print(f"\n  -> Buffering cut syscalls from {r[0].syscalls:,} to {r[-1].syscalls:,} "
              f"and ran {r[0].total_ms / r[-1].total_ms:.0f}x faster.")
        print("  -> This is why every language buffers I/O by default.")

    if want(3):
        banner(3, "SCATTER-GATHER (writev) \u2014 many buffers, one call",
               "Write several separate buffers. Naive code makes one write() per\n"
               "buffer; writev() hands the kernel the whole list at once.\n"
               "We test SMALL and LARGE buffers to find where it actually matters.")
        it = 500 if q else 2000
        for cs in (64, 4096):
            print(f"--- {cs}-byte buffers ---")
            r = E.exp3_writev(chunk_size=cs, iterations=it)
            print(table(r, unit="chunk"))
            print(f"  syscalls {r[0].syscalls:,} -> {r[1].syscalls:,} "
                  f"({r[0].syscalls // r[1].syscalls}x fewer), "
                  f"speedup {r[0].total_ms / r[1].total_ms:.1f}x\n")
        print("  -> KEY INSIGHT: fewer syscalls is not automatically faster.")
        print("     With SMALL buffers the syscall dominates, so writev wins big.")
        print("     With LARGE buffers the DATA COPY dominates, so cutting syscalls")
        print("     barely helps. Optimize the part that actually costs.")

    if want(4):
        banner(4, "mmap \u2014 reading a file with NO read() syscalls",
               "read() copies data kernel->user once per call. mmap maps the file\n"
               "into memory so the data arrives via page faults instead.")
        r = E.exp4_mmap(file_size=(8 << 20) if q else (32 << 20))
        print(table(r, unit="byte"))
        print(f"\n  -> Like-for-like (same chunks copied), mmap used "
              f"{r[2].syscalls} syscalls vs {r[0].syscalls:,} and ran "
              f"{r[0].total_ms / r[2].total_ms:.1f}x faster.")
        print("  -> *The last row does strictly LESS work (touches pages, doesn't")
        print("     bulk-copy), so treat it as an upper bound, not a fair race.")
        print("  -> Caveat: page faults aren't free. mmap wins on large files and")
        print("     random access; it can LOSE on small files or one-pass streaming.")

    if want(5):
        banner(5, "sendfile \u2014 zero-copy, data never enters your program",
               "Copying via read()+write() drags every byte into user space and\n"
               "back out. sendfile() moves it inside the kernel instead.")
        r = E.exp5_sendfile(file_size=(8 << 20) if q else (32 << 20))
        print(table(r, unit="byte"))
        print(f"\n  -> {r[0].syscalls:,} syscalls became {r[-1].syscalls}, and the bytes")
        print("     never made the pointless round trip through user memory.")
        print(f"  -> vs naive 4KB copying: {r[0].total_ms / r[-1].total_ms:.1f}x faster.")
        print("  -> Honest note: against an already well-tuned 64KB loop the gain is")
        print("     smaller here, because file->file copying stays in the page cache.")
        print("     sendfile's big win is file->SOCKET, which is how web servers")
        print("     serve static files without ever touching the data.")

    if want(6):
        banner(6, "vDSO \u2014 the system call that isn't one",
               "Linux maps a shared page into every process holding the current time,\n"
               "so clock_gettime() can be answered WITHOUT entering the kernel.")
        r = E.exp6_vdso(n=50_000 if q else 200_000)
        print(table(r, unit="call"))
        print(f"\n  -> The vDSO call is {r[1].total_ms / r[0].total_ms:.1f}x faster "
              f"because it never traps.")
        print("  -> The fastest system call is the one you never make.")

    print("\n" + "=" * 78)
    print("MEASUREMENT CAVEATS (state these in your report)")
    print("=" * 78)
    print("""
  * These are Python-level measurements, so each figure includes some
    interpreter overhead. Experiment 1 subtracts a baseline to isolate the trap,
    but the absolute numbers would be lower in C. The RELATIVE comparisons and
    the syscall COUNTS are the trustworthy part.
  * Results depend on the machine, kernel version, and whether Spectre/Meltdown
    mitigations are enabled (they add meaningful syscall cost). Numbers from a
    VM or container will differ from bare metal.
  * File I/O hits the page cache, not necessarily the disk. These measure
    syscall and copy overhead, not storage speed. Add fsync() or O_DIRECT if
    you want to involve real hardware.
  * Re-run on your own machine before quoting any absolute number.
""")


if __name__ == "__main__":
    main()
