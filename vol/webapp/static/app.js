"use strict";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const readings = new Map(); // id -> summary
let status = {};
let gpsdActive = false;
let picking = false;
let geoWatch = null;
let lastSentGeo = null;

// ---------- theme ----------

const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("#theme").textContent = t[0].toUpperCase() + t.slice(1);
  try { if (t === "auto") localStorage.removeItem("theme"); else localStorage.setItem("theme", t); } catch (e) {}
}
applyTheme(document.documentElement.dataset.theme || "auto");
$("#theme").onclick = () => {
  const cur = document.documentElement.dataset.theme || "auto";
  applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
};

// ---------- stopwatch ----------

function hms(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

function tick() {
  const el = $("#timer");
  const st = status;
  if (st.running && st.started) {
    const total = (Date.now() - Date.parse(st.started)) / 1000;
    const band = st.band_started && st.step ? ` (B${st.band} ${hms((Date.now() - Date.parse(st.band_started)) / 1000)})` : "";
    el.textContent = `⏱ ${hms(total)}${band}`;
  } else if (st.started && st.finished) {
    const n = (st.bands || []).length;
    el.textContent = `last run ${hms((Date.parse(st.finished) - Date.parse(st.started)) / 1000)}` +
      (n > 1 ? ` (${n} bands)` : "");
  } else {
    el.textContent = "";
  }
}
setInterval(tick, 1000);

// ---------- API ----------

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// ---------- map ----------

const map = L.map("map", { zoomControl: true }).setView([39.5, -8.0], 7);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const readingsLayer = L.layerGroup().addTo(map);
let hereMarker = null;
let hereCircle = null;
let centred = false;

function rsrpColor(rsrp) {
  if (rsrp == null) return "#9ca3af";
  if (rsrp >= -80) return "#16a34a";
  if (rsrp >= -90) return "#65a30d";
  if (rsrp >= -100) return "#eab308";
  if (rsrp >= -110) return "#f97316";
  return "#dc2626";
}

function drawReadings() {
  readingsLayer.clearLayers();
  // one marker per place (≈1 m), listing every cell read there
  const places = new Map();
  for (const r of filteredReadings()) {
    if (r.lat == null || r.lon == null) continue;
    const key = r.lat.toFixed(5) + "," + r.lon.toFixed(5);
    if (!places.has(key)) places.set(key, []);
    places.get(key).push(r);
  }
  for (const list of places.values()) {
    const best = Math.max(...list.map((r) => r.rsrp ?? -200));
    const m = L.circleMarker([list[0].lat, list[0].lon], {
      radius: 8 + Math.min(list.length, 8), color: "#111827", weight: 1,
      fillColor: rsrpColor(best > -200 ? best : null), fillOpacity: 0.85,
    });
    const rows = list.map((r) =>
      `<tr><td>B${esc(r.band)}</td><td>${esc(r.dl_freq_mhz ?? "")} MHz</td><td>${esc(r.earfcn)}</td><td>PCI ${esc(r.pci ?? "?")}</td>` +
      `<td>${esc(r.cgi ?? "")}</td><td>${r.rsrp != null ? esc(r.rsrp) + " dBm" : ""}</td></tr>`).join("");
    m.bindPopup(`<b>${list.length} reading(s)</b><br>${esc(list[0].location_source ?? "")}` +
      (list[0].accuracy_m ? ` ±${Math.round(list[0].accuracy_m)} m` : "") +
      `<table>${rows}</table>`);
    readingsLayer.addLayer(m);
  }
}

function drawHere(loc) {
  if (!loc) {
    if (hereMarker) { map.removeLayer(hereMarker); map.removeLayer(hereCircle); hereMarker = null; }
    return;
  }
  const ll = [loc.lat, loc.lon];
  if (!hereMarker) {
    hereMarker = L.marker(ll, { title: "current position" }).addTo(map);
    hereCircle = L.circle(ll, { radius: loc.accuracy || 0, color: "#2563eb", weight: 1, fillOpacity: 0.08 }).addTo(map);
  } else {
    hereMarker.setLatLng(ll);
    hereCircle.setLatLng(ll).setRadius(loc.accuracy || 0);
  }
  if (!centred) {
    map.setView(ll, 15);
    centred = true;
  }
}

map.on("click", async (e) => {
  if (!picking) return;
  setPicking(false);
  try {
    await api("/api/location", { lat: e.latlng.lat, lon: e.latlng.lng, accuracy: null, source: "manual" });
  } catch (err) {
    $("#loc-hint").textContent = err.message;
  }
});

// ---------- location ----------

function showLocation(loc) {
  const t = $("#loc-text");
  if (!loc) {
    t.textContent = "no position yet";
  } else {
    const acc = loc.accuracy != null ? ` ±${Math.round(loc.accuracy)} m` : "";
    t.textContent = `${loc.source}: ${loc.lat.toFixed(6)}, ${loc.lon.toFixed(6)}${acc}`;
  }
  $("#loc-browser").disabled = gpsdActive;
  $("#loc-pick").disabled = gpsdActive;
  if (gpsdActive) $("#loc-hint").textContent = "gpsd has a fix: it is used for every reading.";
  drawHere(loc);
}

function setPicking(on) {
  picking = on;
  $("#map").classList.toggle("picking", on);
  $("#loc-pick").classList.toggle("active", on);
  $("#loc-hint").textContent = on ? "Click on the map where you are." :
    "gpsd is used when it has a fix; otherwise the browser's position (Wi-Fi based), " +
    "which you can correct by clicking on the map.";
}

function metres(a, b) {
  const R = 6371000, rad = Math.PI / 180;
  const dlat = (b.lat - a.lat) * rad, dlon = (b.lon - a.lon) * rad;
  const h = Math.sin(dlat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dlon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function startBrowserGeo() {
  if (!navigator.geolocation) {
    $("#loc-hint").textContent = "This browser has no geolocation: set your position on the map.";
    return;
  }
  if (geoWatch != null) return;
  geoWatch = navigator.geolocation.watchPosition(async (p) => {
    const loc = { lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy };
    // send only meaningful changes
    if (lastSentGeo && metres(lastSentGeo, loc) < 5 &&
        Math.abs((lastSentGeo.accuracy || 0) - loc.accuracy) < 5) return;
    lastSentGeo = loc;
    try { await api("/api/location", { ...loc, source: "browser" }); } catch (e) { /* manual wins */ }
  }, (err) => {
    $("#loc-hint").textContent = `Browser position unavailable (${err.message}): set it on the map.`;
  }, { enableHighAccuracy: true, maximumAge: 10000, timeout: 30000 });
}

$("#loc-browser").onclick = async () => {
  setPicking(false);
  lastSentGeo = null;
  await api("/api/location", { clear: true });
  if (geoWatch != null) { navigator.geolocation.clearWatch(geoWatch); geoWatch = null; }
  startBrowserGeo();
};
$("#loc-pick").onclick = () => setPicking(!picking);

// ---------- readings table ----------

function filteredReadings() {
  const sid = $("#scan-filter").value;
  const all = [...readings.values()].sort((a, b) => b.id - a.id);
  return sid ? all.filter((r) => String(r.scan_id) === sid) : all;
}

function renderTable(freshId) {
  const rows = filteredReadings().map((r) => {
    const loc = r.lat != null ? `${r.lat.toFixed(5)}, ${r.lon.toFixed(5)} (${esc(r.location_source)})` : "";
    const sibs = (r.has_mib ? "MIB " : "") + r.sibs.join(" ");
    const time = new Date(r.time).toLocaleString();
    return `<tr data-id="${r.id}"${r.id === freshId ? ' class="fresh"' : ""}>` +
      `<td>${esc(time)}</td><td>${esc(r.band)}</td>` +
      `<td>${r.dl_freq_mhz != null ? esc(r.dl_freq_mhz) + " MHz" : ""}</td><td>${esc(r.earfcn)}</td><td>${esc(r.pci ?? "")}</td>` +
      `<td>${esc(r.cgi ?? "")}</td><td>${esc(r.plmns ?? "")}</td><td>${esc(r.tac ?? "")}</td>` +
      `<td>${esc(r.enb_id ?? "")}</td><td>${esc(r.cell_id ?? "")}</td>` +
      `<td class="rsrp" style="color:${rsrpColor(r.rsrp)}">${r.rsrp != null ? esc(r.rsrp) : ""}</td>` +
      `<td>${esc(sibs)}</td><td>${loc}</td></tr>`;
  });
  $("#readings tbody").innerHTML = rows.join("");
  $("#count").textContent = `${rows.length} reading(s)`;
  drawReadings();
}

$("#readings tbody").onclick = (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) showDetail(Number(tr.dataset.id));
};
$("#scan-filter").onchange = () => renderTable();

async function loadScans() {
  const scans = await api("/api/scans");
  const sel = $("#scan-filter");
  const cur = sel.value;
  sel.innerHTML = '<option value="">all</option>' + scans.map((s) =>
    `<option value="${s.id}">#${s.id} ${esc(new Date(s.started).toLocaleString())}` +
    `${s.band ? " B" + esc(s.band) : ""} · ${s.readings} reading(s)` +
    `${s.finished ? " · " + hms((Date.parse(s.finished) - Date.parse(s.started)) / 1000) : " · running"}` +
    `</option>`).join("");
  sel.value = cur;
}

async function showDetail(id) {
  const r = await api(`/api/readings/${id}`);
  $("#detail-title").textContent = `EARFCN ${r.earfcn} · PCI ${r.pci ?? "?"} · ${r.cgi ?? "no SIB1"}`;
  const fields = [
    ["Time", r.time], ["Updated", r.updated], ["Scan", r.scan_id], ["Band", r.band],
    ["DL frequency", r.dl_freq_mhz != null ? r.dl_freq_mhz + " MHz" : ""],
    ["PLMNs", r.plmns], ["TAC", r.tac], ["ECI", r.eci], ["eNB ID", r.enb_id], ["Cell ID", r.cell_id],
    ["RSRP", r.rsrp != null ? r.rsrp + " dBm" : ""],
    ["Location", r.lat != null ? `${r.lat}, ${r.lon}` : ""],
    ["Accuracy", r.accuracy_m != null ? Math.round(r.accuracy_m) + " m" : ""],
    ["Location source", r.location_source], ["Location time", r.location_time],
  ];
  const blocks = ["mib", ...Array.from({ length: 13 }, (_, i) => "sib" + (i + 1))]
    .filter((k) => r[k] != null)
    .map((k) => `<details${k === "sib1" ? " open" : ""}><summary>${k.toUpperCase()}</summary>` +
      `<pre>${esc(JSON.stringify(r[k], null, 2))}</pre></details>`);
  $("#detail-body").innerHTML = "<dl>" + fields.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v ?? "")}</dd>`).join("") + "</dl>" + blocks.join("");
  $("#detail").showModal();
}

// ---------- scan form ----------

const form = $("#scan-form");

function updateFormMode() {
  const mode = form.mode.value;
  document.querySelector(".for-band").style.display = mode === "list" ? "none" : "";
  document.querySelector(".for-bands").style.display =
    mode !== "list" && form.band.value === "custom" ? "" : "none";
  document.querySelector(".for-list").style.display = mode === "list" ? "" : "none";
}
form.mode.onchange = updateFormMode;
form.band.onchange = updateFormMode;

form.onsubmit = async (e) => {
  e.preventDefault();
  $("#form-error").textContent = "";
  const body = Object.fromEntries(new FormData(form));
  body.recursive = form.recursive.checked;
  try {
    await api("/api/scan", body);
  } catch (err) {
    $("#form-error").textContent = err.message;
  }
};
$("#stop").onclick = () => api("/api/stop", {}).catch((e) => ($("#form-error").textContent = e.message));

function showStatus(st) {
  status = st;
  tick();
  const b = $("#state");
  b.textContent = st.running ? "running" : "idle";
  b.className = "badge " + (st.running ? "running" : "idle");
  $("#run").disabled = !!st.running;
  $("#stop").disabled = !st.running;
  const parts = [];
  if (st.step) parts.push(`band ${st.step}`);
  if (st.band) parts.push(`B${st.band}`);
  if (st.scan_id) parts.push(`scan #${st.scan_id}`);
  if (st.task) parts.push(st.task);
  if (st.earfcn) parts.push(`EARFCN ${st.earfcn}`);
  if (st.ppm != null) parts.push(`${st.ppm} ppm`);
  if (!st.running && st.exit_code != null) parts.push(`last exit ${st.exit_code}`);
  $("#activity").textContent = parts.join(" · ");
}

