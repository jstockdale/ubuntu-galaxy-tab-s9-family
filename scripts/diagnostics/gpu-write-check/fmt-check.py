#!/usr/bin/env python3
"""Same lost-write check for other pixel formats, to see which bits of the
memory write get hit.  Draws solid colours into a LINEAR dma-buf of the given
format and compares the CPU view of memory byte by byte with the expected
encoding.  fmt-check.py <rgba8|r8|rgb565|rgb10a2|rgba16f> [frames]"""
import ctypes as C, os, sys, mmap, fcntl, struct, collections
os.environ.setdefault("EGL_PLATFORM", "surfaceless")
fmt = sys.argv[1] if len(sys.argv) > 1 else "rgba8"; frames = int(sys.argv[2]) if len(sys.argv) > 2 else 12; W, H = 2560, 1600
def h16(v):  # float -> half bits (v in [0,1])
    import struct as st; return st.unpack("<H", st.pack("<e", v))[0]
FMTS = {  # fourcc, bytes/pixel, encoder(colour rgba floats) -> pixel bytes (memory order)
 "rgba8":  (0x34325241, 4, lambda r,g,b,a: bytes([round(b*255), round(g*255), round(r*255), round(a*255)])),         # AR24: B,G,R,A
 "r8":     (0x20203852, 1, lambda r,g,b,a: bytes([round(r*255)])),                                                    # R8
 "rgb565": (0x36314752, 2, lambda r,g,b,a: struct.pack("<H", (round(r*31) << 11) | (round(g*63) << 5) | round(b*31))),  # RG16
 "rgb10a2":(0x30334241, 4, lambda r,g,b,a: struct.pack("<I", round(r*1023) | (round(g*1023) << 10) | (round(b*1023) << 20) | (round(a*3) << 30))),  # AB30
 "rgba16f":(0x48344241, 8, lambda r,g,b,a: struct.pack("<4H", h16(r), h16(g), h16(b), h16(a))),                      # AB4H
}
fourcc, bpp, enc = FMTS[fmt]
egl = C.CDLL("libEGL.so.1"); gl = C.CDLL("libGLESv2.so.2"); gbm = C.CDLL("libgbm.so.1")
egl.eglGetDisplay.restype = C.c_void_p; egl.eglCreateContext.restype = C.c_void_p; egl.eglGetProcAddress.restype = C.c_void_p; egl.eglGetError.restype = C.c_int
gbm.gbm_create_device.restype = C.c_void_p; gbm.gbm_bo_create.restype = C.c_void_p; gbm.gbm_bo_get_fd.restype = C.c_int; gbm.gbm_bo_get_stride.restype = C.c_uint
dpy = egl.eglGetDisplay(C.c_void_p(0)); maj, mnr = C.c_int(), C.c_int(); assert egl.eglInitialize(C.c_void_p(dpy), C.byref(maj), C.byref(mnr))
egl.eglBindAPI(0x30A0); attrs = (C.c_int * 5)(0x3040, 4, 0x3033, 1, 0x3038); cfg = C.c_void_p(); n = C.c_int()
assert egl.eglChooseConfig(C.c_void_p(dpy), attrs, C.byref(cfg), 1, C.byref(n)) and n.value
ctx = egl.eglCreateContext(C.c_void_p(dpy), cfg, C.c_void_p(0), (C.c_int * 3)(0x3098, 3, 0x3038)); assert egl.eglMakeCurrent(C.c_void_p(dpy), C.c_void_p(0), C.c_void_p(0), C.c_void_p(ctx))
drmfd = os.open("/dev/dri/renderD128", os.O_RDWR); dev = gbm.gbm_create_device(drmfd)
bo = gbm.gbm_bo_create(C.c_void_p(dev), W, H, fourcc, (1 << 2) | (1 << 4)); assert bo, "gbm_bo_create failed for %s" % fmt
fd = gbm.gbm_bo_get_fd(C.c_void_p(bo)); stride = gbm.gbm_bo_get_stride(C.c_void_p(bo))
createImage = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(egl.eglGetProcAddress(b"eglCreateImageKHR"))
targetTex = C.CFUNCTYPE(None, C.c_uint, C.c_void_p)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
ia = (C.c_int * 15)(0x3057, W, 0x3056, H, 0x3271, fourcc, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3038, 0, 0)
img = createImage(dpy, None, 0x3270, None, ia); assert img, "eglCreateImageKHR failed for %s (0x%x)" % (fmt, egl.eglGetError())
tex = C.c_uint(); gl.glGenTextures(1, C.byref(tex)); gl.glBindTexture(0x0DE1, tex); targetTex(0x0DE1, img)
fbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(fbo)); gl.glBindFramebuffer(0x8D40, fbo); gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, tex, 0)
st = gl.glCheckFramebufferStatus(0x8D40); assert st == 0x8CD5, "framebuffer incomplete for %s (0x%x)" % (fmt, st); gl.glViewport(0, 0, W, H)
def shader(kind, src):
    s = gl.glCreateShader(kind); p = C.c_char_p(src.encode()); gl.glShaderSource(s, 1, C.byref(p), None); gl.glCompileShader(s); return s
prog = gl.glCreateProgram(); gl.glAttachShader(prog, shader(0x8B31, "attribute vec2 p; void main(){gl_Position=vec4(p,0.0,1.0);}")); gl.glAttachShader(prog, shader(0x8B30, "precision highp float; uniform vec4 c; void main(){gl_FragColor=c;}"))
gl.glBindAttribLocation(prog, 0, b"p"); gl.glLinkProgram(prog); gl.glUseProgram(prog); uc = gl.glGetUniformLocation(prog, b"c"); gl.glUniform4f.argtypes = [C.c_int] + [C.c_float] * 4
quad = (C.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1); gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, quad)
m = mmap.mmap(fd, stride * H, prot=mmap.PROT_READ); rowlen = W * bpp
cols = [(1.0, 1.0, 1.0, 1.0), (0.5, 0.5, 0.5, 0.5), (1.0, 1.0, 1.0, 1.0), (0.25, 0.75, 0.125, 1.0)]
per_frame = []; vals = collections.Counter(); off192 = collections.Counter(); ymod = collections.Counter(); bytepos = collections.Counter()
for f in range(frames):
    r, g, b, a = cols[f % len(cols)]; gl.glUniform4f(uc, r, g, b, a); gl.glDrawArrays(5, 0, 4); gl.glFinish()
    want = enc(r, g, b, a); exp = want * W
    fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 3)); raw = m[:]; fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 6)); bad = 0
    for y in range(H):
        row = raw[y * stride:y * stride + rowlen]
        if row == exp: continue
        for x in range(W):
            p = row[x*bpp:(x+1)*bpp]
            if p != want:
                bad += 1; ymod[y % 16] += 1; off192[(x * bpp) % 192] += 1
                for i in range(bpp):
                    if p[i] != want[i]: vals[(i, want[i], p[i])] += 1; bytepos[i] += 1
    per_frame.append(bad)
print(f"{fmt}: stride {stride}, lost per frame {per_frame}")
print("  byte index within pixel that is wrong:", dict(bytepos))
print("  (byte, wanted, got) top:", [(k, hex(v1), hex(v2), c) for (k, v1, v2), c in vals.most_common(6)])
print("  byte offset in row mod 192:", dict(off192.most_common(6)), " y%16:", dict(ymod.most_common(4)))
