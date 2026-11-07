#!/usr/bin/env python3
"""Which GPU path produces the G/A x0.25 corruption?  Writes a LINEAR RGBA8
dma-buf through different paths and checks memory from the CPU.
  path-check.py <clear|uniform|const|texture|blit|blend> [frames]
  clear   : glClear only (no shader)                uniform: fragment colour from a uniform
  const   : fragment colour as a shader constant    texture: colour sampled from a 1x1 texture
  blit    : glBlitFramebuffer from a clean texture FBO (blitter / 2D path)
  blend   : uniform draw with blending enabled (dst*0 + src*1)
Also prints a coarse map of where the bad pixels are in the worst frame."""
import ctypes as C, os, sys, mmap, fcntl, struct, collections
os.environ.setdefault("EGL_PLATFORM", "surfaceless")
mode = sys.argv[1] if len(sys.argv) > 1 else "uniform"; frames = int(sys.argv[2]) if len(sys.argv) > 2 else 12; W, H = 2560, 1600
egl = C.CDLL("libEGL.so.1"); gl = C.CDLL("libGLESv2.so.2"); gbm = C.CDLL("libgbm.so.1")
egl.eglGetDisplay.restype = C.c_void_p; egl.eglCreateContext.restype = C.c_void_p; egl.eglGetProcAddress.restype = C.c_void_p
gbm.gbm_create_device.restype = C.c_void_p; gbm.gbm_bo_create.restype = C.c_void_p; gbm.gbm_bo_get_fd.restype = C.c_int; gbm.gbm_bo_get_stride.restype = C.c_uint
dpy = egl.eglGetDisplay(C.c_void_p(0)); maj, mnr = C.c_int(), C.c_int(); assert egl.eglInitialize(C.c_void_p(dpy), C.byref(maj), C.byref(mnr))
egl.eglBindAPI(0x30A0); attrs = (C.c_int * 5)(0x3040, 4, 0x3033, 1, 0x3038); cfg = C.c_void_p(); n = C.c_int()
assert egl.eglChooseConfig(C.c_void_p(dpy), attrs, C.byref(cfg), 1, C.byref(n)) and n.value
ctx = egl.eglCreateContext(C.c_void_p(dpy), cfg, C.c_void_p(0), (C.c_int * 3)(0x3098, 3, 0x3038)); assert egl.eglMakeCurrent(C.c_void_p(dpy), C.c_void_p(0), C.c_void_p(0), C.c_void_p(ctx))
drmfd = os.open("/dev/dri/renderD128", os.O_RDWR); dev = gbm.gbm_create_device(drmfd)
bo = gbm.gbm_bo_create(C.c_void_p(dev), W, H, 0x34325241, (1 << 2) | (1 << 4)); fd = gbm.gbm_bo_get_fd(C.c_void_p(bo)); stride = gbm.gbm_bo_get_stride(C.c_void_p(bo))
createImage = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(egl.eglGetProcAddress(b"eglCreateImageKHR"))
targetTex = C.CFUNCTYPE(None, C.c_uint, C.c_void_p)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
ia = (C.c_int * 15)(0x3057, W, 0x3056, H, 0x3271, 0x34325241, 0x3272, fd, 0x3273, 0, 0x3274, stride, 0x3038, 0, 0)
img = createImage(dpy, None, 0x3270, None, ia); assert img
tex = C.c_uint(); gl.glGenTextures(1, C.byref(tex)); gl.glBindTexture(0x0DE1, tex); targetTex(0x0DE1, img)
fbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(fbo)); gl.glBindFramebuffer(0x8D40, fbo); gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, tex, 0)
assert gl.glCheckFramebufferStatus(0x8D40) == 0x8CD5; gl.glViewport(0, 0, W, H)
gl.glClearColor.argtypes = [C.c_float] * 4; gl.glUniform4f.argtypes = [C.c_int] + [C.c_float] * 4
def shader(kind, src):
    s = gl.glCreateShader(kind); p = C.c_char_p(src.encode()); gl.glShaderSource(s, 1, C.byref(p), None); gl.glCompileShader(s); ok = C.c_int(); gl.glGetShaderiv(s, 0x8B81, C.byref(ok)); assert ok.value, src; return s
def program(fs):
    pr = gl.glCreateProgram(); gl.glAttachShader(pr, shader(0x8B31, "attribute vec2 p; varying vec2 uv; void main(){uv=p*0.5+0.5; gl_Position=vec4(p,0.0,1.0);}")); gl.glAttachShader(pr, shader(0x8B30, fs)); gl.glBindAttribLocation(pr, 0, b"p"); gl.glLinkProgram(pr); return pr
