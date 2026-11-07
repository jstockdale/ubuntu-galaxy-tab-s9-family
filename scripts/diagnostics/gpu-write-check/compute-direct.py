#!/usr/bin/env python3
"""Compute shader imageStore straight into the LINEAR dma-buf (no render backend,
no blit): SP -> UCHE -> memory.  Memory checked from the CPU."""
import ctypes as C, os, sys, mmap, fcntl, struct, collections
os.environ.setdefault("EGL_PLATFORM", "surfaceless")
frames = int(sys.argv[1]) if len(sys.argv) > 1 else 12; W, H = 2560, 1600
egl = C.CDLL("libEGL.so.1"); gl = C.CDLL("libGLESv2.so.2"); gbm = C.CDLL("libgbm.so.1")
egl.eglGetDisplay.restype = C.c_void_p; egl.eglCreateContext.restype = C.c_void_p; egl.eglGetProcAddress.restype = C.c_void_p; egl.eglGetError.restype = C.c_int
gbm.gbm_create_device.restype = C.c_void_p; gbm.gbm_bo_create.restype = C.c_void_p; gbm.gbm_bo_get_fd.restype = C.c_int; gbm.gbm_bo_get_stride.restype = C.c_uint
dpy = egl.eglGetDisplay(C.c_void_p(0)); maj, mnr = C.c_int(), C.c_int(); assert egl.eglInitialize(C.c_void_p(dpy), C.byref(maj), C.byref(mnr))
egl.eglBindAPI(0x30A0); attrs = (C.c_int * 5)(0x3040, 0x40, 0x3033, 1, 0x3038); cfg = C.c_void_p(); n = C.c_int()
assert egl.eglChooseConfig(C.c_void_p(dpy), attrs, C.byref(cfg), 1, C.byref(n)) and n.value
ctx = egl.eglCreateContext(C.c_void_p(dpy), cfg, C.c_void_p(0), (C.c_int * 5)(0x3098, 3, 0x30FB, 1, 0x3038)); assert ctx
assert egl.eglMakeCurrent(C.c_void_p(dpy), C.c_void_p(0), C.c_void_p(0), C.c_void_p(ctx))
drmfd = os.open("/dev/dri/renderD128", os.O_RDWR); dev = gbm.gbm_create_device(drmfd)
bo = gbm.gbm_bo_create(C.c_void_p(dev), W, H, 0x34325241, (1 << 2) | (1 << 4)); fd = gbm.gbm_bo_get_fd(C.c_void_p(bo)); stride = gbm.gbm_bo_get_stride(C.c_void_p(bo))
createImage = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(egl.eglGetProcAddress(b"eglCreateImageKHR"))
targetTex = C.CFUNCTYPE(None, C.c_uint, C.c_void_p)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
ia = (C.c_int * 19)(0x3057, W, 0x3056, H, 0x3271, 0x34325241, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3443, 0, 0x3444, 0, 0x3038, 0, 0)
img = createImage(dpy, None, 0x3270, None, ia); assert img
tex = C.c_uint(); gl.glGenTextures(1, C.byref(tex)); gl.glBindTexture(0x0DE1, tex); targetTex(0x0DE1, img)
def shader(kind, src):
    s = gl.glCreateShader(kind); p = C.c_char_p(src.encode()); gl.glShaderSource(s, 1, C.byref(p), None); gl.glCompileShader(s); ok = C.c_int(); gl.glGetShaderiv(s, 0x8B81, C.byref(ok))
    if not ok.value: buf = C.create_string_buffer(4096); gl.glGetShaderInfoLog(s, 4096, None, buf); raise SystemExit("shader: " + buf.value.decode())
    return s
cs = shader(0x91B9, "#version 310 es\nlayout(local_size_x=16, local_size_y=16) in;\nlayout(rgba8, binding=0) writeonly uniform highp image2D img;\nuniform vec4 c;\nvoid main(){ imageStore(img, ivec2(gl_GlobalInvocationID.xy), c); }\n")
prog = gl.glCreateProgram(); gl.glAttachShader(prog, cs); gl.glLinkProgram(prog); ok = C.c_int(); gl.glGetProgramiv(prog, 0x8B82, C.byref(ok)); assert ok.value
gl.glUseProgram(prog); uc = gl.glGetUniformLocation(prog, b"c"); gl.glUniform4f.argtypes = [C.c_int] + [C.c_float] * 4
gl.glBindImageTexture(0, tex, 0, 0, 0, 0x88B9, 0x8058); e = gl.glGetError(); print("bind image on the dma-buf texture:", "ok" if e == 0 else "GL error 0x%x" % e)
m = mmap.mmap(fd, stride * H, prot=mmap.PROT_READ); rowlen = W * 4; cols = [(0.6, 0.8, 0.4, 1.0), (0.2, 1.0, 0.8, 0.6)]; per = []; vals = collections.Counter(); xm = collections.Counter()
for f in range(frames):
    r, g, b, a = cols[f % 2]; wb = bytes([round(r*255), round(g*255), round(b*255), round(a*255)])  # image stores land in RGBA order on this import
    gl.glUniform4f(uc, r, g, b, a); gl.glDispatchCompute(W // 16, H // 16, 1); gl.glMemoryBarrier(0xFFFFFFFF); gl.glFinish(); e = gl.glGetError(); assert e == 0, "GL error 0x%x" % e
    fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 3)); raw = m[:]; fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 6)); exp = wb * W; bad = 0
    for y in range(H):
        row = raw[y * stride:y * stride + rowlen]
        if row == exp: continue
        for x in range(W):
            p = row[x*4:x*4+4]
            if p != wb:
                bad += 1; xm[x % 48] += 1
                for i in range(4):
                    if p[i] != wb[i]: vals[("BGRA"[i], hex(wb[i]), hex(p[i]))] += 1
    per.append(bad)
print(f"compute-direct: lost per frame {per}; (channel, wanted, got) {vals.most_common(4)}; x%48 {dict(xm.most_common(4))}")
