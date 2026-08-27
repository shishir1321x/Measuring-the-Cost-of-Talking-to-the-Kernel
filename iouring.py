"""
iouring.py — a minimal io_uring binding written directly with ctypes.

WHAT io_uring IS
----------------
Every optimization in the base project reduces how OFTEN you cross into the
kernel. io_uring attacks the problem differently: it sets up two SHARED RING
BUFFERS in memory that both your program and the kernel can see.

  * You write requests into the SUBMISSION queue (plain memory writes, no
    syscall).
  * The kernel writes results into the COMPLETION queue (again, plain memory).
  * ONE io_uring_enter() syscall submits an entire BATCH.

So instead of "1 operation = 1 syscall", you get "N operations = 1 syscall".
With a kernel polling thread (SQPOLL mode) it can even reach ZERO syscalls in
the steady state.

This is the modern Linux answer to syscall overhead, added in kernel 5.1 (2019),
and it's what high-performance servers and databases use today.

NOTE: Python has no standard io_uring binding, so this is written from scratch
against the raw kernel ABI. It implements only what this benchmark needs
(NOP, READ, WRITE with batched submit + reap) — it is a teaching implementation,
not a complete or production-ready library.
"""
import os, ctypes, mmap, struct

libc = ctypes.CDLL("libc.so.6", use_errno=True)

NR_io_uring_setup, NR_io_uring_enter = 425, 426
IORING_OFF_SQ_RING, IORING_OFF_CQ_RING, IORING_OFF_SQES = 0, 0x8000000, 0x10000000
IORING_OP_NOP, IORING_OP_READ, IORING_OP_WRITE = 0, 22, 23
IORING_ENTER_GETEVENTS = 1
IORING_FEAT_SINGLE_MMAP = 1 << 0


class SQOff(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in
                ("head", "tail", "ring_mask", "ring_entries",
                 "flags", "dropped", "array", "resv1")] + [("user_addr", ctypes.c_uint64)]


class CQOff(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in
                ("head", "tail", "ring_mask", "ring_entries",
                 "overflow", "cqes", "flags", "resv1")] + [("user_addr", ctypes.c_uint64)]


class Params(ctypes.Structure):
    _fields_ = [("sq_entries", ctypes.c_uint32), ("cq_entries", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("sq_thread_cpu", ctypes.c_uint32),
                ("sq_thread_idle", ctypes.c_uint32), ("features", ctypes.c_uint32),
                ("wq_fd", ctypes.c_uint32), ("resv", ctypes.c_uint32 * 3),
                ("sq_off", SQOff), ("cq_off", CQOff)]


def u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def set_u32(buf, off, val):
    struct.pack_into("<I", buf, off, val)


class Ring:
    def __init__(self, entries=256):
        self.p = Params()
        ctypes.memset(ctypes.byref(self.p), 0, ctypes.sizeof(self.p))
        fd = libc.syscall(NR_io_uring_setup, entries, ctypes.byref(self.p))
        if fd < 0:
            raise OSError(ctypes.get_errno(), "io_uring_setup failed")
        self.fd = fd
        p = self.p
        sq_sz = p.sq_off.array + p.sq_entries * 4
        cq_sz = p.cq_off.cqes + p.cq_entries * 16
        if p.features & IORING_FEAT_SINGLE_MMAP:
            sz = max(sq_sz, cq_sz)
            self.sq_ring = mmap.mmap(fd, sz, offset=IORING_OFF_SQ_RING)
            self.cq_ring = self.sq_ring
        else:
            self.sq_ring = mmap.mmap(fd, sq_sz, offset=IORING_OFF_SQ_RING)
            self.cq_ring = mmap.mmap(fd, cq_sz, offset=IORING_OFF_CQ_RING)
        self.sqes = mmap.mmap(fd, p.sq_entries * 64, offset=IORING_OFF_SQES)
        self.sq_mask = u32(self.sq_ring, p.sq_off.ring_mask)
        self.cq_mask = u32(self.cq_ring, p.cq_off.ring_mask)

    # One packed write instead of a 64-byte clear plus four separate packs.
    # SQE layout (64 bytes): opcode,flags,ioprio,fd, off, addr, len, rw_flags,
    #                        user_data, buf_index, personality, splice_fd_in, pad[16]
    _SQE_FMT = "<BBHiQQIIQHHi16x"

    def prep(self, idx, opcode, fd=-1, addr=0, length=0, offset=0, user_data=0):
        struct.pack_into(self._SQE_FMT, self.sqes, idx * 64,
                         opcode, 0, 0, fd, offset, addr, length, 0,
                         user_data, 0, 0, 0)

    def submit_batch(self, n, wait=None):
        """Queue n already-prepared SQEs and submit them with ONE syscall."""
        p = self.p
        tail = u32(self.sq_ring, p.sq_off.tail)
        for i in range(n):
            set_u32(self.sq_ring, p.sq_off.array + ((tail + i) & self.sq_mask) * 4,
                    (tail + i) & self.sq_mask)
        set_u32(self.sq_ring, p.sq_off.tail, tail + n)
        if wait is None:
            wait = n
        r = libc.syscall(NR_io_uring_enter, self.fd, n, wait,
                         IORING_ENTER_GETEVENTS, 0, 0)
        if r < 0:
            raise OSError(ctypes.get_errno(), "io_uring_enter failed")
        return r

    def reap(self, n):
        p = self.p
        head = u32(self.cq_ring, p.cq_off.head)
        results = []
        for i in range(n):
            off = p.cq_off.cqes + ((head + i) & self.cq_mask) * 16
            ud, res, _fl = struct.unpack_from("<QiI", self.cq_ring, off)
            results.append(res)
        set_u32(self.cq_ring, p.cq_off.head, head + n)
        return results

    def close(self):
        os.close(self.fd)


def available() -> bool:
    """Can io_uring actually be used here? (kernel support, seccomp, etc.)"""
    try:
        r = Ring(8)
        r.close()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    r = Ring(256)
    print("io_uring_setup OK, fd =", r.fd,
          "| sq_entries =", r.p.sq_entries, "| features =", hex(r.p.features))
    N = 64
    for i in range(N):
        r.prep(i, IORING_OP_NOP, user_data=i)
    submitted = r.submit_batch(N)
    res = r.reap(N)
    print(f"submitted {submitted} NOPs in ONE syscall; "
          f"results all zero: {all(x == 0 for x in res)}")
    r.close()
