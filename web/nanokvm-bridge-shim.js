// Loaded before the NanoKVM-USB app: swaps navigator.serial, mediaDevices and camera/mic
// permissions for bridge-backed versions. No real device API, so plain http:// works.
(function () {
  'use strict';

  var VIDEO_DEVICE_ID = 'nanokvm-bridge-video';
  var AUDIO_DEVICE_ID = 'nanokvm-bridge-audio';
  var DEVICE_GROUP_ID = 'nanokvm-bridge';
  var AUDIO_SAMPLE_RATE = 48000;
  var AUDIO_CHANNELS = 2;
  var RETRY_MS = 500;
  var MAX_STREAM_BUFFER = 8 * 1024 * 1024;
  var CRLF2 = new Uint8Array([13, 10, 13, 10]);
  // Identifies this page to the bridge, which only lets a page change the
  // resolution when no other page is watching.
  var CLIENT_ID = Math.random().toString(36).slice(2);

  function wsUrl(path) {
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path;
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function domException(name, message) {
    return new DOMException(message, name);
  }

  function openWebSocket(path) {
    var ws = new WebSocket(wsUrl(path));
    ws.binaryType = 'arraybuffer';
    return new Promise(function (resolve, reject) {
      ws.addEventListener('open', function () { resolve(ws); }, { once: true });
      ws.addEventListener('close', function () {
        reject(domException('NetworkError', 'could not connect to ' + path + ' (see /api/status)'));
      }, { once: true });
    });
  }

  // navigator.serial

  var serialListeners = new Set();
  var serialPort = null;

  // Like real Web Serial, 'disconnect' fires only when the device goes away,
  // never for an explicit port.close().
  function RemoteSerialPort() {
    this._ws = null;
    this.readable = null;
    this.writable = null;
  }

  RemoteSerialPort.prototype.open = async function () {
    if (this._ws) throw domException('InvalidStateError', 'port is already open');
    var port = this;
    var ws = await openWebSocket('/ws/serial');
    port._ws = ws;

    port.readable = new ReadableStream({
      start: function (controller) {
        ws.addEventListener('message', function (ev) {
          if (!(ev.data instanceof ArrayBuffer)) return;
          try { controller.enqueue(new Uint8Array(ev.data)); } catch (e) { /* reader cancelled */ }
        });
        ws.addEventListener('close', function () {
          try { controller.close(); } catch (e) { /* already closed */ }
          if (port._ws !== ws) return;
          port._ws = port.readable = port.writable = null;
          var event = { type: 'disconnect', target: port };
          serialListeners.forEach(function (listener) { listener(event); });
        });
      }
    });

    port.writable = new WritableStream({
      write: function (chunk) {
        if (ws.readyState !== WebSocket.OPEN) {
          throw domException('NetworkError', 'the device has been disconnected');
        }
        ws.send(chunk);
      }
    });
  };

  RemoteSerialPort.prototype.close = async function () {
    var ws = this._ws;
    this._ws = this.readable = this.writable = null;
    if (ws) ws.close();
  };

  var remoteSerial = {
    requestPort: async function () {
      if (!serialPort) serialPort = new RemoteSerialPort();
      return serialPort;
    },
    getPorts: async function () {
      return serialPort ? [serialPort] : [];
    },
    addEventListener: function (type, listener) {
      if (type === 'disconnect') serialListeners.add(listener);
    },
    removeEventListener: function (type, listener) {
      if (type === 'disconnect') serialListeners.delete(listener);
    }
  };

  // navigator.mediaDevices: MJPEG over HTTP -> canvas -> MediaStream

  function concatBytes(a, b) {
    if (a.length === 0) return b;
    var out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function indexOfBytes(haystack, needle, from) {
    var last = haystack.length - needle.length;
    outer: for (var i = from; i <= last; i++) {
      for (var j = 0; j < needle.length; j++) {
        if (haystack[i + j] !== needle[j]) continue outer;
      }
      return i;
    }
    return -1;
  }

  // null if more data is needed, else { jpeg, end }; jpeg is null for parts
  // without a Content-Length.
  function nextPart(buffer, boundary) {
    var start = indexOfBytes(buffer, boundary, 0);
    if (start === -1) return null;
    var headersStart = start + boundary.length;
    var headersEnd = indexOfBytes(buffer, CRLF2, headersStart);
    if (headersEnd === -1) return null;
    var headers = new TextDecoder().decode(buffer.subarray(headersStart, headersEnd));
    var length = /Content-Length:\s*(\d+)/i.exec(headers);
    var bodyStart = headersEnd + CRLF2.length;
    if (!length) return { jpeg: null, end: bodyStart };
    var bodyEnd = bodyStart + parseInt(length[1], 10);
    if (buffer.length < bodyEnd) return null;
    return { jpeg: buffer.subarray(bodyStart, bodyEnd), end: bodyEnd };
  }

  async function drawFrame(jpeg, video) {
    var bitmap;
    try {
      bitmap = await createImageBitmap(new Blob([jpeg], { type: 'image/jpeg' }));
    } catch (e) {
      return; // corrupt frame
    }
    if (video.stopped) {
      bitmap.close();
      return;
    }
    if (video.canvas.width !== bitmap.width || video.canvas.height !== bitmap.height) {
      video.canvas.width = bitmap.width;
      video.canvas.height = bitmap.height;
    }
    video.ctx.drawImage(bitmap, 0, 0);
    bitmap.close();
    if (video.requestFrame) video.track.requestFrame();
  }

  async function pumpMjpeg(video) {
    while (!video.stopped) {
      try {
        var res = await fetch('/stream?client=' + CLIENT_ID, { signal: video.abort.signal, cache: 'no-store' });
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
        var boundaryMatch = /boundary="?([^;"]+)"?/i.exec(res.headers.get('content-type') || '');
        var boundary = new TextEncoder().encode('--' + (boundaryMatch ? boundaryMatch[1] : 'boundarydonotcross'));
        var reader = res.body.getReader();
        var buffer = new Uint8Array(0);

        while (!video.stopped) {
          var chunk = await reader.read();
          if (chunk.done) break;
          buffer = concatBytes(buffer, chunk.value);

          // Newest frame only, so a slow client drops frames instead of lagging.
          var newest = null;
          var part;
          while ((part = nextPart(buffer, boundary))) {
            if (part.jpeg) newest = part.jpeg;
            buffer = buffer.subarray(part.end);
          }
          if (newest) await drawFrame(newest, video);
          if (buffer.length > MAX_STREAM_BUFFER) buffer = new Uint8Array(0);
        }
      } catch (err) {
        if (video.stopped) return;
        console.warn('nanokvm-bridge: video stream interrupted, retrying', err);
      }
      if (!video.stopped) await sleep(RETRY_MS);
    }
  }

  function createVideoTrack(width, height) {
    var canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    var stream = canvas.captureStream(0);
    var track = stream.getVideoTracks()[0];
    var requestFrame = typeof track.requestFrame === 'function';
    if (!requestFrame) {
      // No on-demand frames in this browser: capture at a fixed rate instead.
      track.stop();
      track = canvas.captureStream(30).getVideoTracks()[0];
    }
    var video = {
      canvas: canvas,
      ctx: canvas.getContext('2d', { alpha: false }),
      track: track,
      requestFrame: requestFrame,
      stopped: false,
      abort: new AbortController()
    };
    var stop = track.stop.bind(track);
    track.stop = function () {
      video.stopped = true;
      video.abort.abort();
      stop();
    };
    pumpMjpeg(video);
    return track;
  }

  async function createAudioTrack() {
    var ws = await openWebSocket('/ws/audio');
    var AudioContextImpl = window.AudioContext || window.webkitAudioContext;
    var ctx = new AudioContextImpl({ sampleRate: AUDIO_SAMPLE_RATE });
    startOnFirstInteraction(ctx);
    var destination = ctx.createMediaStreamDestination();
    var nextStart = ctx.currentTime + 0.08; // small jitter buffer

    ws.addEventListener('message', function (ev) {
      if (!(ev.data instanceof ArrayBuffer)) return;
      var samples = new Int16Array(ev.data);
      var frames = Math.floor(samples.length / AUDIO_CHANNELS);
      if (frames === 0) return;
      var buffer = ctx.createBuffer(AUDIO_CHANNELS, frames, AUDIO_SAMPLE_RATE);
      for (var ch = 0; ch < AUDIO_CHANNELS; ch++) {
        var data = buffer.getChannelData(ch);
        for (var i = 0; i < frames; i++) data[i] = samples[i * AUDIO_CHANNELS + ch] / 32768;
      }
      var source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(destination);
      if (nextStart < ctx.currentTime) nextStart = ctx.currentTime + 0.02;
      source.start(nextStart);
      nextStart += buffer.duration;
    });

    var track = destination.stream.getAudioTracks()[0];
    var stop = track.stop.bind(track);
    track.stop = function () {
      ws.close();
      ctx.close().catch(function () { /* already closed */ });
      stop();
    };
    return track;
  }

  // Auto-connect has no user gesture, so video starts muted (see getUserMedia).
  // Unmute and start audio on the first click or key.
  function startOnFirstInteraction(ctx) {
    var video = document.getElementById('video');
    ctx.resume().catch(function () { /* needs a gesture */ });
    function start() {
      document.removeEventListener('pointerdown', start, true);
      document.removeEventListener('keydown', start, true);
      ctx.resume().catch(function () { /* closed meanwhile */ });
      if (video) {
        video.muted = false;
        if (video.paused) video.play().catch(function () { /* no stream yet */ });
      }
    }
    document.addEventListener('pointerdown', start, true);
    document.addEventListener('keydown', start, true);
  }

  function idealDimension(value, fallback) {
    if (typeof value === 'number') return value;
    if (value && typeof value.ideal === 'number') return value.ideal;
    if (value && typeof value.exact === 'number') return value.exact;
    return fallback;
  }

  async function enumerateDevices() {
    var devices = [
      { deviceId: VIDEO_DEVICE_ID, groupId: DEVICE_GROUP_ID, kind: 'videoinput', label: 'NanoKVM-USB (network)' }
    ];
    try {
      var res = await fetch('/api/status', { cache: 'no-store' });
      var status = res.ok ? await res.json() : null;
      if (status && status.audio.enabled) {
        devices.push({ deviceId: AUDIO_DEVICE_ID, groupId: DEVICE_GROUP_ID, kind: 'audioinput', label: 'NanoKVM-USB (network audio)' });
      }
    } catch (e) { /* bridge unreachable - offer video only */ }
    return devices;
  }

  async function getUserMedia(constraints) {
    constraints = constraints || {};
    if (!constraints.video) {
      // Audio alone is only a permission probe; real audio comes with video.
      throw domException('NotAllowedError', 'audio-only capture is not supported');
    }
    var width = idealDimension(constraints.video.width, 1920);
    var height = idealDimension(constraints.video.height, 1080);

    try {
      await fetch('/api/video', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ width: width, height: height, client: CLIENT_ID })
      });
    } catch (e) {
      console.warn('nanokvm-bridge: could not set the capture resolution', e);
    }

    // Only muted media may autoplay before a gesture, and the app relies on autoplay.
    var videoElement = document.getElementById('video');
    if (videoElement) videoElement.muted = true;

    var stream = new MediaStream([createVideoTrack(width, height)]);
    if (constraints.audio) {
      try {
        stream.addTrack(await createAudioTrack());
      } catch (e) {
        console.warn('nanokvm-bridge: audio unavailable, continuing with video only', e);
      }
    }
    return stream;
  }

  // navigator.permissions: camera/microphone always "granted"

  function installPermissionsShim() {
    var permissions = navigator.permissions;
    var query = permissions && typeof permissions.query === 'function'
      ? permissions.query.bind(permissions)
      : function () { return Promise.reject(new TypeError('permissions API unavailable')); };
    var wrapped = function (descriptor) {
      var name = descriptor && descriptor.name;
      if (name === 'camera' || name === 'microphone') {
        return Promise.resolve({ name: name, state: 'granted', onchange: null });
      }
      return query(descriptor);
    };
    if (permissions) {
      Object.defineProperty(permissions, 'query', { configurable: true, writable: true, value: wrapped });
    } else {
      override('permissions', { query: wrapped });
    }
  }

  // Update banner

  function styled(tag, css, text) {
    var el = document.createElement(tag);
    el.setAttribute('style', css.join(';'));
    if (text) el.textContent = text;
    return el;
  }

  function dismissKey(version) {
    return 'nanokvm-bridge-update-dismissed-' + version;
  }

  function showUpdateBanner(info) {
    var banner = styled('div', [
      'position:fixed', 'top:12px', 'right:12px', 'z-index:2147483647', 'max-width:360px',
      'background:#1f1f1f', 'color:#f0f0f0', 'border:1px solid #434343', 'border-radius:8px',
      'padding:12px 14px', 'box-shadow:0 4px 16px rgba(0,0,0,0.4)',
      'font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif'
    ]);

    var close = styled('button', [
      'position:absolute', 'top:6px', 'right:8px', 'background:none', 'border:none',
      'color:#999', 'font-size:14px', 'cursor:pointer', 'padding:2px 4px'
    ], '\u00d7');
    close.setAttribute('aria-label', 'Dismiss');
    close.onclick = function () {
      try { sessionStorage.setItem(dismissKey(info.latest), '1'); } catch (e) { /* storage blocked */ }
      banner.remove();
    };

    var title = styled('div', ['padding-right:16px', 'margin-bottom:8px'],
      'NanoKVM-USB update available: v' + info.latest + ' (installed: v' + info.installed + ')');

    var status = styled('div', ['margin-top:6px', 'color:#ff9c6e']);

    var update = styled('button', [
      'background:#1668dc', 'color:#fff', 'border:none', 'border-radius:4px',
      'padding:5px 12px', 'cursor:pointer', 'font-size:13px', 'margin-right:8px'
    ], 'Update now');
    update.onclick = async function () {
      update.disabled = true;
      update.textContent = 'Updating...';
      status.textContent = '';
      try {
        var res = await fetch('/api/update', { method: 'POST' });
        var body = await res.json().catch(function () { return {}; });
        if (!res.ok || !body.ok) throw new Error(body.error || 'HTTP ' + res.status);
        status.style.color = '#95de64';
        status.textContent = 'Updated to v' + body.version + '. Reloading...';
        setTimeout(function () { location.reload(); }, 800);
      } catch (err) {
        update.disabled = false;
        update.textContent = 'Update now';
        status.textContent = 'Update failed: ' + err.message;
      }
    };

    var manual = styled('pre', [
      'display:none', 'white-space:pre-wrap', 'background:#141414', 'border-radius:4px',
      'padding:8px', 'margin:8px 0 0', 'font-size:12px', 'color:#d9d9d9', 'user-select:all'
    ], '# In place, same as "Update now":\n' +
       'curl -X POST ' + location.origin + '/api/update\n\n' +
       '# Or pin it in the image: set NANOKVM_USB_VERSION to ' + info.latest + '\n' +
       '# in docker-compose.yml, then: docker compose up -d --build');

    var toggle = styled('button', [
      'background:none', 'color:#91caff', 'border:none', 'cursor:pointer',
      'font-size:13px', 'text-decoration:underline', 'padding:5px 0'
    ], 'Show manual steps');
    toggle.onclick = function () {
      var hidden = manual.style.display === 'none';
      manual.style.display = hidden ? 'block' : 'none';
      toggle.textContent = hidden ? 'Hide manual steps' : 'Show manual steps';
    };

    [close, title, update, toggle, manual, status].forEach(function (el) { banner.appendChild(el); });
    document.body.appendChild(banner);
  }

  async function checkForUpdate() {
    try {
      var res = await fetch('/api/version', { cache: 'no-store' });
      var info = res.ok ? await res.json() : null;
      if (!info || !info.update_available) return;
      try {
        if (sessionStorage.getItem(dismissKey(info.latest))) return;
      } catch (e) { /* storage blocked - show it anyway */ }
      showUpdateBanner(info);
    } catch (e) { /* bridge unreachable - no banner */ }
  }

  // Auto-connect: picks the only video device and clicks "Select serial device",
  // once per dialog showing. DOM-driven, so a Sipeed UI change only disables it.

  function isShown(el) {
    return !!el && el.getClientRects().length > 0;
  }

  async function waitFor(find, timeoutMs) {
    var deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      var found = find();
      if (found) return found;
      await sleep(50);
    }
    return null;
  }

  // Keeps trying while the dialog is up: after a restart or replug the select
  // and its device list can take a few seconds to appear.
  async function pickOnlyVideoDevice(dialog) {
    var deadline = Date.now() + 15000;
    while (Date.now() < deadline && isShown(dialog)) {
      var select = dialog.querySelector('.ant-select');
      if (select && select.querySelector('.ant-select-selection-item')) return;
      if (select) {
        var selector = select.querySelector('.ant-select-selector');
        var toggle = function () { selector.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })); };
        toggle();
        var options = await waitFor(function () {
          var found = document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option');
          return found.length ? found : null;
        }, 1500);
        if (options && options.length === 1) {
          options[0].click();
          return;
        }
        toggle();
        if (options) return; // several devices: leave it to the user
      }
      await sleep(1000);
    }
  }

  async function clickSerialButton(dialog) {
    // The dialog's only button; it turns primary once the port is connected.
    var button = await waitFor(function () {
      var found = dialog.querySelector('button');
      return found && !found.classList.contains('ant-btn-loading') ? found : null;
    }, 2000);
    if (button && !button.classList.contains('ant-btn-primary')) button.click();
  }

  function installAutoConnect() {
    var handled = false;
    var running = false;
    async function onDomChange() {
      var dialog = document.querySelector('.ant-modal');
      if (!isShown(dialog)) {
        handled = false;
        return;
      }
      if (handled || running) return;
      handled = running = true;
      try {
        await sleep(300); // let the dialog finish rendering and list devices
        await pickOnlyVideoDevice(dialog);
        await clickSerialButton(dialog);
      } catch (e) {
        console.warn('nanokvm-bridge: auto-connect failed; connect manually', e);
      } finally {
        running = false;
      }
    }
    new MutationObserver(onDomChange).observe(document.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style']
    });
    onDomChange();
  }

  // Touch keyboard: a phone can't open its keyboard over a video, so a button
  // focuses a hidden textarea and what's typed there is replayed as key presses.

  // A layout file (web/layouts/*.json) maps characters to strokes: space-separated
  // key codes, with "S+" holding Shift and "G+" holding AltGr.
  function buildTable(layout) {
    var table = { ' ': 'Space', '\n': 'Enter', '\t': 'Tab' };
    if (layout.latin !== false) {
      for (var i = 0; i < 26; i++) {
        var letter = String.fromCharCode(97 + i);
        table[letter] = 'Key' + letter.toUpperCase();
        table[letter.toUpperCase()] = 'S+Key' + letter.toUpperCase();
      }
    }
    for (var d = 0; d < 10; d++) table[String(d)] = 'Digit' + d;
    Object.keys(layout.keys).forEach(function (ch) { table[ch] = layout.keys[ch]; });
    // Each accented vowel is its dead key, then the plain vowel.
    Object.keys(layout.dead || {}).forEach(function (dead) {
      var accented = Array.from(layout.dead[dead]);
      Array.from('aeiouAEIOU').forEach(function (vowel, i) {
        if (!(accented[i] in table) && table[vowel]) table[accented[i]] = dead + ' ' + table[vowel];
      });
    });
    return table;
  }

  function loadLayout(name) {
    return fetch('/layouts/' + encodeURIComponent(name) + '.json', { cache: 'no-store' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  // Phone keyboards autocorrect to typographic quotes.
  var SMART_PUNCTUATION = { '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"' };

  // The app reads only event.code from document key events, so these reach the dongle like real keys.
  function sendKey(type, code) {
    document.dispatchEvent(new KeyboardEvent(type, { code: code, bubbles: true, cancelable: true }));
  }

  var typing = Promise.resolve();

  function tap(stroke, held) {
    var parts = stroke.split('+');
    var code = parts.pop();
    var mods = held.concat(parts.map(function (p) { return p === 'S' ? 'ShiftLeft' : 'AltRight'; }));
    typing = typing.then(async function () {
      mods.forEach(function (m) { sendKey('keydown', m); });
      sendKey('keydown', code);
      await sleep(20);
      sendKey('keyup', code);
      mods.slice().reverse().forEach(function (m) { sendKey('keyup', m); });
      await sleep(20);
    });
  }

  function installTouchKeyboard(layout, table) {
    var sticky = {};

    function takeSticky() {
      var held = Object.keys(sticky).filter(function (code) { return sticky[code].on; });
      held.forEach(function (code) { sticky[code].on = false; sticky[code].button.style.background = '#333'; });
      return held;
    }

    function typeText(text) {
      Array.from(text).forEach(function (ch) {
        ch = SMART_PUNCTUATION[ch] || ch;
        var strokes = table[ch];
        if (!strokes) {
          console.warn('nanokvm-bridge: no key for ' + JSON.stringify(ch) + ' in ' + layout);
          return;
        }
        var held = takeSticky();
        strokes.split(' ').forEach(function (stroke) { tap(stroke, held); });
      });
    }

    // One character stays in the textarea so Backspace always has something to delete.
    var SENTINEL = ' ';
    var input = styled('textarea', [
      'position:fixed', 'left:0', 'top:0', 'width:1px', 'height:1px', 'opacity:0',
      'font-size:16px', 'border:0', 'padding:0', 'resize:none'
    ]);
    ['autocomplete', 'autocorrect', 'autocapitalize'].forEach(function (a) { input.setAttribute(a, 'off'); });
    input.spellcheck = false;
    var last = SENTINEL;
    var composing = false;

    function reset() {
      input.value = last = SENTINEL;
      input.setSelectionRange(1, 1);
    }

    // Keep the app from handling the textarea's own key and composition events.
    ['keydown', 'keyup', 'keypress', 'compositionstart', 'compositionupdate', 'compositionend'].forEach(function (type) {
      input.addEventListener(type, function (e) { e.stopPropagation(); });
    });
    input.addEventListener('compositionstart', function () { composing = true; });
    input.addEventListener('compositionend', function () { composing = false; });
    input.addEventListener('input', function () {
      var now = input.value;
      var same = 0;
      while (same < last.length && same < now.length && last[same] === now[same]) same++;
      for (var n = last.length - same; n > 0; n--) tap('Backspace', takeSticky());
      typeText(now.slice(same));
      last = now;
      if (!composing && (now.length === 0 || now.length > 64)) reset();
    });

    var bar = styled('div', [
      'position:fixed', 'left:0', 'top:0', 'z-index:2147483646', 'display:none', 'box-sizing:border-box',
      'flex-wrap:wrap', 'gap:4px', 'padding:4px', 'background:#1f1f1f', 'border-top:1px solid #434343',
      'transform-origin:0 0'
    ]);

    function barButton(label, onPress) {
      var button = styled('button', [
        'flex:1 0 auto', 'min-width:40px', 'height:36px', 'background:#333', 'color:#f0f0f0',
        'border:1px solid #555', 'border-radius:4px', 'font-size:14px', 'padding:0 6px'
      ], label);
      // Keep the focus (and the phone's keyboard) on the textarea.
      button.addEventListener('pointerdown', function (e) { e.preventDefault(); });
      button.addEventListener('click', onPress);
      bar.appendChild(button);
      return button;
    }

    [['Esc', 'Escape'], ['Tab', 'Tab']].forEach(function (k) {
      barButton(k[0], function () { tap(k[1], takeSticky()); });
    });
    [['Ctrl', 'ControlLeft'], ['Alt', 'AltLeft'], ['Win', 'MetaLeft']].forEach(function (k) {
      var entry = { on: false };
      entry.button = barButton(k[0], function () {
        entry.on = !entry.on;
        entry.button.style.background = entry.on ? '#1668dc' : '#333';
      });
      sticky[k[1]] = entry;
    });
    [['\u2190', 'ArrowLeft'], ['\u2191', 'ArrowUp'], ['\u2193', 'ArrowDown'], ['\u2192', 'ArrowRight'], ['Del', 'Delete']].forEach(function (k) {
      barButton(k[0], function () { tap(k[1], takeSticky()); });
    });
    barButton('Ctrl+Alt+Del', function () { takeSticky(); tap('Delete', ['ControlLeft', 'AltLeft']); });
    barButton('\u00d7', function () { input.blur(); });

    var open = styled('button', [
      'position:fixed', 'left:0', 'top:0', 'z-index:2147483646', 'width:48px', 'height:48px',
      'border-radius:24px', 'background:#1668dc', 'color:#fff', 'border:none', 'font-size:22px',
      'box-shadow:0 2px 8px rgba(0,0,0,0.5)', 'transform-origin:0 0'
    ], '\u2328');
    open.setAttribute('aria-label', 'Keyboard');
    open.addEventListener('click', function () {
      reset();
      input.focus({ preventScroll: true });
    });

    // Pinned to what's on screen (the visual viewport) and scaled against pinch
    // zoom, so the controls keep their size and the textarea never pulls the
    // view away when the browser scrolls it into sight.
    function place() {
      var vv = window.visualViewport || { offsetLeft: 0, offsetTop: 0, width: innerWidth, height: innerHeight, scale: 1 };
      var k = 1 / vv.scale;
      bar.style.width = vv.width * vv.scale + 'px';
      bar.style.transform = 'scale(' + k + ')';
      bar.style.left = vv.offsetLeft + 'px';
      bar.style.top = vv.offsetTop + vv.height - bar.offsetHeight * k + 'px';
      open.style.transform = 'scale(' + k + ')';
      open.style.left = vv.offsetLeft + vv.width - 64 * k + 'px';
      open.style.top = vv.offsetTop + vv.height - 64 * k + 'px';
      input.style.left = vv.offsetLeft + 'px';
      input.style.top = vv.offsetTop + 'px';
    }
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', place);
      window.visualViewport.addEventListener('scroll', place);
    }
    input.addEventListener('focus', function () {
      bar.style.display = 'flex';
      open.style.display = 'none';
      place();
    });
    input.addEventListener('blur', function () {
      bar.style.display = 'none';
      open.style.display = '';
      takeSticky();
      place();
    });

    [input, bar, open].forEach(function (el) { document.body.appendChild(el); });
    place();
  }

  // The app gives the video a 640x360 minimum, which overflows a phone held upright.
  function fitSmallScreens() {
    var style = document.createElement('style');
    style.textContent =
      '@media (max-width: 639px), (max-height: 359px) { #video { min-width: 0 !important; min-height: 0 !important; } }' +
      // On phones the video sits at the top, next to the keyboard's text, instead of centered.
      '@media (pointer: coarse) and (hover: none) { #root > div { justify-content: flex-start !important; } }';
    document.head.appendChild(style);
    // Shrink the page above the phone's keyboard instead of letting it cover the video.
    var meta = document.querySelector('meta[name="viewport"]');
    if (meta && meta.content.indexOf('interactive-widget') === -1) meta.content += ', interactive-widget=resizes-content';
  }

  function setUpTouchKeyboard() {
    // Phones and tablets only: a touch laptop also has a mouse and a real keyboard.
    if (!window.matchMedia('(pointer: coarse) and (hover: none)').matches) return;
    fetch('/api/status', { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (status) { return status.keyboard.layout; })
      .catch(function () { return 'en-US'; })
      .then(function (name) {
        return loadLayout(name).catch(function (e) {
          console.warn('nanokvm-bridge: no keyboard layout ' + name + ', using en-US', e);
          name = 'en-US';
          return loadLayout(name);
        }).then(function (layout) { installTouchKeyboard(name, buildTable(layout)); });
      })
      .catch(function (e) { console.warn('nanokvm-bridge: touch keyboard unavailable', e); });
  }

  // Install

  function override(name, value) {
    Object.defineProperty(navigator, name, { configurable: true, writable: true, value: value });
  }

  override('serial', remoteSerial);
  override('mediaDevices', { enumerateDevices: enumerateDevices, getUserMedia: getUserMedia });
  installPermissionsShim();

  window.addEventListener('DOMContentLoaded', function () {
    if (new URLSearchParams(location.search).get('autoconnect') !== '0') installAutoConnect();
    fitSmallScreens();
    setUpTouchKeyboard();
    // Let the app connect to the device before asking about updates.
    setTimeout(checkForUpdate, 1000);
  });
})();
