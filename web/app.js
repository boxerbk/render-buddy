// Render Buddy dashboard. Everything goes through the local server's /api; nothing here talks to Phaze directly.
const $ = (sel) => document.querySelector(sel);
const jobs = new Map();        // id -> job (active only)
let profiles = [];
let pendingMachine = null;

const STATE = {
  starting:       ["Starting", ""],
  rendering:      ["Rendering", "ok"],
  finished:       ["Finished", "ok"],
  unknown:        ["Watching", ""],
  stalled:        ["Stalled", "warn"],
  locked:         ["Locked screen", "warn"],
  offline:        ["Offline", "bad"],
  error_dialog:   ["Error", "bad"],
  not_responding: ["Not responding", "bad"],
  app_closed:     ["App closed", "bad"],
};

async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

// ---------- status chips + API key ----------
async function loadStatus() {
  const s = await api("/status");
  const chip = (label, cls, title) =>
    `<li class="chip" title="${esc(title)}"><span class="dot ${cls}"></span>${esc(label)}</li>`;
  $("#chips").innerHTML = [
    s.mcp.ok ? chip(`Phaze app ${s.mcp.app_version || ""}`, "ok", "Local Phaze MCP is reachable")
             : chip("Phaze app not found", "bad", s.mcp.error || ""),
    chip(s.vision ? `Claude · ${s.model}` : "Claude off", s.vision ? "ok" : "warn",
         s.vision ? "Screenshots are read by Claude" : "No ANTHROPIC_API_KEY: stall detection by pixel diff only"),
    chip(s.slack ? "Slack on" : "Slack off", s.slack ? "ok" : "warn",
         s.slack ? "Alerts post to Slack" : "No Slack tokens: alerts print in the console"),
  ].join("");

  const connected = s.api_key;
  $("#key-form").hidden = connected;
  $("#key-help").hidden = connected;
  $("#key-connected").hidden = !connected;
  $("#key-state").textContent = connected ? "Connected" : "Optional";
  $("#key-orgs").textContent = connected ? `Org: ${s.orgs.map((o) => o.name).join(", ")}` : "";
}

$("#key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("#key-input").value.trim();
  if (!key) return;
  const btn = e.submitter;
  btn.disabled = true;
  try {
    await api("/session", { method: "POST", body: { api_key: key } });
    $("#key-input").value = "";
    await Promise.all([loadStatus(), loadMachines()]);
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
});

$("#key-forget").addEventListener("click", async () => {
  await api("/session", { method: "DELETE" });
  await Promise.all([loadStatus(), loadMachines()]);
});

// ---------- machines ----------
async function loadMachines() {
  const { machines, mcp_error } = await api("/machines");
  $("#mcp-error").hidden = !mcp_error;
  $("#mcp-error").textContent = mcp_error
    ? `${mcp_error}. Open the Phaze app on this PC, sign in, and turn on the MCP server (experimental features).`
    : "";
  const el = $("#machines");
  if (!machines.length) {
    el.innerHTML = `<p class="empty machine">No machines found.</p>`;
    return;
  }
  el.innerHTML = machines.map((m) => {
    const meta = [m.org, m.assignee || m.group, m.os].filter(Boolean).join(" · ");
    const dot = m.online ? "ok" : "";
    let action;
    if (m.job_id) action = `<span class="badge ok" style="position:static">Watching</span>`;
    else if (!m.reachable) action = `<span class="why">Not reachable from this PC's Phaze login</span>`;
    else if (!m.online) action = `<span class="why">Offline</span>`;
    else action = `<button class="btn btn-sm" data-watch="${esc(m.machine_id)}" data-name="${esc(m.name || "")}">Watch render</button>`;
    return `<div class="machine ${m.reachable ? "" : "unreachable"}">
      <span class="dot ${dot}" title="${m.online ? "Online" : "Offline"}"></span>
      <div class="machine-name"><strong>${esc(m.name || m.machine_id.slice(0, 10))}</strong><span>${esc(meta)}</span></div>
      ${action}
    </div>`;
  }).join("");
}

$("#machines").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-watch]");
  if (!btn) return;
  pendingMachine = btn.dataset.watch;
  $("#watch-name").textContent = btn.dataset.name;
  $("#watch-dialog").showModal();
});

$("#watch-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok" || !pendingMachine) return;
  e.preventDefault();
  const ok = $("#watch-ok");
  ok.disabled = true;
  try {
    const job = await api("/jobs", {
      method: "POST",
      body: {
        machine_id: pendingMachine,
        profile: $("#watch-profile").value,
        check_interval_sec: +$("#watch-interval").value,
        stall_minutes: +$("#watch-stall").value,
      },
    });
    jobs.set(job.id, job);
    renderFarm();
    $("#watch-dialog").close();
    loadMachines();
  } catch (err) {
    alert(err.message);
  } finally {
    ok.disabled = false;
  }
});

$("#refresh").addEventListener("click", loadMachines);

