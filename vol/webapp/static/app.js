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
let lastGeo = null; // last browser position, resent if the server lost it (restart)

// ---------- theme ----------

const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("#theme").textContent = t[0].toUpperCase() + t.slice(1);
  try { if (t === "auto") localStorage.removeItem("theme"); else localStorage.setItem("theme", t); } catch (e) {}
}
const params = new URLSearchParams(location.search);
if (params.has("theme")) {
  // theme from the URL: show it on the button without saving it
  $("#theme").textContent = (document.documentElement.dataset.theme || "auto").replace(/^./, (c) => c.toUpperCase());
} else {
  applyTheme(document.documentElement.dataset.theme || "auto");
}
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
    const band = st.band_started && st.step
      ? ` (${stepLabel(st.band)} ${hms((Date.now() - Date.parse(st.band_started)) / 1000)})` : "";
    // repeating: which run, and how long this one has taken
    const run = st.repeat || st.run > 1
      ? ` · run ${st.run} ${hms((Date.now() - Date.parse(st.run_started || st.started)) / 1000)}` +
        (st.repeat ? " ↻" : " (last)") : "";
    el.textContent = `⏱ ${hms(total)}${run}${band}`;
  } else if (st.started && st.finished) {
    // st.bands: the steps that have a band (LTE bands, "2G"), not an LTE list
    const steps = st.bands || [];
    const lte = steps.filter((b) => /^\d+$/.test(String(b))).length;
    const parts = [st.run > 1 ? `${st.run} runs` : "", lte > 1 ? `${lte} bands` : "",
      steps.includes("2G") ? "with 2G" : ""].filter(Boolean);
    el.textContent = `last ${st.run > 1 ? "session" : "run"} ${hms((Date.parse(st.finished) - Date.parse(st.started)) / 1000)}` +
      (parts.length ? ` (${parts.join(", ")})` : "");
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

// ?view=lat,lon,zoom sets the initial map view and keeps it (e.g. for sharing or screenshots)
const urlView = (params.get("view") || "").split(",").map(Number);
const hasView = urlView.length === 3 && urlView.every(Number.isFinite);
const map = L.map("map", { zoomControl: true })
  .setView(hasView ? [urlView[0], urlView[1]] : [39.5, -8.0], hasView ? urlView[2] : 7);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const readingsLayer = L.layerGroup().addTo(map);
let hereMarker = null;
let hereCircle = null;
let centred = hasView;

function rsrpColor(rsrp) {
  if (rsrp == null) return "#9ca3af";
  if (rsrp >= -80) return "#16a34a";
  if (rsrp >= -90) return "#65a30d";
  if (rsrp >= -100) return "#eab308";
  if (rsrp >= -110) return "#f97316";
  return "#dc2626";
}

// a run's step: an LTE band (B20), the 2G step, or an LTE list/preset (no band)
function stepLabel(b) {
  return b == null || b === "" ? "LTE" : /^\d+$/.test(String(b)) ? `B${b}` : String(b);
}

// LTE bands are numbers (B20), GSM ones names (GSM900)
const bandLabel = (b) => (/^\d+$/.test(String(b)) ? `B${b}` : String(b ?? ""));
const isGsm = (r) => r.rat === "GSM";
// what stands in for a missing CGI. GSM "BSIC only": the SCH decoded (it only
// exists in GSM, so not 3G) but no system information did: weak or interfered
const missingCgi = (r, lteDefault = "") => isGsm(r)
  ? ((r.gsm_si ?? []).length ? "no SI3" : "BSIC only (weak signal)")
  : r.detection === "pss" ? "detected only" : lteDefault;
const cgiHtml = (r) => (r.cgi != null ? esc(r.cgi) : `<span class="muted">${esc(missingCgi(r))}</span>`);
// operators by PLMN: coloured badge, or the user's logo from static/logos/
// (<plmn>.svg/.png, not in git: trademarks). RAN sharing lists several PLMNs.
const OPERATORS = {
  "268-01": { name: "Vodafone", bg: "#e60000", fg: "#fff" },
  "268-02": { name: "DIGI", bg: "#1d4f9c", fg: "#fff" },
  "268-03": { name: "NOS", bg: "#5360cb", fg: "#fff" }, // rgb(83, 96, 203), measured on the logo
  "268-06": { name: "MEO", bg: "#00a3e0", fg: "#fff" },
};
let logos = {}; // plmn -> URL, from /api/logos
function plmnHtml(plmns) {
  const codes = String(plmns ?? "").split(/\s+/).filter(Boolean);
  if (!codes.length) return "";
  // known operators: only the logo or badge (the code in its tooltip); any
  // other PLMN (e.g. abroad) as its code
  const marks = codes.map((p) => {
    const op = OPERATORS[p];
    const label = op ? `${op.name} (${p})` : p;
    if (logos[p]) return `<img class="op-logo" src="${esc(logos[p])}" alt="${esc(label)}" title="${esc(label)}">`;
    if (op) return `<span class="op-badge" style="background:${op.bg};color:${op.fg}" title="${esc(label)}">${esc(op.name)}</span>`;
    return `<span>${esc(p)}</span>`;
  }).join("");
  return `<span class="plmn-wrap">${marks}</span>`;
}
const decodedText = (r) => isGsm(r)
  ? (r.gsm_si.length ? "SI " + r.gsm_si.map((k) => k.slice(2)).join(" ") : "BSIC only")
  : r.detection === "pss" ? "detected only" : (r.has_mib ? "MIB " : "") + r.sibs.join(" ");

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
      `<tr><td>${esc(bandLabel(r.band))}</td><td>${esc(r.dl_freq_mhz ?? "")} MHz</td><td>${esc(r.earfcn)}</td>` +
      `<td>${isGsm(r) ? "BSIC" : "PCI"} ${esc(r.pci ?? "?")}</td>` +
      `<td>${cgiHtml(r)}</td>` +
      `<td>${r.rsrp != null ? esc(r.rsrp) + " dBm" : ""}</td></tr>`).join("");
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
    lastGeo = loc;
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

