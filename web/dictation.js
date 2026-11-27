const microphoneErrors = {
  NotAllowedError: "Разрешите доступ к микрофону в настройках сайта и попробуйте ещё раз.",
  NotFoundError: "Микрофон не найден. Подключите его или введите вопрос текстом.",
  NotReadableError: "Микрофон недоступен. Проверьте, может ли браузер его использовать.",
};
const noSpeech = "Речь не распознана. Попробуйте продиктовать вопрос ещё раз.";
const recordingFailed = "Не удалось записать звук. Попробуйте ещё раз или введите вопрос текстом.";
const MAX_RECORDING_MS = 25000;
const SAMPLE_RATE = 16000;

function releaseMicrophone(session) {
  clearTimeout(session.timer);
  session.stream?.getTracks().forEach((track) => track.stop());
}

async function pcmFromRecording(recording) {
  const context = new OfflineAudioContext(1, 1, SAMPLE_RATE);
  const decoded = await context.decodeAudioData(await recording.arrayBuffer());
  const channel = decoded.getChannelData(0);
  const pcm = new DataView(new ArrayBuffer(channel.length * 2));
  for (let index = 0; index < channel.length; index++) {
    const sample = Math.max(-1, Math.min(1, channel[index]));
    pcm.setInt16(index * 2, sample * (sample < 0 ? 32768 : 32767), true);
  }
  return pcm.buffer;
}

export function bindDictation(button, status, question, onActivityChange) {
  const canRecord = navigator.mediaDevices?.getUserMedia && window.MediaRecorder && window.OfflineAudioContext;
  const unavailable = !window.isSecureContext
    ? "Для микрофона откройте сайт по HTTPS или на localhost."
    : !canRecord ? "Этот браузер не поддерживает запись с микрофона. Введите вопрос текстом." : "";
  let enabled = false;
  let current = null;

  function update(message) {
    if (message !== undefined) status.textContent = unavailable || message;
    button.disabled = Boolean(unavailable) || (current ? current.phase === "processing" : !enabled);
    button.dataset.active = String(Boolean(current));
    const label = current ? "Остановить диктовку" : "Диктовать вопрос";
    button.setAttribute("aria-label", label);
    button.title = label;
  }

  function finish(message) {
    const previous = current;
    current = null;
    previous.controller.abort();
    releaseMicrophone(previous);
    update(message);
    onActivityChange();
  }

  function reset() {
    if (current) finish("");
    else update("");
  }

  function insertTranscript(transcript) {
    const text = question.value;
    const separator = text && !/\s$/.test(text) ? " " : "";
    const room = Math.max(0, question.maxLength - text.length - separator.length);
    const addition = transcript.slice(0, room).replace(/[\uD800-\uDBFF]$/, "").trimEnd();
    if (addition) {
      question.value = text + separator + addition;
      question.setSelectionRange(question.value.length, question.value.length);
      question.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return addition.length < transcript.length
      ? `Вопрос ограничен ${question.maxLength} символами. Проверьте, поместился ли весь текст.`
      : "Проверьте распознанный текст и отправьте вопрос.";
  }

  function stopRecording(session) {
    if (current !== session || session.phase !== "recording") return;
    session.phase = "processing";
    update("Распознаём речь на стенде…");
    if (session.recorder.state !== "inactive") session.recorder.stop();
  }

  async function processRecording(session, chunks) {
    releaseMicrophone(session);
    if (current !== session) return;
    session.phase = "processing";
    update("Распознаём речь на стенде…");

    let pcm;
    try {
      const recording = new Blob(chunks, { type: session.recorder.mimeType });
      if (!recording.size) {
        finish(noSpeech);
        return;
      }
      pcm = await pcmFromRecording(recording);
    } catch {
      if (current === session) finish(recordingFailed);
      return;
    }
    if (current !== session) return;

    try {
      const response = await fetch("/api/dictation", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: pcm,
        signal: session.controller.signal,
      });
      const result = await response.json();
      if (current !== session) return;
      if (!response.ok) {
        finish(result.error.message);
        return;
      }
      const transcript = result.transcript.trim();
      finish(transcript ? insertTranscript(transcript) : noSpeech);
    } catch {
      if (current === session) finish("Не удалось получить ответ от стенда. Попробуйте ещё раз.");
    }
  }

  button.addEventListener("click", async () => {
    if (current) {
      if (current.phase === "starting") reset();
      else stopRecording(current);
      return;
    }
    if (!enabled || unavailable) return;

    const session = { phase: "starting", controller: new AbortController() };
    current = session;
    update("Ожидаем доступ к микрофону…");
    onActivityChange();
    try {
      session.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
      if (current !== session) {
        releaseMicrophone(session);
        return;
      }
      const recorder = new MediaRecorder(session.stream);
      const chunks = [];
      session.recorder = recorder;
      recorder.ondataavailable = (event) => chunks.push(event.data);
      recorder.onstop = () => processRecording(session, chunks);
      recorder.onerror = () => {
        if (current === session) finish(recordingFailed);
      };
      recorder.start();
      session.phase = "recording";
      update("Говорите. Нажмите кнопку ещё раз, чтобы закончить.");
      session.timer = setTimeout(() => stopRecording(session), MAX_RECORDING_MS);
    } catch (error) {
      if (current === session) finish(microphoneErrors[error.name] || recordingFailed);
    }
  });

  update("");
  return {
    get active() { return Boolean(current); },
    setEnabled(value) {
      enabled = value;
      update();
    },
    reset,
  };
}
