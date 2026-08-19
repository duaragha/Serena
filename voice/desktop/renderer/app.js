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
const conversationHistoryEl = document.getElementById('conversation-history');
const conversationHistoryListEl = document.getElementById('conversation-history-list');
const conversationHistoryCountEl = document.getElementById('conversation-history-count');
const dashboardEl = document.getElementById('dashboard');
const overlayEl = document.getElementById('overlay');
const voiceMuteEl = document.getElementById('voice-mute');
const voiceMuteLabelEl = document.getElementById('voice-mute-label');
const microphoneMuteEl = document.getElementById('microphone-mute');
const microphoneMuteLabelEl = document.getElementById('microphone-mute-label');

let responseTimeout = null;
let responseFadeTimeout = null;
let typewriterTimeout = null;
let responseTracksVoice = false;
let responseSpeechStarted = false;
let reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
const HISTORY_LIMIT = 12;
const conversationHistory = [];
let pendingUserText = '';
let voiceMuted = false;
let microphoneMuted = false;

function showVoiceMuted(value) {
  voiceMuted = Boolean(value);
  if (!voiceMuteEl || !voiceMuteLabelEl) return;
  voiceMuteEl.setAttribute('aria-pressed', String(voiceMuted));
  voiceMuteEl.setAttribute(
    'aria-label',
    voiceMuted ? 'Unmute Serena voice' : 'Mute Serena voice',
  );
  voiceMuteEl.title = voiceMuted ? 'Unmute Serena voice' : 'Mute Serena voice';
  voiceMuteLabelEl.textContent = voiceMuted ? 'muted' : 'mute';
}

if (voiceMuteEl && window.serena?.setVoiceMuted) {
  voiceMuteEl.addEventListener('click', () => window.serena.setVoiceMuted(!voiceMuted));
  voiceMuteEl.addEventListener('keydown', (event) => event.stopPropagation());
  window.serena.onVoiceMuted?.(showVoiceMuted);
  showVoiceMuted(false);
}

function showMicrophoneMuted(value) {
  microphoneMuted = Boolean(value);
  if (!microphoneMuteEl || !microphoneMuteLabelEl) return;
  microphoneMuteEl.setAttribute('aria-pressed', String(microphoneMuted));
  microphoneMuteEl.setAttribute(
    'aria-label',
    microphoneMuted ? 'Unmute Serena microphone' : 'Mute Serena microphone',
  );
  microphoneMuteEl.title = microphoneMuted
    ? 'Unmute Serena microphone'
    : 'Mute Serena microphone';
  microphoneMuteLabelEl.textContent = microphoneMuted ? 'mic off' : 'mic';
}

if (microphoneMuteEl && window.serena?.setMicrophoneMuted) {
  microphoneMuteEl.addEventListener(
    'click',
    () => window.serena.setMicrophoneMuted(!microphoneMuted),
  );
  microphoneMuteEl.addEventListener('keydown', (event) => event.stopPropagation());
  window.serena.onMicrophoneMuted?.(showMicrophoneMuted);
  showMicrophoneMuted(false);
}

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

  if (state === 'speaking' && !responseEl.classList.contains('hidden')) {
    // The state-file watcher and the response socket are independent. If the
    // response wins that race, let the later speaking event take ownership
    // before the text-only fallback clock can dismiss it.
    clearTimeout(responseTimeout);
    responseTimeout = null;
    responseTracksVoice = true;
    responseSpeechStarted = true;
  } else if (responseTracksVoice) {
    if (
      responseSpeechStarted
      || state === 'idle'
      || state === 'listening'
      || state === 'offline'
    ) {
      finishResponseDisplay();
    }
  }
});

window.serena.onAmplitude((value) => {
  window.serena.setAmplitude(value);
});

// --- Transcription display ---

window.serena.onTranscription((text) => {
  resetResponseDisplay();
  pendingUserText = String(text || '').trim();
  transcriptionEl.textContent = text;
  transcriptionEl.classList.remove('hidden');
});

// --- Response display ---

