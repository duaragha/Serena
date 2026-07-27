// Serena overlay renderer entry point.

const stateColors = {
  idle: '#a78bfa',
  listening: '#f472b6',
  thinking: '#f6ad55',
  working: '#c084fc',
  speaking: '#fb7185',
  offline: '#6b7280',
};

const stateEl = document.getElementById('state-dot');
const stateLabelEl = document.getElementById('state-label');
const transcriptionEl = document.getElementById('transcription');
const responseEl = document.getElementById('response');
const dashboardEl = document.getElementById('dashboard');
const overlayEl = document.getElementById('overlay');

let responseTimeout = null;
let responseFadeTimeout = null;
let typewriterTimeout = null;
const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');

// --- Mouse passthrough handling ---
// The overlay is click-through by default. When the mouse enters an
// interactive area, we disable passthrough so clicks register.

overlayEl.addEventListener('mouseenter', () => {
  window.serena.setIgnoreMouse(false);
});

overlayEl.addEventListener('mouseleave', () => {
  window.serena.setIgnoreMouse(true);
});

// --- State changes ---

window.serena.onStateChange((state) => {
  window._serenaVoiceState = state;
  const color = stateColors[state] || stateColors.offline;
  stateEl.style.backgroundColor = color;
  stateEl.style.boxShadow = `0 0 8px ${color}`;
  stateLabelEl.textContent = state;

  // Listening and working stay visibly alive in the compact header.
  if (state === 'listening' || state === 'working') {
    stateEl.classList.add('pulse');
  } else {
    stateEl.classList.remove('pulse');
  }

  // Update the dot-field visualization.
  if (window.serena.setState) {
    window.serena.setState(state);
  }

});

window.serena.onAmplitude((value) => {
  window.serena.setAmplitude(value);
});

// --- Transcription display ---

window.serena.onTranscription((text) => {
  transcriptionEl.textContent = text;
  transcriptionEl.classList.remove('hidden');

  // Clear previous response when new transcription starts
  responseEl.classList.add('hidden');
  responseEl.textContent = '';
});

// --- Response display ---

window.serena.onResponse((text) => {
  clearTimeout(responseTimeout);
  clearTimeout(responseFadeTimeout);
  clearTimeout(typewriterTimeout);
  responseEl.classList.remove('hidden');
  responseEl.classList.remove('fade-out');
  typewriterEffect(responseEl, text);

  // Fade out after a delay
  responseTimeout = setTimeout(() => {
    clearTimeout(typewriterTimeout);
    transcriptionEl.classList.add('hidden');
    responseEl.classList.add('fade-out');
    responseFadeTimeout = setTimeout(() => {
      responseEl.classList.add('hidden');
      responseEl.classList.remove('fade-out');
    }, 500);
  }, 8000);
});

function typewriterEffect(el, text) {
  clearTimeout(typewriterTimeout);
  el.textContent = '';
  if (reducedMotion?.matches) {
    el.textContent = text;
    return;
  }
  let i = 0;
  const speed = 20; // ms per character
  function type() {
    if (i < text.length) {
      el.textContent += text.charAt(i);
      i++;
      typewriterTimeout = setTimeout(type, speed);
    }
  }
  type();
}

// --- Dashboard ---
// Toggle the slide-in dashboard panel (created by dashboard.js module).
// The inline #dashboard card is hidden — the panel replaces it.

window.serena.onToggleDashboard((_visible) => {
  // Use the Dashboard class if available (loaded async via module)
  if (window._serenaDashboard) {
    const dash = window._serenaDashboard;
    // Sync visibility: if the tray sent a specific state, match it
    if (dash.isVisible !== _visible) {
      dash.toggle();
    }
  } else {
    // Fallback to inline card
    if (_visible) {
      dashboardEl.classList.remove('hidden');
    } else {
      dashboardEl.classList.add('hidden');
    }
  }
});

