// JARVIS 3D core — a golden holographic "mind" in the style of JARVIS in Age of Ultron.
// Layers: circuit-trace shells, swirling arc shards, data panels, radial filaments, a spiral swoosh,
// a ring at the heart, golden bokeh, an audio spectrum ring, beat shockwaves and screen-wide dust.
// Everything is fine line-work with restrained bloom, so the HUD stays readable on top.
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const canvas = document.getElementById("gl");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
renderer.setClearColor(0x020206, 1);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 600);
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.45, 0.35, 0.22);  // gentle: detail over haze
composer.addPass(bloom);
composer.addPass(new OutputPass());

const STATES = {
  idle: 0xffa832, speaking: 0xffc04d, listening: 0xff6a2a, thinking: 0xffd98a, permission: 0xff3a24,
};
const color = new THREE.Color(STATES.idle);
const target = new THREE.Color();
const R = 2.2;                                   // hologram radius (world units)
const holo = new THREE.Group(); scene.add(holo);

// ---------------------------------------------------------------- helpers
const rand = (a, b) => a + Math.random() * (b - a);
const onSphere = (lat, lon, r) => new THREE.Vector3(r * Math.cos(lat) * Math.cos(lon), r * Math.sin(lat), r * Math.cos(lat) * Math.sin(lon));
function softDot() {
  const c = document.createElement("canvas"); c.width = c.height = 64; const g = c.getContext("2d");
  const gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  gr.addColorStop(0, "rgba(255,255,255,1)"); gr.addColorStop(.35, "rgba(255,255,255,.55)"); gr.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = gr; g.fillRect(0, 0, 64, 64); return new THREE.CanvasTexture(c);
}
const DOT = softDot();

// Line material whose brightness follows audio bands + flicker. Each vertex carries intensity, band, seed.
const BANDS = 16;
const lineUniforms = { uColor: { value: color }, uTime: { value: 0 }, uBands: { value: new Array(BANDS).fill(0) },
                       uScan: { value: -10 }, uGain: { value: 1 } };
const lineMat = new THREE.ShaderMaterial({
  uniforms: lineUniforms, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  vertexShader: `
    attribute float aI; attribute float aBand; attribute float aSeed;
    uniform float uTime; uniform float uBands[${BANDS}]; uniform float uScan; uniform float uGain;
    varying float vA;
    void main(){
      int b = int(aBand * ${BANDS - 1}.0 + .5);
      float band = 0.0; for (int i = 0; i < ${BANDS}; i++) if (i == b) band = uBands[i];
      float flicker = .72 + .28 * sin(uTime * (1.5 + aSeed * 5.0) + aSeed * 40.0);
      float scan = smoothstep(.35, 0., abs(position.y - uScan));                 // "thinking" sweep
      vA = aI * uGain * (.30 * flicker + band * 1.35 + scan * .9);
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.); }`,
  fragmentShader: `uniform vec3 uColor; varying float vA;
    void main(){ gl_FragColor = vec4(mix(uColor, vec3(1., .93, .75), clamp(vA - .8, 0., .6)), clamp(vA, 0., 1.)); }`,
});
function lineMesh(points, attrs) {  // points: flat xyz array of segment pairs; attrs: per-vertex {aI, aBand, aSeed}
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
  for (const k of ["aI", "aBand", "aSeed"]) g.setAttribute(k, new THREE.Float32BufferAttribute(attrs[k], 1));
  return new THREE.LineSegments(g, lineMat);
}
function builder() {
  const pts = [], aI = [], aBand = [], aSeed = [];
  return {
    seg(a, b, i, band, seed) { pts.push(a.x, a.y, a.z, b.x, b.y, b.z); aI.push(i, i); aBand.push(band, band); aSeed.push(seed, seed); },
    mesh() { return lineMesh(pts, { aI, aBand, aSeed }); },
  };
}

