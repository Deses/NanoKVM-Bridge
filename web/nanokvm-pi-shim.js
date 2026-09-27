// Loaded before the NanoKVM-USB app: swaps navigator.serial, mediaDevices and camera/mic
// permissions for bridge-backed versions. No real device API, so plain http:// works.
(function () {
  'use strict';

  var VIDEO_DEVICE_ID = 'nanokvm-pi-video';
  var AUDIO_DEVICE_ID = 'nanokvm-pi-audio';
  var DEVICE_GROUP_ID = 'nanokvm-pi';
  var AUDIO_SAMPLE_RATE = 48000;
  var AUDIO_CHANNELS = 2;
  var RETRY_MS = 500;
  var MAX_STREAM_BUFFER = 8 * 1024 * 1024;
  var CRLF2 = new Uint8Array([13, 10, 13, 10]);

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
        reject(domException('NetworkError', 'could not connect to the NanoKVM-Pi bridge at ' + path));
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
        var res = await fetch('/stream', { signal: video.abort.signal, cache: 'no-store' });
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
        console.warn('nanokvm-pi: video stream interrupted, retrying', err);
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
    ctx.resume().catch(function () { /* resumes on the next user gesture */ });
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
      ctx.close();
      stop();
    };
    return track;
  }

  function idealDimension(value, fallback) {
    if (typeof value === 'number') return value;
    if (value && typeof value.ideal === 'number') return value.ideal;
    if (value && typeof value.exact === 'number') return value.exact;
    return fallback;
  }

  async function enumerateDevices() {
    var devices = [
      { deviceId: VIDEO_DEVICE_ID, groupId: DEVICE_GROUP_ID, kind: 'videoinput', label: 'NanoKVM-USB via Pi' }
    ];
    try {
      var res = await fetch('/api/status', { cache: 'no-store' });
      var status = res.ok ? await res.json() : null;
      if (status && status.audio.enabled) {
        devices.push({ deviceId: AUDIO_DEVICE_ID, groupId: DEVICE_GROUP_ID, kind: 'audioinput', label: 'NanoKVM-USB via Pi (audio)' });
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
        body: JSON.stringify({ width: width, height: height })
      });
    } catch (e) {
      console.warn('nanokvm-pi: could not set the capture resolution', e);
    }

    var stream = new MediaStream([createVideoTrack(width, height)]);
    if (constraints.audio) {
      try {
        stream.addTrack(await createAudioTrack());
      } catch (e) {
        console.warn('nanokvm-pi: audio unavailable, continuing with video only', e);
      }
    }
    return stream;
  }

  // navigator.permissions: camera/microphone always "granted"

  function installPermissionsShim() {
    var permissions = navigator.permissions;
    if (!permissions || typeof permissions.query !== 'function') return;
    var query = permissions.query.bind(permissions);
    Object.defineProperty(permissions, 'query', {
      configurable: true,
      writable: true,
      value: function (descriptor) {
        var name = descriptor && descriptor.name;
        if (name === 'camera' || name === 'microphone') {
          return Promise.resolve({ name: name, state: 'granted', onchange: null });
        }
        return query(descriptor);
      }
    });
  }

  // Update banner

  function styled(tag, css, text) {
    var el = document.createElement(tag);
    el.setAttribute('style', css.join(';'));
    if (text) el.textContent = text;
    return el;
  }

  function dismissKey(version) {
    return 'nanokvm-pi-update-dismissed-' + version;
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

  // Install

  function override(name, value) {
    Object.defineProperty(navigator, name, { configurable: true, writable: true, value: value });
  }

  override('serial', remoteSerial);
  override('mediaDevices', { enumerateDevices: enumerateDevices, getUserMedia: getUserMedia });
  installPermissionsShim();

  // Let the app start its own device handshake before asking about updates.
  window.addEventListener('DOMContentLoaded', function () {
    setTimeout(checkForUpdate, 1000);
  });
})();
