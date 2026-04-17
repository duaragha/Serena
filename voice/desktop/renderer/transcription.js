/**
 * TranscriptionDisplay — speech-to-text overlay for the brain visualization.
 * Shows the user's spoken input and Serena's typewriter-style response.
 */

const TYPEWRITER_DELAY_MS = 30;
const AUTO_CLEAR_DELAY_MS = 8000;

export class TranscriptionDisplay {
  constructor(container) {
    this._container = container;
    this._typewriterTimer = null;
    this._autoClearTimer = null;
    this._charQueue = '';
    this._charIndex = 0;

    this._createElements();
  }


  // --- Setup ---

  _createElements() {
    // Shared styles injected once
    if (!document.getElementById('transcription-styles')) {
      const style = document.createElement('style');
      style.id = 'transcription-styles';
      style.textContent = `
        .transcription-layer {
          position: absolute;
          left: 0;
          right: 0;
          pointer-events: none;
          display: flex;
          justify-content: center;
          z-index: 10;
        }

        .transcription-text {
          font-family: 'Inter', 'Segoe UI', -apple-system, system-ui, sans-serif;
          text-align: center;
          max-width: 80%;
          padding: 0 24px;
          opacity: 0;
          transition: opacity 0.4s ease;
          text-shadow:
            0 0 12px rgba(0, 0, 0, 0.8),
            0 0 4px rgba(0, 0, 0, 0.6);
          word-wrap: break-word;
          overflow-wrap: break-word;
        }

        .transcription-text.visible {
          opacity: 1;
        }

        .transcription-user {
          top: 32px;
        }

        .transcription-user .transcription-text {
          font-size: 14px;
          color: rgba(255, 255, 255, 0.55);
          font-weight: 400;
          letter-spacing: 0.02em;
        }

        .transcription-response {
          bottom: 48px;
        }

        .transcription-response .transcription-text {
          font-size: 20px;
          color: rgba(255, 255, 255, 0.92);
          font-weight: 500;
          letter-spacing: 0.01em;
        }

        .transcription-cursor {
          display: inline-block;
          width: 2px;
          height: 1.1em;
          background: rgba(255, 255, 255, 0.7);
          margin-left: 2px;
          vertical-align: text-bottom;
          animation: transcription-blink 0.6s step-end infinite;
        }

        @keyframes transcription-blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
      `;
      document.head.appendChild(style);
    }

    // User text container (top)
    this._userLayer = document.createElement('div');
    this._userLayer.className = 'transcription-layer transcription-user';

    this._userText = document.createElement('div');
    this._userText.className = 'transcription-text';
    this._userLayer.appendChild(this._userText);
    this._container.appendChild(this._userLayer);

    // Response text container (bottom)
    this._responseLayer = document.createElement('div');
    this._responseLayer.className = 'transcription-layer transcription-response';

    this._responseText = document.createElement('div');
    this._responseText.className = 'transcription-text';
    this._responseLayer.appendChild(this._responseText);
    this._container.appendChild(this._responseLayer);
  }


  // --- Public API ---

  /**
   * Show the user's transcribed speech at the top of the container.
   * Replaces any existing user text immediately.
   */
  showUserText(text) {
    this._resetAutoClear();
    this._userText.textContent = text;
    this._userText.classList.add('visible');
    this._scheduleAutoClear();
  }


  /**
   * Show Serena's response with a typewriter effect at the bottom.
   * Cancels any in-progress typewriter animation and starts fresh.
   */
  showResponseText(text) {
    this._resetAutoClear();
    this._stopTypewriter();

    this._charQueue = text;
    this._charIndex = 0;
    this._responseText.textContent = '';
    this._responseText.classList.add('visible');

    this._typewriterStep();
  }


  /**
   * Fade out all text elements.
   */
  clear() {
    this._stopTypewriter();
    this._resetAutoClear();

    this._userText.classList.remove('visible');
    this._responseText.classList.remove('visible');

    // Remove content after fade transition completes
    setTimeout(() => {
      this._userText.textContent = '';
      this._responseText.textContent = '';
    }, 450);
  }


  /**
   * Remove DOM elements and clean up timers.
   */
  dispose() {
    this._stopTypewriter();
    this._resetAutoClear();

    this._userLayer.remove();
    this._responseLayer.remove();

    const style = document.getElementById('transcription-styles');
    if (style) style.remove();
  }


  // --- Typewriter ---

  _typewriterStep() {
    if (this._charIndex >= this._charQueue.length) {
      // Done typing, remove cursor
      this._removeCursor();
      this._scheduleAutoClear();
      return;
    }

    // Append next character
    this._removeCursor();

    const textNode = document.createTextNode(this._charQueue[this._charIndex]);
    this._responseText.appendChild(textNode);

    // Add blinking cursor
    const cursor = document.createElement('span');
    cursor.className = 'transcription-cursor';
    this._responseText.appendChild(cursor);

    this._charIndex++;

    this._typewriterTimer = setTimeout(() => this._typewriterStep(), TYPEWRITER_DELAY_MS);
  }


  _removeCursor() {
    const cursor = this._responseText.querySelector('.transcription-cursor');
    if (cursor) cursor.remove();
  }


  _stopTypewriter() {
    if (this._typewriterTimer !== null) {
      clearTimeout(this._typewriterTimer);
      this._typewriterTimer = null;
    }
    this._removeCursor();
  }


  // --- Auto-clear ---

  _scheduleAutoClear() {
    this._resetAutoClear();
    this._autoClearTimer = setTimeout(() => this.clear(), AUTO_CLEAR_DELAY_MS);
  }

  _resetAutoClear() {
    if (this._autoClearTimer !== null) {
      clearTimeout(this._autoClearTimer);
      this._autoClearTimer = null;
    }
  }
}
