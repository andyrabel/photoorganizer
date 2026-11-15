'use strict';
/* Photo Organizer client: left pane (People / Folders), right pane (library grid + viewer). */

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const store = {
  get(k, d) { try { const v = localStorage.getItem('po.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('po.' + k, JSON.stringify(v)); } catch { /* ignore */ } },
};

async function api(url, opts = {}) {
  const o = { ...opts };
  if (o.body && typeof o.body !== 'string') {
    o.body = JSON.stringify(o.body);
    o.headers = { 'Content-Type': 'application/json' };
  }
  const r = await fetch(url, o);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}
const post = (url, body = {}) => api(url, { method: 'POST', body });

let toastTimer;
function toast(msg, ms = 3000) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
}
function fail(e) { console.error(e); toast('Error: ' + e.message, 5000); }

const thumbUrl = m => `/api/media/${m.id}/thumb?v=${m.ver}`;
const faceUrl = id => `/api/faces/${id}/thumb`;
const FOLDER_SVG = '<svg class="folder-ico" viewBox="0 0 20 16"><path d="M1 3a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1z" fill="#e8c26a" stroke="#b8923a"/></svg>';

function fmtDate(ts, opts = { month: 'short', day: 'numeric', year: 'numeric' }) {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, opts) : '';
}

// ------------------------------------------------------------------ state
const S = {
  mode: null,         // 'folder' | 'person' | 'unknown'
  folder: null, personId: null, clusterId: null,
  items: [], shown: [],
  folders: [], roots: [], people: [], clusters: [],
  lastTotal: -1,
};

// ------------------------------------------------------------------ left pane
function setupSections() {
  const collapsed = store.get('collapsed', {});
  document.querySelectorAll('.sec').forEach(sec => {
    if (collapsed[sec.dataset.sec]) sec.classList.add('collapsed');
    $('.sec-head', sec).addEventListener('click', () => {
      sec.classList.toggle('collapsed');
      const c = store.get('collapsed', {}); c[sec.dataset.sec] = sec.classList.contains('collapsed'); store.set('collapsed', c);
    });
  });
  // resizable left pane
  const sp = $('#splitter');
  const w = store.get('leftW', null); if (w) document.documentElement.style.setProperty('--left-w', w + 'px');
  sp.addEventListener('pointerdown', e => {
    sp.setPointerCapture(e.pointerId);
    const move = ev => { const nw = clamp(ev.clientX, 180, 600); document.documentElement.style.setProperty('--left-w', nw + 'px'); store.set('leftW', nw); };
    const up = () => { sp.removeEventListener('pointermove', move); sp.removeEventListener('pointerup', up); if (!$('#viewer').hidden) layout(); };
    sp.addEventListener('pointermove', move); sp.addEventListener('pointerup', up);
  });
}

function folderLabel(f) {
  if (f.rel === '.') return f.root.split('/').pop();
  const parts = f.rel.split('/');
  const last = parts[parts.length - 1];
  if (parts.length > 1 && /^\d{1,2}$/.test(last)) return parts.slice(-2).join('-');
  return last;
}

// Year for grouping: prefer a year in the path (e.g. PhotoSync/2020/08), else the earliest photo date
function folderYear(f) {
  const m = f.rel.match(/(?:^|\/)((?:19|20)\d\d)(?:\/|$)/);
  if (m) return +m[1];
  return f.first ? new Date(f.first * 1000).getFullYear() : 'Undated';
}
function folderSortKey(f) {
  const m = f.rel.match(/(?:^|\/)((?:19|20)\d\d)\/(\d{1,2})(?:\/|$)/);
  if (m) return `${m[1]}-${m[2].padStart(2, '0')}-${f.rel}`;
  const d = f.first ? new Date(f.first * 1000) : null;
  return d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${f.rel}` : '0';
}

async function loadFolders() {
  const d = await api('/api/folders');
  S.folders = d.folders; S.roots = d.roots;
  renderFolders();
}

// The search box matches a folder by its display name or its path below the watched root
const filterQuery = () => $('#filter').value.trim().toLowerCase();
function folderMatches(f, q) {
  return !q || folderLabel(f).toLowerCase().includes(q) || f.rel.toLowerCase().includes(q);
}

function renderFolders() {
  const el = $('#folderList');
  const q = filterQuery();
  const folders = S.folders.filter(f => folderMatches(f, q));
  $('#folderCount').textContent = q ? `(${folders.length} of ${S.folders.length})` : `(${S.folders.length})`;
  if (!S.folders.length) {
    el.innerHTML = `<div class="hint">No photos found yet. ${S.roots.length ? 'Indexing may still be running…' : 'Use Folder Manager to add your Pictures folder.'}</div>`;
    return;
  }
  if (!folders.length) {
    el.innerHTML = '<div class="hint">No folder names match the search.</div>';
    return;
  }
  const byYear = new Map();
  for (const f of folders) {
    const y = folderYear(f);
    if (!byYear.has(y)) byYear.set(y, []);
    byYear.get(y).push(f);
  }
  const years = [...byYear.keys()].sort((a, b) => (b === 'Undated') - (a === 'Undated') || b - a);
  let html = '';
  for (const y of years) {
    html += `<div class="year">${y}</div>`;
    for (const f of byYear.get(y).sort((a, b) => folderSortKey(b).localeCompare(folderSortKey(a))))
      html += `<div class="item folder${S.mode === 'folder' && S.folder === f.path ? ' sel' : ''}" data-path="${esc(f.path)}" title="${esc(f.path)}">${FOLDER_SVG}<span class="label">${esc(folderLabel(f))}</span><span class="n">(${f.count})</span></div>`;
  }
  el.innerHTML = html;
}

$('#folderList').addEventListener('click', e => {
  const it = e.target.closest('.item'); if (it) openFolder(it.dataset.path);
});

async function loadPeople() {
  const d = await api('/api/people');
  S.people = d.people;
  $('#peopleNames').innerHTML = S.people.map(p => `<option value="${esc(p.name)}">`).join('');
  renderPeople();
}
async function loadClusters() {
  try { S.clusters = (await api('/api/unknown')).clusters; } catch (e) { S.clusters = []; }
  renderPeople();
}
const personById = id => S.people.find(p => p.id === id);

function renderPeople() {
  const el = $('#peopleList');
  $('#peopleCount').textContent = `(${S.people.length})`;
  let html = '';
  for (const p of S.people) {
    html += `<div class="item person${S.mode === 'person' && S.personId === p.id ? ' sel' : ''}" data-person="${p.id}">
      ${p.cover ? `<img class="avatar" loading="lazy" src="${faceUrl(p.cover)}">` : '<span class="avatar"></span>'}
      <span class="label">${esc(p.name)}</span><span class="n">(${p.n})</span></div>`;
  }
  if (S.clusters.length) {
    html += '<div class="subhead">Unnamed — click to name</div>';
    for (const c of S.clusters)
      html += `<div class="item person unknown${S.mode === 'unknown' && S.clusterId === c.id ? ' sel' : ''}" data-cluster="${c.id}">
        <img class="avatar" loading="lazy" src="${faceUrl(c.faces[0])}"><span class="label">&lt;Unknown Person&gt;</span><span class="n">(${c.count})</span></div>`;
  }
  if (!html) html = '<div class="hint">No faces found yet. Faces are detected in the background — open a folder to scan it first. Name a face in the viewer to start a person.</div>';
  el.innerHTML = html;
}

$('#peopleList').addEventListener('click', e => {
  const it = e.target.closest('.item'); if (!it) return;
  if (it.dataset.person) openPerson(+it.dataset.person);
  else openCluster(+it.dataset.cluster);
});

let peopleRefreshTimer;
function refreshPeopleSoon(withClusters = true) {
  clearTimeout(peopleRefreshTimer);
  peopleRefreshTimer = setTimeout(async () => {
    await loadPeople().catch(fail);
    if (withClusters) loadClusters();
  }, 400);
}

// ------------------------------------------------------------------ library (right pane)
function showLibrary() {
  exitCrop(); exitDraw();
  $('#viewer').hidden = true; $('#library').hidden = false;
  const v = $('#mediaBox video'); if (v) v.pause();
}

function selectLeft() { renderFolders(); renderPeople(); }

async function openFolder(path) {
  S.mode = 'folder'; S.folder = path; store.set('last', { folder: path });
  selectLeft(); showLibrary();
  const f = S.folders.find(x => x.path === path);
  setHeader(`${FOLDER_SVG.replace('folder-ico', 'big-ico')}
    <div><h2>${esc(f ? folderLabel(f) : path.split('/').pop())}</h2>
    <div class="sub" id="folderDate"></div><div class="path">${esc(path)}</div></div>`);
  $('#libExtra').innerHTML = '';
  setItems([]);
  const d = await api('/api/media?folder=' + encodeURIComponent(path));
  if (S.mode !== 'folder' || S.folder !== path) return;
  setItems(d.items);
  const first = Math.min(...d.items.map(m => m.taken || Infinity));
  if (isFinite(first)) $('#folderDate').textContent = fmtDate(first);
}

async function openPerson(pid, keepScroll = false) {
  S.mode = 'person'; S.personId = pid; store.set('last', { person: pid });
  selectLeft(); showLibrary();
  const p = personById(pid);
  if (!p) return;
  setHeader(`${p.cover ? `<img class="avatar" src="${faceUrl(p.cover)}">` : ''}
    <div><h2>${esc(p.name)}</h2><div class="sub">${p.n} photo${p.n === 1 ? '' : 's'}</div></div>
    <div class="actions"><button id="pRename">Rename…</button><button id="pDelete">Delete person</button></div>`);
  $('#pRename').onclick = async () => {
    const name = prompt('Rename person (use an existing name to merge):', p.name);
    if (!name || name.trim() === p.name) return;
    const r = await api(`/api/people/${pid}`, { method: 'PATCH', body: { name } }).catch(fail);
    if (!r) return;
    await loadPeople(); openPerson(r.person_id);
    if (r.merged) toast('Merged into existing person');
  };
  $('#pDelete').onclick = async () => {
    if (!confirm(`Remove "${p.name}"? Their faces become unnamed again; photos are not touched.`)) return;
    await api(`/api/people/${pid}`, { method: 'DELETE' }).catch(fail);
    await loadPeople(); loadClusters(); S.mode = null; setHeader(''); setItems([]); $('#libExtra').innerHTML = '';
  };
  const grid = $('#grid'); const st = keepScroll ? grid.scrollTop : 0;
  const d = await api('/api/media?person=' + pid);
  if (S.mode !== 'person' || S.personId !== pid) return;
  setItems(d.items); grid.scrollTop = st;
  loadSuggestions(pid);
}

async function loadSuggestions(pid) {
  const box = $('#libExtra');
  box.innerHTML = '<span class="muted">Looking for more photos of this person…</span>';
  const d = await api(`/api/people/${pid}/suggestions`).catch(() => ({ suggestions: [] }));
  if (S.mode !== 'person' || S.personId !== pid) return;
  const p = personById(pid);
  const list = d.suggestions.slice(0, 60);
  if (!list.length) { box.innerHTML = `<span class="muted">No suggested matches for ${esc(p.name)} right now. More appear as photos are scanned and more faces are confirmed.</span>`; return; }
  box.innerHTML = `<div class="extra-head"><h4>Is this ${esc(p.name)}? <span class="muted">(${d.suggestions.length} suggested match${d.suggestions.length === 1 ? '' : 'es'})</span></h4>
      <button id="sgAll" class="primary">✓ Yes to all shown</button><button id="sgNone">✗ No to all shown</button>
      <span class="muted">Click a face to open its photo.</span></div>
    <div class="faces-row">${list.map(s => `<div class="ftile" data-face="${s.face_id}" data-media="${s.media_id}" title="similarity ${Math.round(s.score * 100)}%">
      <img loading="lazy" src="${faceUrl(s.face_id)}"><div class="btns"><button class="yes" title="Yes">✓</button><button class="no" title="No">✗</button></div>
      ${Math.round(s.score * 100)}%</div>`).join('')}</div>`;
  const done = () => { if (!box.querySelector('.ftile')) { refreshPeopleSoon(); openPerson(pid, true); } };
  box.querySelectorAll('.ftile').forEach(t => {
    const fid = +t.dataset.face;
    $('.yes', t).onclick = async () => { t.remove(); await post('/api/faces/assign', { face_ids: [fid], person_id: pid }).catch(fail); done(); };
    $('.no', t).onclick = async () => { t.remove(); await post('/api/faces/reject', { face_ids: [fid], person_id: pid }).catch(fail); done(); };
    $('img', t).onclick = () => openStandalone(+t.dataset.media);
  });
  const ids = () => [...box.querySelectorAll('.ftile')].map(t => +t.dataset.face);
  $('#sgAll').onclick = async () => { const f = ids(); await post('/api/faces/assign', { face_ids: f, person_id: pid }).catch(fail); toast(`Added ${f.length} faces to ${p.name}`); refreshPeopleSoon(); openPerson(pid); };
  $('#sgNone').onclick = async () => { await post('/api/faces/reject', { face_ids: ids(), person_id: pid }).catch(fail); loadSuggestions(pid); };
}

function openCluster(cid) {
  const c = S.clusters.find(x => x.id === cid); if (!c) return;
  S.mode = 'unknown'; S.clusterId = cid;
  selectLeft(); showLibrary();
  setHeader(`<img class="avatar" src="${faceUrl(c.faces[0])}"><div><h2>&lt;Unknown Person&gt;</h2>
    <div class="sub">${c.count} similar faces</div></div>`);
  const sg = c.suggest && c.suggest.name;
  $('#libExtra').innerHTML = `<div class="extra-head">
      <form id="nameForm" style="display:flex;gap:6px"><input id="nameInput" list="peopleNames" placeholder="Who is this?" autocomplete="off" required><button class="primary">Add name</button></form>
      ${sg ? `<span class="pill">Looks like <b>${esc(sg)}</b> <button id="sgYes" class="yes">✓ Yes</button></span>` : ''}
      <button id="ignoreSel" title="Hide the selected faces (not real faces, or people you don't want to name)">Ignore selected</button>
    </div><div class="muted">Click faces that are <b>not</b> this person to leave them out. Double-click a face to open its photo.</div>`;
  const grid = $('#grid');
  grid.className = 'faces';
  $('#empty').hidden = true;
  grid.innerHTML = c.faces.map(f => `<div class="ftile" data-face="${f}"><img loading="lazy" src="${faceUrl(f)}"></div>`).join('');
  $('#libInfo').textContent = `${c.count} faces`;
  grid.querySelectorAll('.ftile').forEach(t => {
    t.onclick = () => t.classList.toggle('off');
    t.ondblclick = async () => {
      t.classList.remove('off');
      const d = await api(`/api/faces/${t.dataset.face}`).catch(fail);
      if (d) openStandalone(d.media_id);
    };
  });
  const selected = () => [...grid.querySelectorAll('.ftile:not(.off)')].map(t => +t.dataset.face);
  const assign = async body => {
    const f = selected(); if (!f.length) return toast('No faces selected');
    const r = await post('/api/faces/assign', { face_ids: f, ...body }).catch(fail);
    if (!r) return;
    toast(`Named ${f.length} face${f.length === 1 ? '' : 's'}`);
    await loadPeople(); loadClusters(); openPerson(r.person_id);
  };
  $('#nameForm').onsubmit = e => { e.preventDefault(); assign({ name: $('#nameInput').value.trim() }); };
  if (sg) $('#sgYes').onclick = () => assign({ person_id: c.suggest.person_id });
  $('#ignoreSel').onclick = async () => {
    const f = selected(); if (!f.length) return;
    if (!confirm(`Ignore ${f.length} face(s)? They will no longer appear under People.`)) return;
    await post('/api/faces/ignore', { face_ids: f }).catch(fail);
    S.clusters = S.clusters.filter(x => x.id !== cid); renderPeople(); setHeader(''); grid.innerHTML = ''; $('#libExtra').innerHTML = '';
    loadClusters();
  };
  $('#nameInput').focus();
}

function setHeader(html) { $('#libHeader').innerHTML = html; }

function setItems(items) {
  S.items = items;
  applyFilter();
}
function applyFilter() {
  const q = filterQuery();
  if (!q) { S.shown = S.items.slice(); renderGrid(); return; }
  const byPath = new Map(S.folders.map(f => [f.path, f]));
  const folderHit = new Map();  // folder path -> does its name match?
  S.shown = S.items.filter(m => {
    if (m.name.toLowerCase().includes(q)) return true;
    if (!folderHit.has(m.folder)) {
      const f = byPath.get(m.folder);
      folderHit.set(m.folder, f ? folderMatches(f, q) : m.folder.toLowerCase().includes(q));
    }
    return folderHit.get(m.folder);
  });
  renderGrid();
}
$('#filter').addEventListener('input', () => { renderFolders(); if (S.mode !== 'unknown') applyFilter(); });

function renderGrid() {
  const grid = $('#grid');
  grid.className = '';
  grid.innerHTML = S.shown.map((m, i) => `<div class="tile" data-i="${i}" title="${esc(m.name)}${m.taken ? ' — ' + fmtDate(m.taken) : ''}">
      <img loading="lazy" decoding="async" src="${thumbUrl(m)}" alt="">
      ${m.kind === 'video' ? '<span class="vid">▶ video</span>' : ''}${m.rotation || m.crop ? '<span class="edited">edited</span>' : ''}</div>`).join('');
  const empty = $('#empty');
  empty.hidden = !!S.shown.length || !S.mode;
  empty.textContent = S.items.length ? 'Nothing matches the filter.' : 'Loading…';
  const vids = S.shown.filter(m => m.kind === 'video').length;
  $('#libInfo').textContent = S.shown.length ? `${S.shown.length - vids} photos${vids ? `, ${vids} videos` : ''}` : '';
}

$('#grid').addEventListener('click', e => {
  const t = e.target.closest('.tile'); if (t) openViewer(+t.dataset.i);
});

$('#thumbSize').addEventListener('input', e => {
  document.documentElement.style.setProperty('--thumb', e.target.value + 'px'); store.set('thumb', +e.target.value);
});

// ------------------------------------------------------------------ viewer
const V = {
  idx: -1, item: null, natW: 0, natH: 0, zoom: 1, panX: 0, panY: 0, fit: 1, s: 1,
  faces: [], cropMode: false, cropRect: null, aspect: 0, drawMode: false, token: 0,
  standalone: null,  // {items, back} when opened from a suggestion/face outside the current list
};
const stage = $('#stage'), viewport = $('#viewport'), plane = $('#plane'), mediaBox = $('#mediaBox');

function viewerList() { return V.standalone ? V.standalone.items : S.shown; }

function openViewer(i) {
  V.standalone = null;
  $('#library').hidden = true; $('#viewer').hidden = false;
  showItem(i);
}

async function openStandalone(mediaId) {
  const d = await api(`/api/media/${mediaId}`).catch(fail);
  if (!d) return;
  V.standalone = { items: [d.media] };
  $('#library').hidden = true; $('#viewer').hidden = false;
  showItem(0);
}

function showItem(i) {
  const list = viewerList();
  if (i < 0 || i >= list.length) return;
  exitCrop(); exitDraw();
  const m = list[i];
  V.idx = i; V.item = m; V.zoom = 1; V.panX = V.panY = 0; V.natW = V.natH = 0; V.faces = [];
  const token = ++V.token;
  mediaBox.innerHTML = '';
  $('#faceList').innerHTML = '';
  $('#vName').textContent = m.name;
  $('#stageMsg').hidden = true;
  let el;
  if (m.kind === 'video') {
    el = document.createElement('video');
    el.controls = true; el.preload = 'metadata'; el.playsInline = true;
    el.onloadedmetadata = () => { if (token === V.token) { V.natW = el.videoWidth; V.natH = el.videoHeight; layout(); } };
    el.onerror = () => { if (token === V.token) stageMsg('This video format cannot be played by the browser (often HEVC/H.265 .mov files from iPhones). Try Microsoft Edge, or install the HEVC Video Extensions in Windows.'); };
  } else {
    el = document.createElement('img');
    el.draggable = false;
    el.onload = () => { if (token === V.token) { V.natW = el.naturalWidth; V.natH = el.naturalHeight; layout(); } };
    el.onerror = () => { if (token === V.token) stageMsg('This image could not be displayed by the browser.'); };
  }
  el.src = `/api/media/${m.id}/file`;
  mediaBox.appendChild(el);
  layout();
  renderFilmstrip();
  updateNavButtons();
  $('#fileInfo').innerHTML = `<div><b>${esc(m.name)}</b></div><div>${m.taken ? fmtDate(m.taken, { dateStyle: 'full' }) + ' ' + new Date(m.taken * 1000).toLocaleTimeString() : ''}</div>
     <div>${m.width && m.height ? `${m.width} × ${m.height}` : ''}</div><div>${esc(m.folder)}</div>`;
  loadFaces(m.id, token);
  // preload neighbours
  for (const j of [i + 1, i - 1]) { const n = list[j]; if (n && n.kind === 'image') { const im = new Image(); im.src = `/api/media/${n.id}/file`; } }
}

function stageMsg(t) { const el = $('#stageMsg'); el.textContent = t; el.hidden = false; }

function updateNavButtons() {
  const n = viewerList().length;
  $('#vPrev').disabled = V.idx <= 0; $('#vNext').disabled = V.idx >= n - 1;
  const vid = V.item && V.item.kind === 'video';
  $('#vExport').disabled = vid; $('#vAddFace').disabled = vid;
  $('#vExport').title = vid ? 'Saving edited videos is not supported yet (edits are still shown here)' : 'Save an edited copy next to the original';
}

function layout() {
  const sw = stage.clientWidth, sh = stage.clientHeight;
  if (!V.natW || !sw) { viewport.style.width = '0px'; return; }
  const W = V.natW, H = V.natH, r = (V.item.rotation || 0) % 4;
  const RW = r % 2 ? H : W, RH = r % 2 ? W : H;
  const c = V.cropMode ? [0, 0, 1, 1] : (V.item.crop || [0, 0, 1, 1]);
  const CW = c[2] * RW, CH = c[3] * RH;
  const pad = V.cropMode ? 50 : 0;
  V.fit = Math.min((sw - pad) / CW, (sh - pad) / CH);
  const s = V.s = V.fit * V.zoom;
  Object.assign(viewport.style, { width: CW * s + 'px', height: CH * s + 'px', left: (sw - CW * s) / 2 + V.panX + 'px', top: (sh - CH * s) / 2 + V.panY + 'px' });
  Object.assign(plane.style, { width: RW * s + 'px', height: RH * s + 'px', left: -c[0] * RW * s + 'px', top: -c[1] * RH * s + 'px' });
  Object.assign(mediaBox.style, { width: W * s + 'px', height: H * s + 'px', left: (RW - W) * s / 2 + 'px', top: (RH - H) * s / 2 + 'px', transform: `rotate(${r * 90}deg)` });
  mediaBox.style.setProperty('--unrot', `${-r * 90}deg`);
  $('#vZoomLabel').textContent = Math.round(s * 100) + '%';
  if (V.cropMode) renderCropBox();
}
new ResizeObserver(() => { if (!$('#viewer').hidden) layout(); }).observe(stage);

function zoomAt(clientX, clientY, newZoom) {
  const rect = stage.getBoundingClientRect();
  const qx = clientX - rect.left - rect.width / 2, qy = clientY - rect.top - rect.height / 2;
  newZoom = clamp(newZoom, 0.2, Math.max(1, 10 / V.fit));
  const k = newZoom / V.zoom;
  V.panX = qx - (qx - V.panX) * k; V.panY = qy - (qy - V.panY) * k;
  V.zoom = newZoom;
  if (Math.abs(V.zoom - 1) < 0.01 && k < 1) { V.zoom = 1; V.panX = V.panY = 0; }
  layout();
}
function zoomCenter(f) { const r = stage.getBoundingClientRect(); zoomAt(r.left + r.width / 2, r.top + r.height / 2, V.zoom * f); }
function zoomFit() { V.zoom = 1; V.panX = V.panY = 0; layout(); }
function zoomActual() { const r = stage.getBoundingClientRect(); zoomAt(r.left + r.width / 2, r.top + r.height / 2, 1 / V.fit); }

stage.addEventListener('wheel', e => {
  if (V.cropMode || !V.natW) return;
  e.preventDefault();
  zoomAt(e.clientX, e.clientY, V.zoom * Math.exp(-e.deltaY * 0.0015));
}, { passive: false });

stage.addEventListener('dblclick', e => {
  if (V.cropMode || V.drawMode || e.target.closest('.fbox') || e.target.tagName === 'VIDEO') return;
  if (V.zoom > 1.01) zoomFit(); else zoomAt(e.clientX, e.clientY, Math.max(2.5, 1 / V.fit));
});

stage.addEventListener('pointerdown', e => {
  if (e.button !== 0 || !V.natW) return;
  if (V.cropMode) return cropPointerDown(e);
  if (V.drawMode) return drawPointerDown(e);
  if (e.target.closest('.fbox')) return;
  if (e.target.tagName === 'VIDEO' && V.zoom <= 1.01) return; // let the video controls work
  e.preventDefault();
  const sx = e.clientX, sy = e.clientY, px = V.panX, py = V.panY;
  stage.setPointerCapture(e.pointerId); stage.classList.add('panning');
  const move = ev => { V.panX = px + ev.clientX - sx; V.panY = py + ev.clientY - sy; layout(); };
  const up = () => { stage.classList.remove('panning'); stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerup', up); };
  stage.addEventListener('pointermove', move); stage.addEventListener('pointerup', up);
});

// ---- edits
async function saveEdit(patch) {
  const m = V.item;
  const r = await post(`/api/media/${m.id}/edit`, patch).catch(fail);
  if (!r) return;
  Object.assign(m, r.media);
  // keep the copy in the library list in sync too
  const other = S.items.find(x => x.id === m.id); if (other && other !== m) Object.assign(other, r.media);
  layout(); renderFilmstrip(); renderGridLater();
}
let gridDirty = false;
function renderGridLater() { gridDirty = true; }

function rotateCrop(c, dir) { // dir: +1 = clockwise
  if (!c) return null;
  const [x, y, w, h] = c;
  return dir > 0 ? [1 - (y + h), x, h, w] : [y, 1 - (x + w), h, w];
}
function rotate(dir) {
  if (V.cropMode) return;
  const m = V.item;
  saveEdit({ rotation: ((m.rotation || 0) + dir + 4) % 4, crop: rotateCrop(m.crop, dir) });
  V.zoom = 1; V.panX = V.panY = 0;
}

// ---- crop
function enterCrop() {
  if (!V.natW) return;
  exitDraw();
  V.cropMode = true; V.zoom = 1; V.panX = V.panY = 0;
  V.cropRect = (V.item.crop || [0, 0, 1, 1]).slice();
  $('#cropBar').hidden = false; $('#vCrop').classList.add('on');
  ['#vRotL', '#vRotR', '#vAddFace'].forEach(s => ($(s).disabled = true));
  let box = $('#cropBox');
  if (!box) {
    box = document.createElement('div'); box.id = 'cropBox';
    box.innerHTML = '<i class="h nw" data-c="nw"></i><i class="h ne" data-c="ne"></i><i class="h sw" data-c="sw"></i><i class="h se" data-c="se"></i>';
    plane.appendChild(box);
  }
  V.aspect = 0; $('#cropAspect').value = '0';
  layout();
}
function exitCrop() {
  if (!V.cropMode) return;
  V.cropMode = false; $('#cropBar').hidden = true; $('#vCrop').classList.remove('on');
  const b = $('#cropBox'); if (b) b.remove();
  ['#vRotL', '#vRotR'].forEach(s => ($(s).disabled = false));
  updateNavButtons();
  layout();
}
function renderCropBox() {
  const [x, y, w, h] = V.cropRect, b = $('#cropBox'); if (!b) return;
  Object.assign(b.style, { left: x * 100 + '%', top: y * 100 + '%', width: w * 100 + '%', height: h * 100 + '%' });
}
function rotatedDims() { const r = (V.item.rotation || 0) % 2; return r ? [V.natH, V.natW] : [V.natW, V.natH]; }
function planePoint(e) {
  const r = plane.getBoundingClientRect();
  return [clamp((e.clientX - r.left) / r.width, 0, 1), clamp((e.clientY - r.top) / r.height, 0, 1)];
}
// rectangle from fixed anchor (ax,ay) to point (px,py), honouring the aspect ratio (pixel w/h)
function rectFrom(ax, ay, px, py) {
  let w = Math.abs(px - ax), h = Math.abs(py - ay);
  const sx = px < ax ? -1 : 1, sy = py < ay ? -1 : 1;
  if (V.aspect) {
    const [RW, RH] = rotatedDims();
    const k = RW / (V.aspect * RH);               // normalized h per normalized w
    const maxW = sx < 0 ? ax : 1 - ax, maxH = sy < 0 ? ay : 1 - ay;
    if (h < w * k) h = w * k; else w = h / k;       // grow to cover the pointer
    if (w > maxW) { w = maxW; h = w * k; }
    if (h > maxH) { h = maxH; w = h / k; }
  }
  return [sx < 0 ? ax - w : ax, sy < 0 ? ay - h : ay, w, h];
}
function cropPointerDown(e) {
  e.preventDefault();
  const handle = e.target.closest('.h'), onBox = e.target.closest('#cropBox');
  const [x, y, w, h] = V.cropRect;
  const start = planePoint(e);
  let fn;
  if (handle) {
    const c = handle.dataset.c;
    const ax = c.includes('w') ? x + w : x, ay = c.includes('n') ? y + h : y;
    fn = p => rectFrom(ax, ay, p[0], p[1]);
  } else if (onBox) {
    fn = p => [clamp(x + p[0] - start[0], 0, 1 - w), clamp(y + p[1] - start[1], 0, 1 - h), w, h];
  } else {
    fn = p => rectFrom(start[0], start[1], p[0], p[1]);
  }
  stage.setPointerCapture(e.pointerId);
  const move = ev => { const r = fn(planePoint(ev)); if (r[2] > 0.005 && r[3] > 0.005) { V.cropRect = r; renderCropBox(); } };
  const up = () => { stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerup', up); };
  stage.addEventListener('pointermove', move); stage.addEventListener('pointerup', up);
}
$('#cropAspect').addEventListener('change', e => {
  const v = e.target.value;
  const [RW, RH] = rotatedDims();
  V.aspect = v === 'orig' ? RW / RH : +v;
  if (!V.aspect) return;
  // re-fit current selection around its centre
  let [x, y, w, h] = V.cropRect;
  const cx = x + w / 2, cy = y + h / 2, k = RW / (V.aspect * RH);
  h = w * k; if (h > 1) { h = 1; w = h / k; }
  if (w > 1) { w = 1; h = w * k; }
  V.cropRect = [clamp(cx - w / 2, 0, 1 - w), clamp(cy - h / 2, 0, 1 - h), w, h];
  renderCropBox();
});
$('#cropApply').onclick = async () => {
  const c = V.cropRect.map(v => +v.toFixed(5));
  const full = c[0] < 0.002 && c[1] < 0.002 && c[2] > 0.996 && c[3] > 0.996;
  exitCrop(); await saveEdit({ crop: full ? null : c });
};
$('#cropClear').onclick = async () => { exitCrop(); await saveEdit({ crop: null }); };
$('#cropCancel').onclick = exitCrop;

// ---- faces in the viewer
async function loadFaces(mid, token) {
  const d = await api(`/api/media/${mid}`).catch(() => null);
  if (!d || token !== V.token) return;
  Object.assign(V.item, d.media);
  V.faces = d.faces;
  renderFaces();
  $('#fileInfo').innerHTML = `<div><b>${esc(d.media.name)}</b></div><div>${d.media.taken ? fmtDate(d.media.taken, { dateStyle: 'full' }) + ' ' + new Date(d.media.taken * 1000).toLocaleTimeString() : ''}</div>
     <div>${d.media.width && d.media.height ? `${d.media.width} × ${d.media.height}` : ''}</div><div>${esc(d.path)}</div>`;
}

function renderFaces() {
  mediaBox.querySelectorAll('.fbox').forEach(b => b.remove());
  const list = $('#faceList');
  if (V.item.kind === 'video') { list.innerHTML = '<div class="muted">Face tagging is for photos.</div>'; return; }
  if (!V.faces.length) list.innerHTML = '<div class="muted">No faces found. Use <b>Tag a person</b> to draw a box around someone.</div>';
  else list.innerHTML = '';
  for (const f of V.faces) {
    const b = document.createElement('div');
    b.className = 'fbox' + (f.person_id ? ' named' : '');
    Object.assign(b.style, { left: f.x * 100 + '%', top: f.y * 100 + '%', width: f.w * 100 + '%', height: f.h * 100 + '%' });
    b.innerHTML = `<span class="flabel" style="transform:translateX(-50%) rotate(var(--unrot))">${esc(f.name || (f.suggest[0] ? f.suggest[0].name + '?' : '?'))}</span>`;
    b.dataset.face = f.id;
    mediaBox.appendChild(b);

    const row = document.createElement('div');
    row.className = 'frow'; row.dataset.face = f.id;
    if (f.person_id) {
      row.innerHTML = `<img src="${faceUrl(f.id)}"><div class="fbody"><div class="fname">${esc(f.name)}</div>
        <div class="links"><a data-act="person">See all</a><a data-act="unassign">Not ${esc(f.name)}</a></div></div>`;
    } else {
      const sg = f.suggest[0];
      row.innerHTML = `<img src="${faceUrl(f.id)}"><div class="fbody">
        ${sg ? `<div class="q">Is this <b>${esc(sg.name)}</b>? <button class="yes" data-act="yes">✓</button> <button class="no" data-act="no">✗</button></div>` : ''}
        <input list="peopleNames" placeholder="Add a name…" autocomplete="off">
        <div class="links"><a data-act="ignore" class="danger">Not a face / ignore</a></div></div>`;
      const inp = $('input', row);
      inp.addEventListener('keydown', ev => { if (ev.key === 'Enter' && inp.value.trim()) nameFace(f, { name: inp.value.trim() }); ev.stopPropagation(); });
      inp.addEventListener('change', () => { if (S.people.some(p => p.name === inp.value.trim())) nameFace(f, { name: inp.value.trim() }); });
    }
    row.addEventListener('click', ev => {
      const act = ev.target.dataset.act; if (!act) return;
      if (act === 'yes') nameFace(f, { person_id: f.suggest[0].person_id });
      if (act === 'no') rejectSuggestion(f);
      if (act === 'ignore') faceAction('/api/faces/ignore', { face_ids: [f.id] });
      if (act === 'unassign') faceAction(`/api/faces/${f.id}/unassign`, {});
      if (act === 'person') openPerson(f.person_id);
    });
    const hl = on => { b.classList.toggle('hl', on); row.classList.toggle('hl', on); };
    row.onmouseenter = () => hl(true); row.onmouseleave = () => hl(false);
    b.onmouseenter = () => hl(true); b.onmouseleave = () => hl(false);
    b.onclick = () => { row.scrollIntoView({ block: 'nearest' }); const i = $('input', row); if (i) i.focus(); };
    list.appendChild(row);
  }
}

async function nameFace(f, body) {
  const r = await post('/api/faces/assign', { face_ids: [f.id], ...body }).catch(fail);
  if (!r) return;
  await loadPeople();
  const p = personById(r.person_id);
  await loadFaces(V.item.id, V.token);
  refreshPeopleSoon();
  // Picasa-style follow-up: offer other likely matches
  const sg = await api(`/api/people/${r.person_id}/suggestions`).catch(() => null);
  if (sg && sg.suggestions.length && p) toast(`Tagged ${p.name}. ${sg.suggestions.length} other photo${sg.suggestions.length === 1 ? '' : 's'} may show ${p.name} — open them under People.`, 6000);
}
async function rejectSuggestion(f) {
  await post('/api/faces/reject', { face_ids: [f.id], person_id: f.suggest[0].person_id }).catch(fail);
  f.suggest.shift(); renderFaces();
}
async function faceAction(url, body) {
  await post(url, body).catch(fail);
  await loadFaces(V.item.id, V.token);
  refreshPeopleSoon();
}

// ---- tag a person (draw a box)
function enterDraw() { if (V.item.kind !== 'image' || !V.natW) return; exitCrop(); V.drawMode = true; stage.classList.add('drawing'); $('#vAddFace').classList.add('on'); toast('Drag a box around the face'); }
function exitDraw() { V.drawMode = false; stage.classList.remove('drawing'); $('#vAddFace').classList.remove('on'); plane.querySelectorAll('.drawbox').forEach(d => d.remove()); }
function unrotate([u, v], r) { for (let i = 0; i < r; i++) [u, v] = [v, 1 - u]; return [u, v]; }
function drawPointerDown(e) {
  e.preventDefault();
  const a = planePoint(e);
  const box = document.createElement('div'); box.className = 'drawbox'; plane.appendChild(box);
  let b = a;
  stage.setPointerCapture(e.pointerId);
  const move = ev => {
    b = planePoint(ev);
    Object.assign(box.style, { left: Math.min(a[0], b[0]) * 100 + '%', top: Math.min(a[1], b[1]) * 100 + '%', width: Math.abs(b[0] - a[0]) * 100 + '%', height: Math.abs(b[1] - a[1]) * 100 + '%' });
  };
  const up = async () => {
    stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerup', up);
    if (Math.abs(b[0] - a[0]) < 0.01 || Math.abs(b[1] - a[1]) < 0.01) { box.remove(); return; }
    const r = (V.item.rotation || 0) % 4;
    const p1 = unrotate(a, r), p2 = unrotate(b, r);
    const bx = [Math.min(p1[0], p2[0]), Math.min(p1[1], p2[1]), Math.abs(p2[0] - p1[0]), Math.abs(p2[1] - p1[1])];
    const name = prompt('Who is this? (leave blank to name later)', '');
    exitDraw();
    if (name === null) return;
    const res = await post(`/api/media/${V.item.id}/faces`, { box: bx, name: name.trim() }).catch(fail);
    if (!res) return;
    V.faces = res.faces; renderFaces(); refreshPeopleSoon();
    if (name.trim()) loadPeople();
  };
  stage.addEventListener('pointermove', move); stage.addEventListener('pointerup', up);
}

// ---- filmstrip
function renderFilmstrip() {
  const list = viewerList(), fs = $('#filmstrip');
  const from = Math.max(0, V.idx - 40), to = Math.min(list.length, V.idx + 41);
  let html = '';
  for (let i = from; i < to; i++) html += `<img data-i="${i}" class="${i === V.idx ? 'cur' : ''}" loading="lazy" src="${thumbUrl(list[i])}">`;
  fs.innerHTML = html;
  const cur = $('.cur', fs); if (cur) cur.scrollIntoView({ inline: 'center', block: 'nearest' });
}
$('#filmstrip').addEventListener('click', e => { const i = e.target.dataset.i; if (i != null) showItem(+i); });

// ---- toolbar
function backToLibrary() {
  showLibrary();
  if (V.standalone) { V.standalone = null; if (S.mode === 'person') openPerson(S.personId, true); return; }
  if (gridDirty) { gridDirty = false; const st = $('#grid').scrollTop; renderGrid(); $('#grid').scrollTop = st; }
  const t = $(`#grid .tile[data-i="${V.idx}"]`);
  if (t) { t.scrollIntoView({ block: 'nearest' }); document.querySelectorAll('.tile.sel').forEach(x => x.classList.remove('sel')); t.classList.add('sel'); }
}
$('#vBack').onclick = backToLibrary;
$('#vPrev').onclick = () => showItem(V.idx - 1);
$('#vNext').onclick = () => showItem(V.idx + 1);
$('#vZoomIn').onclick = () => zoomCenter(1.25);
$('#vZoomOut').onclick = () => zoomCenter(0.8);
$('#vFit').onclick = zoomFit;
$('#v100').onclick = zoomActual;
$('#vRotL').onclick = () => rotate(-1);
$('#vRotR').onclick = () => rotate(1);
$('#vCrop').onclick = () => (V.cropMode ? exitCrop() : enterCrop());
$('#vReset').onclick = () => { exitCrop(); saveEdit({ rotation: 0, crop: null }); };
$('#vAddFace').onclick = () => (V.drawMode ? exitDraw() : enterDraw());
$('#vShowFaces').onchange = e => { $('#viewer').classList.toggle('hide-faces', !e.target.checked); store.set('showFaces', e.target.checked); };
$('#vExport').onclick = async () => {
  if (!V.item.rotation && !V.item.crop && !confirm('This photo has no edits. Save an identical copy anyway?')) return;
  const r = await post(`/api/media/${V.item.id}/export`).catch(fail);
  if (r) { toast('Saved copy: ' + r.path.split('/').pop(), 5000); if (S.mode === 'folder') api('/api/media?folder=' + encodeURIComponent(S.folder)).then(d => { S.items = d.items; gridDirty = true; }); }
};

document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
  if ($('#viewer').hidden) return;
  const k = e.key;
  if (k === 'Escape') { if (V.cropMode) exitCrop(); else if (V.drawMode) exitDraw(); else backToLibrary(); }
  else if (k === 'ArrowLeft' && !V.cropMode) showItem(V.idx - 1);
  else if (k === 'ArrowRight' && !V.cropMode) showItem(V.idx + 1);
  else if (k === '+' || k === '=') zoomCenter(1.25);
  else if (k === '-') zoomCenter(0.8);
  else if (k === '0') zoomFit();
  else if (k === '1') zoomActual();
  else if (k === '[') rotate(-1);
  else if (k === ']') rotate(1);
  else if (k === 'c' || k === 'C') V.cropMode ? exitCrop() : enterCrop();
  else if (k === 'Enter' && V.cropMode) $('#cropApply').click();
  else if (k === 'f' || k === 'F') { const c = $('#vShowFaces'); c.checked = !c.checked; c.onchange({ target: c }); }
  else return;
  e.preventDefault();
});

