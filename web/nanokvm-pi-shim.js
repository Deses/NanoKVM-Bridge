// Loaded before the NanoKVM-USB app: swaps navigator.serial, mediaDevices and camera/mic
// permissions for bridge-backed versions. No real device API, so plain http:// works.
(function () {
  'use strict';

  var FAKE_VIDEO_ID = 'nanokvm-pi-video';
  var FAKE_AUDIO_ID = 'nanokvm-pi-audio';
  var FAKE_GROUP_ID = 'nanokvm-pi-group';
  var DOUBLE_CRLF = new Uint8Array([13, 10, 13, 10]);

  function wsUrl(path) {
    var proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return proto + '://' + window.location.host + path;
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function domException(name, message) {
    try {
      return new DOMException(message, name);
    } catch (e) {
      var err = new Error(message);
      err.name = name;
      return err;
    }
  }

  function concatBytes(a, b) {
    var out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function indexOfBytes(haystack, needle, fromIndex) {
    if (needle.length === 0) return -1;
    var limit = haystack.length - needle.length;
    outer: for (var i = Math.max(fromIndex, 0); i <= limit; i++) {
      for (var j = 0; j < needle.length; j++) {
        if (haystack[i + j] !== needle[j]) continue outer;
      }
      return i;
    }
    return -1;
  }

  async function fetchStatus() {
    try {
      var res = await fetch('/api/status', { cache: 'no-store' });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  // navigator.serial

  var serialListeners = new Set();
  var currentPort = null;

  function fireSerialDisconnect(port) {
    var event = { type: 'disconnect', target: port };
    serialListeners.forEach(function (handler) {
      try { handler(event); } catch (e) { console.error('nanokvm-pi: serial listener error', e); }
    });
  }

  function RemotePort() {
    this._ws = null;
    this.readable = null;
    this.writable = null;
  }

  RemotePort.prototype.open = async function (options) {
    var port = this;
    var ws = new WebSocket(wsUrl('/ws/serial'));
    ws.binaryType = 'arraybuffer';

    await new Promise(function (resolve, reject) {
      ws.addEventListener('open', function onOpen() { resolve(); }, { once: true });
      ws.addEventListener('error', function onError() {
        reject(domException('NetworkError', 'failed to reach the NanoKVM-Pi bridge (/ws/serial)'));
      }, { once: true });
    });

    port._ws = ws;

    port.readable = new ReadableStream({
      start: function (controller) {
        ws.addEventListener('message', function (ev) {
          if (ev.data instanceof ArrayBuffer) {
            controller.enqueue(new Uint8Array(ev.data));
          }
        });
        ws.addEventListener('close', function () {
          try { controller.close(); } catch (e) { /* already closed */ }
          fireSerialDisconnect(port);
        });
        ws.addEventListener('error', function () {
          try { controller.error(domException('NetworkError', 'serial websocket error')); } catch (e) { /* noop */ }
        });
      },
      cancel: function () {
        try { ws.close(); } catch (e) { /* noop */ }
      }
    });

    port.writable = new WritableStream({
      write: function (chunk) {
        ws.send(chunk);
      },
      close: function () {
        try { ws.close(); } catch (e) { /* noop */ }
      },
      abort: function () {
        try { ws.close(); } catch (e) { /* noop */ }
      }
    });
  };

  RemotePort.prototype.close = async function () {
    if (this._ws) {
      try { this._ws.close(); } catch (e) { /* noop */ }
    }
  };

  var fakeSerial = {
    requestPort: async function () {
      if (!currentPort) currentPort = new RemotePort();
      return currentPort;
    },
    getPorts: async function () {
      return currentPort ? [currentPort] : [];
    },
    addEventListener: function (type, handler) {
      if (type === 'disconnect') serialListeners.add(handler);
    },
    removeEventListener: function (type, handler) {
      if (type === 'disconnect') serialListeners.delete(handler);
    }
  };

  // navigator.mediaDevices

  function createCanvasVideoTrack(canvas) {
    var stream = canvas.captureStream(0);
    var track = stream.getVideoTracks()[0];
    var useRequestFrame = typeof track.requestFrame === 'function';
    if (!useRequestFrame) {
      // No on-demand frames in this browser: capture at a fixed rate instead.
      stream = canvas.captureStream(30);
      track = stream.getVideoTracks()[0];
    }
    return { stream: stream, track: track, useRequestFrame: useRequestFrame };
  }

  async function pumpMjpeg(canvas, ctx, videoHandle, state) {
    while (!state.stopped) {
      try {
        var res = await fetch('/stream', { signal: state.abortController.signal, cache: 'no-store' });
        if (!res.body) throw new Error('stream response has no body');

        var contentType = res.headers.get('content-type') || '';
        var boundaryMatch = /boundary=("?)([^;"]+)\1/i.exec(contentType);
        var boundary = boundaryMatch ? boundaryMatch[2] : 'boundarydonotcross';
        var boundaryBytes = new TextEncoder().encode('--' + boundary);

        var reader = res.body.getReader();
        var buffer = new Uint8Array(0);

        while (!state.stopped) {
          var chunkResult = await reader.read();
          if (chunkResult.done) break;
          buffer = concatBytes(buffer, chunkResult.value);

          var progress = true;
          while (progress) {
            progress = false;
            var boundaryIndex = indexOfBytes(buffer, boundaryBytes, 0);
            if (boundaryIndex === -1) break;

            var headerStart = boundaryIndex + boundaryBytes.length;
            var headerEnd = indexOfBytes(buffer, DOUBLE_CRLF, headerStart);
            if (headerEnd === -1) break; // headers not fully buffered yet

            var headerText = new TextDecoder().decode(buffer.slice(headerStart, headerEnd));
            var lengthMatch = /Content-Length:\s*(\d+)/i.exec(headerText);
            if (!lengthMatch) {
              // Malformed/unexpected part; skip past these headers and resync.
              buffer = buffer.slice(headerEnd + 4);
              progress = true;
              continue;
            }

            var frameLength = parseInt(lengthMatch[1], 10);
            var frameStart = headerEnd + 4;
            var frameEnd = frameStart + frameLength;
            if (buffer.length < frameEnd) break; // frame not fully buffered yet

            var frameBytes = buffer.slice(frameStart, frameEnd);
            buffer = buffer.slice(frameEnd);
            progress = true;

            try {
              var bitmap = await createImageBitmap(new Blob([frameBytes], { type: 'image/jpeg' }));
              if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
                canvas.width = bitmap.width;
                canvas.height = bitmap.height;
              }
              ctx.drawImage(bitmap, 0, 0);
              bitmap.close();
              if (videoHandle.useRequestFrame) {
                videoHandle.track.requestFrame();
              }
            } catch (drawErr) {
              // Corrupt/partial JPEG frame - drop it and keep going.
            }
          }

          // Far behind (slow decode, paused tab): keep only from the latest boundary.
          if (buffer.length > 5 * 1024 * 1024) {
            var recentBoundary = indexOfBytes(buffer, boundaryBytes, buffer.length - 1024 * 1024);
            buffer = recentBoundary === -1 ? new Uint8Array(0) : buffer.slice(recentBoundary);
          }
        }
      } catch (err) {
        if (state.stopped) return;
        console.warn('nanokvm-pi: video stream interrupted, retrying', err);
        await sleep(500);
      }
    }
  }

  async function createRemoteAudioTrack() {
    var ws = new WebSocket(wsUrl('/ws/audio'));
    ws.binaryType = 'arraybuffer';

    await new Promise(function (resolve, reject) {
      ws.addEventListener('open', function onOpen() { resolve(); }, { once: true });
      ws.addEventListener('error', function onError() {
        reject(domException('NetworkError', 'failed to reach the NanoKVM-Pi bridge (/ws/audio)'));
      }, { once: true });
      ws.addEventListener('close', function onClose() {
        reject(domException('NetworkError', 'audio websocket closed before opening (is AUDIO=on set?)'));
      }, { once: true });
    });

    var AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    var sampleRate = 48000;
    var channels = 2;
    var audioCtx = new AudioContextCtor({ sampleRate: sampleRate });
    var destination = audioCtx.createMediaStreamDestination();
    var nextStartTime = audioCtx.currentTime + 0.08; // small jitter buffer

    ws.addEventListener('message', function (ev) {
      if (!(ev.data instanceof ArrayBuffer)) return;
      var samples = new Int16Array(ev.data);
      var frameCount = Math.floor(samples.length / channels);
      if (frameCount <= 0) return;

      var audioBuffer = audioCtx.createBuffer(channels, frameCount, sampleRate);
      for (var ch = 0; ch < channels; ch++) {
        var channelData = audioBuffer.getChannelData(ch);
        for (var i = 0; i < frameCount; i++) {
          channelData[i] = samples[i * channels + ch] / 32768;
        }
      }

      var source = audioCtx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(destination);

      var now = audioCtx.currentTime;
      if (nextStartTime < now) nextStartTime = now + 0.02;
      source.start(nextStartTime);
      nextStartTime += audioBuffer.duration;
    });

    var track = destination.stream.getAudioTracks()[0];
    var originalStop = track.stop.bind(track);
    track.stop = function () {
      try { ws.close(); } catch (e) { /* noop */ }
      try { audioCtx.close(); } catch (e) { /* noop */ }
      originalStop();
    };
    return track;
  }

  async function fakeEnumerateDevices() {
    var status = await fetchStatus();
    var devices = [
      { deviceId: FAKE_VIDEO_ID, kind: 'videoinput', label: 'NanoKVM-USB via Pi', groupId: FAKE_GROUP_ID, toJSON: function () { return this; } }
    ];
    if (status && status.audio && status.audio.enabled) {
      devices.push({ deviceId: FAKE_AUDIO_ID, kind: 'audioinput', label: 'NanoKVM-USB via Pi (audio)', groupId: FAKE_GROUP_ID, toJSON: function () { return this; } });
    }
    return devices;
  }

  async function fakeGetUserMedia(constraints) {
    constraints = constraints || {};
    var wantsVideo = !!constraints.video;
    var wantsAudio = !!constraints.audio;

    if (!wantsVideo) {
      // Audio alone is only a permission probe; real audio comes with video.
      throw domException('NotAllowedError', 'NanoKVM-Pi only supports video(+audio) capture, not audio-only');
    }

    var width = 1920;
    var height = 1080;
    if (constraints.video && constraints.video.width && constraints.video.width.ideal) {
      width = constraints.video.width.ideal;
    }
    if (constraints.video && constraints.video.height && constraints.video.height.ideal) {
      height = constraints.video.height.ideal;
    }

    try {
      await fetch('/api/video', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ width: width, height: height })
      });
    } catch (e) {
      console.warn('nanokvm-pi: failed to request resolution change', e);
    }

    var canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    var ctx = canvas.getContext('2d', { alpha: false });

    var videoHandle = createCanvasVideoTrack(canvas);
    var state = { stopped: false, abortController: new AbortController() };
    pumpMjpeg(canvas, ctx, videoHandle, state);

    var originalStop = videoHandle.track.stop.bind(videoHandle.track);
    videoHandle.track.stop = function () {
      state.stopped = true;
      try { state.abortController.abort(); } catch (e) { /* noop */ }
      originalStop();
    };

    var outStream = new MediaStream();
    outStream.addTrack(videoHandle.track);

    if (wantsAudio) {
      try {
        var audioTrack = await createRemoteAudioTrack();
        outStream.addTrack(audioTrack);
      } catch (e) {
        console.warn('nanokvm-pi: audio unavailable, continuing with video only', e);
      }
    }

    return outStream;
  }

  var fakeMediaDevices = {
    enumerateDevices: fakeEnumerateDevices,
    getUserMedia: fakeGetUserMedia
  };

  // navigator.permissions: camera/microphone always "granted"

  function installPermissionsShim() {
    var originalPermissions = navigator.permissions;
    if (!originalPermissions || typeof originalPermissions.query !== 'function') return;

    var originalQuery = originalPermissions.query.bind(originalPermissions);
    var wrappedQuery = function (descriptor) {
      var name = descriptor && descriptor.name;
      if (name === 'camera' || name === 'microphone') {
        return Promise.resolve({ state: 'granted', onchange: null, addEventListener: function () {}, removeEventListener: function () {} });
      }
      return originalQuery(descriptor);
    };

    try {
      Object.defineProperty(originalPermissions, 'query', { value: wrappedQuery, writable: true, configurable: true });
    } catch (e) {
      console.warn('nanokvm-pi: could not shim navigator.permissions.query', e);
    }
  }

  // Install

  function defineOwnProperty(target, name, value) {
    try {
      Object.defineProperty(target, name, { value: value, configurable: true, writable: true });
    } catch (e) {
      console.error('nanokvm-pi: failed to install shim for navigator.' + name, e);
    }
  }

  defineOwnProperty(navigator, 'serial', fakeSerial);
  defineOwnProperty(navigator, 'mediaDevices', fakeMediaDevices);
  installPermissionsShim();

  console.info('nanokvm-pi: shim installed (serial + mediaDevices + permissions)');
})();
