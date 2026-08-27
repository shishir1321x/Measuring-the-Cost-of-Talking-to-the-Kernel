"""
visualize.py — charts for the system-call optimization project.

    python3 visualize.py        # writes PNGs into ./charts/

  * buffering.png    — the headline: syscall count vs time, log-log.
  * speedups.png     — speedup achieved by each optimization.
  * cost_vs_size.png — WHEN syscall optimization matters (the nuanced result).
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import experiments as E

OUT = "charts"


def chart_buffering():
    r = E.exp2_buffering(total_bytes=1 << 20)
    calls = [x.syscalls for x in r]
    times = [x.total_ms for x in r]
    labels = ["1 byte/write", "4KB buffer", "64KB buffer", "single write"]

    fig, ax = plt.subplots(figsize=(8, 5.5))
    ax.plot(calls, times, "o-", color="#264653", lw=2, ms=9)
    for c, t, l in zip(calls, times, labels):
        ax.annotate(l, (c, t), xytext=(8, 6), textcoords="offset points", fontsize=9)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Number of write() system calls  (log scale)")
    ax.set_ylabel("Time to write 1 MB, ms  (log scale)")
    ax.set_title("Fewer system calls = less time (writing the same 1 MB)")
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(f"{OUT}/buffering.png", dpi=130)
    plt.close(fig)


def chart_speedups():
    """One bar per optimization: how much faster than its naive baseline."""
    pairs = []

    b = E.exp2_buffering(total_bytes=1 << 20)
    pairs.append(("Buffering\n(vs 1 byte/write)", b[0].total_ms / b[-1].total_ms))

    w_small = E.exp3_writev(chunk_size=64, iterations=2000)
    pairs.append(("writev, 64B bufs\n(vs N writes)",
                  w_small[0].total_ms / w_small[1].total_ms))

    w_big = E.exp3_writev(chunk_size=4096, iterations=2000)
    pairs.append(("writev, 4KB bufs\n(vs N writes)",
                  w_big[0].total_ms / w_big[1].total_ms))

    m = E.exp4_mmap(file_size=32 << 20)
    pairs.append(("mmap\n(vs 4KB read loop)", m[0].total_ms / m[2].total_ms))

    s = E.exp5_sendfile(file_size=32 << 20)
    pairs.append(("sendfile\n(vs 4KB copy loop)", s[0].total_ms / s[-1].total_ms))

    v = E.exp6_vdso(n=200_000)
    pairs.append(("vDSO\n(vs real trap)", v[1].total_ms / v[0].total_ms))

    names = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]
    colors = ["#2a9d8f" if v >= 2 else "#e9c46a" for v in vals]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    bars = ax.bar(names, vals, color=colors)
    ax.axhline(1.0, color="#c1121f", ls="--", lw=1, label="no improvement")
    ax.set_ylabel("Speedup vs naive approach (x)")
    ax.set_yscale("log")
    ax.set_title("How much each system-call optimization actually buys you")
    for b_, v_ in zip(bars, vals):
        ax.text(b_.get_x() + b_.get_width() / 2, v_ * 1.06, f"{v_:.1f}x",
                ha="center", fontsize=10, fontweight="bold")
    ax.legend()
    ax.grid(axis="y", alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(f"{OUT}/speedups.png", dpi=130)
    plt.close(fig)


def chart_cost_vs_size():
    """
    The nuanced result: cutting syscalls helps a LOT for small transfers and
    barely at all for large ones, because the data copy takes over.
    """
    sizes = [16, 64, 256, 1024, 4096, 16384, 65536]
    speedups = []
    for cs in sizes:
        it = max(200, 200_000 // cs)
        r = E.exp3_writev(chunks=16, chunk_size=cs, iterations=it)
        speedups.append(r[0].total_ms / r[1].total_ms)

    fig, ax = plt.subplots(figsize=(8, 5.5))
    ax.plot(sizes, speedups, "o-", color="#e76f51", lw=2, ms=8)
    ax.axhline(1.0, color="#888", ls="--", lw=1)
    ax.set_xscale("log", base=2)
    ax.set_xlabel("Size of each buffer (bytes, log scale)")
    ax.set_ylabel("writev() speedup vs separate write() calls")
    ax.set_title("Syscall optimization matters most for SMALL operations")
    ax.grid(alpha=0.3, which="both")
    ax.annotate("syscall overhead dominates\n-> big win",
                xy=(32, speedups[1]), xytext=(40, max(speedups) * 0.75),
                fontsize=9, arrowprops=dict(arrowstyle="->", color="#555"))
    ax.annotate("data copy dominates\n-> little to gain",
                xy=(32768, speedups[-1]), xytext=(2000, min(speedups) + 0.3),
                fontsize=9, arrowprops=dict(arrowstyle="->", color="#555"))
    fig.tight_layout()
    fig.savefig(f"{OUT}/cost_vs_size.png", dpi=130)
    plt.close(fig)


def main():
    os.makedirs(OUT, exist_ok=True)
    print("generating buffering.png ...");    chart_buffering()
    print("generating speedups.png ...");     chart_speedups()
    print("generating cost_vs_size.png ..."); chart_cost_vs_size()
    print(f"done -> ./{OUT}/")


if __name__ == "__main__":
    main()
