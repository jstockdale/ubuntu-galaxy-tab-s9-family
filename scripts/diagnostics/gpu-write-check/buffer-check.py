#!/usr/bin/env python3
"""Is the corruption tied to the memory the target lives in?  Allocate N separate
LINEAR dma-bufs, render F frames into each (round-robin), and tally lost pixels
per buffer from the CPU view.  buffer-check.py [N] [F]"""
import ctypes as C, os, sys, mmap, fcntl, struct
os.environ.setdefault("EGL_PLATFORM", "surfaceless")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 8; F = int(sys.argv[2]) if len(sys.argv) > 2 else 6; W, H = 2560, 1600
egl = C.CDLL("libEGL.so.1"); gl = C.CDLL("libGLESv2.so.2"); gbm = C.CDLL("libgbm.so.1")
egl.eglGetDisplay.restype = C.c_void_p; egl.eglCreateContext.restype = C.c_void_p; egl.eglGetProcAddress.restype = C.c_void_p; egl.eglGetError.restype = C.c_int
gbm.gbm_create_device.restype = C.c_void_p; gbm.gbm_bo_create.restype = C.c_void_p; gbm.gbm_bo_get_fd.restype = C.c_int; gbm.gbm_bo_get_stride.restype = C.c_uint
dpy = egl.eglGetDisplay(C.c_void_p(0)); maj, mnr = C.c_int(), C.c_int(); assert egl.eglInitialize(C.c_void_p(dpy), C.byref(maj), C.byref(mnr))
egl.eglBindAPI(0x30A0); attrs = (C.c_int * 5)(0x3040, 4, 0x3033, 1, 0x3038); cfg = C.c_void_p(); n = C.c_int()
assert egl.eglChooseConfig(C.c_void_p(dpy), attrs, C.byref(cfg), 1, C.byref(n)) and n.value
ctx = egl.eglCreateContext(C.c_void_p(dpy), cfg, C.c_void_p(0), (C.c_int * 3)(0x3098, 2, 0x3038)); assert egl.eglMakeCurrent(C.c_void_p(dpy), C.c_void_p(0), C.c_void_p(0), C.c_void_p(ctx))
drmfd = os.open("/dev/dri/renderD128", os.O_RDWR); dev = gbm.gbm_create_device(drmfd)
createImage = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(egl.eglGetProcAddress(b"eglCreateImageKHR"))
targetTex = C.CFUNCTYPE(None, C.c_uint, C.c_void_p)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
bufs = []
for i in range(N):
    bo = gbm.gbm_bo_create(C.c_void_p(dev), W, H, 0x34325241, (1 << 2) | (1 << 4)); fd = gbm.gbm_bo_get_fd(C.c_void_p(bo)); stride = gbm.gbm_bo_get_stride(C.c_void_p(bo))
    ia = (C.c_int * 19)(0x3057, W, 0x3056, H, 0x3271, 0x34325241, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3443, 0, 0x3444, 0, 0x3038, 0, 0)
    img = createImage(dpy, None, 0x3270, None, ia); assert img, "import %d" % i
    tex = C.c_uint(); gl.glGenTextures(1, C.byref(tex)); gl.glBindTexture(0x0DE1, tex); targetTex(0x0DE1, img)
    fbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(fbo)); gl.glBindFramebuffer(0x8D40, fbo); gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, tex, 0); assert gl.glCheckFramebufferStatus(0x8D40) == 0x8CD5
    bufs.append((fd, stride, fbo, mmap.mmap(fd, stride * H, prot=mmap.PROT_READ)))
gl.glViewport(0, 0, W, H)
def shader(kind, src):
    s = gl.glCreateShader(kind); p = C.c_char_p(src.encode()); gl.glShaderSource(s, 1, C.byref(p), None); gl.glCompileShader(s); return s
prog = gl.glCreateProgram(); gl.glAttachShader(prog, shader(0x8B31, "attribute vec2 p; void main(){gl_Position=vec4(p,0.0,1.0);}")); gl.glAttachShader(prog, shader(0x8B30, "precision mediump float; uniform vec4 c; void main(){gl_FragColor=c;}"))
gl.glBindAttribLocation(prog, 0, b"p"); gl.glLinkProgram(prog); gl.glUseProgram(prog); uc = gl.glGetUniformLocation(prog, b"c"); gl.glUniform4f.argtypes = [C.c_int] + [C.c_float] * 4
quad = (C.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1); gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, quad)
cols = [((0x11, 0x22, 0x33), bytes((0x33, 0x22, 0x11, 0xff))), ((0xee, 0xdd, 0xcc), bytes((0xcc, 0xdd, 0xee, 0xff)))]; rowlen = W * 4
per_buf = [[] for _ in range(N)]
for f in range(F):
    for i, (fd, stride, fbo, m) in enumerate(bufs):
        (r, g, b), wb = cols[(f + i) % 2]
        gl.glBindFramebuffer(0x8D40, fbo); gl.glUniform4f(uc, r / 255.0, g / 255.0, b / 255.0, 1.0); gl.glDrawArrays(5, 0, 4); gl.glFinish()
        fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 3)); raw = m[:]; fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 6)); exp = wb * W; bad = 0
        for y in range(H):
            row = raw[y * stride:y * stride + rowlen]
            if row != exp: bad += sum(1 for x in range(W) if row[x*4:x*4+4] != wb)
        per_buf[i].append(bad)
for i in range(N): print(f"buffer {i}: {sum(1 for v in per_buf[i] if v)}/{F} frames bad, {sum(per_buf[i])} px  {per_buf[i]}")
