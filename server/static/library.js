"use strict";

const byId = id => document.getElementById(id);
const connectForm = byId("connect");
const createForm = byId("create");
const player = byId("video");
const keySlot = "reeldub.instance-key";
const languages = new Intl.DisplayNames(["en"], {type: "language"});
let key = "";
let profile;
let currentJob;
let generation = 0;
let timer;
let lastSubmission;
let recent = [];

function remember(value) {
  try {
    if (value) sessionStorage.setItem(keySlot, value);
    else sessionStorage.removeItem(keySlot);
  } catch { /* Private browsing can disable storage. */ }
}

function message(text = "") { byId("message").textContent = text; }

async function api(path, payload) {
  const headers = {Authorization: "Bearer " + key};
  const options = {headers, cache: "no-store", redirect: "error", referrerPolicy: "no-referrer"};
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    Object.assign(options, {method: "POST", body: JSON.stringify(payload)});
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    if (response.status === 401) disconnect();
    const detail = typeof problem.detail === "string" ? problem.detail : "Request failed (" + response.status + ").";
    throw new Error(response.status === 401 ? "The server rejected that key." : detail);
  }
  return response.json();
}

function resetPlayer() {
  player.pause();
  player.removeAttribute("src");
  player.load();
  player.hidden = true;
  byId("retry-play").hidden = true;
}

function disconnect() {
  generation++;
  clearTimeout(timer);
  key = "";
  profile = null;
  currentJob = null;
  lastSubmission = null;
  recent = [];
  remember("");
  resetPlayer();
  byId("studio").hidden = byId("disconnect").hidden = byId("result").hidden = true;
  byId("connection").hidden = false;
  createForm.reset();
  byId("receipt").textContent = "";
  byId("jobs").replaceChildren();
  message();
}

function describeMode() {
  byId("mode-description").textContent = createForm.elements.quality.value === "fast"
    ? "Kitten matches the speaker. Qwen takes over if Kitten can’t finish this dub."
    : "Qwen matches the original speaker in your chosen language.";
}

async function connect(value) {
  if (!window.isSecureContext) throw new Error("Open this server over HTTPS, or use localhost on the server itself.");
  const version = ++generation;
  key = value.trim();
  const info = await api("/v1/me");
  if (version !== generation) return;
  if (!info.quality_languages?.best?.length) throw new Error("This server does not advertise ReelDub voice matching.");
  profile = info;
  const targets = [...new Set([...info.quality_languages.best, ...(info.quality_languages.fast || [])])];
  byId("target").replaceChildren(...targets.map(code => new Option(languages.of(code), code)));
  if (targets.includes("en")) byId("target").value = "en";
  remember(key);
  byId("key").value = "";
  byId("connection").hidden = true;
  byId("studio").hidden = byId("disconnect").hidden = false;
  describeMode();
  message();
  const jobs = await api("/v1/jobs");
  if (version === generation) { recent = jobs; drawRecent(); }
}

function title(job) { return job.title || "Instagram Reel"; }

function drawRecent() {
  byId("recent").hidden = recent.length === 0;
  const rows = recent.map(job => {
    const row = document.createElement("li");
    const button = document.createElement("button");
    const label = document.createElement("span");
    label.textContent = title(job);
    const detail = document.createElement("small");
    detail.textContent = languages.of(job.target_language || "en") + " · " + (job.stage || job.state);
    button.append(label, detail);
    button.onclick = () => openJob(job);
    row.append(button);
    return row;
  });
  byId("jobs").replaceChildren(...rows);
}

async function play(job, version) {
  try {
    const {ticket} = await api("/v1/jobs/" + encodeURIComponent(job.id) + "/playback", {});
    if (version !== generation) return;
    player.src = "/v1/play/" + encodeURIComponent(job.id) + "/" + encodeURIComponent(ticket);
    player.hidden = false;
    byId("retry-play").hidden = true;
    player.onloadedmetadata = () => {
      const track = player.textTracks[0];
      if (track) track.mode = "showing";
    };
  } catch (error) {
    if (version === generation) { message(error.message); byId("retry-play").hidden = false; }
  }
}

