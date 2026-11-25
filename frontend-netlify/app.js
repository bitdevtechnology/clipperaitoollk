"use strict";
const API = (window.APP_CONFIG && window.APP_CONFIG.API_BASE_URL || "").replace(/\/$/, "");
const $ = (s) => document.querySelector(s);
const YT = /^(?:https?:\/\/)?(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?(?:[^#\s]*&)?v=|shorts\/|live\/)|youtu\.be\/)[A-Za-z0-9_-]{11}/;
const STAGES = [["queued", "Waiting in queue"], ["download", "Downloading video"], ["audio", "Extracting audio"],
  ["transcribe", "Transcribing speech (Whisper)"], ["detect", "Finding best moments"], ["render", "Rendering clips"]];
let jobId = null, timer = null, polling = false;

function el(tag, props = {}, ...kids) {
  const e = Object.assign(document.createElement(tag), props);
  kids.forEach((k) => e.append(k));
  return e;
}
function setMsg(node, text, kind) { node.textContent = text || ""; node.className = "msg" + (kind ? " " + kind : ""); }
function fmt(sec) { sec = Math.round(sec); const m = Math.floor(sec / 60), s = sec % 60; return `${m}:${String(s).padStart(2, "0")}`; }

async function api(path, opts = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), opts.timeout || 60000);
  try {
    const r = await fetch(API + path, { signal: ctl.signal, headers: { "Content-Type": "application/json" }, ...opts });
    let data = null;
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) {
      const d = data && data.detail;
      throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || `Request failed (${r.status})`);
    }
    return data;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("The server took too long to answer.");
    if (e instanceof TypeError) throw new Error("Cannot reach the server. Free hosts sleep when idle, so wait a minute and try again. Also check API_BASE_URL in config.js and ALLOWED_ORIGINS on the backend.");
    throw e;
  } finally { clearTimeout(t); }
}

function chips(host, name, values, def, label) {
  values.forEach((v) => {
    const i = el("input", { type: "radio", name, value: v, checked: v === def });
    host.append(el("label", { className: "chip" }, i, el("span", {}, label(v))));
  });
}
chips($("#durations"), "dur", [15, 30, 45, 60, 90], 30, (v) => v + "s");
chips($("#ratios"), "ratio", ["9:16", "16:9", "1:1"], "9:16",
  (v) => ({ "9:16": "9:16 Vertical", "16:9": "16:9 Wide", "1:1": "1:1 Square" }[v]));
const val = (name) => document.querySelector(`input[name=${name}]:checked`).value;

$("#count").oninput = (e) => ($("#countOut").textContent = e.target.value);
$("#size").oninput = (e) => ($("#sizeOut").textContent = e.target.value + "%");
$("#captions").onchange = (e) => $("#capOpts").classList.toggle("off", !e.target.checked);

async function checkHealth() {
  const h = $("#health");
  try {
    const d = await api("/api/health", { timeout: 20000 });
    const ok = d.ffmpeg && d.whisper_installed;
    h.textContent = ok ? `Server online · Whisper ${d.whisper_model}` : "Server online but FFmpeg/Whisper missing";
    h.className = "pill " + (ok ? "ok" : "bad");
    $("#keepNote").dataset.mins = d.clips_kept_minutes;
    window.__maxMin = d.max_video_minutes;
  } catch (e) { h.textContent = "Server offline"; h.className = "pill bad"; h.title = e.message; }
}

$("#checkBtn").onclick = async () => {
  const url = $("#url").value.trim();
  $("#info").hidden = true; $("#form").hidden = true;
  if (!YT.test(url)) return setMsg($("#urlMsg"), "Enter a YouTube link such as youtube.com/watch?v=… , youtu.be/… or youtube.com/shorts/…", "err");
  $("#checkBtn").disabled = true;
  setMsg($("#urlMsg"), "Checking video…");
  try {
    const d = await api("/api/info", { method: "POST", body: JSON.stringify({ url }), timeout: 90000 });
    $("#thumb").src = d.thumbnail || ""; $("#vTitle").textContent = d.title || "Untitled";
    $("#vMeta").textContent = `${d.channel || "Unknown channel"} · ${fmt(d.duration)} long`;
    $("#info").hidden = false;
    if (d.is_live) return setMsg($("#urlMsg"), "Live streams are not supported.", "err");
    if (d.too_long) return setMsg($("#urlMsg"), `This video is longer than the server limit (${window.__maxMin || 60} minutes).`, "err");
    setMsg($("#urlMsg"), "Video found. Choose your options below.", "good");
    $("#form").hidden = false;
  } catch (e) { setMsg($("#urlMsg"), e.message, "err"); }
  finally { $("#checkBtn").disabled = false; }
};

