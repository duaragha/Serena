// Serena overlay — renderer entry point

const stateColors = {
  idle: '#4ade80',
  listening: '#3b82f6',
  thinking: '#f59e0b',
  speaking: '#a855f7',
  offline: '#6b7280',
};

const stateEl = document.getElementById('state-dot');
const stateLabelEl = document.getElementById('state-label');
const transcriptionEl = document.getElementById('transcription');
const responseEl = document.getElementById('response');
const dashboardEl = document.getElementById('dashboard');
const overlayEl = document.getElementById('overlay');

let responseTimeout = null;

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
  const color = stateColors[state] || stateColors.offline;
  stateEl.style.backgroundColor = color;
  stateEl.style.boxShadow = `0 0 8px ${color}`;
  stateLabelEl.textContent = state;

  // Pulse animation for listening state
  if (state === 'listening') {
    stateEl.classList.add('pulse');
  } else {
    stateEl.classList.remove('pulse');
  }

  // Update Three.js brain visualization
  if (window.serena.setState) {
    window.serena.setState(state);
  }

  // Update transcription display
  if (state === 'idle' && window.serena.clearText) {
    window.serena.clearText();
  }
});

// --- Transcription display ---

window.serena.onTranscription((text) => {
  transcriptionEl.textContent = text;
  transcriptionEl.classList.remove('hidden');

  // Clear previous response when new transcription starts
  responseEl.classList.add('hidden');
  responseEl.textContent = '';

  // Show in brain overlay text
  if (window.serena.showUserText) {
    window.serena.showUserText(text);
  }
});

// --- Response display ---

window.serena.onResponse((text) => {
  responseEl.classList.remove('hidden');
  typewriterEffect(responseEl, text);

  // Show in brain overlay text
  if (window.serena.showResponseText) {
    window.serena.showResponseText(text);
  }

  // Fade out after a delay
  clearTimeout(responseTimeout);
  responseTimeout = setTimeout(() => {
    transcriptionEl.classList.add('hidden');
    responseEl.classList.add('fade-out');
    setTimeout(() => {
      responseEl.classList.add('hidden');
      responseEl.classList.remove('fade-out');
    }, 500);
  }, 8000);
});

function typewriterEffect(el, text) {
  el.textContent = '';
  let i = 0;
  const speed = 20; // ms per character
  function type() {
    if (i < text.length) {
      el.textContent += text.charAt(i);
      i++;
      setTimeout(type, speed);
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
