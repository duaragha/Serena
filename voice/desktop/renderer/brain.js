/**
 * Serena's lightweight dot-field voice anchor.
 *
 * The public API intentionally matches the retired Three.js renderer:
 *   new BrainVisualizer(container)
 *   .setState('idle' | 'listening' | 'thinking' | 'working' | 'speaking')
 *   .setAmplitude(0..1)
 *   .animate()
 *   .resize()
 *   .dispose()
 *
 * Speaking accepts real amplitude through setAmplitude(). Until an audio
 * meter is connected, a restrained synthetic speech envelope keeps the state
 * visibly alive without changing the existing state-file websocket bridge.
 */

const TAU = Math.PI * 2;
const TRANSITION_MS = 520;
const RING_COUNT = 13;
const VALID_STATES = new Set([
  'idle',
  'listening',
  'thinking',
  'working',
  'speaking',
  'offline',
]);

const PALETTES = {
  idle: {
    base: [143, 112, 255],
    accent: [222, 102, 205],
    hot: [255, 190, 105],
  },
  listening: {
    base: [174, 112, 255],
    accent: [246, 105, 189],
    hot: [255, 194, 112],
  },
  thinking: {
    base: [126, 91, 240],
    accent: [217, 88, 201],
    hot: [255, 177, 79],
  },
  working: {
    base: [126, 92, 246],
    accent: [221, 105, 211],
    hot: [255, 194, 92],
  },
  speaking: {
    base: [201, 100, 240],
    accent: [255, 105, 171],
    hot: [255, 192, 96],
  },
  offline: {
    base: [92, 82, 119],
    accent: [119, 85, 126],
    hot: [141, 111, 91],
  },
};

function clamp(value, minimum = 0, maximum = 1) {
  return Math.max(minimum, Math.min(maximum, value));
}

function lerp(from, to, amount) {
  return from + (to - from) * amount;
}

function smoothstep(value) {
  const x = clamp(value);
  return x * x * (3 - 2 * x);
}

function mixColor(from, to, amount) {
  return [
    Math.round(lerp(from[0], to[0], amount)),
    Math.round(lerp(from[1], to[1], amount)),
    Math.round(lerp(from[2], to[2], amount)),
  ];
}

function colorFor(palette, tone) {
  if (tone < 0.66) {
    return mixColor(palette.base, palette.accent, tone / 0.66);
  }
  return mixColor(palette.accent, palette.hot, (tone - 0.66) / 0.34);
}

function rgba(color, alpha) {
  return `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${alpha})`;
}

function makeRandom(seed = 0x51e2a9b7) {
  let value = seed >>> 0;
  return () => {
    value ^= value << 13;
    value ^= value >>> 17;
    value ^= value << 5;
    return (value >>> 0) / 4294967296;
  };
}

function buildDots() {
  const random = makeRandom();
  const dots = [];
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));

  dots.push({ radius: 0, angle: 0, phase: 0, size: 1.7, tone: 0.95 });

  for (let ring = 1; ring <= RING_COUNT; ring += 1) {
    const count = 5 + Math.round(ring * 2.6);
    const radius = ring / RING_COUNT;
    const ringOffset = ring * goldenAngle;

    for (let index = 0; index < count; index += 1) {
      dots.push({
        radius: clamp(radius + (random() - 0.5) * 0.032, 0, 1),
        angle: ringOffset + (index / count) * TAU + (random() - 0.5) * 0.055,
        phase: random() * TAU,
        size: 0.62 + random() * 1.18,
        tone: random(),
      });
    }
  }

  return dots;
}

function syntheticSpeechEnvelope(time) {
  const syllable = Math.pow(Math.max(0, Math.sin(time * 10.7)), 1.8);
  const cadence = 0.5 + 0.5 * Math.sin(time * 3.1 + 0.7);
  const texture = 0.5 + 0.5 * Math.sin(time * 17.3 + 1.9);
  return clamp(0.14 + syllable * (0.38 + cadence * 0.26) + texture * 0.08);
}

