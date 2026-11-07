#!/usr/bin/env python3
"""Characterise the lost writes: render one quad per frame into a linear dma-buf,
read memory from the CPU after glFinish, and tabulate where and which bytes are stale."""
import ctypes as C, os, sys, time, mmap, fcntl, struct, collections
os.environ.setdefault("EGL_PLATFORM", "surfaceless")
args = [a for a in sys.argv[1:] if not a.startswith("--")]; noclear = "--noclear" in sys.argv
alpha = float([a for a in sys.argv if a.startswith("--alpha=")][0][8:]) if any(a.startswith("--alpha=") for a in sys.argv) else 1.0
colset = [a for a in sys.argv if a.startswith("--cols=")][0][7:] if any(a.startswith("--cols=") for a in sys.argv) else "A"
frames = int(args[0]) if args else 16; W, H = 2560, 1600
egl = C.CDLL("libEGL.so.1"); gl = C.CDLL("libGLESv2.so.2"); gbm = C.CDLL("libgbm.so.1")
egl.eglGetDisplay.restype = C.c_void_p; egl.eglCreateContext.restype = C.c_void_p; egl.eglGetProcAddress.restype = C.c_void_p; egl.eglGetError.restype = C.c_int
gbm.gbm_create_device.restype = C.c_void_p; gbm.gbm_bo_create.restype = C.c_void_p; gbm.gbm_bo_get_fd.restype = C.c_int; gbm.gbm_bo_get_stride.restype = C.c_uint
dpy = egl.eglGetDisplay(C.c_void_p(0)); maj, mnr = C.c_int(), C.c_int(); assert egl.eglInitialize(C.c_void_p(dpy), C.byref(maj), C.byref(mnr))
egl.eglBindAPI(0x30A0); attrs = (C.c_int * 5)(0x3040, 4, 0x3033, 1, 0x3038); cfg = C.c_void_p(); n = C.c_int()
assert egl.eglChooseConfig(C.c_void_p(dpy), attrs, C.byref(cfg), 1, C.byref(n)) and n.value
ctx = egl.eglCreateContext(C.c_void_p(dpy), cfg, C.c_void_p(0), (C.c_int * 3)(0x3098, 2, 0x3038)); assert egl.eglMakeCurrent(C.c_void_p(dpy), C.c_void_p(0), C.c_void_p(0), C.c_void_p(ctx))
drmfd = os.open("/dev/dri/renderD128", os.O_RDWR); dev = gbm.gbm_create_device(drmfd)
bo = gbm.gbm_bo_create(C.c_void_p(dev), W, H, 0x34325241, (1 << 2) | (1 << 4)); fd = gbm.gbm_bo_get_fd(C.c_void_p(bo)); stride = gbm.gbm_bo_get_stride(C.c_void_p(bo))
createImage = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(egl.eglGetProcAddress(b"eglCreateImageKHR"))
targetTex = C.CFUNCTYPE(None, C.c_uint, C.c_void_p)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
ia = (C.c_int * 15)(0x3057, W, 0x3056, H, 0x3271, 0x34325241, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3038, 0, 0)
img = createImage(dpy, None, 0x3270, None, ia)
if not img:  # older Mesa wants the modifier spelled out: DRM_FORMAT_MOD_LINEAR = 0
    ia2 = (C.c_int * 19)(0x3057, W, 0x3056, H, 0x3271, 0x34325241, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3443, 0, 0x3444, 0, 0x3038, 0, 0)
    img = createImage(dpy, None, 0x3270, None, ia2)
assert img, "eglCreateImageKHR failed, EGL error 0x%x" % egl.eglGetError()
tex = C.c_uint(); gl.glGenTextures(1, C.byref(tex)); gl.glBindTexture(0x0DE1, tex); targetTex(0x0DE1, img)
fbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(fbo)); gl.glBindFramebuffer(0x8D40, fbo); gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, tex, 0)
assert gl.glCheckFramebufferStatus(0x8D40) == 0x8CD5; gl.glViewport(0, 0, W, H)
def shader(kind, src):
    s = gl.glCreateShader(kind); p = C.c_char_p(src.encode()); gl.glShaderSource(s, 1, C.byref(p), None); gl.glCompileShader(s); return s
