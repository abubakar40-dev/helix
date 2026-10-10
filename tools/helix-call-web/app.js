/* HELIX Call - Android-style UI, real mic capture, Gemini Live over the box.
   Audio is PCM16 16 kHz; the box relays it to Gemini and sends speech back. */
const $ = (s) => document.querySelector(s);
const callId = "call-" + Date.now() + "-" + Math.random().toString(16).slice(2, 8);

let ws = null, stream = null, ac = null, srcNode = null, gainNode = null;
let startedAt = null, muted = false, tickTimer = null;

/* ---------------- screens ---------------- */
function show(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("on"));
  $("#" + id).classList.add("on");
}
function cstat(t, ringing) {
  $("#cstat").textContent = t;
  $("#face").classList.toggle("ringing", !!ringing);
}
function net(t) { $("#net").textContent = t; }

/* ---------------- transcript ---------------- */
function addTurn(who, text) {
  const d = document.createElement("div");
  d.className = "t" + (who === "me" ? " me" : "");
  d.innerHTML = "<b>" + (who === "me" ? "you" : "helix") + "</b>" +
                text.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const log = $("#log");
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

/* ---------------- timer ---------------- */
function startClock() {
  const t0 = startedAt;
  tickTimer = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    $("#clock").textContent =
      String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }, 1000);
}

/* ---------------- call ---------------- */
async function startCall() {
  show("incall");
  startedAt = Date.now();
  startClock();
  cstat("connecting…", true);

  // 1. microphone -> 16 kHz PCM
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    });
  } catch (e) {
    cstat("microphone blocked: " + e.message, false);
    net("error");
    return;
  }
  ac = new (window.AudioContext || window.webkitAudioContext)();
  await ac.audioWorklet.addModule("/app/pcm-worklet.js");
  await ac.resume();

  const playCtx = new AudioContext();
  playCtx.audioWorklet.addModule("/app/pcm-worklet.js").then(() => {
    const src = new AudioWorkletNode(playCtx, "playback");
    gainNode = playCtx.createGain();
    src.connect(gainNode).connect(playCtx.destination);
    window.__playPort = src.port;
    window.__playCtx = playCtx;
  });

  // 2. open the relay
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") +
                     location.host + "/voice/" + encodeURIComponent(callId));
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    net("connected");
    cstat("connected", false);
    const cap = new AudioWorkletNode(ac, "capture");
    cap.port.onmessage = (e) => {
      if (ws.readyState === 1 && !muted) ws.send(e.data);
    };
    srcNode = ac.createMediaStreamSource(stream);
    srcNode.connect(cap);
    // worklet has no output to connect, but must stay alive in the graph
    cap.connect(ac.destination);
  };

  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      const m = JSON.parse(ev.data);
      if (m.transcript) { addTurn("helix", m.transcript); cstat("listening…", false); }
    } else if (window.__playPort) {
      // GEMINI SPEECH -> SPEAKER
      window.__playPort.postMessage(ev.data, [ev.data]);
      cstat("helix is speaking", false);
    }
  };
  ws.onerror = () => { net("error"); cstat("connection failed", false); };
  ws.onclose = () => { if (!ended) { net("disconnected"); cstat("call ended", false); } };
}

let ended = false;
function endCall() {
  if (ended) return;
  ended = true;
  clearInterval(tickTimer);
  try { ws && ws.close(); } catch (e) {}
  try { srcNode && srcNode.disconnect(); } catch (e) {}
  try { stream && stream.getTracks().forEach(t => t.stop()); } catch (e) {}
  try { ac && ac.close(); window.__playCtx && window.__playCtx.close(); } catch (e) {}
  show("dialer");
}

/* ---------------- controls ---------------- */
$("#mute").onclick = () => {
  muted = !muted;
  $("#mute").classList.toggle("off", muted);
  $("#mute").textContent = muted ? "🔇" : "🎤";
  if (stream) stream.getAudioTracks().forEach(t => (t.enabled = !muted));
};
$("#spk").onclick = () => {
  const g = gainNode;
  if (!g) return;
  g.gain.value = g.gain.value > 0 ? 0 : 1;
  $("#spk").textContent = g.gain.value > 0 ? "🔊" : "🔈";
};
$("#hangup").onclick = endCall;
$("#startcall").onclick = startCall;

// dialpad feel
document.querySelectorAll(".key").forEach(b => {
  b.addEventListener("click", () => {
    navigator.vibrate && navigator.vibrate(12);
    if (b.dataset.k === "back") return;
  });
});
$("#padback").onclick = () => navigator.vibrate && navigator.vibrate(12);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/app/sw.js").catch(() => {});
}
window.addEventListener("beforeunload", endCall);