export class BrainVisualizer {
  constructor(container) {
    if (!container) {
      throw new Error('BrainVisualizer requires a container');
    }

    this._container = container;
    this._canvas = document.createElement('canvas');
    this._canvas.className = 'serena-dot-field';
    this._canvas.setAttribute('aria-hidden', 'true');
    this._canvas.dataset.renderer = 'dot-field';
    this._canvas.dataset.state = 'idle';
    this._container.appendChild(this._canvas);

    this._context = this._canvas.getContext('2d', { alpha: true });
    if (!this._context) {
      throw new Error('2D canvas is unavailable');
    }

    this._dots = buildDots();
    this._state = 'idle';
    this._fromState = 'idle';
    this._targetState = 'idle';
    this._transitionStart = performance.now();
    this._timeOrigin = this._transitionStart;
    this._lastFrame = this._transitionStart;
    this._amplitude = 0;
    this._amplitudeTarget = 0;
    this._manualAmplitudeAt = 0;
    this._disposed = false;
    this._frameId = null;
    this._width = 1;
    this._height = 1;
    this._pixelRatio = 1;

    this._loop = this._loop.bind(this);
    this._resizeHandler = () => this.resize();
    window.addEventListener('resize', this._resizeHandler);

    this._motionQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    this._reducedMotion = Boolean(this._motionQuery?.matches);
    this._motionHandler = (event) => {
      this._reducedMotion = event.matches;
      this._scheduleFrame();
    };
    this._motionQuery?.addEventListener?.('change', this._motionHandler);

    if (window.ResizeObserver) {
      this._resizeObserver = new ResizeObserver(() => this.resize());
      this._resizeObserver.observe(this._container);
    }

    this.resize();
    this._scheduleFrame();
  }

  setState(state) {
    const next = String(state || '').toLowerCase();
    if (!VALID_STATES.has(next) || next === this._targetState) {
      return;
    }

    this._fromState = this._targetState;
    this._targetState = next;
    this._transitionStart = performance.now();
    this._canvas.dataset.state = next;
    this._container.dataset.voiceState = next;
    this._scheduleFrame();
  }