// ---------- log ----------

function appendLog(lines) {
  const el = $("#log");
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
  el.textContent += lines.map((l) => l + "\n").join("");
  // keep the last ~1500 lines
  const all = el.textContent.split("\n");
  if (all.length > 1500) el.textContent = all.slice(-1500).join("\n");
  if (atBottom) el.scrollTop = el.scrollHeight;
}

// ---------- live events ----------

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => $("#conn").classList.add("up");
  es.onerror = () => $("#conn").classList.remove("up");
  es.addEventListener("status", (e) => {
    const st = JSON.parse(e.data);
    const changed = st.scan_id !== status.scan_id || st.running !== status.running;
    showStatus(st);
    if (changed) loadScans();
  });
  es.addEventListener("backlog", (e) => {
    $("#log").textContent = "";
    appendLog(JSON.parse(e.data));
  });
  es.addEventListener("log", (e) => appendLog([JSON.parse(e.data)]));
  es.addEventListener("location", (e) => {
    const d = JSON.parse(e.data);
    gpsdActive = d.gpsd;
    showLocation(d.location);
  });
  es.addEventListener("reading", (e) => {
    const r = JSON.parse(e.data);
    readings.set(r.id, r);
    renderTable(r.id);
  });
}

// ---------- start ----------

(async function init() {
  const { bands, presets } = await api("/api/bands");
  form.band.innerHTML =
    presets.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("") +
    bands.map((b) =>
      `<option value="${b.band}"${b.band === 20 ? " selected" : ""}>B${b.band} ${esc(b.name)} ` +
      `(${b.start_mhz}–${b.end_mhz} MHz, ${esc(b.mode)})</option>`).join("") +
    '<option value="custom">Custom list…</option>';
  updateFormMode();
  for (const r of await api("/api/readings")) readings.set(r.id, r);
  await loadScans();
  renderTable();
  connect();
  startBrowserGeo();
})();
