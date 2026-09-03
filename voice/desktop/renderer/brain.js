/**
 * BrainVisualizer - Three.js "Jarvis" organic neural core.
 *
 * Warm emissive core (orange/yellow) surrounded by a volumetric particle
 * cloud that flows through a 3D curl-noise field, plus curved tendrils
 * (CatmullRomCurve3) that wiggle like jellyfish tentacles. Morphs between
 * idle / listening / thinking / speaking states with 800ms transitions.
 *
 * Design goals matched to the Jarvis reference footage:
 *   - Warm yellow/orange hot core with cool blue rim light
 *   - Curved flowing tendrils, NOT rigid spokes
 *   - Particles visibly flow through space via noise-field velocities
 *   - Organic breathing + rotation, never static
 *
 * Public API (stable):
 *   new BrainVisualizer(container)
 *   .setState(state)   // 'idle' | 'listening' | 'thinking' | 'speaking'
 *   .animate()
 *   .resize()
 *   .dispose()
 */

import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass }     from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';


// ---------- Tunables ----------

const PARTICLE_COUNT   = 1600;
const TENDRIL_COUNT    = 80;
const TENDRIL_CTRL_PTS = 10;    // control points per tendril
const TENDRIL_SEGMENTS = 48;    // sampled points along the rendered curve
const TRANSITION_MS    = 800;
const CORE_RADIUS      = 0.22;

// Color palette (linear RGB, 0..1).
const COLOR_WARM_CORE  = [1.00, 0.70, 0.28];  // #ffb347 - hot yellow/orange core
const COLOR_WARM_GLOW  = [1.00, 0.55, 0.20];  // tendril warm
const COLOR_COOL_RIM   = [0.32, 0.64, 1.00];  // cool blue rim (idle particles)
const COLOR_VIOLET     = [0.72, 0.38, 1.00];  // thinking
const COLOR_ICE        = [0.80, 0.92, 1.00];  // speaking


/**
 * Per-state configuration. Every scalar is lerped on transition,
 * color fields are lerped component-wise.
 *
 *  coreScale          size multiplier on the emissive core
 *  coreIntensity      emissive + point-light strength
 *  coolLightInt       cool blue rim light intensity
 *  cloudRadius        outer bound of the particle cloud
 *  cloudInner         [0..1] density bias: 0 = dense core, 1 = hollow sphere
 *  flowAmp            amplitude of noise-field displacement
 *  flowSpeed          rate at which the flow field evolves
 *  particleSize       point sprite base size (attenuated)
 *  particleOpacity    alpha of the point sprites
 *  tendrilAmp         radial amplitude of tendril wiggle
 *  tendrilLength      median tendril reach
 *  tendrilOpacity     alpha of the curve lines
 *  tendrilTwist       angular jitter on control points (chaos)
 *  tendrilSpeed       wiggle frequency
 *  shapeMode          0 = scattered, 1 = starburst, 2 = chaotic, 3 = latlon sphere
 *  rotationSpeed      global yaw rate (rad/sec)
 *  tiltAmp            x-axis oscillation magnitude
 *  pulseHz            breathing frequency
 *  pulseAmp           breathing amplitude
 *  bloomStrength      UnrealBloomPass.strength
 *  bloomRadius        UnrealBloomPass.radius
 *  bloomThreshold     UnrealBloomPass.threshold
 *  particleColor      linear rgb of non-core particles
 *  tendrilColor       linear rgb of tendrils
 *  coreColor          linear rgb of the warm core
 */