  setAmplitude(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      return;
    }
    this._amplitudeTarget = clamp(numeric);
    this._manualAmplitudeAt = performance.now();
    this._scheduleFrame();
  }

  animate() {
    this._scheduleFrame();
  }

  resize() {
    if (this._disposed) {
      return;
    }

    const bounds = this._container.getBoundingClientRect();
    const width = Math.max(1, Math.round(bounds.width || window.innerWidth || 1));
    const height = Math.max(1, Math.round(bounds.height || window.innerHeight || 1));
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);

    if (
      width === this._width
      && height === this._height
      && pixelRatio === this._pixelRatio
    ) {
      return;
    }

    this._width = width;
    this._height = height;
    this._pixelRatio = pixelRatio;
    this._canvas.width = Math.round(width * pixelRatio);
    this._canvas.height = Math.round(height * pixelRatio);
    this._canvas.style.width = `${width}px`;
    this._canvas.style.height = `${height}px`;
    this._context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    this._scheduleFrame();
  }

  dispose() {
    if (this._disposed) {
      return;
    }
    this._disposed = true;
    if (this._frameId !== null) {
      cancelAnimationFrame(this._frameId);
      this._frameId = null;
    }
    window.removeEventListener('resize', this._resizeHandler);
    this._motionQuery?.removeEventListener?.('change', this._motionHandler);
    this._resizeObserver?.disconnect();
    this._canvas.remove();
  }

  _scheduleFrame() {
    if (!this._disposed && this._frameId === null) {
      this._frameId = requestAnimationFrame(this._loop);
    }
  }

  _loop(now) {
    this._frameId = null;
    if (this._disposed) {
      return;
    }

    const delta = Math.min(64, Math.max(0, now - this._lastFrame));
    this._lastFrame = now;
    this._render(now, delta);

    if (!this._reducedMotion) {
      this._scheduleFrame();
    }
  }

  _sample(state, dot, time, energy) {
    const phase = dot.phase;
    const radius = dot.radius;
    let radialScale = 1;
    let radialOffset = 0;
    let angleOffset = 0;
    let intensity = 1;
    let sizeScale = 1;

    switch (state) {
      case 'listening': {
        const wave = 0.5 + 0.5 * Math.sin(radius * 21 - time * 6.2 + phase * 0.18);
        radialScale = 0.96 + wave * 0.075;
        radialOffset = Math.sin(time * 2.1 + phase) * 0.007;
        angleOffset = Math.sin(time * 0.9 + phase) * 0.012;
        intensity = 0.76 + wave * 0.44;
        sizeScale = 0.92 + wave * 0.32;
        break;
      }
      case 'thinking': {
        const innerPull = 1 - radius;
        radialScale = 0.91 + 0.08 * Math.sin(dot.angle * 3 - time * 2.3 + phase);
        radialOffset = 0.018 * Math.sin(time * 1.6 + radius * 15 + phase);
        angleOffset = time * (0.24 + innerPull * 0.74) + innerPull * 0.48;
        intensity = 0.72 + 0.34 * (0.5 + 0.5 * Math.sin(time * 2.7 + phase));
        sizeScale = 0.88 + innerPull * 0.34;
        break;
      }
      case 'working': {
        const lane = Math.floor(radius * RING_COUNT) % 2 === 0 ? 1 : -1;
        const progress = 0.5 + 0.5 * Math.sin(time * 2.1 + radius * 18 + phase);
        radialScale = 0.965 + progress * 0.055;
        radialOffset = 0.009 * Math.sin(time * 1.4 + phase);
        angleOffset = lane * time * (0.34 + (1 - radius) * 0.24);
        intensity = 0.72 + progress * 0.42;
        sizeScale = 0.9 + progress * 0.28;
        break;
      }
      case 'speaking': {
        radialScale = 0.95 + energy * 0.16;
        radialOffset = Math.sin(radius * 18 - time * 8.4 + phase * 0.25) * energy * 0.018;
        angleOffset = Math.sin(time * 1.5 + phase) * 0.025;
        intensity = 0.72 + energy * 0.58;
        sizeScale = 0.86 + energy * 0.7;
        break;
      }
      case 'offline':
        radialScale = 0.94;
        intensity = 0.34;
        sizeScale = 0.84;
        break;
      case 'idle':
      default:
        radialScale = 1 + 0.025 * Math.sin(time * 0.92 + phase);
        radialOffset = 0.006 * Math.sin(time * 0.37 + phase);
        angleOffset = 0.018 * Math.sin(time * 0.28 + phase);
        intensity = 0.66 + 0.2 * (0.5 + 0.5 * Math.sin(time * 0.8 + phase));
        sizeScale = 0.92 + 0.13 * Math.sin(time * 0.9 + phase);
        break;
    }

    return {
      radius: Math.max(0, radius * radialScale + radialOffset),
      angle: dot.angle + angleOffset,
      intensity,
      sizeScale,
    };
  }

  _render(now, delta) {
    const context = this._context;
    const elapsed = this._reducedMotion ? 0.8 : (now - this._timeOrigin) / 1000;
    const transition = this._reducedMotion
      ? 1
      : smoothstep((now - this._transitionStart) / TRANSITION_MS);
    if (transition >= 1) {
      this._state = this._targetState;
      this._fromState = this._targetState;
    }

    const hasFreshAmplitude = now - this._manualAmplitudeAt < 260;
    let amplitudeTarget = this._targetState === 'speaking'
      ? syntheticSpeechEnvelope(elapsed)
      : 0;
    if (hasFreshAmplitude) {
      amplitudeTarget = this._amplitudeTarget;
    }
    if (this._reducedMotion && !hasFreshAmplitude) {
      amplitudeTarget = this._targetState === 'speaking' ? 0.34 : 0;
    }
    const response = 1 - Math.exp(-delta / 75);
    this._amplitude = lerp(this._amplitude, amplitudeTarget, response);

    const width = this._width;
    const height = this._height;
    const centerX = width * 0.5;
    const centerY = height * 0.48;
    const fieldRadius = Math.min(width, height) * 0.355;
    const verticalScale = 0.92;
    const fromPalette = PALETTES[this._fromState];
    const targetPalette = PALETTES[this._targetState];
    const glowColor = mixColor(fromPalette.base, targetPalette.base, transition);

    context.clearRect(0, 0, width, height);

    const ambient = context.createRadialGradient(
      centerX,
      centerY,
      0,
      centerX,
      centerY,
      fieldRadius * 1.45,
    );
    const glowStrength = this._targetState === 'offline' ? 0.035 : 0.11;
    ambient.addColorStop(0, rgba(glowColor, glowStrength));
    ambient.addColorStop(0.46, rgba(glowColor, glowStrength * 0.36));
    ambient.addColorStop(1, rgba(glowColor, 0));
    context.fillStyle = ambient;
    context.fillRect(0, 0, width, height);

    this._drawStateWave(context, this._targetState, elapsed, centerX, centerY, fieldRadius);

    context.save();
    context.globalCompositeOperation = 'lighter';

    for (const dot of this._dots) {
      const from = this._sample(this._fromState, dot, elapsed, this._amplitude);
      const to = this._sample(this._targetState, dot, elapsed, this._amplitude);
      const radius = lerp(from.radius, to.radius, transition) * fieldRadius;
      const angle = lerp(from.angle, to.angle, transition);
      const intensity = lerp(from.intensity, to.intensity, transition);
      const sizeScale = lerp(from.sizeScale, to.sizeScale, transition);
      const x = centerX + Math.cos(angle) * radius;
      const y = centerY + Math.sin(angle) * radius * verticalScale;
      const edgeFade = 1 - Math.pow(dot.radius, 2) * 0.42;
      const alpha = clamp(intensity * edgeFade * 0.78, 0.08, 0.95);
      const size = Math.max(0.65, dot.size * sizeScale * (1.05 + (1 - dot.radius) * 0.25));

      const fromColor = colorFor(fromPalette, dot.tone);
      const toColor = colorFor(targetPalette, dot.tone);
      const color = mixColor(fromColor, toColor, transition);

      context.beginPath();
      context.arc(x, y, size, 0, TAU);
      context.fillStyle = rgba(color, alpha);
      context.fill();

      if (dot.tone > 0.9 && dot.radius < 0.82) {
        context.beginPath();
        context.arc(x, y, size * 2.7, 0, TAU);
        context.fillStyle = rgba(color, alpha * 0.1);
        context.fill();
      }
    }

    context.restore();
  }

  _drawStateWave(context, state, time, centerX, centerY, radius) {
    context.save();
    context.globalCompositeOperation = 'lighter';

    if (state === 'listening') {
      const progress = (time * 0.72) % 1;
      const opacity = Math.sin(progress * Math.PI) * 0.2;
      context.beginPath();
      context.ellipse(
        centerX,
        centerY,
        radius * (0.18 + progress * 0.98),
        radius * (0.16 + progress * 0.9),
        0,
        0,
        TAU,
      );
      context.strokeStyle = `rgba(244, 114, 182, ${opacity})`;
      context.lineWidth = 1.1;
      context.stroke();
    } else if (state === 'thinking') {
      for (let arm = 0; arm < 3; arm += 1) {
        const angle = time * 1.45 + (arm / 3) * TAU;
        context.beginPath();
        context.arc(centerX, centerY, radius * (0.3 + arm * 0.13), angle, angle + 1.25);
        context.strokeStyle = `rgba(255, 177, 79, ${0.12 - arm * 0.022})`;
        context.lineWidth = 1.2;
        context.stroke();
      }
    } else if (state === 'working') {
      for (let track = 0; track < 3; track += 1) {
        const direction = track % 2 === 0 ? 1 : -1;
        const angle = direction * time * (0.8 + track * 0.16) + track * 1.7;
        context.beginPath();
        context.arc(
          centerX,
          centerY,
          radius * (0.42 + track * 0.17),
          angle,
          angle + 0.72 + track * 0.16,
        );
        context.strokeStyle = `rgba(255, 194, 92, ${0.16 - track * 0.025})`;
        context.lineWidth = 1.15;
        context.stroke();
      }
    } else if (state === 'speaking') {
      const opacity = 0.06 + this._amplitude * 0.19;
      const pulseRadius = radius * (0.68 + this._amplitude * 0.36);
      context.beginPath();
      context.ellipse(centerX, centerY, pulseRadius, pulseRadius * 0.92, 0, 0, TAU);
      context.strokeStyle = `rgba(255, 116, 174, ${opacity})`;
      context.lineWidth = 1 + this._amplitude * 1.4;
      context.stroke();
    }

    context.restore();
  }
}
