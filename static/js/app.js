/**
 * Live Translation & Transcription — Frontend
 *
 * Flow:
 *   1. Capture microphone via Web Audio API at 16 kHz mono
 *   2. Accumulate raw PCM samples into 3-second windows
 *   3. Send each window as Int16 binary over WebSocket
 *   4. Receive JSON results (transcription + translation)
 *   5. Render cards in the live feed
 */

const SAMPLE_RATE   = 16000; // Hz — must match server
const CHUNK_SAMPLES = SAMPLE_RATE * 3; // 3-second windows

// ── DOM refs ─────────────────────────────────────
const startBtn          = document.getElementById('startBtn');
const stopBtn           = document.getElementById('stopBtn');
const clearBtn          = document.getElementById('clearBtn');
const statusDot         = document.getElementById('statusDot');
const statusText        = document.getElementById('statusText');
const audioMeterFill    = document.getElementById('audioMeterFill');
const processingBanner  = document.getElementById('processingBanner');
const feed              = document.getElementById('feed');
const feedEmpty         = feed.querySelector('.feed-empty');

// ── State ─────────────────────────────────────────
let ws             = null;
let audioCtx       = null;
let mediaStream    = null;
let scriptProcessor = null;
let sampleBuffer   = new Float32Array(0);
let meterInterval  = null;
let lastRms        = 0;

// ── Helpers ───────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = `status-dot ${state}`;
  statusText.textContent = text;
}

function floatTo16BitPCM(float32Array) {
  const buf = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return buf;
}

function downsampleBuffer(buffer, inputRate, outputRate) {
  if (inputRate === outputRate) return buffer;
  const ratio      = inputRate / outputRate;
  const newLength  = Math.round(buffer.length / ratio);
  const result     = new Float32Array(newLength);
  for (let i = 0; i < newLength; i++) {
    const start = Math.floor(i * ratio);
    const end   = Math.floor((i + 1) * ratio);
    let sum = 0;
    for (let j = start; j < end; j++) sum += buffer[j];
    result[i] = sum / (end - start);
  }
  return result;
}

function appendToBuffer(existing, incoming) {
  const merged = new Float32Array(existing.length + incoming.length);
  merged.set(existing);
  merged.set(incoming, existing.length);
  return merged;
}

// ── Meter update ──────────────────────────────────
function updateMeter(rms) {
  lastRms = rms;
  const pct = Math.min(100, rms * 800);
  audioMeterFill.style.width = pct + '%';
}

// ── Audio processing ──────────────────────────────
function startAudioCapture(stream, wsConn, nativeRate) {
  audioCtx = new AudioContext({ sampleRate: nativeRate });
  const source = audioCtx.createMediaStreamSource(stream);

  scriptProcessor = audioCtx.createScriptProcessor(4096, 1, 1);
  source.connect(scriptProcessor);
  scriptProcessor.connect(audioCtx.destination);

  scriptProcessor.onaudioprocess = (e) => {
    const raw     = e.inputBuffer.getChannelData(0);
    const chunk   = downsampleBuffer(raw, nativeRate, SAMPLE_RATE);
    sampleBuffer  = appendToBuffer(sampleBuffer, chunk);

    // Update meter with chunk RMS
    let sum = 0;
    for (let i = 0; i < raw.length; i++) sum += raw[i] * raw[i];
    updateMeter(Math.sqrt(sum / raw.length));

    // Send accumulated chunks to server
    while (sampleBuffer.length >= CHUNK_SAMPLES) {
      const toSend   = sampleBuffer.slice(0, CHUNK_SAMPLES);
      sampleBuffer   = sampleBuffer.slice(CHUNK_SAMPLES);
      const pcmBuf   = floatTo16BitPCM(toSend);
      if (wsConn.readyState === WebSocket.OPEN) {
        wsConn.send(pcmBuf);
      }
    }
  };
}

