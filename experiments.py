"""
experiments.py — six real, measurable system-call optimizations.

WHY SYSTEM CALLS ARE EXPENSIVE
------------------------------
Your program runs in "user mode" — a restricted mode where it cannot touch
hardware directly. To read a file or send a packet it must ask the kernel, which
runs in "kernel mode". That request is a SYSTEM CALL, and crossing the boundary
is not free. Each crossing must:

  * switch the CPU's privilege level and swap stacks,
  * save and later restore registers,
  * run kernel entry/exit code, including Spectre/Meltdown mitigations on
    modern CPUs (these made syscalls markedly more expensive after ~2018),
  * pollute the CPU cache and branch predictors, so your code runs slower for
    a while *after* returning.

The cost is roughly a few hundred nanoseconds per call on typical x86-64 Linux.
That sounds tiny — until you make a million of them.

THE ONE BIG IDEA
----------------
Every optimization below is the same idea wearing different clothes:
    ***  DO MORE WORK PER BOUNDARY CROSSING, OR DON'T CROSS AT ALL.  ***
"""

import os
import io
import mmap
import ctypes
import socket
import tempfile
from typing import List

from bench import measure, Result

libc = ctypes.CDLL("libc.so.6", use_errno=True)
SYS_getpid = 39            # x86-64 syscall number for getpid()


# ---------------------------------------------------------------------------
# EXPERIMENT 1 — How much does ONE system call actually cost?
#
# We call getpid() the "hard way": libc.syscall() forces a genuine trap into
# the kernel every time. (Python's os.getpid() and even glibc's getpid() may
# avoid or cache the trap, which would hide exactly what we want to measure.)
# We subtract the cost of an empty Python loop so we isolate the trap itself.
# ---------------------------------------------------------------------------
def exp1_syscall_cost(n: int = 200_000) -> List[Result]:
    def do_syscall():
        s = libc.syscall
        for _ in range(n):
            s(SYS_getpid)

    def do_nothing():
        def noop():
            pass
        for _ in range(n):
            noop()

    return [
        measure("empty user-space call", do_nothing, ops=n, syscalls=0),
        measure("real getpid() syscall", do_syscall, ops=n, syscalls=n),
    ]