const STATE_CONFIG = {
  idle: {
    coreScale: 0.95,
    coreIntensity: 1.1,
    coolLightInt: 1.1,
    cloudRadius: 3.6,
    cloudInner: 0.15,
    flowAmp: 0.55,
    flowSpeed: 0.12,
    particleSize: 0.055,
    particleOpacity: 0.72,
    tendrilAmp: 0.32,
    tendrilLength: 1.8,
    tendrilOpacity: 0.30,
    tendrilTwist: 0.05,
    tendrilSpeed: 0.6,
    tendrilCoverage: 0.35,
    shapeMode: 0,
    rotationSpeed: 0.08,
    tiltAmp: 0.06,
    pulseHz: 0.6,
    pulseAmp: 0.05,
    bloomStrength: 0.95,
    bloomRadius: 0.80,
    bloomThreshold: 0.0,
    particleColor: [...COLOR_COOL_RIM],
    tendrilColor:  [...COLOR_WARM_GLOW],
    coreColor:     [...COLOR_WARM_CORE],
  },

  listening: {
    coreScale: 1.35,
    coreIntensity: 2.4,
    coolLightInt: 1.4,
    cloudRadius: 2.6,
    cloudInner: 0.05,
    flowAmp: 0.35,
    flowSpeed: 0.30,
    particleSize: 0.062,
    particleOpacity: 0.92,
    tendrilAmp: 0.22,
    tendrilLength: 2.4,
    tendrilOpacity: 0.62,
    tendrilTwist: 0.06,
    tendrilSpeed: 1.1,
    tendrilCoverage: 0.90,
    shapeMode: 1,
    rotationSpeed: 0.14,
    tiltAmp: 0.04,
    pulseHz: 2.2,
    pulseAmp: 0.14,
    bloomStrength: 1.55,
    bloomRadius: 0.90,
    bloomThreshold: 0.0,
    particleColor: [...COLOR_WARM_GLOW],
    tendrilColor:  [...COLOR_WARM_CORE],
    coreColor:     [...COLOR_WARM_CORE],
  },

  thinking: {
    coreScale: 1.6,
    coreIntensity: 3.5,
    coolLightInt: 2.2,
    cloudRadius: 5.0,
    cloudInner: 0.05,
    flowAmp: 2.0,
    flowSpeed: 1.5,
    particleSize: 0.075,
    particleOpacity: 1.0,
    tendrilAmp: 1.4,
    tendrilLength: 4.0,
    tendrilOpacity: 0.85,
    tendrilTwist: 0.6,
    tendrilSpeed: 4.5,
    tendrilCoverage: 1.0,
    shapeMode: 2,
    rotationSpeed: 1.5,
    tiltAmp: 0.20,
    pulseHz: 6.0,
    pulseAmp: 0.18,
    bloomStrength: 2.5,
    bloomRadius: 1.1,
    bloomThreshold: 0.0,
    particleColor: [...COLOR_VIOLET],
    tendrilColor:  [...COLOR_WARM_CORE],
    coreColor:     [...COLOR_WARM_CORE],
  },

  speaking: {
    coreScale: 1.5,
    coreIntensity: 2.8,
    coolLightInt: 2.0,
    cloudRadius: 3.4,
    cloudInner: 0.85,
    flowAmp: 0.45,
    flowSpeed: 0.40,
    particleSize: 0.090,
    particleOpacity: 1.0,
    tendrilAmp: 0.50,
    tendrilLength: 2.2,
    tendrilOpacity: 0.55,
    tendrilTwist: 0.15,
    tendrilSpeed: 2.5,
    tendrilCoverage: 0.65,
    shapeMode: 3,
    rotationSpeed: 0.45,
    tiltAmp: 0.10,
    pulseHz: 5.0,
    pulseAmp: 0.18,
    bloomStrength: 1.90,
    bloomRadius: 0.95,
    bloomThreshold: 0.0,
    particleColor: [...COLOR_ICE],
    tendrilColor:  [...COLOR_WARM_CORE],
    coreColor:     [...COLOR_WARM_CORE],
  },
};


// ---------- Textures ----------

function createParticleSprite() {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d');

  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0.00, 'rgba(255,255,255,1.0)');
  g.addColorStop(0.18, 'rgba(255,255,255,0.75)');
  g.addColorStop(0.45, 'rgba(255,255,255,0.18)');
  g.addColorStop(0.80, 'rgba(255,255,255,0.02)');
  g.addColorStop(1.00, 'rgba(255,255,255,0.0)');

  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);

  const tex = new THREE.CanvasTexture(canvas);
  tex.needsUpdate = true;
  return tex;
}


// ---------- Utility ----------

function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

function lerp(a, b, t) { return a + (b - a) * t; }

function sampleDirection() {
  const theta = Math.random() * Math.PI * 2;
  const u = Math.random() * 2 - 1;
  const s = Math.sqrt(1 - u * u);
  return [Math.cos(theta) * s, u, Math.sin(theta) * s];
}


// ---------- GLSL shaders ----------
// Simplex noise by Ian McEwan (ashima arts) - MIT license. Classic reference
// implementation, required for 3D curl noise flow field.

const NOISE_GLSL = /* glsl */ `
vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x) { return mod289(((x * 34.0) + 1.0) * x); }
vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(vec3 v) {
  const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i  = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;
  i = mod289(i);
  vec4 p = permute(permute(permute(
             i.z + vec4(0.0, i1.z, i2.z, 1.0))
           + i.y + vec4(0.0, i1.y, i2.y, 1.0))
           + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.6 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}
`;