// ---------------------------------------------------------------- 1. circuit-trace shells
function circuitShell(radius, traces, density) {
  const b = builder();
  for (let t = 0; t < traces; t++) {
    let lat = rand(-1.35, 1.35), lon = rand(0, Math.PI * 2);
    let dir = Math.floor(Math.random() * 4);
    const step = rand(.03, .06), n = Math.floor(rand(4, 18) * density), inten = rand(.35, 1), seed = Math.random();
    const band = (lon / (Math.PI * 2)) % 1;
    const double = Math.random() < .25;
    for (let s = 0; s < n; s++) {
      if (Math.random() < .28) dir = (dir + (Math.random() < .5 ? 1 : 3)) % 4;
      const nlat = Math.max(-1.45, Math.min(1.45, lat + (dir === 0 ? step : dir === 2 ? -step : 0)));
      const nlon = lon + (dir === 1 ? step : dir === 3 ? -step : 0) / Math.max(.3, Math.cos(lat));
      if (Math.random() > .08) {  // occasional gaps: fragmented, holographic
        b.seg(onSphere(lat, lon, radius), onSphere(nlat, nlon, radius), inten, band, seed);
        if (double) b.seg(onSphere(lat + .012, lon, radius), onSphere(nlat + .012, nlon, radius), inten * .6, band, seed);
      }
      lat = nlat; lon = nlon;
    }
    if (Math.random() < .35) {  // little square pad at the end of a trace
      const d = .018, c = [[-d, -d], [d, -d], [d, d], [-d, d]];
      for (let k = 0; k < 4; k++) b.seg(onSphere(lat + c[k][0], lon + c[k][1], radius), onSphere(lat + c[(k + 1) % 4][0], lon + c[(k + 1) % 4][1], radius), inten, band, seed);
    }
  }
  return b.mesh();
}
const shells = [
  { m: circuitShell(R * 1.00, 260, 1.1), spin: new THREE.Vector3(0, .05, 0) },
  { m: circuitShell(R * 0.80, 170, .9), spin: new THREE.Vector3(.02, -.07, 0) },
  { m: circuitShell(R * 0.58, 110, .8), spin: new THREE.Vector3(-.03, .1, .02) },
  { m: circuitShell(R * 1.14, 90, .7), spin: new THREE.Vector3(0, -.03, .01) },
];
shells.forEach(s => holo.add(s.m));

// ---------------------------------------------------------------- 2. swirling arc shards
const arcs = [];
for (let i = 0; i < 46; i++) {
  const b = builder(), r = R * rand(.7, 1.25), len = rand(.4, 2.4), a0 = rand(0, Math.PI * 2), inten = rand(.4, 1);
  const lines = Math.random() < .4 ? 3 : Math.random() < .5 ? 2 : 1, seed = Math.random(), band = Math.random();
  const dashed = Math.random() < .3, N = Math.ceil(len * 40);
  for (let l = 0; l < lines; l++) {
    const rr = r + l * .025;
    for (let k = 0; k < N; k++) {
      if (dashed && k % 3 === 2) continue;
      const t0 = a0 + len * k / N, t1 = a0 + len * (k + 1) / N;
      b.seg(new THREE.Vector3(Math.cos(t0) * rr, Math.sin(t0) * rr, 0), new THREE.Vector3(Math.cos(t1) * rr, Math.sin(t1) * rr, 0), inten * (1 - l * .25), band, seed);
    }
  }
  const m = b.mesh(); m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), new THREE.Vector3().randomDirection());
  m.userData.speed = rand(-.35, .35); holo.add(m); arcs.push(m);
}

// ---------------------------------------------------------------- 3. spiral swoosh (reference 1)
{
  const b = builder();
  for (let l = 0; l < 4; l++) {
    const N = 260;
    for (let k = 0; k < N; k++) {
      const t0 = k / N, t1 = (k + 1) / N, f = (t) => { const a = t * Math.PI * 3.2, r = (.25 + t * .95) * R + l * .03; return new THREE.Vector3(Math.cos(a) * r, Math.sin(a) * r * .9, (t - .5) * .6); };
      b.seg(f(t0), f(t1), (1 - t0) * .9 * (1 - l * .2), .2, .5 + l * .1);
    }
  }
  const swoosh = b.mesh(); swoosh.rotation.set(.5, .3, 0); holo.add(swoosh); arcs.push(Object.assign(swoosh, { userData: { speed: .12 } }));
}

