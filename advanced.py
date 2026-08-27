"""
advanced.py — LEVEL 2: pushing past the basic optimizations.

The base project (experiments.py) established one idea: crossing into the kernel
costs ~300 ns, so make fewer crossings. This module goes further in three
directions the first project couldn't reach:

  A. io_uring        — stop paying per operation at all. Submit a whole BATCH
                       of I/O with a single syscall via shared ring buffers.
  B. Kernel-side ops — copy_file_range() lets the kernel copy a file without
                       the data ever entering your process, and on modern
                       filesystems it can be nearly instant (reflink).
  C. Telling the kernel what you're about to do — posix_fadvise() and madvise()
                       let you request readahead so data is already in memory
                       before you ask for it.

  D. And one honest finding: after removing the syscalls, the remaining cost in
     a Python program is the INTERPRETER, not the kernel. We measure that too,
     because optimizing the wrong layer is the classic beginner mistake.
"""

import os
import mmap
import ctypes
import tempfile
from typing import List

from bench import measure, Result

try:
    import iouring
    HAVE_IOURING = iouring.available()
except Exception:
    HAVE_IOURING = False

HAVE_COPY_FILE_RANGE = hasattr(os, "copy_file_range")
HAVE_FADVISE = hasattr(os, "posix_fadvise")
HAVE_MADVISE = hasattr(mmap.mmap, "madvise")

O_BINARY = getattr(os, "O_BINARY", 0)
READ_FLAGS = os.O_RDONLY | O_BINARY
WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | O_BINARY


def capabilities() -> dict:
    return {
        "io_uring (batched submission)": HAVE_IOURING,
        "copy_file_range (kernel copy)": HAVE_COPY_FILE_RANGE,
        "posix_fadvise (readahead hint)": HAVE_FADVISE,
        "madvise (mmap hint)": HAVE_MADVISE,
    }


def _make_file(size: int) -> str:
    path = tempfile.mkstemp(suffix=".bin")[1]
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    return path