const PARTICLE_VERTEX_SHADER = /* glsl */ `
attribute vec3 seed;         // stable per-particle seed direction (unit sphere)
attribute float radiusBase;  // [0..1] baseline radius along the ray
attribute float phase;       // per-particle phase offset

uniform float uTime;
uniform float uCloudRadius;
uniform float uCloudInner;
uniform float uFlowAmp;
uniform float uFlowSpeed;
uniform float uShapeMode;    // 0 scattered, 1 starburst, 2 chaotic, 3 latlon
uniform float uPulse;        // global pulse scale
uniform float uSize;
uniform float uPixelRatio;

${NOISE_GLSL}

// Curl of a simplex-noise vector field -> incompressible flow
vec3 curlNoise(vec3 p) {
  const float e = 0.1;
  vec3 dx = vec3(e, 0.0, 0.0);
  vec3 dy = vec3(0.0, e, 0.0);
  vec3 dz = vec3(0.0, 0.0, e);

  float p_x0 = snoise(p - dx);
  float p_x1 = snoise(p + dx);
  float p_y0 = snoise(p - dy);
  float p_y1 = snoise(p + dy);
  float p_z0 = snoise(p - dz);
  float p_z1 = snoise(p + dz);

  float x = p_y1 - p_y0 - p_z1 + p_z0;
  float y = p_z1 - p_z0 - p_x1 + p_x0;
  float z = p_x1 - p_x0 - p_y1 + p_y0;

  return normalize(vec3(x, y, z) / (2.0 * e));
}

void main() {
  // ---- shape basis --------------------------------------------------------
  // mix between a dense volumetric distribution and a hollow-sphere layout
  float weight = mix(pow(radiusBase, 0.3333), pow(radiusBase, 0.35), uCloudInner);

  // starburst (mode 1) compresses radial; latlon sphere (mode 3) snaps to
  // quantized lat/lon bands; chaotic (mode 2) pushes particles out.
  float starburst = smoothstep(0.0, 1.0, max(0.0, 1.0 - abs(uShapeMode - 1.0)));
  float latlon    = smoothstep(0.0, 1.0, max(0.0, 1.0 - abs(uShapeMode - 3.0)));
  float chaotic   = smoothstep(0.0, 1.0, max(0.0, 1.0 - abs(uShapeMode - 2.0)));

  // Direction: in latlon mode, quantize to a 16x24 grid so particles fall
  // on visible great-circle bands (matches frame_0250 globe grid).
  vec3 dir = seed;
  if (latlon > 0.01) {
    float lat = asin(clamp(dir.y, -1.0, 1.0));
    float lon = atan(dir.z, dir.x);
    float latBands = 14.0;
    float lonBands = 22.0;
    float snapLat = floor(lat * latBands / 3.14159265 + 0.5) * 3.14159265 / latBands;
    float snapLon = floor(lon * lonBands / 3.14159265 + 0.5) * 3.14159265 / lonBands;
    vec3 snapped = vec3(cos(snapLat) * cos(snapLon), sin(snapLat), cos(snapLat) * sin(snapLon));
    dir = normalize(mix(dir, snapped, latlon));
  }

  float radius = uCloudRadius * weight * uPulse;
  radius *= mix(1.0, 0.82, starburst);
  radius *= mix(1.0, 1.12, chaotic);

  vec3 basePos = dir * radius;

  // ---- curl-noise flow field ---------------------------------------------
  vec3 sampleP = basePos * 0.35 + vec3(uTime * uFlowSpeed, uTime * uFlowSpeed * 0.7, -uTime * uFlowSpeed * 1.1);
  vec3 flow = curlNoise(sampleP) * uFlowAmp;

  // Add a swirl that rotates the whole field slightly so motion feels orbital
  float swirl = sin(phase + uTime * 0.4) * 0.15;
  mat2 rot = mat2(cos(swirl), -sin(swirl), sin(swirl), cos(swirl));
  vec2 xz = rot * basePos.xz;
  basePos = vec3(xz.x, basePos.y, xz.y);

  vec3 finalPos = basePos + flow;

  // Pass to FS
  gl_Position = projectionMatrix * modelViewMatrix * vec4(finalPos, 1.0);

  // Size attenuation (matches PointsMaterial sizeAttenuation)
  float dist = -(modelViewMatrix * vec4(finalPos, 1.0)).z;
  gl_PointSize = uSize * uPixelRatio * (300.0 / max(dist, 0.001));
}
`;

