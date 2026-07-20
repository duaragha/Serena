/**
 * Dashboard — slide-in panel showing calendar, weather, and notifications.
 * Slides from the right edge, 350px wide, full viewport height.
 */

const WEATHER_EMOJI = {
  sunny: '\u2600\uFE0F',
  clear: '\u2600\uFE0F',
  cloudy: '\u2601\uFE0F',
  clouds: '\u2601\uFE0F',
  overcast: '\u2601\uFE0F',
  'partly cloudy': '\u26C5',
  rain: '\uD83C\uDF27\uFE0F',
  rainy: '\uD83C\uDF27\uFE0F',
  drizzle: '\uD83C\uDF27\uFE0F',
  thunderstorm: '\u26C8\uFE0F',
  snow: '\u2744\uFE0F',
  snowy: '\u2744\uFE0F',
  fog: '\uD83C\uDF2B\uFE0F',
  mist: '\uD83C\uDF2B\uFE0F',
  wind: '\uD83C\uDF2C\uFE0F',
  windy: '\uD83C\uDF2C\uFE0F',
};

function weatherEmoji(condition) {
  if (!condition) return '\u2600\uFE0F';
  const key = condition.toLowerCase().trim();
  for (const [k, v] of Object.entries(WEATHER_EMOJI)) {
    if (key.includes(k)) return v;
  }
  return '\u2600\uFE0F';
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export class Dashboard {
  /** @param {HTMLElement} container */
  constructor(container) {
    this._visible = false;

    // Root panel
    this._panel = el('div', 'dashboard glass');
    container.appendChild(this._panel);

    // Header
    const header = el('div', 'dashboard__header');
    header.appendChild(el('span', 'dashboard__title text-shadow', 'Dashboard'));
    const closeBtn = el('button', 'dashboard__close', '\u00D7');
    closeBtn.setAttribute('aria-label', 'Close dashboard');
    closeBtn.addEventListener('click', () => this.toggle());
    header.appendChild(closeBtn);
    this._panel.appendChild(header);

    // Scrollable content area
    this._content = el('div', 'dashboard__content');
    this._panel.appendChild(this._content);

    // Sections
    this._calendarSection = this._createSection('Next Events');
    this._weatherSection = this._createSection('Weather');
    this._notifSection = this._createSection('Notifications');

    this._content.appendChild(this._calendarSection.wrapper);
    this._content.appendChild(this._weatherSection.wrapper);
    this._content.appendChild(this._notifSection.wrapper);

    // Initial empty states
    this._setEmpty(this._calendarSection, 'No upcoming events');
    this._setEmpty(this._weatherSection, 'Weather unavailable');
    this._setEmpty(this._notifSection, 'No notifications');

    // Enable mouse interaction when dashboard is visible
    this._panel.addEventListener('mouseenter', () => {
      if (window.serena) window.serena.setIgnoreMouse(false);
    });
    this._panel.addEventListener('mouseleave', () => {
      if (window.serena) window.serena.setIgnoreMouse(true);
    });
  }

  get isVisible() {
    return this._visible;
  }

  toggle() {
    this._visible = !this._visible;
    this._panel.classList.toggle('dashboard--visible', this._visible);
  }

  /**
   * @param {Array<{summary: string, start_display: string, end_display?: string, location?: string}>} events
   */
  updateCalendar(events) {
    const body = this._calendarSection.body;
    body.innerHTML = '';

    if (!events || events.length === 0) {
      this._setEmpty(this._calendarSection, 'No upcoming events');
      return;
    }

    const shown = events.slice(0, 5);
    for (const evt of shown) {
      const row = el('div', 'calendar-event');

      const time = el('span', 'calendar-event__time', evt.start_display || '');
      row.appendChild(time);

      const details = el('div', 'calendar-event__details');
      details.appendChild(el('div', 'calendar-event__summary text-shadow', evt.summary || 'Untitled'));
      if (evt.location) {
        details.appendChild(el('div', 'calendar-event__location', evt.location));
      }
      row.appendChild(details);

      body.appendChild(row);
    }
  }

  /**
   * @param {{temperature: number|string, condition: string, humidity?: string, wind?: string, forecast?: Array<{date: string, high: number|string, low: number|string, condition: string}>}} data
   */
  updateWeather(data) {
    const body = this._weatherSection.body;
    body.innerHTML = '';

    if (!data) {
      this._setEmpty(this._weatherSection, 'Weather unavailable');
      return;
    }

    // Current weather
    const current = el('div', 'weather-current');

    current.appendChild(el('div', 'weather-current__icon', weatherEmoji(data.condition)));

    const info = el('div');
    info.appendChild(el('div', 'weather-current__temp', `${data.temperature}\u00B0`));
    info.appendChild(el('div', 'weather-current__condition', data.condition || ''));

    const detailParts = [];
    if (data.humidity) detailParts.push(`Humidity ${data.humidity}`);
    if (data.wind) detailParts.push(`Wind ${data.wind}`);
    if (detailParts.length) {
      info.appendChild(el('div', 'weather-current__details', detailParts.join(' \u00B7 ')));
    }

    current.appendChild(info);
    body.appendChild(current);

    // 3-day forecast
    if (data.forecast && data.forecast.length > 0) {
      const forecastRow = el('div', 'weather-forecast');
      const days = data.forecast.slice(0, 3);
      for (const day of days) {
        const dayEl = el('div', 'weather-forecast__day');
        dayEl.appendChild(el('div', 'weather-forecast__label', this._shortDay(day.date)));
        dayEl.appendChild(el('div', 'weather-forecast__icon', weatherEmoji(day.condition)));

        const temps = el('div', 'weather-forecast__temps');
        temps.innerHTML = `${day.high}\u00B0 <span>${day.low}\u00B0</span>`;
        dayEl.appendChild(temps);

        forecastRow.appendChild(dayEl);
      }
      body.appendChild(forecastRow);
    }
  }

  /**
   * @param {Array<{text: string, time: string, priority?: string}>} notifications
   */
  updateNotifications(notifications) {
    const body = this._notifSection.body;
    body.innerHTML = '';

    if (!notifications || notifications.length === 0) {
      this._setEmpty(this._notifSection, 'No notifications');
      return;
    }

    for (const notif of notifications) {
      const priorityClass = notif.priority === 'high' ? ' notification-item--high' : '';
      const item = el('div', `notification-item${priorityClass}`);
      item.appendChild(el('div', 'notification-item__text', notif.text));
      if (notif.time) {
        item.appendChild(el('div', 'notification-item__time', notif.time));
      }
      body.appendChild(item);
    }
  }

  // ---- Internal ----

  _createSection(title) {
    const wrapper = el('div', 'dashboard__section');
    wrapper.appendChild(el('div', 'dashboard__section-title', title));
    const body = el('div');
    wrapper.appendChild(body);
    return { wrapper, body };
  }

  _setEmpty(section, message) {
    section.body.innerHTML = '';
    section.body.appendChild(el('div', 'dashboard__empty', message));
  }

  _shortDay(dateStr) {
    if (!dateStr) return '';
    try {
      const d = new Date(dateStr);
      return d.toLocaleDateString('en-US', { weekday: 'short' });
    } catch {
      return dateStr.slice(0, 3);
    }
  }
}
