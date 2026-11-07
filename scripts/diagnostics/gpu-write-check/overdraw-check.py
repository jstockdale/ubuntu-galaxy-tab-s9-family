#!/usr/bin/env python3
"""Standalone GPU write-back check, no display or numpy needed.
Renders N overlapping full-frame quads of different colours into an RGBA8
target, reads it back and counts pixels that did not end up with the last
colour (a dropped write leaves the previous colour).  Reports the x%48 / y%16
phases (the on-screen lattice is x = 20,28 mod 48, rows 16 apart).
  overdraw-check.py [--linear] [frames] [quads/frame] [W] [H]
--linear renders into a LINEAR dma-buf from GBM (what the compositor scans
out) instead of a GPU-internal tiled texture."""
import ctypes as C, os, sys, time
os.environ.setdefault("EGL_PLATFORM", "surfaceless")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
linear = "--linear" in sys.argv
delay_ms = float([a for a in sys.argv if a.startswith("--delay=")][0].split("=")[1]) if any(a.startswith("--delay=") for a in sys.argv) else 0
frames = int(args[0]) if len(args) > 0 else 20
quads = int(args[1]) if len(args) > 1 else 8
W = int(args[2]) if len(args) > 2 else 2560
H = int(args[3]) if len(args) > 3 else 1600
egl = C.CDLL("libEGL.so.1"); gl = C.CDLL("libGLESv2.so.2")
egl.eglGetDisplay.restype = C.c_void_p; egl.eglCreateContext.restype = C.c_void_p
egl.eglGetError.restype = C.c_int; egl.eglGetProcAddress.restype = C.c_void_p
dpy = egl.eglGetDisplay(C.c_void_p(0)); assert dpy, "no EGL display"
maj, mnr = C.c_int(), C.c_int(); assert egl.eglInitialize(C.c_void_p(dpy), C.byref(maj), C.byref(mnr)), "eglInitialize failed"
egl.eglBindAPI(0x30A0)
attrs = (C.c_int * 5)(0x3040, 4, 0x3033, 1, 0x3038); cfg = C.c_void_p(); n = C.c_int()  # ES2, pbuffer-type config (surfaceless)
assert egl.eglChooseConfig(C.c_void_p(dpy), attrs, C.byref(cfg), 1, C.byref(n)) and n.value, "no EGL config"
ctx = egl.eglCreateContext(C.c_void_p(dpy), cfg, C.c_void_p(0), (C.c_int * 3)(0x3098, 2, 0x3038)); assert ctx, "no context"
assert egl.eglMakeCurrent(C.c_void_p(dpy), C.c_void_p(0), C.c_void_p(0), C.c_void_p(ctx)), "makecurrent failed 0x%x" % egl.eglGetError()
gl.glGetString.restype = C.c_char_p
print("renderer:", gl.glGetString(0x1F01).decode(), "|", gl.glGetString(0x1F02).decode(), "| FD_MESA_DEBUG=%s | target=%s" % (os.environ.get("FD_MESA_DEBUG", ""), "linear dma-buf" if linear else "tiled texture"))
GL_TEXTURE_2D, GL_RGBA, GL_UNSIGNED_BYTE, GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0 = 0x0DE1, 0x1908, 0x1401, 0x8D40, 0x8CE0
tex = C.c_uint(); gl.glGenTextures(1, C.byref(tex)); gl.glBindTexture(GL_TEXTURE_2D, tex)
if linear:
    gbm = C.CDLL("libgbm.so.1"); gbm.gbm_create_device.restype = C.c_void_p; gbm.gbm_bo_create.restype = C.c_void_p
    gbm.gbm_bo_get_fd.restype = C.c_int; gbm.gbm_bo_get_stride.restype = C.c_uint
    drmfd = os.open("/dev/dri/renderD128", os.O_RDWR); dev = gbm.gbm_create_device(drmfd); assert dev, "gbm device"
    GBM_FORMAT_ARGB8888 = 0x34325241; bo = gbm.gbm_bo_create(C.c_void_p(dev), W, H, GBM_FORMAT_ARGB8888, (1 << 2) | (1 << 4))  # RENDERING | LINEAR
    assert bo, "gbm_bo_create linear failed"; fd = gbm.gbm_bo_get_fd(C.c_void_p(bo)); stride = gbm.gbm_bo_get_stride(C.c_void_p(bo))
    createImage = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(egl.eglGetProcAddress(b"eglCreateImageKHR"))
    targetTex = C.CFUNCTYPE(None, C.c_uint, C.c_void_p)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
    ia = (C.c_int * 15)(0x3057, W, 0x3056, H, 0x3271, GBM_FORMAT_ARGB8888, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3038, 0, 0)
    img = createImage(dpy, None, 0x3270, None, ia); assert img, "eglCreateImageKHR dma-buf failed 0x%x" % egl.eglGetError()
    targetTex(GL_TEXTURE_2D, img); print("linear dma-buf: stride %d bytes" % stride)