# ---------------------------------------------------------------------------
# EXPERIMENT 2 — BUFFERING: the single biggest win in everyday code.
#
# Writing a file one byte at a time issues ONE SYSCALL PER BYTE. Buffering
# collects bytes in user-space memory and issues one syscall per full buffer.
# Same bytes written, thousands of times fewer boundary crossings.
#
# This is exactly why every language's standard library gives you buffered I/O
# by default — and why code that calls write() in a tight loop is so slow.
# ---------------------------------------------------------------------------
def exp2_buffering(total_bytes: int = 1 << 20) -> List[Result]:
    payload = b"x"
    results = []
    tmpdir = tempfile.mkdtemp()

    def unbuffered():
        path = os.path.join(tmpdir, "unbuf")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        for _ in range(total_bytes):
            os.write(fd, payload)          # 1 syscall per byte!
        os.close(fd)

    results.append(measure("write() 1 byte at a time", unbuffered,
                           ops=total_bytes, syscalls=total_bytes + 2))

    for bufsize in (4096, 65536):
        def buffered(bufsize=bufsize):
            path = os.path.join(tmpdir, f"buf{bufsize}")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            buf = bytearray()
            for _ in range(total_bytes):
                buf += payload
                if len(buf) >= bufsize:
                    os.write(fd, buf)      # 1 syscall per FULL BUFFER
                    buf.clear()
            if buf:
                os.write(fd, buf)
            os.close(fd)

        n_calls = -(-total_bytes // bufsize)
        results.append(measure(f"buffered, {bufsize//1024}KB buffer", buffered,
                               ops=total_bytes, syscalls=n_calls + 2))

    def one_shot():
        path = os.path.join(tmpdir, "oneshot")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.write(fd, b"x" * total_bytes)   # exactly 1 write syscall
        os.close(fd)

    results.append(measure("single write() of everything", one_shot,
                           ops=total_bytes, syscalls=3))
    return results


# ---------------------------------------------------------------------------
# EXPERIMENT 3 — SCATTER/GATHER I/O (writev).
#
# Suppose you must write several separate buffers (an HTTP header, a body, a
# trailer). The naive way is one write() per buffer. writev() hands the kernel
# a LIST of buffers and writes them all in ONE syscall — no copying them into a
# single combined buffer first, and no extra boundary crossings.
#
# Real servers use this constantly to emit headers+body without concatenating.
# ---------------------------------------------------------------------------
def exp3_writev(chunks: int = 16, chunk_size: int = 4096,
                iterations: int = 2000) -> List[Result]:
    bufs = [os.urandom(chunk_size) for _ in range(chunks)]
    joined = b"".join(bufs)
    tmpdir = tempfile.mkdtemp()

    def many_writes():
        fd = os.open(os.path.join(tmpdir, f"many{chunk_size}"),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        for _ in range(iterations):
            for b in bufs:
                os.write(fd, b)            # `chunks` syscalls per iteration
        os.close(fd)

    def gathered():
        fd = os.open(os.path.join(tmpdir, f"vec{chunk_size}"),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        for _ in range(iterations):
            os.writev(fd, bufs)            # ONE syscall for all buffers
        os.close(fd)

    def precombined():
        fd = os.open(os.path.join(tmpdir, f"cat{chunk_size}"),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        for _ in range(iterations):
            os.write(fd, joined)           # 1 syscall, but we paid to concatenate
        os.close(fd)

    ops = iterations * chunks
    return [
        measure(f"{chunks} separate write() calls", many_writes,
                ops=ops, syscalls=iterations * chunks + 2),
        measure("one writev() (scatter-gather)", gathered,
                ops=ops, syscalls=iterations + 2),
        measure("pre-join then one write()", precombined,
                ops=ops, syscalls=iterations + 2),
    ]


# ---------------------------------------------------------------------------
# EXPERIMENT 4 — mmap: reading a file with ZERO read() syscalls.
#
# read() copies data kernel -> user buffer, once per call. mmap() instead maps
# the file straight into your address space: you touch memory, and the kernel
# pages data in on demand via page faults. After the map is set up, sequential
# access issues NO read syscalls at all.
#
# Trade-off worth stating in a report: page faults aren't free either, and mmap
# can lose for small files or one-pass streaming. It shines on large files and
# random access.
# ---------------------------------------------------------------------------
def exp4_mmap(file_size: int = 32 << 20) -> List[Result]:
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "big.bin")
    with open(path, "wb") as f:
        f.write(os.urandom(file_size))

    results = []

    # --- baseline: read() in a loop. Each call = 1 syscall + 1 kernel->user copy
    for bufsize in (4096, 65536):
        def read_loop(bufsize=bufsize):
            fd = os.open(path, os.O_RDONLY)
            while True:
                b = os.read(fd, bufsize)
                if not b:
                    break
            os.close(fd)

        n_calls = -(-file_size // bufsize) + 1
        results.append(measure(f"read() loop, {bufsize//1024}KB buffer", read_loop,
                               ops=file_size, syscalls=n_calls + 2))

    # --- FAIR comparison: mmap, but still COPY every byte out in the same chunk
    #     size. Same data movement as read(); the only thing removed is the
    #     per-chunk syscall. This isolates the syscall cost specifically.
    def mmap_chunks():
        fd = os.open(path, os.O_RDONLY)
        mm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
        bufsize = 4096
        pos, n = 0, len(mm)
        while pos < n:
            _ = mm[pos:pos + bufsize]      # a copy, but NO syscall
            pos += bufsize
        mm.close()
        os.close(fd)

    results.append(measure("mmap() + copy same chunks", mmap_chunks,
                           ops=file_size, syscalls=4))

    # --- best case: mmap and only TOUCH pages (no bulk copy at all).
    #     Faster still, but note it does strictly less work than the others,
    #     so it is shown as an upper bound rather than a like-for-like result.
    def mmap_touch():
        fd = os.open(path, os.O_RDONLY)
        mm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
        s = 0
        for i in range(0, len(mm), 4096):
            s += mm[i]
        mm.close()
        os.close(fd)

    results.append(measure("mmap() touch pages only*", mmap_touch,
                           ops=file_size, syscalls=4))
    return results


# ---------------------------------------------------------------------------
# EXPERIMENT 5 — sendfile: ZERO-COPY file transfer.
#
# Copying a file the normal way means read() into user memory, then write() back
# out: two syscalls and two data copies per chunk, with the bytes making a
# pointless round trip through your process. sendfile() tells the kernel to move
# the data internally — the bytes never enter user space at all.
#
# This is how high-performance web servers serve static files.
# ---------------------------------------------------------------------------
def exp5_sendfile(file_size: int = 32 << 20) -> List[Result]:
    tmpdir = tempfile.mkdtemp()
    src_path = os.path.join(tmpdir, "src.bin")
    with open(src_path, "wb") as f:
        f.write(os.urandom(file_size))

    results = []

    for bufsize in (4096, 65536):
        def read_write_copy(bufsize=bufsize):
            src = os.open(src_path, os.O_RDONLY)
            dst = os.open(os.path.join(tmpdir, f"dst{bufsize}.bin"),
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            while True:
                b = os.read(src, bufsize)
                if not b:
                    break
                os.write(dst, b)
            os.close(src); os.close(dst)

        n_chunks = -(-file_size // bufsize)
        results.append(measure(f"read()+write() loop, {bufsize//1024}KB",
                               read_write_copy,
                               ops=file_size, syscalls=2 * n_chunks + 4))

    def sendfile_copy():
        src = os.open(src_path, os.O_RDONLY)
        dst = os.open(os.path.join(tmpdir, "dst_sf.bin"),
                      os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        offset = 0
        while offset < file_size:
            sent = os.sendfile(dst, src, offset, file_size - offset)
            if sent == 0:
                break
            offset += sent
        os.close(src); os.close(dst)

    results.append(measure("sendfile() zero-copy", sendfile_copy,
                           ops=file_size, syscalls=6))
    return results


# ---------------------------------------------------------------------------
# EXPERIMENT 6 — the vDSO: a "system call" that never enters the kernel.
#
# Some calls are so common (getting the time) that trapping into the kernel
# every time would be wasteful. Linux maps a small shared library called the
# vDSO into every process; the kernel keeps a timestamp there and updates it,
# so clock_gettime() can read it in USER SPACE with no trap at all.
#
# Comparing it to a genuine trap shows the boundary cost in isolation. This is
# the purest illustration of the project's thesis: the fastest system call is
# the one you don't make.
# ---------------------------------------------------------------------------
def exp6_vdso(n: int = 200_000) -> List[Result]:
    import time as _time

    def vdso_clock():
        cg = _time.clock_gettime
        m = _time.CLOCK_MONOTONIC
        for _ in range(n):
            cg(m)                          # served by vDSO — no kernel trap

    def real_trap():
        s = libc.syscall
        for _ in range(n):
            s(SYS_getpid)                  # genuine kernel trap

    return [
        measure("clock_gettime() via vDSO", vdso_clock, ops=n, syscalls=0),
        measure("getpid() real kernel trap", real_trap, ops=n, syscalls=n),
    ]
