#!/usr/bin/env python3
"""Dump GPU registers through the command processor (CP_REG_TO_MEM) using the
msm DRM ioctls directly.  Reads only registers outside the CP protect ranges.
  regdump.py <regdump-list.txt> [out.txt]"""
import ctypes as C, fcntl, mmap, os, struct, sys, time
LIST = sys.argv[1]; OUT = sys.argv[2] if len(sys.argv) > 2 else "/dev/stdout"
regs = [(l.split()[0], int(l.split()[1], 16)) for l in open(LIST) if l.strip()]
fd = os.open("/dev/dri/renderD128", os.O_RDWR)
def IOWR(nr, size): return (3 << 30) | (size << 16) | (ord('d') << 8) | nr
def IOW(nr, size): return (1 << 30) | (size << 16) | (ord('d') << 8) | nr
def gem_new(size, flags=0x20000):  # MSM_BO_WC
    b = bytearray(struct.pack("QII", size, flags, 0)); fcntl.ioctl(fd, IOWR(0x42, 16), b); return struct.unpack("QII", b)[2]
def gem_info(handle, info):
    b = bytearray(struct.pack("IIQII", handle, info, 0, 0, 0)); fcntl.ioctl(fd, IOWR(0x43, 24), b); return struct.unpack("IIQII", b)[2]
def gem_map(handle, size):
    off = gem_info(handle, 0); return mmap.mmap(fd, size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=off)
def parity(v): return (0x9669 >> (0xf & (v ^ (v >> 4) ^ (v >> 8) ^ (v >> 12)))) & 1
def pkt7(opcode, cnt): return 0x70000000 | (cnt & 0x3fff) | (parity(cnt) << 15) | ((opcode & 0x7f) << 16) | (parity(opcode) << 23)
# submit queue (normal priority)
q = bytearray(struct.pack("III", 0, 6, 0)); fcntl.ioctl(fd, IOWR(0x4A, 12), q); queueid = struct.unpack("III", q)[2]
dst_size = 4096 * ((len(regs) * 4 + 4095) // 4096); cmd_size = 4096 * ((len(regs) * 16 + 4095) // 4096)
dst = gem_new(dst_size); cmd = gem_new(cmd_size); dst_iova = gem_info(dst, 1); cmd_iova = gem_info(cmd, 1)
dm = gem_map(dst, dst_size); cm = gem_map(cmd, cmd_size)
dm[:dst_size] = b"\xee" * dst_size  # sentinel: anything still 0xeeeeeeee was not written by the GPU
words = []
for i, (name, off) in enumerate(regs):
    addr = dst_iova + 4 * i
    words += [pkt7(62, 3), off & 0x3ffff, addr & 0xffffffff, (addr >> 32) & 0xffffffff]  # CP_REG_TO_MEM, CNT=0 (one register)
words += [pkt7(0x26, 0)]  # CP_WAIT_FOR_IDLE
cm[:len(words) * 4] = struct.pack("<%dI" % len(words), *words)
bos = struct.pack("IIQ", 2, dst, 0) + struct.pack("IIQ", 1, cmd, 0)  # WRITE dst, READ cmd
cmds = struct.pack("IIIIIIQ", 1, 1, 0, len(words) * 4, 0, 0, 0)       # MSM_SUBMIT_CMD_BUF, bo index 1
bos_buf = C.create_string_buffer(bos); cmds_buf = C.create_string_buffer(cmds)
sub = bytearray(struct.pack("IIIIQQiIQQIIII", 0x10, 0, 2, 1, C.addressof(bos_buf), C.addressof(cmds_buf), -1, queueid, 0, 0, 0, 0, 0, 0))
fcntl.ioctl(fd, IOWR(0x46, len(sub)), sub); fence = struct.unpack("IIIIQQiIQQIIII", sub)[1]
deadline = time.clock_gettime(time.CLOCK_MONOTONIC) + 5.0  # the msm timeout is an absolute monotonic time
wf = struct.pack("IIqqII", fence, 0, int(deadline), int((deadline % 1) * 1e9), queueid, 0); fcntl.ioctl(fd, IOW(0x47, len(wf)), wf)
time.sleep(0.05)
vals = struct.unpack("<%dI" % len(regs), dm[:len(regs) * 4])
with open(OUT, "w") as f:
    for (name, off), v in zip(regs, vals): f.write(f"{name:48s} 0x{off:05x} {'UNWRITTEN' if v == 0xeeeeeeee else '0x%08x' % v}\n")
print(f"{len(regs)} registers read, {sum(1 for v in vals if v == 0xeeeeeeee)} unwritten, fence {fence}")