// sort keys of the readings table headers (data-sort); empty values always go last
const SORT_KEYS = {
  time: (r) => r.time,
  band: (r) => (r.band != null ? Number(r.band) : null),
  freq: (r) => r.dl_freq_mhz,
  bw: (r) => r.bandwidth_mhz,
  earfcn: (r) => r.earfcn,
  pci: (r) => r.pci,
  cgi: (r) => r.cgi,
  plmns: (r) => r.plmns,
  tac: (r) => r.tac,
  enb: (r) => r.enb_id,
  cell: (r) => r.cell_id,
  rsrp: (r) => r.rsrp,
  sibs: (r) => (isGsm(r) ? r.gsm_si.length : r.detection === "pss" ? -1 : r.sibs.length + (r.has_mib ? 1 : 0)),
  location: (r) => (r.location_source != null ? `${r.location_source} ${r.lat}` : null),
};
// first click on a column: newest / strongest / most complete first, otherwise ascending
const DESC_FIRST = new Set(["time", "rsrp", "sibs"]);
let sortBy = { key: "time", dir: "desc" };
try {
  const saved = JSON.parse(localStorage.getItem("readingsSort") || "null");
  if (saved && SORT_KEYS[saved.key] && (saved.dir === "asc" || saved.dir === "desc")) sortBy = saved;
} catch (e) {}

function compareReadings(a, b) {
  const get = SORT_KEYS[sortBy.key];
  const va = get(a), vb = get(b);
  if (va == null && vb == null) return b.id - a.id;
  if (va == null) return 1;
  if (vb == null) return -1;
  const c = typeof va === "string" ? va.localeCompare(vb, undefined, { numeric: true }) : va - vb;
  return (sortBy.dir === "asc" ? c : -c) || b.id - a.id;
}

function showSortHeaders() {
  for (const th of document.querySelectorAll("#readings th[data-sort]")) {
    const on = th.dataset.sort === sortBy.key;
    th.classList.toggle("sort-asc", on && sortBy.dir === "asc");
    th.classList.toggle("sort-desc", on && sortBy.dir === "desc");
    th.setAttribute("aria-sort", on ? (sortBy.dir === "asc" ? "ascending" : "descending") : "none");
  }
}

document.querySelector("#readings thead").onclick = (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  sortBy = sortBy.key === key
    ? { key, dir: sortBy.dir === "asc" ? "desc" : "asc" }
    : { key, dir: DESC_FIRST.has(key) ? "desc" : "asc" };
  try { localStorage.setItem("readingsSort", JSON.stringify(sortBy)); } catch (e) {}
  renderTable();
};