// ---------- render farm ----------
function renderFarm() {
  const farm = $("#farm");
  const ids = new Set(jobs.keys());
  for (const card of farm.querySelectorAll(".job")) if (!ids.has(card.dataset.id)) card.remove();
  for (const job of jobs.values()) {
    let card = farm.querySelector(`.job[data-id="${job.id}"]`);
    if (!card) {
      card = $("#job-tpl").content.firstElementChild.cloneNode(true);
      card.dataset.id = job.id;
      farm.prepend(card);
    }
    fillCard(card, job);
  }
  $("#farm-empty").hidden = jobs.size > 0;
  $("#farm-count").textContent = jobs.size ? `${jobs.size} watching` : "";
}

function fillCard(card, job) {
  const [label, cls] = STATE[job.state] || [job.state, ""];
  const badge = card.querySelector(".badge");
  badge.textContent = label;
  badge.className = `badge ${cls}`;
  card.classList.toggle("is-alert", cls === "bad");
  card.querySelector("h3").textContent = job.machine_name;
  card.querySelector(".app").textContent = job.app || profileLabel(job.profile);
  const pct = job.progress_pct;
  card.querySelector(".progress span").style.width = `${pct ?? 0}%`;
  const frames = job.frame != null ? `frame ${job.frame}${job.total_frames ? ` / ${job.total_frames}` : ""}` : "";
  card.querySelector(".pct").textContent = [pct != null ? `${Math.round(pct)}%` : "", frames].filter(Boolean).join(" · ");
  card.querySelector(".eta").textContent = job.eta ? `${job.eta} left` : "";
  card.querySelector(".summary").textContent = job.summary || "Waiting for the first screenshot…";
  const muted = (job.muted_until || 0) * 1000 > Date.now();
  card.querySelector(".mute").textContent = muted ? "Unmute" : "Mute 1h";
  card.querySelector(".connect").href = `phaze://connect?id=${encodeURIComponent(job.machine_id)}`;
  if (job.last_check_at && card.dataset.shot !== String(job.last_check_at)) {
    card.dataset.shot = job.last_check_at;
    const img = card.querySelector("img");
    const next = new Image();
    next.onload = () => { img.src = next.src; };
    next.src = `/api/jobs/${job.id}/latest.png?t=${job.last_check_at}`;
  }
}

$("#farm").addEventListener("click", async (e) => {
  const card = e.target.closest(".job");
  if (!card) return;
  const id = card.dataset.id;
  const job = jobs.get(id);
  try {
    if (e.target.closest(".snap")) {
      await api(`/jobs/${id}/snapshot`, { method: "POST" });
      e.target.textContent = "Requested";
      setTimeout(() => (e.target.textContent = "Snapshot"), 2500);
    } else if (e.target.closest(".mute")) {
      const muted = (job.muted_until || 0) * 1000 > Date.now();
      await api(`/jobs/${id}/mute?minutes=${muted ? 0 : 60}`, { method: "POST" });
    } else if (e.target.closest(".stop")) {
      if (!confirm(`Stop watching ${job.machine_name}?`)) return;
      await api(`/jobs/${id}`, { method: "DELETE" });
      jobs.delete(id);
      renderFarm();
      loadMachines();
      loadHistory();
    }
  } catch (err) {
    alert(err.message);
  }
});

// ---------- history ----------
async function loadHistory() {
  const { active, history } = await api("/jobs");
  jobs.clear();
  for (const j of active) jobs.set(j.id, j);
  renderFarm();
  $("#history-empty").hidden = history.length > 0;
  $("#history").innerHTML = history.map((j) => {
    const [label, cls] = j.status === "finished" ? ["Finished", "ok"]
      : j.status === "stopped" ? ["Stopped", ""] : [j.status, "bad"];
    const took = j.ended_at ? duration(j.ended_at - j.started_at) : "";
    const gif = j.status === "finished" ? `<a href="/api/jobs/${j.id}/timelapse.gif" target="_blank">Timelapse</a>` : "";
    return `<tr>
      <td>${esc(j.machine_name)}</td>
      <td>${esc(j.app || profileLabel(j.profile))}</td>
      <td><span class="badge ${cls}" style="position:static">${esc(label)}</span></td>
      <td class="num">${new Date(j.started_at * 1000).toLocaleString()}</td>
      <td class="num">${took}</td>
      <td>${gif}</td>
    </tr>`;
  }).join("");
  $(".table-wrap").hidden = history.length === 0;
}

// ---------- live updates ----------
function listen() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type !== "job") return;
    const job = msg.job;
    if (job.status === "active") {
      jobs.set(job.id, job);
      renderFarm();
    } else if (jobs.has(job.id)) {
      jobs.delete(job.id);
      renderFarm();
      loadHistory();
      loadMachines();
    }
  };
}

// ---------- helpers ----------
function profileLabel(id) {
  return profiles.find((p) => p.id === id)?.label || "";
}
function duration(sec) {
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

(async function init() {
  profiles = await api("/profiles");
  $("#watch-profile").innerHTML = profiles.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join("");
  await Promise.all([loadStatus(), loadHistory(), loadMachines().catch((e) => {
    $("#machines").innerHTML = `<p class="empty machine">${esc(e.message)}</p>`;
  })]);
  listen();
  setInterval(loadMachines, 60_000);
})();
