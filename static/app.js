const ids = [
  "signal", "signalText", "remoteSignal", "remoteSignalText", "urlForm",
  "videoUrl", "errorBanner", "updatedAt", "remoteSetup", "remoteSetupForm", "tvHost",
  "tvMac", "remoteTest", "remoteForget", "remoteHelp", "remoteRescan",
  "screenFrame", "nowThumbnail", "liveBug", "playbackState", "videoTitle",
  "videoId", "currentTime", "duration", "playButton", "seek", "railProgress",
  "railFuture", "railQueueLabel", "volume", "volumeValue", "queueCount",
  "queueList", "emptyQueue", "queueItemTemplate"
];
const el = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
const model = {
  data: null,
  socket: null,
  socketState: "loading",
  networkError: "",
  commandError: "",
  remoteError: "",
  queueKey: "",
  removingId: "",
  controlBusy: false,
  remoteBusy: false,
  configHydrated: false
};
const interacting = { seek: false, volume: false };
const failedThumbnails = new Set();
let reconnectTimer;
let errorTimer;

function formatTime(value) {
  const seconds = Math.max(0, Number(value) || 0);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = Math.floor(seconds % 60);
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function thumbnailUrl(videoId) {
  return `https://i.ytimg.com/vi/${encodeURIComponent(videoId)}/hqdefault.jpg`;
}

function setThumbnail(image, frame, videoId) {
  const nextId = failedThumbnails.has(videoId) ? "" : videoId;
  frame.classList.toggle("no-image", !nextId);
  image.hidden = !nextId;
  if (image.dataset.videoId === nextId) return;
  image.dataset.videoId = nextId;
  if (nextId) image.src = thumbnailUrl(nextId);
  else image.removeAttribute("src");
}

function thumbnailFailed(image, frame) {
  if (image.dataset.videoId) failedThumbnails.add(image.dataset.videoId);
  setThumbnail(image, frame, "");
}

function isPlaying(value) {
  return value === 1 || ["playing", "play"].includes(String(value).toLowerCase());
}

function renderStatus(backendError) {
  const error = model.commandError || model.networkError || backendError;
  let state = "reconnecting";
  let label = "Reconnecting";

  if (error) {
    state = "error";
    label = "Signal error";
  } else if (!model.data) {
    state = "loading";
    label = "Syncing signal";
  } else if (model.socketState === "open" && model.data.connected) {
    state = "connected";
    label = "YouTube linked";
  } else if (model.socketState === "open") {
    state = "idle";
    label = "YouTube offline";
  }

  el.signal.dataset.state = state;
  el.signalText.textContent = label;
  // Banner is only for actionable command errors; ambient connection state lives in the signal chips.
  const banner = model.commandError || model.remoteError || "";
  el.errorBanner.hidden = !banner;
  el.errorBanner.textContent = banner;
}

function renderRemote(remote) {
  const labels = {
    unconfigured: "Remote setup",
    approval_required: "Approval needed",
    paired: "Remote paired",
    ready: "Remote connected",
    waking: "TV waking",
    offline: "Remote offline"
  };
  const signalState = remote.status === "ready"
    ? "connected"
    : (remote.status === "offline" ? "error" : (remote.status === "waking" ? "reconnecting" : "idle"));
  const configured = Boolean(remote.configured);
  const error = model.remoteError || remote.error || "";

  el.remoteSignal.dataset.state = signalState;
  el.remoteSignalText.textContent = labels[remote.status] || "Remote unknown";
  el.remoteHelp.textContent = error || (
    remote.status === "approval_required"
      ? "Turn the TV on, test the connection, then approve “TV Command Center” on screen."
      : "Samsung control uses the local Wi-Fi API; settings stay on this Mac."
  );
  el.remoteHelp.classList.toggle("has-error", Boolean(error));

  if (!model.configHydrated && remote.host !== undefined) {
    el.tvHost.value = remote.host || "";
    el.tvMac.value = remote.mac || "";
    model.configHydrated = true;
  }
  document.querySelectorAll("button[data-remote], button[data-app]").forEach((button) => {
    button.disabled = model.remoteBusy || !configured;
  });
  el.remoteTest.disabled = model.remoteBusy || !configured;
  el.remoteForget.disabled = model.remoteBusy || !remote.approved;
  if (el.remoteRescan) el.remoteRescan.disabled = model.remoteBusy || !configured;
  el.remoteSetupForm.querySelector("button[type=submit]").disabled = model.remoteBusy;
}

function renderQueue(queue, currentVideoId) {
  if (queue[0]?.video_id === currentVideoId) queue = queue.slice(1);
  const key = `${queue.map((item) => item.video_id).join(",")}|${model.removingId}`;
  if (key === model.queueKey) return;
  model.queueKey = key;
  el.queueList.replaceChildren();
  queue.forEach((item, index) => {
    const card = el.queueItemTemplate.content.cloneNode(true);
    const link = card.querySelector("a");
    const frame = card.querySelector(".queue-thumb");
    const image = card.querySelector("img");
    const remove = card.querySelector(".queue-remove");
    const videoId = item.video_id || "Unknown video";

    link.href = item.url || `https://youtube.com/watch?v=${encodeURIComponent(videoId)}`;
    link.setAttribute("aria-label", `Open queued video ${videoId} on YouTube`);
    card.querySelector("strong").textContent = videoId;
    card.querySelector(".queue-number").textContent = String(index + 1).padStart(2, "0");
    remove.dataset.videoId = item.video_id;
    remove.setAttribute("aria-label", `Remove ${videoId} from TV queue`);
    remove.disabled = model.removingId === item.video_id;
    image.addEventListener("error", () => thumbnailFailed(image, frame));
    setThumbnail(image, frame, item.video_id);
    el.queueList.append(card);
  });

  el.queueCount.textContent = `${queue.length} queued`;
  el.emptyQueue.hidden = queue.length > 0;
  el.railQueueLabel.textContent = queue.length ? `${queue.length} queued next` : "Queue clear";
  el.railFuture.replaceChildren(...queue.slice(0, 10).map(() => Object.assign(document.createElement("i"), { className: "rail-dot" })));
}

function render() {
  const data = model.data || {};
  const playback = data.playback || {};
  const queue = Array.isArray(data.queue) ? data.queue : [];
  const videoId = playback.video_id || "";
  const duration = Math.max(0, Number(playback.duration) || 0);
  const current = Math.min(duration || Infinity, Math.max(0, Number(playback.current_time) || 0));
  const playing = isPlaying(playback.state);
  const hasVideo = Boolean(videoId);
  const volume = Number(playback.volume);

  renderStatus(data.error ? String(data.error) : "");
  renderRemote(data.remote || {});
  el.liveBug.textContent = playing ? "Live" : (hasVideo ? "Paused" : "Idle");
  el.liveBug.classList.toggle("is-live", playing);
  el.liveBug.classList.toggle("is-paused", hasVideo && !playing);
  el.playbackState.textContent = hasVideo ? (playing ? "Live transmission" : "Playback paused") : "Standby";
  el.playbackState.classList.toggle("is-live", playing);
  el.videoTitle.textContent = hasVideo ? "YouTube on TV" : "Nothing playing";
  el.videoId.textContent = hasVideo ? videoId : "Send a YouTube URL to wake the screen.";
  el.duration.textContent = formatTime(duration);
  el.seek.max = String(duration);
  el.seek.disabled = !duration || model.controlBusy;
  if (!interacting.seek) {
    el.currentTime.textContent = formatTime(current);
    el.seek.value = String(current);
    el.railProgress.style.width = `${duration ? (current / duration) * 100 : 0}%`;
    el.seek.setAttribute("aria-valuetext", formatTime(current));
  }
  el.playButton.dataset.action = playing ? "pause" : "play";
  el.playButton.setAttribute("aria-label", playing ? "Pause" : "Play");
  el.playButton.classList.toggle("is-playing", playing);
  document.querySelectorAll("button[data-action]").forEach((button) => {
    button.disabled = !data.connected || model.controlBusy;
  });
  el.volume.disabled = !data.connected || model.controlBusy;
  if (Number.isFinite(volume) && !interacting.volume) {
    el.volume.value = String(volume);
    el.volumeValue.textContent = `${Math.round(volume)}%`;
    el.volume.setAttribute("aria-valuetext", `${Math.round(volume)} percent`);
  } else if (!interacting.volume) {
    el.volumeValue.textContent = "--";
    el.volume.setAttribute("aria-valuetext", "Unknown; adjust to set volume");
  }
  el.updatedAt.textContent = data.updated_at
    ? `Updated ${new Date(data.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
    : "Awaiting TV state";
  setThumbnail(el.nowThumbnail, el.screenFrame, videoId);
  renderQueue(queue, videoId);
  recordHistory(videoId);
}

async function readJson(response) {
  const body = await response.json().catch(() => ({}));
  const detail = Array.isArray(body.detail)
    ? body.detail.map((item) => item.msg || String(item)).join("; ")
    : body.detail;
  if (!response.ok) throw new Error(detail || body.error || `Request failed (${response.status})`);
  return body;
}

async function loadState() {
  try {
    model.data = await fetch("/state").then(readJson);
    model.networkError = "";
  } catch (error) {
    model.networkError = error.message || "Local server unavailable";
  }
  render();
}

async function mutate(path, body, channel = "command") {
  const errorKey = channel === "remote" ? "remoteError" : "commandError";
  clearTimeout(errorTimer);
  model[errorKey] = "";
  render();
  try {
    model.data = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(readJson);
    render();
    return true;
  } catch (error) {
    model[errorKey] = error.message || "Command failed";
    render();
    errorTimer = setTimeout(() => {
      model[errorKey] = "";
      render();
    }, 6000);
    return false;
  }
}

let remoteInFlight = false;

async function runRemoteMutation(path, body, disableControls = true) {
  if (remoteInFlight) return false;
  remoteInFlight = true;
  if (disableControls) {
    model.remoteBusy = true;
    render();
  }
  try {
    return await mutate(path, body, "remote");
  } finally {
    remoteInFlight = false;
    model.remoteBusy = false;
    render();
  }
}

function connectSocket() {
  clearTimeout(reconnectTimer);
  model.socketState = model.data ? "reconnecting" : "loading";
  render();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws`);
  model.socket = socket;

  socket.addEventListener("open", () => {
    model.socketState = "open";
    model.networkError = "";
    render();
  });
  socket.addEventListener("message", (event) => {
    try {
      model.data = JSON.parse(event.data);
      model.networkError = "";
      render();
    } catch {
      model.networkError = "Received an invalid TV state";
      render();
    }
  });
  socket.addEventListener("close", () => {
    if (model.socket !== socket) return;
    model.socketState = "reconnecting";
    render();
    reconnectTimer = setTimeout(connectSocket, 2500);
  });
  socket.addEventListener("error", () => socket.close());
}