window.serena.onDashboardData((data) => {
  // Route to the Dashboard panel if available
  const dash = window._serenaDashboard;
  if (dash) {
    if (data.calendar) dash.updateCalendar(data.calendar);
    if (data.weather) dash.updateWeather(data.weather);
    if (data.notifications) dash.updateNotifications(data.notifications);
  }

  // Also update inline card as fallback
  if (data.calendar) {
    const calEl = document.getElementById('dash-calendar');
    if (data.calendar.length === 0) {
      calEl.innerHTML = '<span class="muted">No upcoming events</span>';
    } else {
      calEl.innerHTML = data.calendar
        .map((e) => `<div class="dash-item">${e.time || ''} ${e.title || e.summary || ''}</div>`)
        .join('');
    }
  }

  if (data.weather) {
    const wxEl = document.getElementById('dash-weather');
    const temp = data.weather.temp || data.weather.temperature || '--';
    wxEl.innerHTML = `<div class="dash-item">${temp}° — ${data.weather.condition || ''}</div>`;
  }

  if (data.notifications) {
    const notifEl = document.getElementById('dash-notifications');
    if (data.notifications.length === 0) {
      notifEl.innerHTML = '<span class="muted">None</span>';
    } else {
      notifEl.innerHTML = data.notifications
        .map((n) => `<div class="dash-item">${typeof n === 'string' ? n : n.text || ''}</div>`)
        .join('');
    }
  }
});

// --- Code panel ---

window.serena.onCodeStart((data) => {
  const fn = window.serena.codePanelOnStart;
  if (fn) fn(data);
});

window.serena.onCodeEvent((event) => {
  const fn = window.serena.codePanelOnEvent;
  if (fn) fn(event);
});

window.serena.onCodeDone((data) => {
  const fn = window.serena.codePanelOnDone;
  if (fn) fn(data);
});

window.serena.onToggleCodePanel(() => {
  const fn = window.serena.codePanelOnToggle;
  if (fn) fn();
});

// --- Focus mode ---

window.serena.onFocusMode((enabled) => {
  if (enabled) {
    overlayEl.classList.add('focus-mode');
  } else {
    overlayEl.classList.remove('focus-mode');
  }
});

// ── Type bar ────────────────────────────────────────────────────────────
// A typed line runs the same turn as a spoken one, so this is an input
// channel, not a separate mode. Kept usable while she is mid-answer: the
// bar dims and holds the text rather than dropping it.
(() => {
  const bar = document.getElementById('type-bar');
  const input = document.getElementById('type-input');
  if (!bar || !input || !window.serena || !window.serena.sendTyped) return;

  let busy = false;

  const setBusy = (value) => {
    busy = value;
    bar.classList.toggle('busy', value);
    input.placeholder = value ? 'thinking…' : 'type to serena';
  };

  input.addEventListener('keydown', (event) => {
    // Never let the overlay's global shortcuts swallow ordinary typing.
    event.stopPropagation();
    if (event.key !== 'Enter' || event.shiftKey) return;
    const text = input.value.trim();
    if (!text || busy) return;
    window.serena.sendTyped(text);
    input.value = '';
    setBusy(true);
  });

  // She has answered (or failed); take the bar back.
  window.serena.onResponse(() => setBusy(false));

  // Safety net: never strand the bar if a turn dies without a response.
  window.serena.onStateChange((state) => {
    if (state === 'idle') setBusy(false);
  });

  // Focus the bar on any keystroke that is plainly someone starting to type.
  document.addEventListener('keydown', (event) => {
    if (document.activeElement === input) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key.length !== 1) return;
    input.focus();
  });
})();

// --- How fast she talks ---

(function setUpSpeedSlider() {
  const range = document.getElementById('speed-range');
  const readout = document.getElementById('speed-value');
  if (!range || !readout || !window.serena || !window.serena.setVoiceSpeed) return;

  const show = (value) => {
    readout.textContent = `${Number(value).toFixed(2).replace(/0$/, '')}\u00d7`;
  };

  // Reflect the saved rate, so the slider shows what she is actually doing
  // rather than snapping back to 1 every time the overlay restarts.
  window.serena.onVoiceSpeed?.((value) => {
    range.value = String(value);
    show(value);
  });

  range.addEventListener('input', () => {
    show(range.value);
    window.serena.setVoiceSpeed(range.value);
  });
  // Dragging must not be read as typing or trip the global shortcuts.
  range.addEventListener('keydown', (event) => event.stopPropagation());
  show(range.value);
})();