prog = gl.glCreateProgram(); gl.glAttachShader(prog, shader(0x8B31, "attribute vec2 p; void main(){gl_Position=vec4(p,0.0,1.0);}")); gl.glAttachShader(prog, shader(0x8B30, "precision mediump float; uniform vec4 c; void main(){gl_FragColor=c;}"))
gl.glBindAttribLocation(prog, 0, b"p"); gl.glLinkProgram(prog); gl.glUseProgram(prog); uc = gl.glGetUniformLocation(prog, b"c"); gl.glUniform4f.argtypes = [C.c_int] + [C.c_float] * 4
quad = (C.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1); gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, quad); gl.glClearColor.argtypes = [C.c_float] * 4
m = mmap.mmap(fd, stride * H, prot=mmap.PROT_READ); rowlen = W * 4
# alternate two colours whose bytes all differ (so a stale byte is unambiguous): memory order B,G,R,A
ab = int(round(alpha * 255))
cols = {"A": [((0x11, 0x22, 0x33), (0x33, 0x22, 0x11, ab)), ((0xee, 0xdd, 0xcc), (0xcc, 0xdd, 0xee, ab))], "B": [((0x80, 0x37, 0x80), (0x80, 0x37, 0x80, ab)), ((0x40, 0x40, 0x40), (0x40, 0x40, 0x40, ab))], "C": [((0xff, 0xff, 0xff), (0xff, 0xff, 0xff, ab)), ((0x00, 0x00, 0x00), (0x00, 0x00, 0x00, ab))]}[colset]
seen = collections.Counter(); bytes_hist = collections.Counter(); vals = collections.Counter(); nbytes_hist = collections.Counter(); pfd = os.open("/dev/dri/renderD128", os.O_RDWR)
def suspends_now():
    b = bytearray(struct.pack("IIQII", 0x10, 0x0a, 0, 0, 0)); fcntl.ioctl(pfd, 0xC0186440, b); return struct.unpack("IIQII", b)[2]
def pack_mv():
    try: return int(open("/sys/class/power_supply/sm5714-battery/voltage_now").read()) // 1000
    except Exception: return -1
vmv = []; susp = []; per_frame = []
xmod = collections.Counter(); ymod = collections.Counter(); x192 = collections.Counter(); y32 = collections.Counter(); rows_hit = collections.Counter()
for f in range(frames):
    (r, g, b), wb = cols[f % 2]; wb = bytes(wb)
    if not noclear: gl.glClearColor(0.5, 0.0, 0.5, 1.0); gl.glClear(0x4000)
    gl.glUniform4f(uc, r / 255.0, g / 255.0, b / 255.0, alpha); gl.glDrawArrays(5, 0, 4); gl.glFinish()
    fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 3)); raw = m[:]; fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 6))
    exp = wb * W; lost = 0
    for y in range(H):
        row = raw[y * stride:y * stride + rowlen]
        if row == exp: continue
        for x in range(W):
            p = row[x*4:x*4+4]
            if p != wb:
                lost += 1; seen[(x, y)] += 1; xmod[x % 48] += 1; ymod[y % 16] += 1; x192[x % 192] += 1; y32[y % 32] += 1; rows_hit[y // 100] += 1
                diff = tuple(i for i in range(4) if p[i] != wb[i]); nbytes_hist[len(diff)] += 1
                for i in diff: vals[(("B", "G", "R", "A")[i], p[i])] += 1
                for i in diff: bytes_hist[("B", "G", "R", "A")[i] + ("=old" if p[i] == cols[(f + 1) % 2][1][i] else "=other")] += 1
    per_frame.append(lost); susp.append(suspends_now()); vmv.append(pack_mv())
print("lost per frame:", per_frame)
print("pack voltage (mV) after each frame:", vmv)
print("GPU suspend counter after each frame:", [v - susp[0] for v in susp], "(relative; +1 means the GPU suspended and resumed between frames)")
print("distinct positions:", len(seen), " positions hit in 2+ frames:", sum(1 for v in seen.values() if v >= 2), " max hits on one position:", max(seen.values()) if seen else 0)
print("stale bytes per lost pixel:", dict(sorted(nbytes_hist.items())))
print("which byte is stale (old = previous frame's value):", dict(bytes_hist.most_common()))
print("stale byte values seen (byte, value): ", [(k[0], hex(k[1]), v) for k, v in vals.most_common(8)])
print("x%48:", dict(xmod.most_common(4)), " y%16:", dict(ymod.most_common(4)))
print("x%192 (is the period 48 or longer?):", dict(x192.most_common(8)))
print("y%32 (is the period 16 or 32?):", dict(y32.most_common(4)))
print("losses per 100-row band (y//100):", dict(sorted(rows_hit.items())))