// ---------------------------------------------------------------- 4. radial filaments with travelling data pulses
const filaments = [], pulseGeo = new THREE.BufferGeometry();
{
  const b = builder();
  for (let i = 0; i < 22; i++) {
    const d = new THREE.Vector3().randomDirection(), r0 = rand(.2, .5), r1 = R * rand(1.1, 1.7);
    b.seg(d.clone().multiplyScalar(r0), d.clone().multiplyScalar(r1), rand(.15, .45), Math.random(), Math.random());
    filaments.push({ d, r0, r1, t: Math.random(), v: rand(.15, .5) });
  }
  holo.add(b.mesh());
  pulseGeo.setAttribute("position", new THREE.Float32BufferAttribute(new Float32Array(filaments.length * 3), 3));
}
const pulses = new THREE.Points(pulseGeo, new THREE.PointsMaterial({ size: .09, map: DOT, color, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
holo.add(pulses);

// ---------------------------------------------------------------- 5. floating data panels (reference 1)
function panelTexture() {
  const c = document.createElement("canvas"); c.width = 256; c.height = 160; const g = c.getContext("2d");
  g.strokeStyle = "rgba(255,255,255,.9)"; g.fillStyle = "rgba(255,255,255,.85)"; g.lineWidth = 2;
  g.strokeRect(3, 3, 250, 154);
  const kind = Math.floor(Math.random() * 3);
  if (kind === 0) for (let i = 0; i < 14; i++) { const h = rand(10, 120); g.fillRect(14 + i * 16, 150 - h, 9, h); }
  else if (kind === 1) for (let y = 18; y < 150; y += 12) g.fillRect(14, y, rand(40, 220), 4);
  else { for (let x = 14; x < 250; x += 20) { g.beginPath(); g.moveTo(x, 10); g.lineTo(x, 150); g.globalAlpha = .35; g.stroke(); }
         g.globalAlpha = 1; g.beginPath(); g.moveTo(10, 120); for (let x = 10; x < 250; x += 8) g.lineTo(x, 80 + Math.sin(x * .05) * 30 + rand(-8, 8)); g.stroke(); }
  return new THREE.CanvasTexture(c);
}
const panels = [];
for (let i = 0; i < 14; i++) {
  const w = rand(.35, .75), m = new THREE.Mesh(new THREE.PlaneGeometry(w, w * .62), new THREE.MeshBasicMaterial({
    map: panelTexture(), color, transparent: true, opacity: .5, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending }));
  const p = onSphere(rand(-1.1, 1.1), rand(0, Math.PI * 2), R * rand(.85, 1.05));
  m.position.copy(p); m.lookAt(p.clone().multiplyScalar(2)); m.userData = { seed: Math.random(), base: rand(.25, .55) };
  holo.add(m); panels.push(m);
}

// ---------------------------------------------------------------- 6. the heart: a ring with orbiting hoops
const heart = new THREE.Group(); holo.add(heart);
const heartRing = new THREE.Mesh(new THREE.TorusGeometry(.26, .045, 20, 96),
  new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .9, blending: THREE.AdditiveBlending }));
heart.add(heartRing);
const hoops = [];
for (let i = 0; i < 4; i++) {
  const h = new THREE.Mesh(new THREE.TorusGeometry(.36 + i * .07, .004, 6, 128),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .55, blending: THREE.AdditiveBlending }));
  h.rotation.set(rand(0, 3), rand(0, 3), 0); heart.add(h); hoops.push(h);
}
const spark = new THREE.Sprite(new THREE.SpriteMaterial({ map: DOT, color: 0xfff1d0, transparent: true, opacity: .8, blending: THREE.AdditiveBlending, depthWrite: false }));
spark.scale.setScalar(.35); heart.add(spark);