// ------------------------------------------------------------------ folder manager + status
function renderRoots() {
  $('#rootList').innerHTML = S.roots.map(r => `<li><span>${esc(r)}</span><button data-root="${esc(r)}">Remove</button></li>`).join('') || '<li class="muted">No folders yet.</li>';
}
$('#btnFolders').onclick = () => { renderRoots(); $('#dlgFolders').showModal(); };
$('#dlgFoldersClose').onclick = () => $('#dlgFolders').close();
$('#rootList').addEventListener('click', async e => {
  const r = e.target.dataset.root; if (!r) return;
  if (!confirm(`Stop watching ${r}? Files on disk are not touched.`)) return;
  await api('/api/roots', { method: 'DELETE', body: { path: r } }).catch(fail);
  S.roots = S.roots.filter(x => x !== r); renderRoots(); setTimeout(loadFolders, 1500);
});
$('#addRootForm').onsubmit = async e => {
  e.preventDefault();
  const r = await post('/api/roots', { path: $('#addRootPath').value }).catch(fail);
  if (!r) return;
  if (!S.roots.includes(r.path)) S.roots.push(r.path);
  $('#addRootPath').value = ''; renderRoots(); toast('Indexing ' + r.path); setTimeout(loadFolders, 2000);
};
$('#btnRescan').onclick = async () => { await post('/api/rescan').catch(fail); toast('Rescanning folders…'); setTimeout(loadFolders, 3000); };