const PARTICLE_FRAGMENT_SHADER = /* glsl */ `
uniform sampler2D uMap;
uniform vec3 uColor;
uniform vec3 uCoreColor;
uniform float uOpacity;

void main() {
  vec4 tex = texture2D(uMap, gl_PointCoord);
  if (tex.a < 0.01) discard;

  // Particles keep their state color - subtle warm kick only at the very
  // center of each sprite so the cloud reads as the cool/violet/ice color
  // it was assigned while still blending to the warm core where they overlap.
  float hot = smoothstep(0.22, 0.0, length(gl_PointCoord - 0.5));
  vec3 col = mix(uColor, uCoreColor * 1.5, hot * 0.15);

  gl_FragColor = vec4(col * 1.15, tex.a * uOpacity);
}
`;


// ============================================================
//  BrainVisualizer
// ============================================================

export class BrainVisualizer {
  constructor(container) {
    this._container = container;
    this._disposed = false;

    this._state = 'idle';
    this._targetState = 'idle';
    this._transitionStart = 0;
    this._transitionProgress = 1.0;

    this._params = this._cloneParams(STATE_CONFIG.idle);
    this._prevParams = this._cloneParams(STATE_CONFIG.idle);

    this._clock = performance.now();
    this._elapsed = 0;

    // Reusable scratch vectors to keep per-frame allocations low
    this._tmpV = new THREE.Vector3();

    this._initScene();
    this._initCore();
    this._initParticles();
    this._initTendrils();
    this._initPost();
    this._bindResize();

    this._loop = this._loop.bind(this);
    this._frameId = requestAnimationFrame(this._loop);
  }


  // ---------- Construction ----------

  _cloneParams(p) {
    return {
      ...p,
      particleColor: [...p.particleColor],
      tendrilColor:  [...p.tendrilColor],
      coreColor:     [...p.coreColor],
    };
  }