else:
    gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
gl.glTexParameteri(GL_TEXTURE_2D, 0x2801, 0x2600); gl.glTexParameteri(GL_TEXTURE_2D, 0x2800, 0x2600)
fbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(fbo)); gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
gl.glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0)
assert gl.glCheckFramebufferStatus(GL_FRAMEBUFFER) == 0x8CD5, "framebuffer incomplete"
gl.glViewport(0, 0, W, H)
def shader(kind, src):
    s = gl.glCreateShader(kind); p = C.c_char_p(src.encode()); gl.glShaderSource(s, 1, C.byref(p), None); gl.glCompileShader(s)
    ok = C.c_int(); gl.glGetShaderiv(s, 0x8B81, C.byref(ok)); assert ok.value, "shader compile failed"; return s
prog = gl.glCreateProgram(); gl.glAttachShader(prog, shader(0x8B31, "attribute vec2 p; void main(){gl_Position=vec4(p,0.0,1.0);}"))
gl.glAttachShader(prog, shader(0x8B30, "precision mediump float; uniform vec4 c; void main(){gl_FragColor=c;}"))
gl.glBindAttribLocation(prog, 0, b"p"); gl.glLinkProgram(prog); gl.glUseProgram(prog)
uc = gl.glGetUniformLocation(prog, b"c"); gl.glUniform4f.argtypes = [C.c_int, C.c_float, C.c_float, C.c_float, C.c_float]
quad = (C.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1); gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, quad)
gl.glClearColor.argtypes = [C.c_float] * 4
buf = (C.c_ubyte * (W * H * 4))()
palette = [(255, 255, 255), (0, 0, 0), (250, 250, 251), (34, 34, 38), (200, 60, 20), (20, 120, 200), (120, 200, 20), (60, 60, 60)]
bad_total = 0; px = {}; py = {}; t0 = time.time(); rowlen = W * 4
for f in range(frames):
    cols = [palette[(f * 3 + i) % len(palette)] for i in range(quads)]
    gl.glClearColor(0.5, 0.0, 0.5, 1.0); gl.glClear(0x4000)
    for c in cols:
        gl.glUniform4f(uc, c[0] / 255.0, c[1] / 255.0, c[2] / 255.0, 1.0); gl.glDrawArrays(5, 0, 4)
    gl.glFinish()
    if delay_ms: time.sleep(delay_ms / 1000.0)
    gl.glReadPixels(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, buf)
    want = bytes(cols[-1]) + b"\xff"; got = bytes(buf); exp_row = want * W; nb = 0; shown = 0
    if got != exp_row * H:
        for y in range(H):
            row = got[y * rowlen:(y + 1) * rowlen]
            if row == exp_row: continue
            for x in range(W):
                p = row[x * 4:x * 4 + 4]
                if p != want:
                    nb += 1; px[x % 48] = px.get(x % 48, 0) + 1; py[y % 16] = py.get(y % 16, 0) + 1
                    if shown < 3: print(f"  frame {f}: wrong pixel at ({x},{y}) = {tuple(p)} wanted {tuple(want)}"); shown += 1
    bad_total += nb
    if nb: print(f"frame {f}: {nb} wrong pixels")
    if "--verify" in sys.argv:
        time.sleep(0.02)
        buf2 = (C.c_ubyte * (W * H * 4))(); gl.glReadPixels(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, buf2); got2 = bytes(buf2)
        nb2 = 0 if got2 == exp_row * H else sum(1 for i in range(0, len(got2), 4) if got2[i:i+4] != want)
        line = f"  verify frame {f}: GL readback immediately after glFinish: {nb} wrong; GL readback 20 ms later: {nb2} wrong"
        if linear:
            import mmap, fcntl, struct
            fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 3)); m = mmap.mmap(fd, stride * H, prot=mmap.PROT_READ); raw = m[:]; m.close(); fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 6))
            wb = bytes((want[2], want[1], want[0], want[3])); exp_b = wb * W; nb3 = 0
            for y in range(H):
                r = raw[y * stride:y * stride + rowlen]
                if r != exp_b: nb3 += sum(1 for x in range(W) if r[x*4:x*4+4] != wb)
            line += f"; CPU mmap of the dma-buf after that: {nb3} wrong"
        print(line)
    print(f"{frames} frames x {quads} quads at {W}x{H}: {bad_total} wrong pixels total, {time.time() - t0:.1f}s")
if bad_total:
    print("x%48 top phases:", sorted(px.items(), key=lambda kv: -kv[1])[:4], " y%16 top:", sorted(py.items(), key=lambda kv: -kv[1])[:3])