let lastScanned = 0, lastClusterLoad = 0;
async function pollStatus() {
  try {
    const s = await api('/api/status');
    const pct = s.images ? Math.round(100 * s.scanned / s.images) : 100;
    $('#status').innerHTML = s.indexing ? 'Indexing folders…' :
      `${s.total.toLocaleString()} items · ${s.faces.toLocaleString()} faces` +
      (s.scanned < s.images ? ` · scanning ${pct}%<span class="bar"><i style="width:${pct}%"></i></span>` : '');
    if (s.total !== S.lastTotal && !s.indexing) { S.lastTotal = s.total; loadFolders(); }
    // refresh unnamed groups now and then while the scanner is finding faces
    if (s.scanned !== lastScanned && Date.now() - lastClusterLoad > 60000 && S.mode !== 'unknown') {
      lastScanned = s.scanned; lastClusterLoad = Date.now(); loadClusters(); loadFolders();
    }
  } catch { $('#status').textContent = 'Server not reachable'; }
  setTimeout(pollStatus, 3000);
}

// ------------------------------------------------------------------ boot
(async function boot() {
  setupSections();
  const ts = store.get('thumb', 170); $('#thumbSize').value = ts; document.documentElement.style.setProperty('--thumb', ts + 'px');
  if (!store.get('showFaces', true)) { $('#vShowFaces').checked = false; $('#viewer').classList.add('hide-faces'); }
  await Promise.all([loadFolders(), loadPeople()]).catch(fail);
  lastClusterLoad = Date.now(); loadClusters();
  pollStatus();
  const last = store.get('last', null);
  if (last && last.folder && S.folders.some(f => f.path === last.folder)) openFolder(last.folder);
  else if (last && last.person && personById(last.person)) openPerson(last.person);
  else if (S.folders.length) openFolder([...S.folders].sort((a, b) => folderSortKey(b).localeCompare(folderSortKey(a)))[0].path);
})();
