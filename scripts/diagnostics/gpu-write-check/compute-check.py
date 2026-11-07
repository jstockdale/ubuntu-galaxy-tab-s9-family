#!/usr/bin/env python3
"""Does the corruption come from the render backend (RB/CCU) or from the shader
cores / memory path?  Fill an RGBA8 texture either with a compute shader
(imageStore: SP -> UCHE -> memory, no RB) or with glClear (2D engine), copy it
to a LINEAR dma-buf with glCopyImageSubData, and check memory from the CPU.
  compute-check.py <compute|clearcopy|draw> [frames]"""
import ctypes as C, os, sys, mmap, fcntl, struct, collections
os.environ.setdefault("EGL_PLATFORM", "surfaceless")
mode = sys.argv[1] if len(sys.argv) > 1 else "compute"; frames = int(sys.argv[2]) if len(sys.argv) > 2 else 12; W, H = 2560, 1600
egl = C.CDLL("libEGL.so.1"); gl = C.CDLL("libGLESv2.so.2"); gbm = C.CDLL("libgbm.so.1")
egl.eglGetDisplay.restype = C.c_void_p; egl.eglCreateContext.restype = C.c_void_p; egl.eglGetProcAddress.restype = C.c_void_p; egl.eglGetError.restype = C.c_int
gbm.gbm_create_device.restype = C.c_void_p; gbm.gbm_bo_create.restype = C.c_void_p; gbm.gbm_bo_get_fd.restype = C.c_int; gbm.gbm_bo_get_stride.restype = C.c_uint
dpy = egl.eglGetDisplay(C.c_void_p(0)); maj, mnr = C.c_int(), C.c_int(); assert egl.eglInitialize(C.c_void_p(dpy), C.byref(maj), C.byref(mnr))
egl.eglBindAPI(0x30A0); attrs = (C.c_int * 5)(0x3040, 0x40, 0x3033, 1, 0x3038); cfg = C.c_void_p(); n = C.c_int()  # ES3 bit
assert egl.eglChooseConfig(C.c_void_p(dpy), attrs, C.byref(cfg), 1, C.byref(n)) and n.value
ctx = egl.eglCreateContext(C.c_void_p(dpy), cfg, C.c_void_p(0), (C.c_int * 5)(0x3098, 3, 0x30FB, 1, 0x3038)); assert ctx, "ES 3.1 context"
assert egl.eglMakeCurrent(C.c_void_p(dpy), C.c_void_p(0), C.c_void_p(0), C.c_void_p(ctx))
gl.glGetString.restype = C.c_char_p; print("GL:", gl.glGetString(0x1F02).decode())
drmfd = os.open("/dev/dri/renderD128", os.O_RDWR); dev = gbm.gbm_create_device(drmfd)
bo = gbm.gbm_bo_create(C.c_void_p(dev), W, H, 0x34325241, (1 << 2) | (1 << 4)); fd = gbm.gbm_bo_get_fd(C.c_void_p(bo)); stride = gbm.gbm_bo_get_stride(C.c_void_p(bo))
createImage = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(egl.eglGetProcAddress(b"eglCreateImageKHR"))
targetTex = C.CFUNCTYPE(None, C.c_uint, C.c_void_p)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
ia = (C.c_int * 15)(0x3057, W, 0x3056, H, 0x3271, 0x34325241, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3038, 0, 0)
img = createImage(dpy, None, 0x3270, None, ia); assert img
dst = C.c_uint(); gl.glGenTextures(1, C.byref(dst)); gl.glBindTexture(0x0DE1, dst); targetTex(0x0DE1, img)
src = C.c_uint(); gl.glGenTextures(1, C.byref(src)); gl.glBindTexture(0x0DE1, src); gl.glTexStorage2D(0x0DE1, 1, 0x8058, W, H)  # GL_RGBA8 immutable
dfbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(dfbo)); gl.glBindFramebuffer(0x8D40, dfbo); gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, dst, 0); assert gl.glCheckFramebufferStatus(0x8D40) == 0x8CD5, "dst fbo"
sfbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(sfbo)); gl.glBindFramebuffer(0x8D40, sfbo); gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, src, 0)
assert gl.glCheckFramebufferStatus(0x8D40) == 0x8CD5, "src fbo"; e = gl.glGetError(); assert e == 0, "setup error 0x%x" % e; gl.glViewport(0, 0, W, H); gl.glClearColor.argtypes = [C.c_float] * 4; gl.glUniform4f.argtypes = [C.c_int] + [C.c_float] * 4
def shader(kind, s_):
    s = gl.glCreateShader(kind); p = C.c_char_p(s_.encode()); gl.glShaderSource(s, 1, C.byref(p), None); gl.glCompileShader(s); ok = C.c_int(); gl.glGetShaderiv(s, 0x8B81, C.byref(ok))
    if not ok.value:
        buf = C.create_string_buffer(4096); gl.glGetShaderInfoLog(s, 4096, None, buf); raise SystemExit("shader: " + buf.value.decode())
    return s