function showResponseText(text) {
  resetResponseDisplay();
  responseEl.classList.remove('hidden');
  typewriterEffect(responseEl, text);
  appendCompletedTurn(pendingUserText, String(text || '').trim());
  pendingUserText = '';

  const voiceState = window._serenaVoiceState;
  responseTracksVoice = voiceState === 'thinking' || voiceState === 'speaking';
  responseSpeechStarted = voiceState === 'speaking';
  if (!responseTracksVoice) {
    // Keep the old bounded display for text-only errors and previews, which
    // have no playback completion state to dismiss them.
    responseTimeout = setTimeout(finishResponseDisplay, 8000);
  }
}

function finishResponseDisplay() {
  if (
    responseEl.classList.contains('hidden')
    || responseEl.classList.contains('fade-out')
  ) {
    return;
  }
  clearTimeout(responseTimeout);
  clearTimeout(responseFadeTimeout);
  clearTimeout(typewriterTimeout);
  responseTimeout = null;
  responseTracksVoice = false;
  responseSpeechStarted = false;
  transcriptionEl.classList.add('hidden');
  responseEl.classList.add('fade-out');
  responseFadeTimeout = setTimeout(() => {
    responseEl.classList.add('hidden');
    responseEl.classList.remove('fade-out');
    responseEl.textContent = '';
    responseFadeTimeout = null;
  }, 500);
}

function resetResponseDisplay() {
  clearTimeout(responseTimeout);
  clearTimeout(responseFadeTimeout);
  clearTimeout(typewriterTimeout);
  responseTimeout = null;
  responseFadeTimeout = null;
  typewriterTimeout = null;
  responseTracksVoice = false;
  responseSpeechStarted = false;
  responseEl.classList.add('hidden');
  responseEl.classList.remove('fade-out');
  responseEl.textContent = '';
}

function clearTranscriptionCards() {
  resetResponseDisplay();
  transcriptionEl.classList.add('hidden');
  transcriptionEl.textContent = '';
}

window.serena.onResponse(showResponseText);

function appendCompletedTurn(userText, responseText) {
  if (!userText || !responseText || !conversationHistoryListEl) return;
  conversationHistory.push({ user: userText, assistant: responseText });
  if (conversationHistory.length > HISTORY_LIMIT) {
    conversationHistory.splice(0, conversationHistory.length - HISTORY_LIMIT);
  }
  renderConversationHistory();
}

function renderConversationHistory() {
  conversationHistoryListEl.replaceChildren();
  for (const turn of conversationHistory) {
    const turnEl = document.createElement('div');
    turnEl.className = 'conversation-turn';
    turnEl.append(
      conversationLine('user', 'You', turn.user),
      conversationLine('serena', 'Serena', turn.assistant),
    );
    conversationHistoryListEl.appendChild(turnEl);
  }
  if (conversationHistoryEl) conversationHistoryEl.classList.remove('hidden');
  if (conversationHistoryCountEl) {
    const count = conversationHistory.length;
    conversationHistoryCountEl.textContent = `${count} turn${count === 1 ? '' : 's'}`;
  }
  conversationHistoryListEl.scrollTop = conversationHistoryListEl.scrollHeight;
}

function conversationLine(kind, label, text) {
  const line = document.createElement('div');
  line.className = `conversation-line ${kind}`;
  const role = document.createElement('span');
  role.className = 'conversation-role';
  role.textContent = label;
  const body = document.createElement('span');
  body.className = 'conversation-text';
  body.textContent = text;
  line.append(role, body);
  return line;
}

// A narrow inspection surface for the Electron smoke test. The array itself
// stays bounded and contains only the same user/assistant text already shown.
window._serenaConversationHistory = {
  entries: conversationHistory,
  appendCompletedTurn,
  limit: HISTORY_LIMIT,
};

// Kept as a stable smoke-test surface for the transient cards. Production
// events still enter through the preload bridge above.
window._serenaTranscription = {
  _responseText: responseEl,
  showResponseText,
  clear: clearTranscriptionCards,
};
Object.defineProperty(window._serenaTranscription, '_reducedMotion', {
  get: () => reducedMotion,
  set: (value) => { reducedMotion = value; },
});