el.nowThumbnail.addEventListener("error", () => thumbnailFailed(el.nowThumbnail, el.screenFrame));
el.urlForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitter = event.submitter?.value === "queue" ? "queue" : "play";
  const url = el.videoUrl.value.trim();
  const buttons = el.urlForm.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  const succeeded = await mutate(`/${submitter}`, { url });
  buttons.forEach((button) => { button.disabled = false; });
  if (succeeded) el.urlForm.reset();
});

document.addEventListener("click", (event) => {
  const history = event.target.closest("button[data-history]");
  if (history) {
    const intent = history.dataset.intent === "queue" ? "queue" : "play";
    mutate(`/${intent}`, { url: `https://www.youtube.com/watch?v=${history.dataset.history}` });
    return;
  }
  const app = event.target.closest("button[data-app]");
  if (app) {
    runRemoteMutation("/remote/app", { app: app.dataset.app });
    return;
  }
  const remote = event.target.closest("button[data-remote]");
  if (remote) {
    if (!remote.hasAttribute("data-repeat") || event.detail === 0) {
      runRemoteMutation("/remote", { action: remote.dataset.remote });
    }
    return;
  }
  const remove = event.target.closest("button[data-video-id]");
  if (remove) {
    if (!window.confirm("Remove this video by rebuilding the TV queue? Items added from another remote may be lost.")) return;
    model.removingId = remove.dataset.videoId;
    model.queueKey = "";
    remove.disabled = true;
    mutate("/queue/remove", { video_id: remove.dataset.videoId }).finally(() => {
      model.removingId = "";
      model.queueKey = "";
      render();
    });
    return;
  }
  const button = event.target.closest("button[data-action]");
  if (button && !model.controlBusy) {
    model.controlBusy = true;
    render();
    mutate("/control", { action: button.dataset.action }).finally(() => {
      model.controlBusy = false;
      render();
    });
  }
});