// ── WebSocket ─────────────────────────────────────
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}/ws/audio`;
  const conn  = new WebSocket(url);

  conn.onopen = () => {
    setStatus('listening', 'Listening — speak now');
    startBtn.disabled = true;
    stopBtn.disabled  = false;
  };

  conn.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleServerMessage(msg);
    } catch (e) {
      console.error('Bad server message', e);
    }
  };

  conn.onerror = (err) => {
    console.error('WebSocket error', err);
    setStatus('error', 'Connection error — try reloading');
  };

  conn.onclose = () => {
    if (startBtn.disabled) {
      setStatus('', 'Disconnected');
      stopCapture();
    }
  };

  return conn;
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case 'processing':
      processingBanner.classList.remove('hidden');
      setStatus('processing', 'Processing speech…');
      break;

    case 'result':
      processingBanner.classList.add('hidden');
      setStatus('listening', 'Listening — speak now');
      renderResult(msg);
      break;

    case 'error':
      processingBanner.classList.add('hidden');
      setStatus('error', 'Error processing audio');
      renderError(msg.message);
      break;
  }
}

// ── Render helpers ────────────────────────────────
function clearFeedEmpty() {
  if (feedEmpty) feedEmpty.remove();
}

function renderResult(msg) {
  clearFeedEmpty();

  const {
    transcription,
    translation,
    source_language,
    language_probability,
    is_english,
  } = msg;

  const langName   = languageName(source_language);
  const confidence = Math.round(language_probability * 100);

  const card = document.createElement('div');
  card.className = 'result-card';

  if (is_english) {
    card.innerHTML = `
      <div class="card-meta">
        <span class="lang-badge english">🇺🇸 English</span>
        <span class="confidence">${confidence}% confidence</span>
      </div>
      <div class="translation-block">
        <span class="block-label">Transcription</span>
        ${escapeHtml(transcription)}
      </div>`;
  } else {
    card.innerHTML = `
      <div class="card-meta">
        <span class="lang-badge foreign">🗣 ${langName}</span>
        <span class="confidence">${confidence}% confidence</span>
      </div>
      <div class="transcription-block">
        <span class="block-label">Original (${langName})</span>
        ${escapeHtml(transcription)}
      </div>
      <hr class="divider" />
      <div class="translation-block">
        <span class="block-label">English Translation</span>
        ${escapeHtml(translation)}
      </div>`;
  }

  feed.appendChild(card);
  feed.scrollTop = feed.scrollHeight;
}

function renderError(message) {
  clearFeedEmpty();
  const card = document.createElement('div');
  card.className = 'error-card';
  card.textContent = '⚠ ' + message;
  feed.appendChild(card);
  feed.scrollTop = feed.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

// ISO 639-1 display names for common languages
const LANG_NAMES = {
  af:'Afrikaans', ar:'Arabic', az:'Azerbaijani', be:'Belarusian',
  bg:'Bulgarian', bn:'Bengali', bs:'Bosnian', ca:'Catalan',
  cs:'Czech', cy:'Welsh', da:'Danish', de:'German',
  el:'Greek', en:'English', es:'Spanish', et:'Estonian',
  fa:'Persian', fi:'Finnish', fr:'French', gl:'Galician',
  gu:'Gujarati', he:'Hebrew', hi:'Hindi', hr:'Croatian',
  hu:'Hungarian', hy:'Armenian', id:'Indonesian', is:'Icelandic',
  it:'Italian', ja:'Japanese', ka:'Georgian', kk:'Kazakh',
  km:'Khmer', ko:'Korean', lt:'Lithuanian', lv:'Latvian',
  mk:'Macedonian', ml:'Malayalam', mn:'Mongolian', mr:'Marathi',
  ms:'Malay', my:'Burmese', ne:'Nepali', nl:'Dutch',
  no:'Norwegian', pa:'Punjabi', pl:'Polish', ps:'Pashto',
  pt:'Portuguese', ro:'Romanian', ru:'Russian', si:'Sinhala',
  sk:'Slovak', sl:'Slovenian', sq:'Albanian', sr:'Serbian',
  su:'Sundanese', sv:'Swedish', sw:'Swahili', ta:'Tamil',
  te:'Telugu', tg:'Tajik', th:'Thai', tk:'Turkmen',
  tl:'Tagalog', tr:'Turkish', tt:'Tatar', uk:'Ukrainian',
  ur:'Urdu', uz:'Uzbek', vi:'Vietnamese', yi:'Yiddish',
  yo:'Yoruba', zh:'Chinese',
};

function languageName(code) {
  return LANG_NAMES[code] || code.toUpperCase();
}

// ── Stop capture ──────────────────────────────────
function stopCapture() {
  if (scriptProcessor) { scriptProcessor.disconnect(); scriptProcessor = null; }
  if (audioCtx)         { audioCtx.close(); audioCtx = null; }
  if (mediaStream)      { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  if (ws && ws.readyState === WebSocket.OPEN) { ws.close(); ws = null; }
  sampleBuffer = new Float32Array(0);
  audioMeterFill.style.width = '0%';
  processingBanner.classList.add('hidden');
  startBtn.disabled = false;
  stopBtn.disabled  = true;
  setStatus('', 'Stopped — click Start to resume');
}

// ── Button handlers ───────────────────────────────
startBtn.addEventListener('click', async () => {
  try {
    setStatus('', 'Requesting microphone…');
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });

    // Get native sample rate BEFORE creating WebSocket
    const tmpCtx   = new AudioContext();
    const nativeRate = tmpCtx.sampleRate;
    tmpCtx.close();

    ws = connectWebSocket();
    // ws.onopen creates AudioContext and starts capture
    ws.onopen = () => {
      setStatus('listening', 'Listening — speak now');
      startBtn.disabled = true;
      stopBtn.disabled  = false;
      startAudioCapture(mediaStream, ws, nativeRate);
    };
  } catch (err) {
    setStatus('error', 'Microphone access denied');
    console.error(err);
  }
});

stopBtn.addEventListener('click', stopCapture);

clearBtn.addEventListener('click', () => {
  feed.innerHTML = `
    <div class="feed-empty">
      <span class="feed-empty-icon">🎙️</span>
      <p>Your live transcriptions will appear here.</p>
      <p class="feed-empty-hint">Speak in any language — translations appear instantly.</p>
    </div>`;
});