// ---------------------------------------------------------------- 7. golden bokeh inside the sphere
const BK = 1500, bpos = new Float32Array(BK * 3), bseed = new Float32Array(BK);
for (let i = 0; i < BK; i++) { const v = new THREE.Vector3().randomDirection().multiplyScalar(R * Math.cbrt(Math.random()) * 1.15); bpos.set([v.x, v.y, v.z], i * 3); bseed[i] = Math.random(); }
const bgeo = new THREE.BufferGeometry();
bgeo.setAttribute("position", new THREE.BufferAttribute(bpos, 3)); bgeo.setAttribute("seed", new THREE.BufferAttribute(bseed, 1));
const bokehMat = new THREE.ShaderMaterial({
  uniforms: { uTime: { value: 0 }, uLevel: { value: 0 }, uColor: { value: color }, uTex: { value: DOT } },
  transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  vertexShader: `attribute float seed; uniform float uTime, uLevel; varying float vA;
    void main(){ vec4 mv = modelViewMatrix * vec4(position, 1.);
      vA = (.25 + .75 * pow(.5 + .5 * sin(uTime * (.6 + seed * 2.) + seed * 50.), 3.)) * (.45 + uLevel * 1.4);
      gl_PointSize = (seed > .93 ? 34. : 7. + seed * 12.) * (1. + uLevel * .6) / -mv.z * 3.;
      gl_Position = projectionMatrix * mv; }`,
  fragmentShader: `uniform vec3 uColor; uniform sampler2D uTex; varying float vA;
    void main(){ vec4 t = texture2D(uTex, gl_PointCoord); gl_FragColor = vec4(mix(uColor, vec3(1., .95, .8), .25), 1.) * t * vA * .55; }`,
});
holo.add(new THREE.Points(bgeo, bokehMat));

// ---------------------------------------------------------------- 8. audio spectrum ring (faces the camera)
const SPEC = 200, specPos = new Float32Array(SPEC * 6), specLv = new Float32Array(SPEC);
const specGeo = new THREE.BufferGeometry(); specGeo.setAttribute("position", new THREE.BufferAttribute(specPos, 3));
const specAttrI = new Float32Array(SPEC * 2).fill(1), specBand = new Float32Array(SPEC * 2), specSeed = new Float32Array(SPEC * 2);
specGeo.setAttribute("aI", new THREE.BufferAttribute(specAttrI, 1));
specGeo.setAttribute("aBand", new THREE.BufferAttribute(specBand, 1));
specGeo.setAttribute("aSeed", new THREE.BufferAttribute(specSeed, 1));
const specMat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: .85, blending: THREE.AdditiveBlending, depthWrite: false });
const spectrum = new THREE.LineSegments(specGeo, specMat); scene.add(spectrum);
const specDots = new THREE.Points(new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(new Float32Array(SPEC * 3), 3)),
  new THREE.PointsMaterial({ size: .045, map: DOT, color, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
scene.add(specDots);

// ---------------------------------------------------------------- 9. beats: shock rings + sparks
const shocks = [];
function shock(k) {
  const pts = []; for (let i = 0; i <= 160; i++) { const a = i / 160 * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a), Math.sin(a), 0)); }
  const m = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity: .5 * k, blending: THREE.AdditiveBlending, depthWrite: false }));
  m.scale.setScalar(R * 1.25); scene.add(m); shocks.push(m);
}
const SP = 500, spPos = new Float32Array(SP * 3).fill(9999), spVel = new Float32Array(SP * 3), spLife = new Float32Array(SP);
const spGeo = new THREE.BufferGeometry(); spGeo.setAttribute("position", new THREE.BufferAttribute(spPos, 3));
scene.add(new THREE.Points(spGeo, new THREE.PointsMaterial({ size: .05, map: DOT, color, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending })));
let spNext = 0;
function burst(n, speed) {
  for (let k = 0; k < n; k++) {
    const i = spNext++ % SP, d = new THREE.Vector3().randomDirection();
    spPos.set([d.x * R, d.y * R, d.z * R * .3], i * 3); spVel.set([d.x * speed, d.y * speed, d.z * speed * .3], i * 3); spLife[i] = 1;
  }
}

// ---------------------------------------------------------------- 10. dust across the whole screen (no boundaries)
const DU = 2200, dpos = new Float32Array(DU * 3);
for (let i = 0; i < DU; i++) dpos.set([rand(-40, 40), rand(-22, 22), rand(-40, 6)], i * 3);
const dust = new THREE.Points(new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(dpos, 3)),
  new THREE.PointsMaterial({ size: .11, map: DOT, color, transparent: true, opacity: .35, depthWrite: false, blending: THREE.AdditiveBlending }));
scene.add(dust);