quad = (C.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1); gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, quad)
cols = [(0.6, 0.8, 0.4, 1.0), (0.2, 1.0, 0.8, 0.6)]  # all channels map to exact UNORM8 values
if mode == "uniform" or mode == "blend":
    prog = program("precision highp float; uniform vec4 c; void main(){gl_FragColor=c;}"); gl.glUseProgram(prog); uc = gl.glGetUniformLocation(prog, b"c")
    if mode == "blend": gl.glEnable(0x0BE2); gl.glBlendFunc(1, 0)  # GL_BLEND, src ONE, dst ZERO
elif mode == "const":
    progs = [program("precision highp float; void main(){gl_FragColor=vec4(%f,%f,%f,%f);}" % c) for c in cols]
elif mode == "texture":
    prog = program("precision highp float; uniform sampler2D t; varying vec2 uv; void main(){gl_FragColor=texture2D(t, uv);}"); gl.glUseProgram(prog)
    st = C.c_uint(); gl.glGenTextures(1, C.byref(st)); gl.glActiveTexture(0x84C0); gl.glBindTexture(0x0DE1, st); gl.glTexParameteri(0x0DE1, 0x2801, 0x2600); gl.glTexParameteri(0x0DE1, 0x2800, 0x2600)
    gl.glUniform1i(gl.glGetUniformLocation(prog, b"t"), 0)
elif mode == "blit":
    src = C.c_uint(); gl.glGenTextures(1, C.byref(src)); gl.glBindTexture(0x0DE1, src); gl.glTexImage2D(0x0DE1, 0, 0x1908, W, H, 0, 0x1908, 0x1401, None)
    sfbo = C.c_uint(); gl.glGenFramebuffers(1, C.byref(sfbo)); gl.glBindFramebuffer(0x8CA8, sfbo); gl.glFramebufferTexture2D(0x8CA8, 0x8CE0, 0x0DE1, src, 0)  # READ_FRAMEBUFFER
m = mmap.mmap(fd, stride * H, prot=mmap.PROT_READ); rowlen = W * 4; per_frame = []; vals = collections.Counter(); worst = (0, None)
for f in range(frames):
    r, g, b, a = cols[f % 2]; wb = bytes([round(b*255), round(g*255), round(r*255), round(a*255)])
    if mode == "clear": gl.glClearColor(r, g, b, a); gl.glClear(0x4000)
    elif mode in ("uniform", "blend"): gl.glUniform4f(uc, r, g, b, a); gl.glDrawArrays(5, 0, 4)
    elif mode == "const": gl.glUseProgram(progs[f % 2]); gl.glDrawArrays(5, 0, 4)
    elif mode == "texture":
        px = (C.c_ubyte * 4)(round(r*255), round(g*255), round(b*255), round(a*255)); gl.glTexImage2D(0x0DE1, 0, 0x1908, 1, 1, 0, 0x1908, 0x1401, px); gl.glDrawArrays(5, 0, 4)
    elif mode == "blit":
        gl.glBindFramebuffer(0x8CA8, sfbo); gl.glClearColor(r, g, b, a); gl.glClear(0x4000); gl.glBindFramebuffer(0x8CA9, fbo)  # DRAW_FRAMEBUFFER
        gl.glBlitFramebuffer(0, 0, W, H, 0, 0, W, H, 0x4000, 0x2600)
    gl.glFinish()
    fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 3)); raw = m[:]; fcntl.ioctl(fd, 0x40086200, struct.pack("Q", 6)); exp = wb * W; bad = 0; pts = []
    for y in range(H):
        row = raw[y * stride:y * stride + rowlen]
        if row == exp: continue
        for x in range(W):
            p = row[x*4:x*4+4]
            if p != wb:
                bad += 1; pts.append((x, y))
                for i in range(4):
                    if p[i] != wb[i]: vals[("BGRA"[i], hex(wb[i]), hex(p[i]))] += 1
    per_frame.append(bad)
    if bad > worst[0]: worst = (bad, pts)
    if pts and "--phases" in sys.argv:
        cy = collections.Counter(y % 16 for x, y in pts); cx = collections.Counter(x % 48 for x, y in pts)
        print(f"  frame {f}: {bad} bad, y%16 {dict(cy.most_common(3))}, x%48 {dict(cx.most_common(3))}, rows {min(y for x,y in pts)}-{max(y for x,y in pts)}")
print(f"{mode}: lost per frame {per_frame}; (channel, wanted, got): {vals.most_common(4)}")
if worst[1]:
    grid = [[0] * 16 for _ in range(10)]
    for x, y in worst[1]: grid[min(y // 160, 9)][min(x // 160, 15)] += 1
    print("  worst frame map (160x160 cells, rows = y):"); [print("   " + " ".join(f"{v:4d}" for v in row)) for row in grid]
