(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const el = (tag, className = "", text = "") => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };

  const baseName = (name) => name.replace(/\.[^.]+$/, "");

  const formatBytes = (bytes) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  };

  const formatDuration = (seconds) => {
    const total = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(total / 60);
    const remainder = total % 60;
    return `${minutes}:${String(remainder).padStart(2, "0")}`;
  };

  const download = (filename, text) => {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const copyFrom = async (textarea, button) => {
    if (!textarea.value.trim()) return;
    try {
      await navigator.clipboard.writeText(textarea.value);
    } catch {
      textarea.select();
      document.execCommand("copy");
    }
    const original = button.textContent;
    button.textContent = "Copied";
    button.classList.add("is-copied");
    setTimeout(() => {
      button.textContent = original;
      button.classList.remove("is-copied");
    }, 1200);
  };

  const readError = (payload, status) => {
    if (payload && typeof payload.detail === "string") return payload.detail;
    if (payload && typeof payload.error === "string") return payload.error;
    return `The server returned ${status}.`;
  };

  /* ---------- Tabs ---------- */

  const panels = { live: $("#panel-live"), files: $("#panel-files") };
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.tab;
      $$(".tab").forEach((item) => {
        const active = item === tab;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", String(active));
      });
      Object.entries(panels).forEach(([key, panel]) => {
        const active = key === name;
        panel.hidden = !active;
        panel.classList.toggle("is-active", active);
      });
    });
  });

  /* ---------- Live transcription ---------- */

  const TARGET_RATE = 16000;
  const BrowserRecognition =
    window.SpeechRecognition || window.webkitSpeechRecognition;
  const browserSupported = Boolean(BrowserRecognition);
  const serverSupported = Boolean(
    navigator.mediaDevices &&
      navigator.mediaDevices.getUserMedia &&
      (window.AudioContext || window.webkitAudioContext)
  );

  const micBtn = $("#micBtn");
  const micOrb = $("#micOrb");
  const liveStatus = $("#liveStatus");
  const liveStatusText = $("#liveStatusText");
  const liveHint = $("#liveHint");
  const liveEngine = $("#liveEngine");
  const liveLang = $("#liveLang");
  const liveTranscript = $("#liveTranscript");
  const liveInterim = $("#liveInterim");
  const liveWordCount = $("#liveWordCount");

  const defaultHint = browserSupported
    ? "Click the microphone and speak. Browser mode uses Chrome's speech service; Server mode streams audio to this app."
    : "Click the microphone and speak. Audio streams to this app; microphone access needs HTTPS or localhost.";

  let wantListening = false;
  let finalText = "";
  let restartTimer = null;

  // Browser engine
  let recognition = null;

  // Server engine
  let socket = null;
  let audioContext = null;
  let mediaStream = null;
  let sourceNode = null;
  let processorNode = null;
  let muteNode = null;
  let resampler = null;

  const updateWordCount = () => {
    const text = liveTranscript.value.trim();
    liveWordCount.textContent = text ? `${text.split(/\s+/).length} words` : "";
  };

  const setLiveState = (mode, text, hint) => {
    micOrb.classList.toggle("is-listening", mode === "listening");
    micOrb.classList.toggle("is-error", mode === "error");
    liveStatus.classList.toggle("is-listening", mode === "listening");
    liveStatus.classList.toggle("is-error", mode === "error");
    liveStatusText.textContent = text;
    micBtn.setAttribute("aria-pressed", String(mode === "listening"));
    micBtn.setAttribute(
      "aria-label",
      mode === "listening" ? "Stop live dictation" : "Start live dictation"
    );
    if (hint !== undefined) {
      liveHint.textContent = hint;
      liveHint.classList.toggle("is-error", mode === "error");
    } else if (mode !== "error") {
      liveHint.textContent = defaultHint;
      liveHint.classList.remove("is-error");
    }
  };

  const appendFinal = (text) => {
    const clean = text.replace(/\s+/g, " ").trim();
    if (!clean) return;
    finalText = finalText ? `${finalText} ${clean}` : clean;
    liveTranscript.value = finalText;
    liveTranscript.scrollTop = liveTranscript.scrollHeight;
    updateWordCount();
  };

  const failLive = (code) => {
    const messages = {
      "not-allowed":
        "Microphone access was blocked. Allow it in the address bar, then try again.",
      "audio-capture": "No microphone was found. Connect one and try again.",
      "service-not-allowed":
        "The browser blocked its speech service. Use Chrome, or switch Engine to Server.",
      "language-not-supported":
        "The browser's speech service does not support this language. Try Server mode.",
      "no-browser-asr":
        "This browser has no speech recognition. Use Chrome, or switch Engine to Server.",
      insecure:
        "Live dictation needs HTTPS or localhost. Open the app on http://127.0.0.1:8765.",
      network:
        "Lost the speech service. Check the connection and try again.",
      "start-failed":
        "Could not start the microphone. Check browser permissions and try again.",
    };
    wantListening = false;
    clearTimeout(restartTimer);
    stopBrowserEngine();
    stopServerEngine();
    liveInterim.textContent = "";
    setLiveState(
      "error",
      "Stopped",
      messages[code] || `Live transcription stopped (${code}).`
    );
  };

  /* ----- Browser engine (Chrome Web Speech) ----- */

  const stopBrowserEngine = () => {
    clearTimeout(restartTimer);
    if (!recognition) return;
    const current = recognition;
    recognition = null;
    try {
      current.abort();
    } catch {
      /* ignore */
    }
  };

  const spawnBrowserRecognition = () => {
    if (!wantListening) return;
    if (recognition) {
      try {
        recognition.abort();
      } catch {
        /* ignore */
      }
      recognition = null;
    }

    const rec = new BrowserRecognition();
    recognition = rec;
    rec.lang = liveLang.value;
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      if (recognition !== rec) return;
      setLiveState("listening", "Listening…");
    };

    rec.onresult = (event) => {
      if (recognition !== rec) return;
      let interim = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const chunk = result[0] ? result[0].transcript : "";
        if (result.isFinal) appendFinal(chunk);
        else interim += chunk;
      }
      liveInterim.textContent = interim.trim();
    };

    rec.onerror = (event) => {
      if (recognition !== rec) return;
      if (event.error === "aborted" || event.error === "no-speech") return;
      failLive(event.error);
    };

    rec.onend = () => {
      if (recognition !== rec) return;
      liveInterim.textContent = "";
      if (!wantListening) return;
      // Chrome ends the session at every pause; start a fresh one.
      clearTimeout(restartTimer);
      restartTimer = setTimeout(() => {
        if (wantListening) spawnBrowserRecognition();
      }, 250);
    };

    try {
      rec.start();
    } catch {
      failLive("start-failed");
    }
  };

  const startBrowserEngine = () => {
    setLiveState("listening", "Starting…");
    spawnBrowserRecognition();
  };

  /* ----- Server engine (WebSocket + PCM) ----- */

  const makeResampler = (inputRate) => {
    const step = inputRate / TARGET_RATE;
    let carry = new Float32Array(0);
    return {
      push(input) {
        const buffer = new Float32Array(carry.length + input.length);
        buffer.set(carry, 0);
        buffer.set(input, carry.length);

        const length = Math.max(0, Math.floor((buffer.length - 1) / step));
        const output = new Int16Array(length);
        for (let index = 0; index < length; index += 1) {
          const position = index * step;
          const left = Math.floor(position);
          const fraction = position - left;
          const right = Math.min(left + 1, buffer.length - 1);
          const sample = buffer[left] * (1 - fraction) + buffer[right] * fraction;
          output[index] = Math.max(
            -32768,
            Math.min(32767, Math.round(sample * 32767))
          );
        }
        carry = buffer.slice(Math.floor(length * step));
        return output;
      },
    };
  };

  const closeSocket = () => {
    if (!socket) return;
    const current = socket;
    socket = null;
    try {
      current.close();
    } catch {
      /* ignore */
    }
  };

  const stopCapture = () => {
    if (processorNode) {
      processorNode.onaudioprocess = null;
      processorNode.disconnect();
      processorNode = null;
    }
    if (sourceNode) {
      sourceNode.disconnect();
      sourceNode = null;
    }
    if (muteNode) {
      muteNode.disconnect();
      muteNode = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
    }
    if (audioContext) {
      audioContext.close().catch(() => {});
      audioContext = null;
    }
    resampler = null;
  };

  const stopServerEngine = () => {
    closeSocket();
    stopCapture();
  };

  const handleServerMessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }

    if (message.type === "ready") {
      setLiveState("listening", "Listening…");
    } else if (message.type === "speech") {
      liveInterim.textContent = "…";
    } else if (message.type === "final") {
      liveInterim.textContent = "";
      if (message.text) appendFinal(message.text);
    } else if (message.type === "error") {
      liveHint.textContent = message.message || "Transcription failed.";
      liveHint.classList.add("is-error");
    }
  };

  const startServerCapture = async () => {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (!wantListening) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
      return;
    }

    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    audioContext = new AudioContextCtor();
    await audioContext.resume();
    if (!wantListening) {
      stopCapture();
      return;
    }

    sourceNode = audioContext.createMediaStreamSource(mediaStream);
    processorNode = audioContext.createScriptProcessor(4096, 1, 1);
    resampler = makeResampler(audioContext.sampleRate);

    processorNode.onaudioprocess = (event) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const pcm = resampler.push(event.inputBuffer.getChannelData(0));
      if (pcm.length) socket.send(pcm.buffer);
    };

    // A zero-gain sink keeps the processor running without echoing the mic.
    muteNode = audioContext.createGain();
    muteNode.gain.value = 0;
    sourceNode.connect(processorNode);
    processorNode.connect(muteNode);
    muteNode.connect(audioContext.destination);
  };

  const startServerEngine = async () => {
    setLiveState("listening", "Connecting…");

    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const url = `${protocol}://${location.host}/api/v1/live?language=${encodeURIComponent(
      liveLang.value
    )}`;

    try {
      socket = new WebSocket(url);
    } catch {
      failLive("network");
      return;
    }
    socket.binaryType = "arraybuffer";

    socket.onopen = async () => {
      if (!wantListening) return;
      try {
        await startServerCapture();
      } catch (error) {
        failLive(
          error && error.name === "NotAllowedError" ? "not-allowed" : "audio-capture"
        );
      }
    };

    socket.onmessage = handleServerMessage;

    socket.onerror = () => {
      if (wantListening) failLive("network");
    };

    socket.onclose = () => {
      if (!wantListening) return;
      wantListening = false;
      stopCapture();
      setLiveState(
        "error",
        "Disconnected",
        "The live connection closed. Press the mic to reconnect."
      );
    };
  };

  /* ----- Shared controls ----- */

  const stopListening = () => {
    wantListening = false;
    clearTimeout(restartTimer);
    stopBrowserEngine();
    stopServerEngine();
    liveInterim.textContent = "";
    setLiveState("idle", "Ready");
    updateWordCount();
  };

  const startListening = async () => {
    if (wantListening) return;

    const engine = liveEngine.value;
    if (engine === "browser" && !browserSupported) {
      failLive("no-browser-asr");
      return;
    }
    if (engine === "server" && !serverSupported) {
      failLive(window.isSecureContext ? "audio-capture" : "insecure");
      return;
    }

    wantListening = true;
    finalText = liveTranscript.value.trim();

    if (engine === "browser") startBrowserEngine();
    else await startServerEngine();
  };

  // Disable engines this browser cannot run, and pick a sensible default.
  if (!browserSupported) {
    const option = liveEngine.querySelector('option[value="browser"]');
    if (option) option.disabled = true;
  }
  if (!serverSupported) {
    const option = liveEngine.querySelector('option[value="server"]');
    if (option) option.disabled = true;
  }
  liveEngine.value = browserSupported ? "browser" : "server";

  if (!browserSupported && !serverSupported) {
    micBtn.disabled = true;
    setLiveState(
      "error",
      "Unavailable",
      window.isSecureContext
        ? "This browser has no microphone support. Try Chrome."
        : "Live dictation needs HTTPS or localhost. Open http://127.0.0.1:8765."
    );
  } else {
    micBtn.addEventListener("click", () => {
      if (wantListening) stopListening();
      else void startListening();
    });
  }

  liveEngine.addEventListener("change", () => {
    if (!wantListening) return;
    stopListening();
    setTimeout(() => void startListening(), 300);
  });

  liveLang.addEventListener("change", () => {
    if (!wantListening) return;
    stopListening();
    setTimeout(() => void startListening(), 300);
  });

  liveTranscript.addEventListener("input", () => {
    if (!wantListening) finalText = liveTranscript.value;
    updateWordCount();
  });

  $("#liveCopy").addEventListener("click", (event) => {
    copyFrom(liveTranscript, event.currentTarget);
  });

  $("#liveDownload").addEventListener("click", () => {
    if (!liveTranscript.value.trim()) return;
    download("live-transcript.txt", `${liveTranscript.value.trim()}\n`);
  });

  $("#liveClear").addEventListener("click", () => {
    finalText = "";
    liveTranscript.value = "";
    liveInterim.textContent = "";
    updateWordCount();
  });

  window.addEventListener("beforeunload", () => {
    wantListening = false;
    clearTimeout(restartTimer);
    stopBrowserEngine();
    stopServerEngine();
  });

  /* ---------- File transcription ---------- */

  const dropZone = $("#dropZone");
  const fileInput = $("#fileInput");
  const fileList = $("#fileList");
  const emptyFiles = $("#emptyFiles");
  const transcribeBtn = $("#transcribeBtn");
  const clearFilesBtn = $("#clearFilesBtn");
  const fileLang = $("#fileLang");

  let files = [];
  let nextId = 1;
  let running = false;

  const statusLabel = (status) =>
    ({ queued: "Queued", working: "Transcribing", done: "Done", error: "Error" }[status] || status);

  const musicNoteSvg =
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6Z"/></svg>';

  const addFiles = (list) => {
    let added = 0;
    for (const file of list) {
      if (!file || !file.name) continue;
      const duplicate = files.some(
        (entry) =>
          entry.file.name === file.name &&
          entry.file.size === file.size &&
          entry.file.lastModified === file.lastModified
      );
      if (duplicate) continue;
      files.push({
        id: nextId,
        file,
        status: "queued",
        result: null,
        error: "",
      });
      nextId += 1;
      added += 1;
    }
    if (added) renderFiles();
  };

  const renderFiles = () => {
    fileList.replaceChildren();
    emptyFiles.hidden = files.length > 0;
    clearFilesBtn.disabled = files.length === 0 || running;
    fileLang.disabled = running;
    transcribeBtn.disabled =
      running || !files.some((entry) => entry.status === "queued" || entry.status === "error");
    transcribeBtn.textContent = running ? "Transcribing…" : "Transcribe all";

    for (const entry of files) {
      const card = el("article", "file-card");
      card.dataset.id = String(entry.id);

      const row = el("div", "file-row");
      const icon = el("div", "file-icon");
      icon.innerHTML = musicNoteSvg;
      const info = el("div", "file-info");

      const name = el("div", "file-name", entry.file.name);
      name.title = entry.file.name;
      const meta = el("div", "file-meta");
      meta.append(el("span", "", formatBytes(entry.file.size)));
      meta.append(el("span", `badge ${entry.status}`, statusLabel(entry.status)));
      if (entry.result) {
        meta.append(
          el(
            "span",
            "",
            `${formatDuration(entry.result.duration)} audio · ${entry.result.elapsed.toFixed(1)}s`
          )
        );
      }
      info.append(name, meta);

      const remove = el("button", "icon-btn", "×");
      remove.type = "button";
      remove.dataset.action = "remove";
      remove.title = "Remove";
      remove.disabled = running;
      remove.setAttribute("aria-label", `Remove ${entry.file.name}`);
      row.append(icon, info, remove);
      card.append(row);

      if (entry.result) {
        const resultBox = el("div", "file-result");
        resultBox.append(
          el(
            "div",
            "result-meta",
            `${formatDuration(entry.result.duration)} of audio · processed in ${entry.result.elapsed.toFixed(1)}s · ${entry.result.language}`
          )
        );

        const textarea = el("textarea");
        textarea.readOnly = true;
        textarea.dir = "auto";
        textarea.spellcheck = false;
        textarea.value = entry.result.text;
        resultBox.append(textarea);

        const actions = el("div", "result-actions");
        [
          ["copy", "Copy"],
          ["txt", "Download .txt"],
          ["srt", "Download .srt"],
        ].forEach(([action, label]) => {
          const button = el("button", "btn small", label);
          button.type = "button";
          button.dataset.action = action;
          if (action === "srt" && !entry.result.srt) button.disabled = true;
          actions.append(button);
        });
        resultBox.append(actions);
        card.append(resultBox);
      }

      if (entry.error) {
        card.append(el("div", "file-error", entry.error));
      }

      fileList.append(card);
    }
  };

  const processQueue = async () => {
    if (running) return;
    const queue = files.filter(
      (entry) => entry.status === "queued" || entry.status === "error"
    );
    if (!queue.length) return;

    running = true;
    renderFiles();

    for (const entry of queue) {
      entry.status = "working";
      entry.error = "";
      renderFiles();

      try {
        const form = new FormData();
        form.append("file", entry.file, entry.file.name);
        form.append("language", fileLang.value);

        const response = await fetch("/api/v1/transcribe", {
          method: "POST",
          body: form,
        });
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          /* handled below */
        }
        if (!response.ok) {
          throw new Error(readError(payload, response.status));
        }

        entry.result = payload;
        entry.status = "done";
      } catch (error) {
        entry.status = "error";
        entry.error = error.message || String(error);
      }

      renderFiles();
    }

    running = false;
    renderFiles();
  };

  dropZone.addEventListener("click", () => {
    if (!running) fileInput.click();
  });

  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!running) fileInput.click();
    }
  });

  fileInput.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((type) => {
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      event.stopPropagation();
      dropZone.classList.add("is-dragover");
    });
  });

  ["dragleave", "drop"].forEach((type) => {
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (type === "drop" && event.dataTransfer) addFiles(event.dataTransfer.files);
      dropZone.classList.remove("is-dragover");
    });
  });

  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => {
    if (!dropZone.contains(event.target)) event.preventDefault();
  });

  fileList.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const card = button.closest(".file-card");
    if (!card) return;
    const entry = files.find((item) => item.id === Number(card.dataset.id));
    if (!entry) return;

    const action = button.dataset.action;
    if (action === "remove") {
      if (running) return;
      files = files.filter((item) => item !== entry);
      renderFiles();
    } else if (action === "copy") {
      const textarea = card.querySelector("textarea");
      if (textarea) await copyFrom(textarea, button);
    } else if (action === "txt" && entry.result) {
      download(`${baseName(entry.file.name)}.txt`, `${entry.result.text}\n`);
    } else if (action === "srt" && entry.result) {
      download(`${baseName(entry.file.name)}.srt`, entry.result.srt);
    }
  });

  transcribeBtn.addEventListener("click", processQueue);

  clearFilesBtn.addEventListener("click", () => {
    if (running) return;
    files = [];
    renderFiles();
  });

  updateWordCount();
  renderFiles();
})();