// ---------------------------------------------------------------- 11. deep space behind everything
const space = new THREE.Group(); scene.add(space);
// twinkling stars in every direction
{
  const N = 4200, pos = new Float32Array(N * 3), col = new Float32Array(N * 3), seed = new Float32Array(N);
  const tints = [new THREE.Color(0xffffff), new THREE.Color(0xbfd4ff), new THREE.Color(0xffe2b0), new THREE.Color(0x9fb8ff)];
  for (let i = 0; i < N; i++) {
    const v = new THREE.Vector3().randomDirection().multiplyScalar(rand(70, 160));
    if (v.z > 20) v.z = -v.z;  // keep them mostly behind the hologram
    pos.set([v.x, v.y, v.z], i * 3); col.set(tints[Math.floor(Math.random() * tints.length)].toArray(), i * 3); seed[i] = Math.random();
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3)); g.setAttribute("color", new THREE.BufferAttribute(col, 3));
  g.setAttribute("seed", new THREE.BufferAttribute(seed, 1));
  space.userData.stars = new THREE.ShaderMaterial({
    uniforms: { uTime: { value: 0 }, uTex: { value: DOT } }, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `attribute float seed; attribute vec3 color; uniform float uTime; varying vec3 vC; varying float vA;
      void main(){ vec4 mv = modelViewMatrix * vec4(position, 1.); vC = color;
        vA = .35 + .65 * pow(.5 + .5 * sin(uTime * (.4 + seed * 2.5) + seed * 80.), 4.);
        gl_PointSize = (seed > .985 ? 5.5 : 1.4 + seed * 2.2); gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `uniform sampler2D uTex; varying vec3 vC; varying float vA;
      void main(){ gl_FragColor = vec4(vC, 1.) * texture2D(uTex, gl_PointCoord) * vA * .8; }`,
  });
  space.add(new THREE.Points(g, space.userData.stars));
}
// a spiral galaxy far behind the hologram
{
  const N = 14000, pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
  const inner = new THREE.Color(0xffd79a), mid = new THREE.Color(0x8a6cff), outer = new THREE.Color(0x3a6dff);
  for (let i = 0; i < N; i++) {
    const arm = i % 4, r = Math.pow(Math.random(), 1.7) * 38, a = arm / 4 * Math.PI * 2 + r * .19 + rand(-.45, .45) * (1 + r * .02);
    pos.set([Math.cos(a) * r + rand(-.8, .8), rand(-.6, .6) * (1.4 - r / 40), Math.sin(a) * r + rand(-.8, .8)], i * 3);
    const c = r < 7 ? inner.clone().lerp(mid, r / 7) : mid.clone().lerp(outer, Math.min(1, (r - 7) / 25));
    col.set(c.toArray(), i * 3);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3)); g.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const galaxy = new THREE.Points(g, new THREE.PointsMaterial({ size: .32, map: DOT, vertexColors: true, transparent: true,
    opacity: .38, depthWrite: false, blending: THREE.AdditiveBlending }));
  galaxy.position.set(18, 6, -75); galaxy.rotation.set(1.05, .2, .35);
  space.add(galaxy); space.userData.galaxy = galaxy;
}
// soft nebula clouds
function nebulaTexture(hue) {
  const c = document.createElement("canvas"); c.width = c.height = 256; const g = c.getContext("2d");
  for (let i = 0; i < 26; i++) {
    const x = 128 + rand(-60, 60), y = 128 + rand(-60, 60), r = rand(30, 110);
    const gr = g.createRadialGradient(x, y, 0, x, y, r);
    gr.addColorStop(0, `hsla(${hue + rand(-25, 25)}, 80%, 60%, ${rand(.05, .16)})`); gr.addColorStop(1, "hsla(0,0%,0%,0)");
    g.fillStyle = gr; g.fillRect(0, 0, 256, 256);
  }
  return new THREE.CanvasTexture(c);
}
for (const [hue, x, y, z, s] of [[265, -40, 14, -90, 90], [215, 45, -18, -110, 110], [28, -10, -30, -95, 70], [290, 60, 30, -130, 120], [200, -70, -25, -120, 100]]) {
  const m = new THREE.Sprite(new THREE.SpriteMaterial({ map: nebulaTexture(hue), transparent: true, opacity: .2, depthWrite: false, blending: THREE.AdditiveBlending }));
  m.position.set(x, y, z); m.scale.setScalar(s * .75); space.add(m);
}
// shooting stars
const meteors = [];
function launchMeteor() {
  const start = new THREE.Vector3(rand(-60, 60), rand(10, 35), rand(-90, -60)), dir = new THREE.Vector3(rand(-1, -.4), rand(-.5, -.2), 0).normalize();
  const g = new THREE.BufferGeometry().setFromPoints([start, start.clone().addScaledVector(dir, -6)]);
  const m = new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0xfff2d8, transparent: true, opacity: .9, blending: THREE.AdditiveBlending, depthWrite: false }));
  m.userData = { dir, life: 1 }; space.add(m); meteors.push(m);
}

// ---------------------------------------------------------------- layout: park the core over the HUD's stage
let W = 0, H = 0;
function layout() {
  W = innerWidth; H = innerHeight;
  renderer.setSize(W, H, false); composer.setSize(W, H); bloom.setSize(W, H);
  camera.aspect = W / H;
}
addEventListener("resize", layout); layout();
function placeOverStage(stageEl) {
  const r = stageEl?.getBoundingClientRect();
  const cx = r ? r.left + r.width / 2 : W / 2, cy = r ? r.top + r.height / 2 : H / 2;
  const radiusPx = r ? r.width * .5 : Math.min(W, H) * .25;
  const dist = (R * 1.02) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * (H / 2) / radiusPx;
  camera.setViewOffset(W, H, W / 2 - cx, H / 2 - cy, W, H);
  return dist;
}

// ---------------------------------------------------------------- animation
const clock = new THREE.Clock();
let freq = new Uint8Array(512), avgE = 0, lastBeat = 0, drift = 0, scan = -10;
function frame() {
  const dt = Math.min(clock.getDelta(), .05), t = clock.elapsedTime;
  const vs = window.visState ? window.visState() : { state: "idle" };
  const st = vs.state in STATES ? vs.state : "idle";
  color.lerp(target.set(STATES[st]), .06);

  // audio → spectrum values and 16 bands
  const an = vs.analyser;
  if (an) { if (freq.length !== an.frequencyBinCount) freq = new Uint8Array(an.frequencyBinCount); an.getByteFrequencyData(freq); }
  const gain = vs.loud ? 1.1 : .45, half = SPEC / 2, bands = lineUniforms.uBands.value;
  let energy = 0, bass = 0;
  for (let i = 0; i < SPEC; i++) {
    const k = i < half ? i : SPEC - 1 - i, bin = Math.floor(1 + Math.pow(k / half, 1.7) * freq.length * .5);
    let v = an ? Math.pow(freq[bin] / 255, 1.3) * gain : 0;
    v = Math.max(v, .03 + .02 * Math.sin(t * 1.4 + i * .27) + (st === "thinking" ? .18 * Math.max(0, Math.sin(t * 6 - i * .12)) : 0));
    specLv[i] += (v - specLv[i]) * (v > specLv[i] ? .5 : .12);
    energy += specLv[i]; if (k < 12) bass += specLv[i];
  }
  energy /= SPEC; bass /= 24;
  for (let b = 0; b < BANDS; b++) {
    const i0 = Math.floor(b / BANDS * half);
    bands[b] += ((specLv[i0] + specLv[i0 + 2]) * .5 - bands[b]) * .3;
  }

  // spectrum ring geometry
  const sp = spectrum.geometry.attributes.position.array, dots = specDots.geometry.attributes.position.array;
  const rot = t * .05;
  for (let i = 0; i < SPEC; i++) {
    const a = i / SPEC * Math.PI * 2 + rot - Math.PI / 2, r0 = R * 1.2, r1 = r0 + .04 + specLv[i] * 1.25;
    const c = Math.cos(a), s = Math.sin(a);
    sp.set([c * r0, s * r0, 0, c * r1, s * r1, 0], i * 6); dots.set([c * (r1 + .05), s * (r1 + .05), 0], i * 3);
  }
  spectrum.geometry.attributes.position.needsUpdate = true; specDots.geometry.attributes.position.needsUpdate = true;
  specMat.opacity = .35 + Math.min(.55, energy * 2);

  // hologram motion
  const speed = st === "thinking" ? 3.2 : 1 + energy * 3;
  shells.forEach(s => { s.m.rotation.x += s.spin.x * dt * speed; s.m.rotation.y += s.spin.y * dt * speed; s.m.rotation.z += s.spin.z * dt * speed; });
  arcs.forEach(a => a.rotateZ(a.userData.speed * dt * speed));
  holo.scale.setScalar(1 + energy * .07 + bass * .03);
  heart.rotation.y += dt * .8 * speed; heart.rotation.x += dt * .3;
  hoops.forEach((h, i) => { h.rotation.x += dt * (.4 + i * .2) * speed; h.rotation.y += dt * (.3 - i * .1) * speed; });
  heartRing.scale.setScalar(1 + bass * .5); spark.scale.setScalar(.3 + energy * .9);
  panels.forEach(p => { p.material.opacity = p.userData.base * (.55 + .45 * Math.sin(t * (1 + p.userData.seed * 3) + p.userData.seed * 20)) + energy * .5; });
  filaments.forEach((f, i) => { f.t = (f.t + f.v * dt * speed) % 1; const r = f.r0 + (f.r1 - f.r0) * f.t; pulseGeo.attributes.position.array.set([f.d.x * r, f.d.y * r, f.d.z * r], i * 3); });
  pulseGeo.attributes.position.needsUpdate = true;
  bokehMat.uniforms.uTime.value = t; bokehMat.uniforms.uLevel.value = energy;
  lineUniforms.uTime.value = t; lineUniforms.uGain.value = st === "permission" ? .8 + .4 * Math.sin(t * 6) : 1;
  if (st === "thinking") { scan += dt * 2.6; if (scan > R * 1.3) scan = -R * 1.3; } else scan = -10;
  lineUniforms.uScan.value = scan;
  dust.rotation.y += dt * .004; dust.material.opacity = .25 + energy * .4;
  space.userData.stars.uniforms.uTime.value = t;
  space.userData.galaxy.rotation.y += dt * .01;
  if (Math.random() < dt * .12) launchMeteor();
  for (let i = meteors.length - 1; i >= 0; i--) {
    const m = meteors[i]; m.position.addScaledVector(m.userData.dir, dt * 70); m.userData.life -= dt * .9; m.material.opacity = m.userData.life;
    if (m.userData.life <= 0) { space.remove(m); m.geometry.dispose(); m.material.dispose(); meteors.splice(i, 1); }
  }
  for (const m of [heartRing.material, pulses.material, spark.material, dust.material, specDots.material]) if (m.color) m.color = color;
  hoops.forEach(h => h.material.color = color);

  // beats
  if (energy > avgE * 1.35 && energy > .1 && t - lastBeat > .2) { shock(Math.min(1, energy * 2.5)); burst(20 + energy * 80, 1.5 + energy * 5); lastBeat = t; }
  avgE += (energy - avgE) * .05;
  for (let i = shocks.length - 1; i >= 0; i--) {
    const s = shocks[i]; s.scale.multiplyScalar(1 + dt * .9); s.material.opacity -= dt * .6;
    if (s.material.opacity <= 0) { scene.remove(s); s.geometry.dispose(); s.material.dispose(); shocks.splice(i, 1); }
  }
  for (let i = 0; i < SP; i++) {
    if (spLife[i] <= 0) continue;
    spLife[i] -= dt * .9; for (let k = 0; k < 3; k++) { spPos[i * 3 + k] += spVel[i * 3 + k] * dt; spVel[i * 3 + k] *= .97; }
    if (spLife[i] <= 0) spPos[i * 3 + 1] = 9999;
  }
  spGeo.attributes.position.needsUpdate = true;

  // camera: slow cinematic drift, framed over the stage
  drift += dt * .06;
  const dist = placeOverStage(vs.stage);
  const yaw = Math.sin(drift) * .28, pitch = .08 + Math.sin(drift * .7) * .06;
  camera.position.set(Math.sin(yaw) * dist, Math.sin(pitch) * dist, Math.cos(yaw) * dist);
  camera.lookAt(0, 0, 0);
  spectrum.quaternion.copy(camera.quaternion); specDots.quaternion.copy(camera.quaternion);  // ring always faces Dev
  shocks.forEach(s => s.quaternion.copy(camera.quaternion));
  bloom.strength = .38 + energy * .45;

  composer.render();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