# ---------------------------------------------------------------------------
# A. io_uring — N operations, ONE syscall.
#
# Baseline: pread() in a loop. Each call is its own syscall.
# io_uring:  fill N slots in a shared ring (plain memory writes, NO syscall),
#            then one io_uring_enter() submits and collects them all.
#
# This is the natural climax of the whole project: the earlier optimizations
# reduced the NUMBER of crossings; io_uring decouples the number of crossings
# from the number of operations entirely.
# ---------------------------------------------------------------------------
def expA_iouring(n_ops: int = 256, block: int = 4096,
                 batch: int = 64) -> List[Result]:
    if not HAVE_IOURING:
        return []

    size = n_ops * block
    path = _make_file(size)
    fd = os.open(path, READ_FLAGS)
    results = []

    def pread_loop():
        for i in range(n_ops):
            os.pread(fd, block, i * block)     # 1 syscall PER READ

    results.append(measure(f"pread() loop ({n_ops} reads)", pread_loop,
                           ops=n_ops, syscalls=n_ops))

    # Pre-allocate destination buffers AND the ring itself outside the timed
    # region. Creating a ring involves io_uring_setup plus several mmaps; real
    # code sets the ring up once at startup and reuses it, so including that
    # cost in a per-operation benchmark would be misleading.
    bufs = [ctypes.create_string_buffer(block) for _ in range(batch)]
    addrs = [ctypes.addressof(b) for b in bufs]
    ring = iouring.Ring(max(batch * 2, 64))

    def uring_batched():
        prep, submit, reap = ring.prep, ring.submit_batch, ring.reap
        op = iouring.IORING_OP_READ
        done = 0
        while done < n_ops:
            k = min(batch, n_ops - done)
            for j in range(k):
                prep(j, op, fd=fd, addr=addrs[j], length=block,
                     offset=(done + j) * block, user_data=j)
            submit(k)                          # ONE syscall for k reads
            reap(k)
            done += k

    n_batches = -(-n_ops // batch)
    results.append(measure(f"io_uring, batches of {batch}", uring_batched,
                           ops=n_ops, syscalls=n_batches))

    # --- DECOMPOSITION: where does io_uring's time actually go? -------------
    # Fill ring slots but never submit. This isolates the pure INTERPRETER cost
    # of building a request, with no kernel involvement whatsoever. Comparing
    # it against the full run tells us how much is Python and how much is kernel.
    def prep_only():
        prep = ring.prep
        op = iouring.IORING_OP_READ
        for i in range(n_ops):
            j = i % batch
            prep(j, op, fd=fd, addr=addrs[j], length=block,
                 offset=j * block, user_data=i)

    results.append(measure("  \u2514 of which: filling ring slots (no I/O)",
                           prep_only, ops=n_ops, syscalls=0))
    ring.close()

    os.close(fd)
    os.unlink(path)
    return results


# ---------------------------------------------------------------------------
# B. copy_file_range — the kernel copies the file for you.
#
# sendfile() already avoided the user-space round trip. copy_file_range() goes
# further: on filesystems that support it (XFS, Btrfs, and others) the kernel
# may not copy the data AT ALL — it can share the underlying blocks and mark
# them copy-on-write ("reflink"). The copy becomes nearly free regardless of
# file size.
#
# Watch the results: if you see an enormous speedup, you're seeing reflink, not
# fast copying. On filesystems without it, expect a modest gain instead.
# ---------------------------------------------------------------------------
def expB_copy_file_range(size: int = 32 << 20) -> List[Result]:
    src_path = _make_file(size)
    tmpdir = tempfile.mkdtemp()
    results = []

    def rw_copy():
        s = os.open(src_path, READ_FLAGS)
        d = os.open(os.path.join(tmpdir, "a.bin"), WRITE_FLAGS, 0o644)
        while True:
            b = os.read(s, 65536)
            if not b:
                break
            os.write(d, b)
        os.close(s); os.close(d)

    n_chunks = -(-size // 65536)
    results.append(measure("read()+write(), 64KB", rw_copy,
                           ops=size, syscalls=2 * n_chunks + 4))

    if hasattr(os, "sendfile"):
        def sf_copy():
            s = os.open(src_path, READ_FLAGS)
            d = os.open(os.path.join(tmpdir, "b.bin"), WRITE_FLAGS, 0o644)
            off = 0
            while off < size:
                sent = os.sendfile(d, s, off, size - off)
                if sent == 0:
                    break
                off += sent
            os.close(s); os.close(d)

        results.append(measure("sendfile() zero-copy", sf_copy,
                               ops=size, syscalls=6))

    if HAVE_COPY_FILE_RANGE:
        def cfr_copy():
            s = os.open(src_path, READ_FLAGS)
            d = os.open(os.path.join(tmpdir, "c.bin"), WRITE_FLAGS, 0o644)
            off_s = off_d = 0
            while off_s < size:
                n = os.copy_file_range(s, d, size - off_s, off_s, off_d)
                if n == 0:
                    break
                off_s += n; off_d += n
            os.close(s); os.close(d)

        results.append(measure("copy_file_range() kernel-side", cfr_copy,
                               ops=size, syscalls=6))

    os.unlink(src_path)
    return results


# ---------------------------------------------------------------------------
# C. Telling the kernel your plans (readahead hints).
#
# The kernel guesses your access pattern. You can just TELL it:
#   posix_fadvise(WILLNEED)  — "load this range now, I'll need it"
#   madvise(MADV_SEQUENTIAL) — "I'm reading this mapping front to back"
#
# The win here is not fewer syscalls: it's OVERLAP. The kernel starts fetching
# while your program is still doing other work, so the data is already resident
# when you touch it. Two extra syscalls can save thousands of page-fault stalls.
#
# CAVEAT: on a warm page cache the file is already in memory and hints do
# nothing. The honest way to read a small result here is "no measurable effect
# under these conditions", not "hints don't work".
# ---------------------------------------------------------------------------
def expC_hints(size: int = 32 << 20) -> List[Result]:
    path = _make_file(size)
    results = []

    def plain_mmap():
        fd = os.open(path, READ_FLAGS)
        mm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
        s = 0
        for i in range(0, len(mm), 4096):
            s += mm[i]
        mm.close(); os.close(fd)

    results.append(measure("mmap, no hint", plain_mmap, ops=size, syscalls=4))

    if HAVE_MADVISE:
        def hinted_mmap():
            fd = os.open(path, READ_FLAGS)
            mm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
            mm.madvise(mmap.MADV_SEQUENTIAL)
            mm.madvise(mmap.MADV_WILLNEED)     # 2 extra syscalls, big intent
            s = 0
            for i in range(0, len(mm), 4096):
                s += mm[i]
            mm.close(); os.close(fd)

        results.append(measure("mmap + madvise hints", hinted_mmap,
                               ops=size, syscalls=6))

    if HAVE_FADVISE:
        def fadvise_read():
            fd = os.open(path, READ_FLAGS)
            os.posix_fadvise(fd, 0, size, os.POSIX_FADV_WILLNEED)
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
            os.close(fd)

        n = -(-size // 65536) + 1
        results.append(measure("read() + fadvise(WILLNEED)", fadvise_read,
                               ops=size, syscalls=n + 3))

    os.unlink(path)
    return results


# ---------------------------------------------------------------------------
# D. THE HONEST ONE: once syscalls are gone, what's left?
#
# In the base project, buffered writing cut syscalls from 262,146 to 66 but only
# ran ~5x faster — while a single write() ran ~1000x faster. The gap is NOT the
# kernel. It's the Python loop appending one byte at a time.
#
# This experiment isolates that: same syscall count (one write), different
# amounts of interpreter work to build the data. It's a reminder that
# "optimize the syscalls" is only correct once the syscalls are actually the
# bottleneck.
# ---------------------------------------------------------------------------
def expD_where_time_goes(total: int = 1 << 20) -> List[Result]:
    tmpdir = tempfile.mkdtemp()
    results = []

    def build_byte_loop():
        buf = bytearray()
        for _ in range(total):
            buf += b"x"                        # interpreter-bound
        fd = os.open(os.path.join(tmpdir, "d1"), WRITE_FLAGS, 0o644)
        os.write(fd, buf); os.close(fd)

    def build_join():
        buf = b"x" * total                     # one C-level operation
        fd = os.open(os.path.join(tmpdir, "d2"), WRITE_FLAGS, 0o644)
        os.write(fd, buf); os.close(fd)

    def build_memoryview():
        buf = bytearray(total)
        mv = memoryview(buf)
        mv[:] = b"x" * total                   # bulk copy, no per-byte loop
        fd = os.open(os.path.join(tmpdir, "d3"), WRITE_FLAGS, 0o644)
        os.write(fd, buf); os.close(fd)

    for label, fn in (("build byte-by-byte + 1 write", build_byte_loop),
                      ("build in one op + 1 write", build_join),
                      ("memoryview bulk + 1 write", build_memoryview)):
        results.append(measure(label, fn, ops=total, syscalls=3))
    return results