function filteredReadings() {
  const sid = $("#scan-filter").value;
  const all = [...readings.values()];
  return sid ? all.filter((r) => String(r.scan_id) === sid) : all;
}

function renderTable(freshId) {
  showSortHeaders();
  const rows = filteredReadings().sort(compareReadings).map((r) => {
    const loc = r.lat != null ? `${r.lat.toFixed(5)}, ${r.lon.toFixed(5)} (${esc(r.location_source)})` : "";
    const sibs = decodedText(r);
    const time = new Date(r.time).toLocaleString();
    return `<tr data-id="${r.id}"${r.id === freshId ? ' class="fresh"' : ""}>` +
      `<td>${esc(time)}</td><td>${esc(r.band)}</td>` +
      `<td>${r.dl_freq_mhz != null ? esc(r.dl_freq_mhz) + " MHz" : ""}</td>` +
      `<td>${r.bandwidth_mhz != null ? esc(r.bandwidth_mhz) + " MHz" : ""}</td><td>${esc(r.earfcn)}</td><td>${esc(r.pci ?? "")}</td>` +
      `<td>${cgiHtml(r)}</td><td>${plmnHtml(r.plmns)}</td><td>${esc(r.tac ?? "")}</td>` +
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

// export: the scan chosen in the filter, or every scan
$("#export-csv").onclick = () => {
  const sid = $("#scan-filter").value;
  window.location.href = "/api/export.csv" + (sid ? "?scan_id=" + encodeURIComponent(sid) : "");
};

$("#clear-db").onclick = async () => {
  const scans = $("#scan-filter").options.length - 1;
  if (!confirm(`Delete all ${readings.size} readings of ${scans} scan(s)?\n\n` +
      "This cannot be undone: export them first (Export CSV with Scan = all).\n" +
      "The Known EARFCNs list is kept.")) return;
  try {
    const r = await api("/api/clear", { confirm: true });
    appendLog([`[webapp] cleared ${r.readings} readings, ${r.scans} scans`]);
  } catch (err) {
    alert("Not cleared: " + err.message);
  }
};

function onCleared() {
  readings.clear();
  $("#scan-filter").value = "";
  renderTable();
  loadScans();
}

async function loadScans() {
  const scans = await api("/api/scans");
  const sel = $("#scan-filter");
  const cur = sel.value;
  sel.innerHTML = '<option value="">all</option>' + scans.map((s) =>
    `<option value="${s.id}">#${s.id} ${esc(new Date(s.started).toLocaleString())}` +
    `${s.band ? " B" + esc(s.band) : ""}${(s.args || "").startsWith("gsm_scan") ? " 2G" : ""} · ${s.readings} reading(s)` +
    `${s.finished ? " · " + hms((Date.parse(s.finished) - Date.parse(s.started)) / 1000) : " · running"}` +
    `</option>`).join("");
  sel.value = cur;
}

async function showDetail(id) {
  const r = await api(`/api/readings/${id}`);
  if (r.rat === "GSM") return showGsmDetail(r);
  $("#detail-title").textContent = `EARFCN ${r.earfcn} · PCI ${r.pci ?? "?"} · ` +
    (r.cgi ?? (r.detection === "pss" ? "detected only" : "no SIB1"));
  const fields = [
    ["Time", r.time], ["Updated", r.updated], ["Scan", r.scan_id], ["Band", r.band],
    ["DL frequency", r.dl_freq_mhz != null ? r.dl_freq_mhz + " MHz" : ""],
    ["PLMNs", r.plmns], ["TAC", r.tac], ["ECI", r.eci], ["eNB ID", r.enb_id], ["Cell ID", r.cell_id],
    ["RSRP", r.rsrp != null ? r.rsrp + " dBm" : ""],
    ["Bandwidth", r.bandwidth_mhz != null ? r.bandwidth_mhz + " MHz" : ""],
    ["Detection", r.detection === "pss" ? "PSS/SSS only (not decoded: too wide for this SDR)"
      : r.detection === "decoder" ? "decoded by lte_sib_decoder" : "decoded by srsue"],
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

function showGsmDetail(r) {
  const g = r.gsm ?? {};
  const si = g.si ?? {};
  const nsi = Object.keys(si).length;
  $("#detail-title").textContent = `ARFCN ${r.earfcn} · BSIC ${r.pci ?? "?"} · ` +
    (r.cgi ?? (nsi ? "no SI3" : "BSIC only (weak signal)"));
  const fields = [
    ["Time", r.time], ["Updated", r.updated], ["Scan", r.scan_id], ["Band", r.band],
    ["DL frequency", r.dl_freq_mhz != null ? r.dl_freq_mhz + " MHz" : ""],
    ["Bandwidth", r.bandwidth_mhz != null ? r.bandwidth_mhz + " MHz (every GSM carrier)" : ""],
    ["PLMN", r.plmns], ["LAC", r.tac], ["CI", r.cell_id],
    ["RSSI", r.rsrp != null ? r.rsrp + " dBm (BCCH carrier power, not calibrated: compare with other GSM readings, not with LTE RSRP)" : ""],
    ["Level", g.level_dbfs != null ? g.level_dbfs + " dBFS (at the SDR, before the gain is taken out)" : ""],
    ["Detection", nsi ? "GSM BCCH decoded by gsm_scan.py"
      : "GSM cell found by its FCCH/SCH (BSIC), but no system information decoded: " +
        "weak signal or interference from a nearby channel. Not 3G: UMTS has no FCCH/SCH."],
    ["Location", r.lat != null ? `${r.lat}, ${r.lon}` : ""],
    ["Accuracy", r.accuracy_m != null ? Math.round(r.accuracy_m) + " m" : ""],
    ["Location source", r.location_source], ["Location time", r.location_time],
  ];
  const blocks = Object.keys(si).sort().map((k) =>
    `<details${k === "si3" ? " open" : ""}><summary>${esc(k.toUpperCase())}</summary>` +
    `<pre>${esc(JSON.stringify(si[k], null, 2))}</pre></details>`);
  $("#detail-body").innerHTML = "<dl>" + fields.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v ?? "")}</dd>`).join("") + "</dl>" + blocks.join("");
  $("#detail").showModal();
}

// ---------- scan form ----------

const form = $("#scan-form");

// best values measured per SDR (Cisco 4G-LTE-ANTM-D antenna, strong-signal site;
// with the HackRF's stock antenna gains were 56 / 70)
const SDR_DEFAULTS = {
  hackrf: { device: "soapy", device_args: "driver=hackrf", gain: 44, gain_high: 56, t: 30, T: 30, hackrf_low_gain: "24,16", antennas: "1" },
  bladerf: { device: "bladeRF", device_args: "", gain: 15, gain_high: 40, t: 45, T: 30, hackrf_low_gain: "", antennas: "2" },
};

function applySdr(save) {
  const d = SDR_DEFAULTS[form.sdr.value];
  if (d) for (const [k, v] of Object.entries(d)) form[k].value = v;
  document.querySelector(".for-hackrf").style.display = form.sdr.value === "bladerf" ? "none" : "";
  document.querySelector(".for-bladerf").style.display = form.sdr.value === "bladerf" ? "" : "none";
  // with a bladeRF, sweep means wide captures searched by lte_sib_decoder (no
  // hackrf_sweep); the known-EARFCN preset is still the fastest start
  if (form.sdr.value === "bladerf" && form.mode.value === "cell_search") {
    if ([...form.band.options].some((o) => o.value === "pt_known")) form.band.value = "pt_known";
    updateFormMode();
  }
  if (save) try { localStorage.setItem("sdr", form.sdr.value); } catch (e) {}
}
form.sdr.onchange = () => applySdr(true);
try {
  const saved = localStorage.getItem("sdr");
  if (saved && [...form.sdr.options].some((o) => o.value === saved)) form.sdr.value = saved;
} catch (e) {}
applySdr(false);

// "Also 2G" is remembered per browser
try { form.gsm.checked = localStorage.getItem("gsm") === "1"; } catch (e) {}
form.gsm.onchange = () => { try { localStorage.setItem("gsm", form.gsm.checked ? "1" : "0"); } catch (e) {} };

// "Repeat until Stop" (not remembered: a page reload must not start endless runs).
// Changed during a run it applies at once: unticked, the run in progress is the last.
form.repeat.onchange = () => {
  if (status.running) api("/api/repeat", { repeat: form.repeat.checked }).catch((e) => ($("#form-error").textContent = e.message));
};

function updateFormMode() {
  const mode = form.mode.value;
  document.querySelector(".for-band").style.display = mode === "list" || mode === "gsm" ? "none" : "";
  document.querySelector(".for-bands").style.display =
    mode !== "list" && mode !== "gsm" && form.band.value === "custom" ? "" : "none";
  for (const el of document.querySelectorAll(".for-lte")) el.style.display = mode === "gsm" ? "none" : "";
  document.querySelector(".for-list").style.display = mode === "list" ? "" : "none";
}
// mode and band are remembered per browser (default: the known-EARFCN preset)
function saveChoice(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
form.mode.onchange = () => { saveChoice("mode", form.mode.value); updateFormMode(); };
form.band.onchange = () => { saveChoice("band", form.band.value); updateFormMode(); };

form.onsubmit = async (e) => {
  e.preventDefault();
  $("#form-error").textContent = "";
  const body = Object.fromEntries(new FormData(form));
  body.recursive = form.recursive.checked;
  body.gsm = form.gsm.checked;
  body.repeat = form.repeat.checked;
  try {
    await api("/api/scan", body);
  } catch (err) {
    $("#form-error").textContent = err.message;
  }
};
$("#stop").onclick = () => api("/api/stop", {}).catch((e) => ($("#form-error").textContent = e.message));

function showStatus(st) {
  status = st;
  if (st.running) form.repeat.checked = !!st.repeat; // e.g. another tab changed it
  tick();
  const b = $("#state");
  b.textContent = st.running ? "running" : "idle";
  b.className = "badge " + (st.running ? "running" : "idle");
  $("#run").disabled = !!st.running;
  $("#stop").disabled = !st.running;
  const parts = [];
  if (st.step) parts.push(`band ${st.step}`);
  if (st.band) parts.push(bandLabel(st.band));
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

// lines arrive while the panel is closed: show the latest ones when it opens
$("#log-details").addEventListener("toggle", (e) => {
  if (e.target.open) $("#log").scrollTop = $("#log").scrollHeight;
});

// ---------- known EARFCNs ----------

async function loadEarfcns() {
  const rows = await api("/api/earfcns");
  $("#earfcn-count").textContent = `(${rows.length})`;
  $("#earfcns tbody").innerHTML = rows.map((r) => {
    const src = [r.in_list_file ? "list" : "", r.last_seen ? "read" : "", r.last_advertised ? "SIB5" : ""]
      .filter(Boolean).join(" · ");
    const last = r.last_seen ? new Date(r.last_seen).toLocaleDateString() : "";
    return `<tr><td>${esc(r.earfcn)}</td><td>${esc(r.band ?? "?")}</td><td>${esc(r.dl_freq_mhz ?? "")}</td>` +
      `<td>${r.bandwidth_mhz != null ? esc(r.bandwidth_mhz) : ""}</td><td>${esc(src)}</td><td>${esc(last)}</td></tr>`;
  }).join("");
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
    if (changed && !st.running) loadEarfcns();
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
    // the server restarted and has no position: give it the browser's again
    if (!d.location && lastGeo && geoWatch != null) {
      lastSentGeo = lastGeo;
      api("/api/location", { ...lastGeo, source: "browser" }).catch(() => {});
    }
  });
  es.addEventListener("cleared", () => onCleared()); // also from another tab
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
      `<option value="${b.band}">B${b.band} ${esc(b.name)} ` +
      `(${b.start_mhz}–${b.end_mhz} MHz, ${esc(b.mode)})</option>`).join("") +
    '<option value="custom">Custom list…</option>';
  applySdr(false); // again now that the band list (and its presets) exists
  // the last mode and band chosen in this browser, else Portugal (known EARFCNs)
  const has = (sel, v) => v != null && [...sel.options].some((o) => o.value === v);
  let savedMode = null, savedBand = null;
  try { savedMode = localStorage.getItem("mode"); savedBand = localStorage.getItem("band"); } catch (e) {}
  if (has(form.mode, savedMode)) form.mode.value = savedMode;
  form.band.value = has(form.band, savedBand) ? savedBand : has(form.band, "pt_known") ? "pt_known" : form.band.value;
  updateFormMode();
  logos = await api("/api/logos").catch(() => ({}));
  for (const r of await api("/api/readings")) readings.set(r.id, r);
  await loadScans();
  renderTable();
  loadEarfcns();
  connect();
  startBrowserGeo();
})();