let repeatDelay;
let repeatTimer;

function stopRemoteRepeat() {
  clearTimeout(repeatDelay);
  clearInterval(repeatTimer);
}

document.querySelectorAll("button[data-repeat]").forEach((button) => {
  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || button.disabled) return;
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    runRemoteMutation("/remote", { action: button.dataset.remote }, false);
    repeatDelay = setTimeout(() => {
      repeatTimer = setInterval(() => {
        runRemoteMutation("/remote", { action: button.dataset.remote }, false);
      }, 160);
    }, 400);
  });
  button.addEventListener("pointerup", stopRemoteRepeat);
  button.addEventListener("pointercancel", stopRemoteRepeat);
  button.addEventListener("lostpointercapture", stopRemoteRepeat);
});

el.remoteSetupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await runRemoteMutation("/remote/config", {
    host: el.tvHost.value.trim(),
    mac: el.tvMac.value.trim()
  });
});
el.remoteTest.addEventListener("click", () => runRemoteMutation("/remote/test", {}));
el.remoteRescan?.addEventListener("click", () => runRemoteMutation("/remote/test", {}));
el.remoteForget.addEventListener("click", () => {
  if (window.confirm("Forget this TV approval token? The TV will ask for approval again.")) {
    runRemoteMutation("/remote/forget", {});
  }
});
el.seek.addEventListener("pointerdown", () => { interacting.seek = true; });
el.seek.addEventListener("pointercancel", () => { interacting.seek = false; render(); });
el.seek.addEventListener("keydown", () => { interacting.seek = true; });
el.seek.addEventListener("blur", () => { interacting.seek = false; render(); });
el.seek.addEventListener("input", () => {
  el.currentTime.textContent = formatTime(el.seek.value);
  el.railProgress.style.width = `${el.seek.max > 0 ? (el.seek.value / el.seek.max) * 100 : 0}%`;
  el.seek.setAttribute("aria-valuetext", formatTime(el.seek.value));
});
el.seek.addEventListener("change", async () => {
  await mutate("/control", { action: "seek", value: Number(el.seek.value) });
  interacting.seek = false;
  render();
});
el.volume.addEventListener("pointerdown", () => { interacting.volume = true; });
el.volume.addEventListener("pointercancel", () => { interacting.volume = false; render(); });
el.volume.addEventListener("keydown", () => { interacting.volume = true; });
el.volume.addEventListener("blur", () => { interacting.volume = false; render(); });
el.volume.addEventListener("input", () => {
  el.volumeValue.textContent = `${el.volume.value}%`;
  el.volume.setAttribute("aria-valuetext", `${el.volume.value} percent`);
});
el.volume.addEventListener("change", async () => {
  await mutate("/control", { action: "volume", value: Number(el.volume.value) });
  interacting.volume = false;
  render();
});