if mode == "compute":
    cs = shader(0x91B9, "#version 310 es\nlayout(local_size_x=16, local_size_y=16) in;\nlayout(rgba8, binding=0) writeonly uniform highp image2D img;\nuniform vec4 c;\nvoid main(){ imageStore(img, ivec2(gl_GlobalInvocationID.xy), c); }\n")
    prog = gl.glCreateProgram(); gl.glAttachShader(prog, cs); gl.glLinkProgram(prog); ok = C.c_int(); gl.glGetProgramiv(prog, 0x8B82, C.byref(ok)); assert ok.value, "link"
    gl.glUseProgram(prog); uc = gl.glGetUniformLocation(prog, b"c"); gl.glBindImageTexture(0, src, 0, 0, 0, 0x88B9, 0x8058)  # WRITE_ONLY, RGBA8
elif mode == "draw":
    vs = shader(0x8B31, "#version 300 es\nin vec2 p; void main(){gl_Position=vec4(p,0.0,1.0);}\n"); fs = shader(0x8B30, "#version 300 es\nprecision highp float; uniform vec4 c; out vec4 o; void main(){o=c;}\n")
    prog = gl.glCreateProgram(); gl.glAttachShader(prog, vs); gl.glAttachShader(prog, fs); gl.glBindAttribLocation(prog, 0, b"p"); gl.glLinkProgram(prog); gl.glUseProgram(prog); uc = gl.glGetUniformLocation(prog, b"c")
    quad = (C.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1); gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, quad)
m = mmap.mmap(fd, stride * H, prot=mmap.PROT_READ); rowlen = W * 4; cols = [(0.6, 0.8, 0.4, 1.0), (0.2, 1.0, 0.8, 0.6)]; per_frame = []; vals = collections.Counter(); xm = collections.Counter()
for f in range(frames):
    r, g, b, a = cols[f % 2]; wb = bytes([round(b*255), round(g*255), round(r*255), round(a*255)])
    if mode == "compute": gl.glUniform4f(uc, r, g, b, a); gl.glDispatchCompute(W // 16, H // 16, 1); gl.glMemoryBarrier(0xFFFFFFFF)
    elif mode == "draw": gl.glUniform4f(uc, r, g, b, a); gl.glDrawArrays(5, 0, 4)
    else: gl.glClearColor(r, g, b, a); gl.glClear(0x4000)
    err = gl.glGetError(); assert err == 0, "fill error 0x%x" % err
    gl.glBindFramebuffer(0x8CA8, sfbo); gl.glBindFramebuffer(0x8CA9, dfbo); gl.glBlitFramebuffer(0, 0, W, H, 0, 0, W, H, 0x4000, 0x2600); gl.glBindFramebuffer(0x8D40, sfbo); gl.glFinish()
    err = gl.glGetError(); assert err == 0, "copy error 0x%x" % err
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
    per_frame.append(bad)
print(f"{mode}: lost per frame {per_frame}; (channel, wanted, got) {vals.most_common(4)}; x%48 {dict(xm.most_common(4))}")