function typewriterEffect(el, text) {
  clearTimeout(typewriterTimeout);
  // The response is already complete when this event arrives. Revealing it at
  // 20 ms per character added ten seconds to a 500-character reply and could
  // still be typing after playback ended.
  el.textContent = String(text || '');
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

window.serena.onCodeSnapshot((data) => {
  const fn = window.serena.codePanelOnSnapshot;
  if (fn) fn(data);
});

window.serena.onCodeControlResult((data) => {
  const fn = window.serena.codePanelOnControlResult;
  if (fn) fn(data);
});

window.serena.onToggleCodePanel(() => {
  const fn = window.serena.codePanelOnToggle;
  if (fn) fn();
});

window.serena.onShowCodePanel(() => {
  const fn = window.serena.codePanelOnShow;
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
  const attachmentEl = document.getElementById('typed-attachment');
  const attachmentPreview = document.getElementById('typed-attachment-preview');
  const attachmentRemove = document.getElementById('typed-attachment-remove');
  const errorEl = document.getElementById('type-error');
  const imageHelpers = window.SerenaTypedImages;
  if (!bar || !input || !window.serena || !window.serena.sendTyped) return;

  const supportsImages = Boolean(
    attachmentEl && attachmentPreview && attachmentRemove && imageHelpers,
  );

  let busy = false;
  let attachment = null;
  let pendingSubmission = null;

  const showError = (message = '') => {
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.classList.toggle('hidden', !message);
  };

  const setAttachment = (image, previewUrl = '') => {
    attachment = image;
    if (!supportsImages) return;
    attachmentPreview.src = previewUrl;
    attachmentEl.classList.toggle('hidden', !image);
  };

  const setBusy = (value) => {
    busy = value;
    bar.classList.toggle('busy', value);
    input.placeholder = value
      ? 'thinking…'
      : supportsImages ? 'type or paste an image' : 'type a message';
  };

  if (supportsImages) {
    attachmentRemove.addEventListener('click', () => {
      setAttachment(null);
      showError();
      input.focus();
    });

    input.addEventListener('paste', (event) => {
      const items = Array.from(event.clipboardData?.items || []);
      const imageItem = items.find(
        (item) => item.kind === 'file' && String(item.type || '').startsWith('image/'),
      );
      if (!imageItem) return;
      event.preventDefault();
      event.stopPropagation();
      const file = imageItem.getAsFile();
      const checked = imageHelpers.validateClipboardFile(file);
      if (!checked.ok) {
        showError(checked.error);
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => showError('that clipboard image could not be read.');
      reader.onload = () => {
        const result = imageHelpers.attachmentFromDataUrl(reader.result, file);
        if (!result.ok) {
          showError(result.error);
          return;
        }
        setAttachment(result.image, String(reader.result));
        showError();
      };
      reader.readAsDataURL(file);
    });
  }

  const clearAcceptedSubmission = () => {
    if (!pendingSubmission) return;
    if (input.value.trim() === pendingSubmission.text) input.value = '';
    if (attachment === pendingSubmission.image) setAttachment(null);
    pendingSubmission = null;
    showError();
  };

  input.addEventListener('keydown', (event) => {
    // Never let the overlay's global shortcuts swallow ordinary typing.
    event.stopPropagation();
    if (event.key !== 'Enter' || event.shiftKey) return;
    const text = input.value.trim();
    if ((!text && !attachment) || busy) return;
    showError();
    setBusy(true);
    pendingSubmission = {
      text,
      image: attachment,
    };
    try {
      window.serena.sendTyped({ text, image: attachment });
    } catch (error) {
      pendingSubmission = null;
      setBusy(false);
      showError(error?.message || 'that message could not be sent.');
    }
  });

  // She has answered (or failed); take the bar back.
  window.serena.onTypedInputAccepted?.(() => clearAcceptedSubmission());
  window.serena.onResponse(() => {
    clearAcceptedSubmission();
    setBusy(false);
  });
  window.serena.onTypedInputError?.((message) => {
    pendingSubmission = null;
    setBusy(false);
    showError(message);
  });

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