// Watch history — client-side, per browser. Only renders where the markup exists (mono design).
const historyEl = {
  list: document.getElementById("historyList"),
  count: document.getElementById("historyCount"),
  empty: document.getElementById("emptyHistory"),
  template: document.getElementById("historyItemTemplate")
};
const HISTORY_KEY = "tvcc_history";
const HISTORY_MAX = 12;
let historyVideos = loadHistory();
let lastHistoryId = "";

function loadHistory() {
  try {
    const stored = JSON.parse(localStorage.getItem(HISTORY_KEY));
    return Array.isArray(stored) ? stored.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function recordHistory(videoId) {
  if (!videoId || videoId === lastHistoryId) return;
  lastHistoryId = videoId;
  historyVideos = [videoId, ...historyVideos.filter((id) => id !== videoId)].slice(0, HISTORY_MAX);
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(historyVideos)); } catch {}
  renderHistory();
}

function renderHistory() {
  if (!historyEl.list) return;
  historyEl.list.replaceChildren();
  historyVideos.forEach((videoId) => {
    const card = historyEl.template.content.cloneNode(true);
    const play = card.querySelector(".history-play");
    const next = card.querySelector(".history-next");
    const frame = card.querySelector(".queue-thumb");
    const image = card.querySelector("img");
    play.dataset.history = videoId;
    play.dataset.intent = "play";
    play.setAttribute("aria-label", `Play ${videoId} now`);
    next.dataset.history = videoId;
    next.dataset.intent = "queue";
    next.setAttribute("aria-label", `Play ${videoId} next`);
    card.querySelector("strong").textContent = videoId;
    image.addEventListener("error", () => thumbnailFailed(image, frame));
    setThumbnail(image, frame, videoId);
    historyEl.list.append(card);
  });
  historyEl.count.textContent = `${historyVideos.length} watched`;
  historyEl.empty.hidden = historyVideos.length > 0;
}

// First-ever-run onboarding. Shows once per browser, then setup lives in the gear.
const onboarding = document.getElementById("onboarding");
function dismissOnboarding() {
  if (!onboarding) return;
  try { localStorage.setItem("tvcc_onboarded", "1"); } catch {}
  onboarding.hidden = true;
}
if (onboarding && !localStorage.getItem("tvcc_onboarded")) {
  onboarding.hidden = false;
  document.getElementById("onboardStart")?.focus();
}
document.getElementById("onboardStart")?.addEventListener("click", () => {
  dismissOnboarding();
  if (el.remoteSetup) el.remoteSetup.open = true;
});
document.getElementById("onboardSkip")?.addEventListener("click", dismissOnboarding);

renderHistory();
render();
loadState();
connectSocket();
setInterval(() => {
  if (!model.socket || model.socket.readyState !== WebSocket.OPEN) loadState();
}, 5000);
