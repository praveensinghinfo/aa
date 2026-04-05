/**
 * Live Translation & Transcription — Frontend
 *
 * Uses the browser's built-in SpeechRecognition API for transcription,
 * then sends the recognised text to the FastAPI backend for Claude translation.
 */

// ── DOM ───────────────────────────────────────────
const startBtn    = document.getElementById('startBtn');
const stopBtn     = document.getElementById('stopBtn');
const clearBtn    = document.getElementById('clearBtn');
const langSelect  = document.getElementById('langSelect');
const statusDot   = document.getElementById('statusDot');
const statusText  = document.getElementById('statusText');
const interimBox  = document.getElementById('interimBox');
const interimText = document.getElementById('interimText');
const feed        = document.getElementById('feed');

// ── State ─────────────────────────────────────────
let recognition = null;
let ws          = null;
let running     = false;

// ── Speech Recognition setup ──────────────────────
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

if (!SpeechRecognition) {
  statusText.textContent = '⚠ Speech recognition not supported. Use Chrome or Edge.';
  startBtn.disabled = true;
}

function buildRecognition() {
  const rec = new SpeechRecognition();
  rec.continuous      = true;
  rec.interimResults  = true;
  rec.maxAlternatives = 1;
  rec.lang            = langSelect.value;   // '' = browser default / auto-detect

  rec.onstart = () => {
    setStatus('listening', 'Listening — speak now');
    interimBox.classList.remove('hidden');
  };

  rec.onend = () => {
    if (running) {
      // Auto-restart to keep listening continuously
      try { rec.start(); } catch (_) {}
    } else {
      interimBox.classList.add('hidden');
      interimText.textContent = '';
      setStatus('', 'Stopped');
    }
  };

  rec.onerror = (e) => {
    if (e.error === 'no-speech') return; // normal pause
    if (e.error === 'aborted')  return;  // we called stop()
    setStatus('error', `Error: ${e.error}`);
    console.error('SpeechRecognition error', e.error);
  };

  rec.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const result = e.results[i];
      if (result.isFinal) {
        const text = result[0].transcript.trim();
        if (text) sendForTranslation(text);
        interimText.textContent = '';
      } else {
        interim += result[0].transcript;
      }
    }
    interimText.textContent = interim;
  };

  return rec;
}

// ── WebSocket ─────────────────────────────────────
function connectWebSocket(onReady) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const conn  = new WebSocket(`${proto}://${location.host}/ws/translate`);

  conn.onopen = () => {
    console.log('WS connected');
    if (onReady) onReady(conn);
  };

  conn.onmessage = (evt) => {
    try { handleServerMsg(JSON.parse(evt.data)); }
    catch (e) { console.error('Bad WS message', e); }
  };

  conn.onerror  = () => setStatus('error', 'WebSocket error');
  conn.onclose  = () => { if (running) setStatus('error', 'Connection lost'); };
  return conn;
}

function sendForTranslation(text) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  setStatus('processing', 'Translating…');
  ws.send(JSON.stringify({ type: 'translate', text }));
}

// ── Server message handler ────────────────────────
function handleServerMsg(msg) {
  if (running) setStatus('listening', 'Listening — speak now');
  switch (msg.type) {
    case 'result': renderResult(msg); break;
    case 'error':  renderError(msg.message); break;
  }
}

// ── Status helper ──────────────────────────────────
function setStatus(state, text) {
  statusDot.className = `status-dot ${state}`;
  statusText.textContent = text;
}

// ── Render result card ─────────────────────────────
function clearEmpty() {
  const empty = feed.querySelector('.feed-empty');
  if (empty) empty.remove();
}

function renderResult(msg) {
  clearEmpty();
  const { original, translation, source_language_name, is_english } = msg;
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const card = document.createElement('div');
  card.className = 'result-card';

  if (is_english) {
    card.innerHTML = `
      <div class="card-meta">
        <span class="lang-badge en">🇺🇸 English</span>
        <span class="card-time">${time}</span>
      </div>
      <div class="english-block">
        <span class="block-label">Transcription</span>
        ${esc(original)}
      </div>`;
  } else {
    card.innerHTML = `
      <div class="card-meta">
        <span class="lang-badge xx">🗣 ${esc(source_language_name)}</span>
        <span class="card-time">${time}</span>
      </div>
      <div class="original-block">
        <span class="block-label">Original (${esc(source_language_name)})</span>
        ${esc(original)}
      </div>
      <hr class="divider" />
      <div class="english-block">
        <span class="block-label">English Translation</span>
        ${esc(translation)}
      </div>`;
  }

  feed.appendChild(card);
  feed.scrollTop = feed.scrollHeight;
}

function renderError(message) {
  clearEmpty();
  const card = document.createElement('div');
  card.className = 'error-card';
  card.textContent = '⚠ ' + message;
  feed.appendChild(card);
  feed.scrollTop = feed.scrollHeight;
}

function esc(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str));
  return d.innerHTML;
}

// ── Buttons ────────────────────────────────────────
startBtn.addEventListener('click', () => {
  if (!SpeechRecognition) return;
  running = true;
  startBtn.disabled = true;
  stopBtn.disabled  = false;
  setStatus('', 'Connecting…');

  ws = connectWebSocket(() => {
    recognition = buildRecognition();
    try {
      recognition.start();
    } catch (e) {
      setStatus('error', 'Could not start microphone: ' + e.message);
      stopAll();
    }
  });
});

stopBtn.addEventListener('click', stopAll);

clearBtn.addEventListener('click', () => {
  feed.innerHTML = `
    <div class="feed-empty">
      <span class="feed-empty-icon">🎙️</span>
      <p>Transcriptions and translations appear here.</p>
      <p class="feed-empty-hint">Works best in Chrome / Edge. Speak naturally — pauses trigger translation.</p>
    </div>`;
});

function stopAll() {
  running = false;
  if (recognition) { try { recognition.stop(); } catch (_) {} recognition = null; }
  if (ws && ws.readyState === WebSocket.OPEN) { ws.close(); ws = null; }
  interimBox.classList.add('hidden');
  interimText.textContent = '';
  startBtn.disabled = false;
  stopBtn.disabled  = true;
  setStatus('', 'Stopped — click Start to resume');
}

// Change language on-the-fly by restarting recognition
langSelect.addEventListener('change', () => {
  if (!running || !recognition) return;
  try { recognition.stop(); } catch (_) {}
  recognition = buildRecognition();
  try { recognition.start(); } catch (_) {}
});
