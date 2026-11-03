/* 背景流体：鼠标划过留下随移动变色的墨迹，几秒后退回白底。
 * 代码原样取自个人网站 liyucheng.me（F:\我的编程项目\268_我的个人网站\me.html 的 FLUID 模块），
 * 它参考 Pavel Dobryakov 的 WebGL-Fluid-Simulation（MIT）。
 * 面板收起时暂停（bot.onEvents 的 hidden / shown 事件），不在后台空跑 GPU。
 * 和网站不同：墨迹是单色的（默认紫色），颜色和透明度在面板「设置 → 外观」里调（window.fluid.set）。
 */
(() => {
'use strict';
if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
let MONO = [0.69, 0.32, 0.87];   /* #af52de 网站时钟里的紫 */
const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
const FLUID = (function(){
  const canvas = document.getElementById('fluid');
  const config = {
    SIM_RESOLUTION: 128,
    DYE_RESOLUTION: Math.min(innerWidth, innerHeight) < 640 ? 512 : 1024,
    DENSITY_DISSIPATION: 2.4,     /* 染料消散：越大退回白底越快 */
    VELOCITY_DISSIPATION: 0.8,
    PRESSURE: 0.8,
    PRESSURE_ITERATIONS: 20,
    CURL: 28,                     /* 涡度：造出大理石卷纹 */
    SPLAT_RADIUS: 0.09,
    SPLAT_FORCE: 4500
  };

  const params = { alpha: true, depth: false, stencil: false, antialias: false, preserveDrawingBuffer: false };
  let gl = canvas.getContext('webgl2', params);
  const isWebGL2 = !!gl;
  if (!isWebGL2) gl = canvas.getContext('webgl', params) || canvas.getContext('experimental-webgl', params);
  if (!gl) return null;

  let halfFloatExt = null, supportLinearFiltering = false;
  if (isWebGL2){
    gl.getExtension('EXT_color_buffer_float');
    supportLinearFiltering = true;          /* 16F 过滤是 WebGL2 核心能力 */
  } else {
    halfFloatExt = gl.getExtension('OES_texture_half_float');
    supportLinearFiltering = !!gl.getExtension('OES_texture_half_float_linear');
    if (!halfFloatExt) return null;
  }
  gl.clearColor(0, 0, 0, 0);
  gl.disable(gl.BLEND);
  const texType = isWebGL2 ? gl.HALF_FLOAT : halfFloatExt.HALF_FLOAT_OES;

  function supportsFormat(internalFormat, format){
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, internalFormat, 4, 4, 0, format, texType, null);
    const f = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, f);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
    const ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
    gl.deleteFramebuffer(f); gl.deleteTexture(t);
    return ok;
  }
  function pickFormat(internalFormat, format){
    if (supportsFormat(internalFormat, format)) return { internalFormat, format };
    if (isWebGL2){
      if (internalFormat === gl.R16F)  return pickFormat(gl.RG16F, gl.RG);
      if (internalFormat === gl.RG16F) return pickFormat(gl.RGBA16F, gl.RGBA);
    }
    return null;
  }
  let formatRGBA, formatRG, formatR;
  if (isWebGL2){
    formatRGBA = pickFormat(gl.RGBA16F, gl.RGBA);
    formatRG   = pickFormat(gl.RG16F, gl.RG);
    formatR    = pickFormat(gl.R16F, gl.RED);
  } else {
    formatRGBA = supportsFormat(gl.RGBA, gl.RGBA) ? { internalFormat: gl.RGBA, format: gl.RGBA } : null;
    formatRG = formatRGBA; formatR = formatRGBA;
  }
  if (!formatRGBA || !formatRG || !formatR) return null;

  /* ---------- 着色器 ---------- */
  function compile(type, src){
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    return s;
  }
  const baseVertex = compile(gl.VERTEX_SHADER, `
    precision highp float;
    attribute vec2 aPosition;
    varying vec2 vUv, vL, vR, vT, vB;
    uniform vec2 texelSize;
    void main(){
      vUv = aPosition * 0.5 + 0.5;
      vL = vUv - vec2(texelSize.x, 0.0);
      vR = vUv + vec2(texelSize.x, 0.0);
      vT = vUv + vec2(0.0, texelSize.y);
      vB = vUv - vec2(0.0, texelSize.y);
      gl_Position = vec4(aPosition, 0.0, 1.0);
    }`);
  function Program(fragSrc){
    const p = gl.createProgram();
    gl.attachShader(p, baseVertex);
    gl.attachShader(p, compile(gl.FRAGMENT_SHADER, fragSrc));
    gl.bindAttribLocation(p, 0, 'aPosition');
    gl.linkProgram(p);
    this.program = p;
    this.uniforms = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++){
      const name = gl.getActiveUniform(p, i).name;
      this.uniforms[name] = gl.getUniformLocation(p, name);
    }
  }
  Program.prototype.bind = function(){ gl.useProgram(this.program); };

  const clearProgram = new Program(`
    precision mediump float; precision mediump sampler2D;
    varying vec2 vUv; uniform sampler2D uTexture; uniform float value;
    void main(){ gl_FragColor = value * texture2D(uTexture, vUv); }`);

  const splatProgram = new Program(`
    precision highp float; precision highp sampler2D;
    varying vec2 vUv; uniform sampler2D uTarget;
    uniform float aspectRatio; uniform vec3 color; uniform vec2 point; uniform float radius;
    void main(){
      vec2 p = vUv - point.xy; p.x *= aspectRatio;
      vec3 splat = exp(-dot(p, p) / radius) * color;
      vec3 base = texture2D(uTarget, vUv).xyz;
      gl_FragColor = vec4(base + splat, 1.0);
    }`);

  const advectionProgram = new Program(
    (supportLinearFiltering ? '' : '#define MANUAL_FILTERING\n') + `
    precision highp float; precision highp sampler2D;
    varying vec2 vUv;
    uniform sampler2D uVelocity, uSource;
    uniform vec2 texelSize, dyeTexelSize;
    uniform float dt, dissipation;
    vec4 bilerp(sampler2D sam, vec2 uv, vec2 tsize){
      vec2 st = uv / tsize - 0.5;
      vec2 iuv = floor(st), fuv = fract(st);
      vec4 a = texture2D(sam, (iuv + vec2(0.5, 0.5)) * tsize);
      vec4 b = texture2D(sam, (iuv + vec2(1.5, 0.5)) * tsize);
      vec4 c = texture2D(sam, (iuv + vec2(0.5, 1.5)) * tsize);
      vec4 d = texture2D(sam, (iuv + vec2(1.5, 1.5)) * tsize);
      return mix(mix(a, b, fuv.x), mix(c, d, fuv.x), fuv.y);
    }
    void main(){
    #ifdef MANUAL_FILTERING
      vec2 coord = vUv - dt * bilerp(uVelocity, vUv, texelSize).xy * texelSize;
      vec4 result = bilerp(uSource, coord, dyeTexelSize);
    #else
      vec2 coord = vUv - dt * texture2D(uVelocity, vUv).xy * texelSize;
      vec4 result = texture2D(uSource, coord);
    #endif
      float decay = 1.0 + dissipation * dt;
      gl_FragColor = result / decay;
    }`);

  const divergenceProgram = new Program(`
    precision mediump float; precision mediump sampler2D;
    varying vec2 vUv, vL, vR, vT, vB;
    uniform sampler2D uVelocity;
    void main(){
      float L = texture2D(uVelocity, vL).x;
      float R = texture2D(uVelocity, vR).x;
      float T = texture2D(uVelocity, vT).y;
      float B = texture2D(uVelocity, vB).y;
      vec2 C = texture2D(uVelocity, vUv).xy;
      if (vL.x < 0.0){ L = -C.x; }
      if (vR.x > 1.0){ R = -C.x; }
      if (vT.y > 1.0){ T = -C.y; }
      if (vB.y < 0.0){ B = -C.y; }
      gl_FragColor = vec4(0.5 * (R - L + T - B), 0.0, 0.0, 1.0);
    }`);

  const curlProgram = new Program(`
    precision mediump float; precision mediump sampler2D;
    varying vec2 vUv, vL, vR, vT, vB;
    uniform sampler2D uVelocity;
    void main(){
      float L = texture2D(uVelocity, vL).y;
      float R = texture2D(uVelocity, vR).y;
      float T = texture2D(uVelocity, vT).x;
      float B = texture2D(uVelocity, vB).x;
      gl_FragColor = vec4(0.5 * (R - L - T + B), 0.0, 0.0, 1.0);
    }`);

  const vorticityProgram = new Program(`
    precision highp float; precision highp sampler2D;
    varying vec2 vUv, vL, vR, vT, vB;
    uniform sampler2D uVelocity, uCurl;
    uniform float curl, dt;
    void main(){
      float L = texture2D(uCurl, vL).x;
      float R = texture2D(uCurl, vR).x;
      float T = texture2D(uCurl, vT).x;
      float B = texture2D(uCurl, vB).x;
      float C = texture2D(uCurl, vUv).x;
      vec2 force = 0.5 * vec2(abs(T) - abs(B), abs(R) - abs(L));
      force /= length(force) + 0.0001;
      force *= curl * C;
      force.y *= -1.0;
      vec2 velocity = texture2D(uVelocity, vUv).xy;
      velocity += force * dt;
      velocity = min(max(velocity, -1000.0), 1000.0);
      gl_FragColor = vec4(velocity, 0.0, 1.0);
    }`);

  const pressureProgram = new Program(`
    precision mediump float; precision mediump sampler2D;
    varying vec2 vUv, vL, vR, vT, vB;
    uniform sampler2D uPressure, uDivergence;
    void main(){
      float L = texture2D(uPressure, vL).x;
      float R = texture2D(uPressure, vR).x;
      float T = texture2D(uPressure, vT).x;
      float B = texture2D(uPressure, vB).x;
      float divergence = texture2D(uDivergence, vUv).x;
      gl_FragColor = vec4((L + R + B + T - divergence) * 0.25, 0.0, 0.0, 1.0);
    }`);

  const gradientProgram = new Program(`
    precision mediump float; precision mediump sampler2D;
    varying vec2 vUv, vL, vR, vT, vB;
    uniform sampler2D uPressure, uVelocity;
    void main(){
      float L = texture2D(uPressure, vL).x;
      float R = texture2D(uPressure, vR).x;
      float T = texture2D(uPressure, vT).x;
      float B = texture2D(uPressure, vB).x;
      vec2 velocity = texture2D(uVelocity, vUv).xy;
      velocity -= vec2(R - L, T - B);
      gl_FragColor = vec4(velocity, 0.0, 1.0);
    }`);

  /* 显示：预乘 alpha 输出，浏览器把它合成到白底上 → 白底上出鲜艳彩雾 */
  const displayProgram = new Program(`
    precision highp float; precision highp sampler2D;
    varying vec2 vUv; uniform sampler2D uTexture;
    void main(){
      vec3 c = texture2D(uTexture, vUv).rgb;
      float a = max(c.r, max(c.g, c.b));
      /* 单色版：色相保持饱满（c/a），浓度当透明度并放大一些；深色墨也能看清 */
      float k = clamp(a * 2.6, 0.0, 1.0);
      gl_FragColor = vec4(c / max(a, 1e-4) * k, k);
    }`);

  /* ---------- 全屏四边形 ---------- */
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,-1,1,1,1,1,-1]), gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array([0,1,2,0,2,3]), gl.STATIC_DRAW);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.enableVertexAttribArray(0);

  function blit(target){
    if (target == null){
      gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    } else {
      gl.viewport(0, 0, target.width, target.height);
      gl.bindFramebuffer(gl.FRAMEBUFFER, target.fbo);
    }
    gl.drawElements(gl.TRIANGLES, 6, gl.UNSIGNED_SHORT, 0);
  }

  /* ---------- 帧缓冲 ---------- */
  function createFBO(w, h, internalFormat, format, param){
    gl.activeTexture(gl.TEXTURE0);
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, param);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, param);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, internalFormat, w, h, 0, format, texType, null);
    const fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
    gl.viewport(0, 0, w, h);
    gl.clear(gl.COLOR_BUFFER_BIT);
    return {
      texture, fbo, width: w, height: h,
      texelSizeX: 1 / w, texelSizeY: 1 / h,
      attach(id){ gl.activeTexture(gl.TEXTURE0 + id); gl.bindTexture(gl.TEXTURE_2D, texture); return id; }
    };
  }
  function createDoubleFBO(w, h, internalFormat, format, param){
    let a = createFBO(w, h, internalFormat, format, param);
    let b = createFBO(w, h, internalFormat, format, param);
    return {
      get read(){ return a; },
      get write(){ return b; },
      swap(){ const t = a; a = b; b = t; },
      texelSizeX: 1 / w, texelSizeY: 1 / h
    };
  }
  function getResolution(res){
    let aspect = gl.drawingBufferWidth / gl.drawingBufferHeight;
    if (aspect < 1) aspect = 1 / aspect;
    const min = Math.round(res), max = Math.round(res * aspect);
    return gl.drawingBufferWidth > gl.drawingBufferHeight
      ? { width: max, height: min } : { width: min, height: max };
  }

  let dye, velocity, divergence, curl, pressure;
  const filt = supportLinearFiltering ? gl.LINEAR : gl.NEAREST;
  function initFramebuffers(){
    const simRes = getResolution(config.SIM_RESOLUTION);
    const dyeRes = getResolution(config.DYE_RESOLUTION);
    dye        = createDoubleFBO(dyeRes.width, dyeRes.height, formatRGBA.internalFormat, formatRGBA.format, filt);
    velocity   = createDoubleFBO(simRes.width, simRes.height, formatRG.internalFormat, formatRG.format, filt);
    divergence = createFBO(simRes.width, simRes.height, formatR.internalFormat, formatR.format, gl.NEAREST);
    curl       = createFBO(simRes.width, simRes.height, formatR.internalFormat, formatR.format, gl.NEAREST);
    pressure   = createDoubleFBO(simRes.width, simRes.height, formatR.internalFormat, formatR.format, gl.NEAREST);
  }

  function resize(){
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const w = Math.round(innerWidth * dpr), h = Math.round(innerHeight * dpr);
    if (canvas.width === w && canvas.height === h) return;
    canvas.width = w; canvas.height = h;
    initFramebuffers();
  }
  resize();

  /* ---------- 指针 → 染料 ---------- */
  /* 单色墨迹：MONO 是 0~1 的 RGB，由 window.fluid.set({color}) 设置 */
  function mono(){ return MONO; }
  function hsv(h){
    h = (h % 360 + 360) % 360 / 60;
    const i = Math.floor(h), f = h - i, q = 1 - f;
    switch (i % 6){
      case 0: return [1, f, 0];
      case 1: return [q, 1, 0];
      case 2: return [0, 1, f];
      case 3: return [0, q, 1];
      case 4: return [f, 0, 1];
      default: return [1, 0, q];
    }
  }
  const pointer = { x: -1, y: -1, dx: 0, dy: 0, moved: false, hue: Math.random() * 360 };

  addEventListener('pointermove', e => {
    const x = e.clientX / innerWidth;
    const y = 1 - e.clientY / innerHeight;
    if (pointer.x >= 0){
      pointer.dx = (x - pointer.x) * config.SPLAT_FORCE;
      pointer.dy = (y - pointer.y) * config.SPLAT_FORCE;
      const dist = Math.hypot(x - pointer.x, y - pointer.y);
      pointer.hue = (pointer.hue + dist * 260 + 0.9) % 360;   /* 颜色随移动流转 */
      if (dist > 0) pointer.moved = true;
    }
    pointer.x = x; pointer.y = y;
  }, { passive: true });

  addEventListener('pointerdown', e => {
    const x = e.clientX / innerWidth;
    const y = 1 - e.clientY / innerHeight;
    for (let i = 0; i < 6; i++){
      const a = Math.random() * Math.PI * 2;
      const s = 400 + Math.random() * 700;
      const c = mono();
      splat(x, y, Math.cos(a) * s, Math.sin(a) * s, [c[0] * 0.3, c[1] * 0.3, c[2] * 0.3]);
    }
  }, { passive: true });

  function correctRadius(r){
    const aspect = canvas.width / canvas.height;
    return aspect > 1 ? r * aspect : r;
  }
  function splat(x, y, dx, dy, color){
    splatProgram.bind();
    gl.uniform2f(splatProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(splatProgram.uniforms.uTarget, velocity.read.attach(0));
    gl.uniform1f(splatProgram.uniforms.aspectRatio, canvas.width / canvas.height);
    gl.uniform2f(splatProgram.uniforms.point, x, y);
    gl.uniform3f(splatProgram.uniforms.color, dx, dy, 0);
    gl.uniform1f(splatProgram.uniforms.radius, correctRadius(config.SPLAT_RADIUS / 100));
    blit(velocity.write); velocity.swap();
    gl.uniform1i(splatProgram.uniforms.uTarget, dye.read.attach(0));
    gl.uniform3f(splatProgram.uniforms.color, color[0], color[1], color[2]);
    blit(dye.write); dye.swap();
  }

  /* ---------- 模拟一步 + 渲染 ---------- */
  function step(dt){
    gl.disable(gl.BLEND);

    curlProgram.bind();
    gl.uniform2f(curlProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(curlProgram.uniforms.uVelocity, velocity.read.attach(0));
    blit(curl);

    vorticityProgram.bind();
    gl.uniform2f(vorticityProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(vorticityProgram.uniforms.uVelocity, velocity.read.attach(0));
    gl.uniform1i(vorticityProgram.uniforms.uCurl, curl.attach(1));
    gl.uniform1f(vorticityProgram.uniforms.curl, config.CURL);
    gl.uniform1f(vorticityProgram.uniforms.dt, dt);
    blit(velocity.write); velocity.swap();

    divergenceProgram.bind();
    gl.uniform2f(divergenceProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(divergenceProgram.uniforms.uVelocity, velocity.read.attach(0));
    blit(divergence);

    clearProgram.bind();
    gl.uniform2f(clearProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(clearProgram.uniforms.uTexture, pressure.read.attach(0));
    gl.uniform1f(clearProgram.uniforms.value, config.PRESSURE);
    blit(pressure.write); pressure.swap();

    pressureProgram.bind();
    gl.uniform2f(pressureProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(pressureProgram.uniforms.uDivergence, divergence.attach(0));
    for (let i = 0; i < config.PRESSURE_ITERATIONS; i++){
      gl.uniform1i(pressureProgram.uniforms.uPressure, pressure.read.attach(1));
      blit(pressure.write); pressure.swap();
    }

    gradientProgram.bind();
    gl.uniform2f(gradientProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(gradientProgram.uniforms.uPressure, pressure.read.attach(0));
    gl.uniform1i(gradientProgram.uniforms.uVelocity, velocity.read.attach(1));
    blit(velocity.write); velocity.swap();

    advectionProgram.bind();
    gl.uniform2f(advectionProgram.uniforms.texelSize, velocity.texelSizeX, velocity.texelSizeY);
    if (!supportLinearFiltering)
      gl.uniform2f(advectionProgram.uniforms.dyeTexelSize, velocity.texelSizeX, velocity.texelSizeY);
    gl.uniform1i(advectionProgram.uniforms.uVelocity, velocity.read.attach(0));
    gl.uniform1i(advectionProgram.uniforms.uSource, velocity.read.attach(0));
    gl.uniform1f(advectionProgram.uniforms.dt, dt);
    gl.uniform1f(advectionProgram.uniforms.dissipation, config.VELOCITY_DISSIPATION);
    blit(velocity.write); velocity.swap();

    if (!supportLinearFiltering)
      gl.uniform2f(advectionProgram.uniforms.dyeTexelSize, dye.texelSizeX, dye.texelSizeY);
    gl.uniform1i(advectionProgram.uniforms.uVelocity, velocity.read.attach(0));
    gl.uniform1i(advectionProgram.uniforms.uSource, dye.read.attach(1));
    gl.uniform1f(advectionProgram.uniforms.dissipation, config.DENSITY_DISSIPATION);
    blit(dye.write); dye.swap();
  }

  function render(){
    displayProgram.bind();
    gl.uniform1i(displayProgram.uniforms.uTexture, dye.read.attach(0));
    blit(null);
  }

  function frame(dt){
    resize();
    if (pointer.moved){
      pointer.moved = false;
      const c = mono();
      splat(pointer.x, pointer.y, pointer.dx, pointer.dy, [c[0] * 0.22, c[1] * 0.22, c[2] * 0.22]);
    }
    step(Math.min(dt, 0.0166));
    render();
  }

  return { frame, resize };
})();

if (!FLUID) return;
let running = true, last = performance.now();
function loop(now){
  if (!running) return;
  const dt = Math.min((now - last) / 1000, 0.016666);
  last = now;
  FLUID.frame(dt);
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
addEventListener('resize', () => FLUID.resize());
const canvasEl = document.getElementById('fluid');
window.fluid = {
  set({ color, opacity } = {}){
    if (color) MONO = hexToRgb(color);
    if (opacity != null) canvasEl.style.opacity = String(opacity);
  },
  pause(){ running = false; },
  resume(){ if (!running){ running = true; last = performance.now(); requestAnimationFrame(loop); } },
};
})();