  _initScene() {
    const { clientWidth: w, clientHeight: h } = this._container;

    this._scene = new THREE.Scene();

    this._camera = new THREE.PerspectiveCamera(45, (w / h) || 1, 0.1, 200);
    this._camera.position.set(0, 0, 7);

    this._renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: 'high-performance',
    });
    this._pixelRatio = Math.min(window.devicePixelRatio, 2);
    this._renderer.setPixelRatio(this._pixelRatio);
    this._renderer.setSize(w, h);
    this._renderer.setClearColor(0x000000, 0);
    this._renderer.outputColorSpace = THREE.SRGBColorSpace;
    this._container.appendChild(this._renderer.domElement);

    this._root = new THREE.Group();
    this._scene.add(this._root);
  }


  _initCore() {
    // Hot warm core - emissive yellow/orange sphere
    const geom = new THREE.IcosahedronGeometry(CORE_RADIUS, 4);
    const mat = new THREE.MeshBasicMaterial({
      color: new THREE.Color().fromArray(STATE_CONFIG.idle.coreColor),
      transparent: true,
      opacity: 1.0,
      toneMapped: false,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._core = new THREE.Mesh(geom, mat);
    this._root.add(this._core);

    // Soft billboarded halo around the core
    const haloTex = createParticleSprite();
    const haloMat = new THREE.SpriteMaterial({
      map: haloTex,
      color: new THREE.Color().fromArray(STATE_CONFIG.idle.coreColor),
      transparent: true,
      opacity: 0.9,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      toneMapped: false,
    });
    this._halo = new THREE.Sprite(haloMat);
    this._halo.scale.set(1.6, 1.6, 1.6);
    this._root.add(this._halo);

    // Warm point light at the core
    this._warmLight = new THREE.PointLight(
      new THREE.Color().fromArray(STATE_CONFIG.idle.coreColor),
      2.0, 14, 1.8,
    );
    this._warmLight.position.set(0, 0, 0);
    this._root.add(this._warmLight);

    // Cool blue rim light offset slightly so the overall scene gets that
    // warm-core + cool-rim contrast the reference has.
    this._coolLight = new THREE.PointLight(
      new THREE.Color().fromArray(COLOR_COOL_RIM),
      1.1, 12, 2.0,
    );
    this._coolLight.position.set(-1.6, 0.8, 1.2);
    this._root.add(this._coolLight);
  }


  _initParticles() {
    const geom = new THREE.BufferGeometry();

    // Static positions (not used for vertex output - shader computes position
    // from seed+radiusBase+time) but Three still wants the attribute.
    const positions  = new Float32Array(PARTICLE_COUNT * 3);
    const seeds      = new Float32Array(PARTICLE_COUNT * 3);
    const radiusBase = new Float32Array(PARTICLE_COUNT);
    const phases     = new Float32Array(PARTICLE_COUNT);

    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const [dx, dy, dz] = sampleDirection();
      const r = Math.random();

      positions[i * 3 + 0] = dx;
      positions[i * 3 + 1] = dy;
      positions[i * 3 + 2] = dz;

      seeds[i * 3 + 0] = dx;
      seeds[i * 3 + 1] = dy;
      seeds[i * 3 + 2] = dz;

      radiusBase[i] = r;
      phases[i] = Math.random() * Math.PI * 2;
    }

    geom.setAttribute('position',   new THREE.BufferAttribute(positions, 3));
    geom.setAttribute('seed',       new THREE.BufferAttribute(seeds, 3));
    geom.setAttribute('radiusBase', new THREE.BufferAttribute(radiusBase, 1));
    geom.setAttribute('phase',      new THREE.BufferAttribute(phases, 1));

    const mat = new THREE.ShaderMaterial({
      uniforms: {
        uTime:        { value: 0 },
        uCloudRadius: { value: STATE_CONFIG.idle.cloudRadius },
        uCloudInner:  { value: STATE_CONFIG.idle.cloudInner },
        uFlowAmp:     { value: STATE_CONFIG.idle.flowAmp },
        uFlowSpeed:   { value: STATE_CONFIG.idle.flowSpeed },
        uShapeMode:   { value: STATE_CONFIG.idle.shapeMode },
        uPulse:       { value: 1.0 },
        uSize:        { value: STATE_CONFIG.idle.particleSize },
        uPixelRatio:  { value: this._pixelRatio },
        uMap:         { value: createParticleSprite() },
        uColor:       { value: new THREE.Color().fromArray(STATE_CONFIG.idle.particleColor) },
        uCoreColor:   { value: new THREE.Color().fromArray(STATE_CONFIG.idle.coreColor) },
        uOpacity:     { value: STATE_CONFIG.idle.particleOpacity },
      },
      vertexShader:   PARTICLE_VERTEX_SHADER,
      fragmentShader: PARTICLE_FRAGMENT_SHADER,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });

    // So bounding-sphere tests don't cull the particles - positions live in
    // the shader, not the buffer.
    geom.boundingSphere = new THREE.Sphere(new THREE.Vector3(0, 0, 0), 12);
    geom.boundingBox    = new THREE.Box3(new THREE.Vector3(-12, -12, -12), new THREE.Vector3(12, 12, 12));

    this._particleMat = mat;
    this._particles = new THREE.Points(geom, mat);
    this._particles.frustumCulled = false;
    this._root.add(this._particles);
  }


  _initTendrils() {
    // Each tendril: a CatmullRomCurve3 with TENDRIL_CTRL_PTS control points
    // that wiggle over time. Rendered as a Line (strip), sampled at
    // TENDRIL_SEGMENTS points along the curve per frame.
    this._tendrils = [];

    const positionsCount = TENDRIL_SEGMENTS;
    const group = new THREE.Group();

    for (let i = 0; i < TENDRIL_COUNT; i++) {
      // Base outward direction for this tendril
      const [dx, dy, dz] = sampleDirection();
      const baseDir = new THREE.Vector3(dx, dy, dz);

      // Choose two tangent axes to the base direction so the wiggle is
      // always perpendicular to the tendril's overall direction.
      let up = Math.abs(baseDir.y) > 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
      const tangentA = new THREE.Vector3().crossVectors(baseDir, up).normalize();
      const tangentB = new THREE.Vector3().crossVectors(baseDir, tangentA).normalize();

      // Reusable control-point vectors
      const ctrlPoints = [];
      for (let k = 0; k < TENDRIL_CTRL_PTS; k++) {
        ctrlPoints.push(new THREE.Vector3());
      }

      // Per-control-point phase offsets for independent wiggle. Keep freq
      // low and correlated along the tendril so the curve reads as a smooth
      // arc, not a jagged scribble.
      const phaseA = new Float32Array(TENDRIL_CTRL_PTS);
      const phaseB = new Float32Array(TENDRIL_CTRL_PTS);
      const freq   = new Float32Array(TENDRIL_CTRL_PTS);
      const baseFreq = 0.6 + Math.random() * 0.5;
      for (let k = 0; k < TENDRIL_CTRL_PTS; k++) {
        phaseA[k] = Math.random() * Math.PI * 2;
        phaseB[k] = Math.random() * Math.PI * 2;
        // Slight increase toward the tip so the tip whips a bit faster
        freq[k] = baseFreq * (0.85 + 0.3 * (k / (TENDRIL_CTRL_PTS - 1)));
      }

      const curve = new THREE.CatmullRomCurve3(ctrlPoints, false, 'catmullrom', 0.5);

      const positions = new Float32Array(positionsCount * 3);
      const colors    = new Float32Array(positionsCount * 3);
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geom.setAttribute('color',    new THREE.BufferAttribute(colors, 3));

      const mat = new THREE.LineBasicMaterial({
        vertexColors: true,
        transparent: true,
        opacity: STATE_CONFIG.idle.tendrilOpacity,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      });

      const line = new THREE.Line(geom, mat);
      line.frustumCulled = false;
      group.add(line);

      this._tendrils.push({
        line,
        curve,
        ctrlPoints,
        baseDir,
        tangentA,
        tangentB,
        phaseA,
        phaseB,
        freq,
        mat,
        lengthJitter: 0.75 + Math.random() * 0.55,  // per-tendril length variation
        rotSpeed:     (Math.random() - 0.5) * 0.6,  // per-tendril slow rotation
        rotAngle:     Math.random() * Math.PI * 2,
        // Per-tendril [0..1] activation threshold. Only tendrils whose
        // threshold <= state's tendrilCoverage are drawn. Lets us morph
        // between sparse/dense tendril counts without mutating geometry.
        activation:   Math.random(),
      });
    }

    this._tendrilGroup = group;
    this._root.add(group);
  }


  _initPost() {
    const { clientWidth: w, clientHeight: h } = this._container;

    this._composer = new EffectComposer(this._renderer);
    this._composer.setPixelRatio(this._pixelRatio);
    this._composer.setSize(w, h);

    const renderPass = new RenderPass(this._scene, this._camera);
    renderPass.clearAlpha = 0;
    this._composer.addPass(renderPass);

    this._bloomPass = new UnrealBloomPass(
      new THREE.Vector2(w, h),
      STATE_CONFIG.idle.bloomStrength,
      STATE_CONFIG.idle.bloomRadius,
      STATE_CONFIG.idle.bloomThreshold,
    );
    this._composer.addPass(this._bloomPass);
  }


  _bindResize() {
    this._resizeHandler = () => this.resize();
    window.addEventListener('resize', this._resizeHandler);
  }


  // ---------- Public API ----------

  setState(state) {
    const key = (state || '').toLowerCase();
    console.log('[brain] setState called:', key);
    if (!(key in STATE_CONFIG)) {
      console.warn('[brain] unknown state:', key);
      return;
    }
    if (key === this._targetState) {
      console.log('[brain] state already', key, 'skipping');
      return;
    }

    this._prevParams = this._cloneParams(this._params);
    this._targetState = key;
    this._transitionStart = performance.now();
    this._transitionProgress = 0;
    console.log('[brain] transitioning to', key);
  }

  animate() {
    if (this._disposed) return;
    this._loop(performance.now());
  }

  resize() {
    if (this._disposed) return;
    const w = this._container.clientWidth;
    const h = this._container.clientHeight;
    if (w === 0 || h === 0) return;

    this._camera.aspect = w / h;
    this._camera.updateProjectionMatrix();

    this._renderer.setSize(w, h);
    this._composer.setSize(w, h);
    this._bloomPass.setSize(w, h);
  }

  dispose() {
    if (this._disposed) return;
    this._disposed = true;

    cancelAnimationFrame(this._frameId);
    window.removeEventListener('resize', this._resizeHandler);

    this._core.geometry.dispose();
    this._core.material.dispose();
    this._halo.material.map.dispose();
    this._halo.material.dispose();

    this._particles.geometry.dispose();
    this._particleMat.uniforms.uMap.value.dispose();
    this._particleMat.dispose();

    for (const t of this._tendrils) {
      t.line.geometry.dispose();
      t.mat.dispose();
    }

    this._bloomPass.dispose?.();
    this._composer.dispose?.();
    this._renderer.dispose();

    if (this._renderer.domElement.parentNode) {
      this._renderer.domElement.parentNode.removeChild(this._renderer.domElement);
    }
  }


  // ---------- Animation loop ----------

  _loop(now) {
    if (this._disposed) return;
    this._frameId = requestAnimationFrame(this._loop);

    const dt = Math.min(now - this._clock, 80);
    this._clock = now;
    this._elapsed += dt * 0.001;

    this._updateTransition(now);
    this._updateCore();
    this._updateParticles();
    this._updateTendrils(dt);
    this._updatePost();

    // Continuous global motion
    this._root.rotation.y += this._params.rotationSpeed * dt * 0.001;
    this._root.rotation.x = Math.sin(this._elapsed * 0.28) * this._params.tiltAmp;

    this._composer.render();
  }


  _updateTransition(now) {
    if (this._transitionProgress >= 1.0) return;

    const raw = (now - this._transitionStart) / TRANSITION_MS;
    this._transitionProgress = Math.min(raw, 1.0);
    const t = easeInOutCubic(this._transitionProgress);

    const target = STATE_CONFIG[this._targetState];
    const prev = this._prevParams;

    for (const key of Object.keys(target)) {
      if (key === 'particleColor' || key === 'tendrilColor' || key === 'coreColor') {
        this._params[key][0] = lerp(prev[key][0], target[key][0], t);
        this._params[key][1] = lerp(prev[key][1], target[key][1], t);
        this._params[key][2] = lerp(prev[key][2], target[key][2], t);
      } else {
        this._params[key] = lerp(prev[key], target[key], t);
      }
    }

    if (this._transitionProgress >= 1.0) {
      this._state = this._targetState;
    }
  }


  _updateCore() {
    const p = this._params;
    const pulse = 1.0 + Math.sin(this._elapsed * p.pulseHz) * p.pulseAmp;
    const scale = p.coreScale * pulse;

    this._core.scale.setScalar(scale);
    // Emissive hot center - push RGB above 1.0 so bloom blows it out.
    this._core.material.color.setRGB(
      Math.min(1.8, p.coreColor[0] * p.coreIntensity),
      Math.min(1.8, p.coreColor[1] * p.coreIntensity),
      Math.min(1.8, p.coreColor[2] * p.coreIntensity),
    );

    const haloScale = 1.35 + Math.sin(this._elapsed * p.pulseHz * 0.8 + 0.6) * p.pulseAmp * 1.4;
    this._halo.scale.set(haloScale, haloScale, haloScale);
    this._halo.material.color.setRGB(p.coreColor[0], p.coreColor[1], p.coreColor[2]);
    this._halo.material.opacity = 0.78;

    this._warmLight.color.setRGB(p.coreColor[0], p.coreColor[1], p.coreColor[2]);
    this._warmLight.intensity = p.coreIntensity;

    // Cool rim light orbits slowly for an organic feel
    const angle = this._elapsed * 0.2;
    this._coolLight.position.set(
      Math.cos(angle) * 1.8,
      Math.sin(angle * 0.6) * 0.9,
      Math.sin(angle) * 1.4,
    );
    this._coolLight.intensity = p.coolLightInt;
  }


  _updateParticles() {
    const p = this._params;
    const u = this._particleMat.uniforms;

    // Breathing modulates radius slightly on top of shader-side motion
    const breath = 1.0 + Math.sin(this._elapsed * p.pulseHz * 0.5) * p.pulseAmp * 0.3;

    u.uTime.value        = this._elapsed;
    u.uCloudRadius.value = p.cloudRadius;
    u.uCloudInner.value  = p.cloudInner;
    u.uFlowAmp.value     = p.flowAmp;
    u.uFlowSpeed.value   = p.flowSpeed;
    u.uShapeMode.value   = p.shapeMode;
    u.uPulse.value       = breath;
    u.uSize.value        = p.particleSize;
    u.uOpacity.value     = p.particleOpacity;
    u.uColor.value.setRGB(p.particleColor[0], p.particleColor[1], p.particleColor[2]);
    u.uCoreColor.value.setRGB(p.coreColor[0], p.coreColor[1], p.coreColor[2]);
  }


  _updateTendrils(dt) {
    const p = this._params;
    const t = this._elapsed;

    const amp  = p.tendrilAmp;
    const len  = p.tendrilLength;
    const tw   = p.tendrilTwist;
    const wspd = p.tendrilSpeed;
    const cov  = p.tendrilCoverage;

    for (let i = 0; i < this._tendrils.length; i++) {
      const T = this._tendrils[i];

      // Per-tendril visibility: show only tendrils whose activation
      // threshold is under the state's coverage, with a 0.1-wide
      // feathered edge so transitions look smooth.
      const visible = Math.max(0, Math.min(1, (cov - T.activation) * 10 + 0.5));
      if (visible <= 0.001) {
        T.mat.opacity = 0;
        continue;
      }

      // Slowly rotate the tendril's base direction so tendrils sweep around
      T.rotAngle += T.rotSpeed * dt * 0.001 * (0.3 + wspd * 0.3);
      // Build an effective base direction by rotating about a fixed axis
      this._rotateVector(T.baseDir, T.rotAngle * 0.0, this._tmpV);

      // Endpoint length jittered by per-tendril and by time
      const flick = 0.75 + 0.25 * Math.sin(t * wspd * 0.9 + T.phaseA[0]);
      const endR  = (len * T.lengthJitter) * flick;

      // Inner end just outside the core
      const inner = CORE_RADIUS * 1.05;

      // Low-frequency arc parameters so the whole tendril sweeps as one
      // smooth curve rather than scribbling at each control point. The
      // extra `twist` term only kicks in for chaotic states.
      const arcPhaseA = T.phaseA[0] + t * wspd;
      const arcPhaseB = T.phaseB[0] + t * wspd * 0.7;

      for (let k = 0; k < TENDRIL_CTRL_PTS; k++) {
        const u = k / (TENDRIL_CTRL_PTS - 1);               // [0..1] along tendril
        const radialT = inner + (endR - inner) * u;

        // Sideways offset grows with u so inner end stays near core,
        // outer end whips freely (jellyfish tail).
        const whip = Math.pow(u, 1.3);
        // One-hump-along-the-tendril sinusoidal bend that slides in time -
        // reads as an organic curving arc, not high-frequency noise.
        const bend = Math.sin(u * 2.4 + arcPhaseA) * amp * whip;
        const bend2 = Math.cos(u * 1.8 + arcPhaseB) * amp * whip * 0.7;

        // Only chaotic states add per-point twist; keep it modest.
        const tOff = tw * whip;
        const twistA = bend  + Math.sin(t * 1.7 + T.phaseA[k]) * tOff;
        const twistB = bend2 + Math.cos(t * 1.3 + T.phaseB[k]) * tOff;

        T.ctrlPoints[k].set(
          T.baseDir.x * radialT + T.tangentA.x * twistA + T.tangentB.x * twistB,
          T.baseDir.y * radialT + T.tangentA.y * twistA + T.tangentB.y * twistB,
          T.baseDir.z * radialT + T.tangentA.z * twistA + T.tangentB.z * twistB,
        );
      }

      // Sample the catmull-rom curve into the geometry buffer
      const pos = T.line.geometry.attributes.position.array;
      const col = T.line.geometry.attributes.color.array;

      // Re-init the curve (its internal points array is the same reference,
      // but we need to recompute its arc-length cache if we used getPointAt;
      // getPoint uses the raw parametric curve so we're fine without it).
      for (let s = 0; s < TENDRIL_SEGMENTS; s++) {
        const param = s / (TENDRIL_SEGMENTS - 1);
        T.curve.getPoint(param, this._tmpV);
        pos[s * 3 + 0] = this._tmpV.x;
        pos[s * 3 + 1] = this._tmpV.y;
        pos[s * 3 + 2] = this._tmpV.z;

        // Gradient: hot warm-white at the base fading to the state color at the tip
        const fade = 1.0 - param;
        const hot  = Math.pow(fade, 1.5);
        col[s * 3 + 0] = p.tendrilColor[0] * (0.8 + hot * 0.9) + hot * 0.3;
        col[s * 3 + 1] = p.tendrilColor[1] * (0.8 + hot * 0.9) + hot * 0.25;
        col[s * 3 + 2] = p.tendrilColor[2] * (0.8 + hot * 0.9) + hot * 0.10;
      }

      T.line.geometry.attributes.position.needsUpdate = true;
      T.line.geometry.attributes.color.needsUpdate = true;
      T.mat.opacity = p.tendrilOpacity * visible;
    }
  }


  // Rotate a vector about the world Y axis by `angle` (rad) into `out`.
  _rotateVector(v, angle, out) {
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    out.set(v.x * c - v.z * s, v.y, v.x * s + v.z * c);
    return out;
  }


  _updatePost() {
    const p = this._params;
    this._bloomPass.strength = p.bloomStrength;
    this._bloomPass.radius = p.bloomRadius;
    this._bloomPass.threshold = p.bloomThreshold;
  }
}