function renderStages(job) {
  const idx = STAGES.findIndex((s) => s[0] === job.stage);
  const host = $("#stages"); host.replaceChildren();
  STAGES.forEach(([k, name], i) => {
    const cls = job.status === "done" || i < idx ? "done" : i === idx && job.status !== "error" ? "active" : "";
    host.append(el("li", { className: cls }, name));
  });
}

function showResults(job) {
  const host = $("#clips"); host.replaceChildren();
  job.clips.forEach((c) => {
    const v = el("video", { controls: true, preload: "metadata", src: API + c.preview_url });
    v.setAttribute("playsinline", "");
    const links = el("div", { className: "dl" }, el("a", { className: "main", href: API + c.download_url }, "Download MP4"));
    if (c.srt_url) links.append(el("a", { href: API + c.srt_url }, "SRT"));
    host.append(el("div", { className: "clip" }, v, el("h3", {}, c.title || `Clip ${c.index}`),
      el("p", {}, `${fmt(c.start)}–${fmt(c.end)} · ${c.duration}s · score ${c.score}`),
      el("p", {}, "Why: " + c.reason), links));
  });
  $("#keepNote").textContent = `Download your clips now: they are deleted from the server about ${job.expires_in_minutes} minutes after the job starts.` +
    (job.detected_language ? ` Detected language: ${job.detected_language}.` : "");
  $("#results").hidden = false;
}

async function poll() {
  if (polling || !jobId) return;
  polling = true;
  try {
    const job = await api("/api/jobs/" + jobId, { timeout: 20000 });
    renderStages(job);
    $("#barFill").style.width = job.progress + "%";
    let msg = job.message;
    if (job.status === "queued" && job.queue_position > 0) msg = `Waiting in queue (${job.queue_position} job(s) ahead)…`;
    setMsg($("#statusMsg"), msg, job.status === "error" ? "err" : job.status === "done" ? "good" : "");
    if (job.status === "done") { finish(); showResults(job); }
    else if (job.status === "error" || job.status === "cancelled") finish();
  } catch (e) {
    if (/not found|expired/i.test(e.message)) { setMsg($("#statusMsg"), "This job expired or the server restarted. Please start again.", "err"); finish(); }
    else setMsg($("#statusMsg"), e.message + " (retrying…)", "err");
  } finally { polling = false; }
}
function finish() { clearInterval(timer); timer = null; $("#go").disabled = false; $("#cancel").hidden = true; sessionStorage.removeItem("jobId"); }
function startPolling(id) {
  jobId = id; sessionStorage.setItem("jobId", id);
  $("#empty").hidden = true; $("#status").hidden = false; $("#cancel").hidden = false; $("#results").hidden = true;
  $("#go").disabled = true; renderStages({ stage: "queued", status: "queued" });
  clearInterval(timer); timer = setInterval(poll, 2000); poll();
}

$("#form").onsubmit = async (e) => {
  e.preventDefault();
  if (!$("#rights").checked) return setMsg($("#urlMsg"), "Please confirm you have the right to clip this video.", "err");
  const body = {
    url: $("#url").value.trim(), clip_duration: +val("dur"), num_clips: +$("#count").value, ratio: val("ratio"),
    captions: $("#captions").checked, language: $("#language").value, caption_style: $("#style").value,
    caption_position: $("#position").value, font_size: +$("#size").value, rights_confirmed: true,
  };
  $("#go").disabled = true;
  try { setMsg($("#urlMsg"), ""); startPolling((await api("/api/jobs", { method: "POST", body: JSON.stringify(body) })).job_id); }
  catch (err) { $("#go").disabled = false; setMsg($("#urlMsg"), err.message, "err"); }
};

$("#cancel").onclick = async () => {
  if (!jobId) return;
  try { await api("/api/jobs/" + jobId, { method: "DELETE" }); } catch (_) {}
  setMsg($("#statusMsg"), "Job cancelled.");
  finish();
};

checkHealth();
const saved = sessionStorage.getItem("jobId");
if (saved) startPolling(saved);