function update(job, version) {
  if (version !== generation) return;
  currentJob = job;
  const index = recent.findIndex(item => item.id === job.id);
  if (index >= 0) recent[index] = job; else recent.unshift(job);
  drawRecent();
  const live = job.state === "running" || job.state === "queued";
  const progress = byId("progress");
  progress.hidden = !live;
  if (typeof job.progress === "number") progress.value = job.progress;
  else progress.removeAttribute("value");
  const mode = job.quality === "fast" ? "Fast · Kitten" : job.quality === "best" ? "Best · Qwen" : "";
  const details = [job.error || job.stage || job.state];
  if (mode) details.push(mode);
  if (job.quality_fallback) details.push(job.quality_fallback);
  if (job.state === "queued" && Number.isInteger(job.ahead)) {
    if (job.ahead > 0) details.push(job.ahead + (job.ahead === 1 ? " dub queued ahead" : " dubs queued ahead"));
    else {
      details.push("Next in line");
      if (recent.some(item => item.state === "running")) details.push("Waiting for a worker");
    }
  }
  byId("job-status").textContent = details.join(" / ");
  byId("result-title").textContent = title(job);
  if (live) scheduleRefresh(version);
  if (job.state === "done") {
    if (job.available_on_server) play(job, version);
    else byId("job-status").textContent += " / Server copy expired";
  }
}

function scheduleRefresh(version, delay = 3500) {
  clearTimeout(timer);
  if (!document.hidden) timer = setTimeout(() => refreshJob(version), delay);
}

async function refreshJob(version) {
  if (!currentJob || version !== generation || document.hidden) return;
  try {
    const job = await api("/v1/jobs/" + encodeURIComponent(currentJob.id));
    if (version === generation) { message(); update(job, version); }
  } catch (error) {
    if (version === generation) { message(error.message); scheduleRefresh(version, 10000); }
  }
}

function openJob(job) {
  clearTimeout(timer);
  resetPlayer();
  message();
  byId("result").hidden = false;
  update(job, ++generation);
  byId("result").scrollIntoView({block:"nearest"});
}

connectForm.onsubmit = async event => {
  event.preventDefault();
  const button = connectForm.querySelector("button");
  button.disabled = true;
  try { await connect(byId("key").value); }
  catch (error) { message(error.message); }
  finally { button.disabled = false; }
};
createForm.onsubmit = async event => {
  event.preventDefault();
  if (!profile) return;
  byId("receipt").textContent = "";
  const fields = {url:byId("source").value.trim(), target_language:byId("target").value, quality:createForm.elements.quality.value};
  const fingerprint = JSON.stringify(fields);
  if (!lastSubmission || lastSubmission.fingerprint !== fingerprint) {
    lastSubmission = {fingerprint, request_id:crypto.randomUUID()};
  }
  const button = createForm.querySelector("button");
  button.disabled = true;
  const submittedKey = key;
  try {
    const job = await api("/v1/jobs", {...fields, request_id:lastSubmission.request_id});
    if (key === submittedKey) {
      lastSubmission = null;
      openJob(job);
      byId("receipt").textContent = "Submitted";
      byId("source").value = "";
    }
  } catch (error) { message(error.message); }
  finally { button.disabled = false; }
};
byId("disconnect").onclick = disconnect;
createForm.onchange = describeMode;
byId("retry-play").onclick = () => {
  if (currentJob) { message(); play(currentJob, generation); }
};
player.onerror = () => {
  if (!player.hasAttribute("src")) return;
  message("Couldn’t load the video. Reload it to get a fresh playback link.");
  byId("retry-play").hidden = false;
};
document.addEventListener("visibilitychange", () => {
  clearTimeout(timer);
  if (!document.hidden && ["queued","running"].includes(currentJob?.state)) refreshJob(generation);
});
let saved = "";
try { saved = sessionStorage.getItem(keySlot) || ""; } catch { /* Storage is optional. */ }
if (saved) connect(saved).catch(error => message(error.message));
