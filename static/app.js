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
  mode: null,         // 'folder' | 'person' | 'unknown' | 'search' | 'years' | 'dupes' | 'junk'
  kind: 'all',        // show 'all', only 'image' (photos) or only 'video'
  folder: null, personId: null, clusterId: null, query: '',
  searchIndex: null,  // {enabled, done, total} for describe-a-photo search
  items: [], shown: [],
  folders: [], roots: [], people: [], clusters: [],
  lastTotal: -1,
  ffmpeg: false,      // server can convert videos / save edited videos
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

// Year for grouping: prefer a year in the path (e.g. PhotoSync/2020/08), else the earliest photo date.
// Years still in the future are wrong (the server already drops such photo dates), so they count as unknown.
const NO_YEAR = 'Year unknown';
function folderYear(f) {
  const m = f.rel.match(/(?:^|\/)((?:19|20)\d\d)(?:\/|$)/);
  if (m && +m[1] <= new Date().getFullYear()) return +m[1];
  return f.first ? new Date(f.first * 1000).getFullYear() : NO_YEAR;
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

// number of items in a folder that the Photos / Videos switch lets through
const kindCount = f => S.kind === 'video' ? f.videos : S.kind === 'image' ? f.count - f.videos : f.count;
const KIND_WORD = { image: 'photos', video: 'videos' };

function renderFolders() {
  const el = $('#folderList');
  const q = filterQuery();
  const folders = S.folders.filter(f => folderMatches(f, q) && kindCount(f) > 0);
  $('#folderCount').textContent = q || S.kind !== 'all' ? `(${folders.length} of ${S.folders.length})` : `(${S.folders.length})`;
  if (!S.folders.length) {
    el.innerHTML = `<div class="hint">No photos found yet. ${S.roots.length ? 'Indexing may still be running…' : 'Use Folder Manager to add your Pictures folder.'}</div>`;
    return;
  }
  if (!folders.length) {
    el.innerHTML = `<div class="hint">${q ? 'No folder names match the search' : 'No folders'}${S.kind !== 'all' ? ` with ${KIND_WORD[S.kind]}` : ''}.</div>`;
    return;
  }
  const byYear = new Map();
  for (const f of folders) {
    const y = folderYear(f);
    if (!byYear.has(y)) byYear.set(y, []);
    byYear.get(y).push(f);
  }
  const years = [...byYear.keys()].sort((a, b) => (a === NO_YEAR) - (b === NO_YEAR) || b - a);   // unknown year last
  // a year can be collapsed to its heading; while the filter box is in use every match is shown
  const shut = new Set(store.get('shutYears', []));
  const open = y => q || !shut.has(String(y));
  let html = `<div class="ytools"><button data-yall="open" title="Show the folders of every year">Expand all</button><button data-yall="shut" title="Show only the year headings">Collapse all</button>
    <select id="yJump" title="Jump to a year"><option value="">Jump to…</option>${years.map(y => `<option>${y}</option>`).join('')}</select></div>`;
  for (const y of years) {
    const list = byYear.get(y), isOpen = open(y);
    const n = list.reduce((t, f) => t + kindCount(f), 0);
    html += `<div class="year${isOpen ? '' : ' shut'}" data-year="${y}" title="${isOpen ? 'Collapse' : 'Expand'} ${y}"><span class="ytri"></span>${y} <span class="n">${plural(list.length, 'folder')} · ${n.toLocaleString()}</span></div>`;
    if (!isOpen) continue;
    for (const f of list.sort((a, b) => folderSortKey(b).localeCompare(folderSortKey(a))))
      html += `<div class="item folder${S.mode === 'folder' && S.folder === f.path ? ' sel' : ''}" data-path="${esc(f.path)}" title="${esc(f.path)}">${FOLDER_SVG}<span class="label">${esc(folderLabel(f))}</span><span class="n">(${kindCount(f)})</span></div>`;
  }
  el.innerHTML = html;
  $('#yJump').onchange = e => {
    const y = e.target.value; if (!y) return;
    const sh = new Set(store.get('shutYears', [])); sh.delete(y); store.set('shutYears', [...sh]);
    renderFolders();
    $(`#folderList .year[data-year="${y}"]`)?.scrollIntoView({ block: 'start' });
  };
}

$('#folderList').addEventListener('click', e => {
  const yt = e.target.closest('.year');
  if (yt) {
    const y = yt.dataset.year, sh = new Set(store.get('shutYears', []));
    if (yt.classList.contains('shut')) sh.delete(y); else sh.add(y);
    store.set('shutYears', [...sh]); renderFolders(); return;
  }
  const all = e.target.dataset.yall;
  if (all) {
    const ys = [...$('#folderList').querySelectorAll('.year')].map(x => x.dataset.year);
    store.set('shutYears', all === 'shut' ? ys : []); renderFolders(); return;
  }
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
  leaveTool(); S.mode = 'folder'; S.folder = path; store.set('last', { folder: path });
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
  leaveTool(); S.mode = 'person'; S.personId = pid; store.set('last', { person: pid });
  selectLeft(); showLibrary();
  const p = personById(pid);
  if (!p) return;
  setHeader(`${p.cover ? `<img class="avatar" src="${faceUrl(p.cover)}">` : ''}
    <div><div class="titlerow"><h2>${esc(p.name)}</h2><span class="companions" id="pCompanions"></span></div><div class="sub">${p.n} photo${p.n === 1 ? '' : 's'}</div></div>
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
  loadCompanions(pid);
  const grid = $('#grid'); const st = keepScroll ? grid.scrollTop : 0;
  const d = await api('/api/media?person=' + pid);
  if (S.mode !== 'person' || S.personId !== pid) return;
  setItems(d.items); grid.scrollTop = st;
  loadSuggestions(pid);
}

async function loadCompanions(pid) {
  const d = await api(`/api/people/${pid}/companions`).catch(() => ({ companions: [] }));
  const box = $('#pCompanions');
  if (!box || S.mode !== 'person' || S.personId !== pid || !d.companions.length) return;
  box.innerHTML = 'Often with ' + d.companions.map(c =>
    `<a href="#" data-pid="${c.id}" title="${c.n} photo${c.n === 1 ? '' : 's'} together">${esc(c.name)}</a>`).join(', ');
  box.querySelectorAll('a').forEach(a => a.onclick = e => { e.preventDefault(); openPerson(+a.dataset.pid); });
}

async function loadSuggestions(pid) {
  const box = $('#libExtra');
  box.innerHTML = '<span class="muted">Looking for more photos of this person…</span>';
  const d = await api(`/api/people/${pid}/suggestions`).catch(() => ({ suggestions: [] }));
  if (S.mode !== 'person' || S.personId !== pid) return;
  const p = personById(pid);
  const list = d.suggestions.slice(0, 60);
  if (!list.length) { box.innerHTML = `<span class="muted">No suggested matches for ${esc(p.name)} right now. More appear as photos are scanned and more faces are confirmed.</span>`; return; }
  const count = `<span class="muted">(${d.suggestions.length} suggested match${d.suggestions.length === 1 ? '' : 'es'})</span>`;
  if (store.get('hideSuggest', false)) {
    box.innerHTML = `<div class="extra-head"><h4>Is this ${esc(p.name)}? ${count}</h4>
      <button id="sgToggle">Show suggestions</button></div>`;
    $('#sgToggle').onclick = () => { store.set('hideSuggest', false); loadSuggestions(pid); };
    return;
  }
  box.innerHTML = `<div class="extra-head"><h4>Is this ${esc(p.name)}? ${count}</h4>
      <button id="sgAll" class="primary">✓ Yes to all shown</button><button id="sgNone">✗ No to all shown</button>
      <span class="muted">Click a face to open its photo.</span>
      <button id="sgToggle" class="push-right">Hide suggestions</button></div>
    <div class="faces-row sg-list">${list.map(s => `<div class="ftile" data-face="${s.face_id}" data-media="${s.media_id}" title="similarity ${Math.round(s.score * 100)}%">
      <img loading="lazy" src="${faceUrl(s.face_id)}"><div class="btns"><button class="yes" title="Yes">✓</button><button class="no" title="No">✗</button></div>
      ${Math.round(s.score * 100)}%</div>`).join('')}</div>`;
  const done = () => { if (!box.querySelector('.ftile')) { refreshPeopleSoon(); openPerson(pid, true); } };
  box.querySelectorAll('.ftile').forEach(t => {
    const fid = +t.dataset.face;
    $('.yes', t).onclick = async () => { t.remove(); await post('/api/faces/assign', { face_ids: [fid], person_id: pid }).catch(fail); done(); };
    $('.no', t).onclick = async () => { t.remove(); await post('/api/faces/reject', { face_ids: [fid], person_id: pid }).catch(fail); done(); };
    $('img', t).onclick = () => openStandalone(+t.dataset.media, +t.dataset.face);
  });
  $('#sgToggle').onclick = () => { store.set('hideSuggest', true); loadSuggestions(pid); };
  const ids = () => [...box.querySelectorAll('.ftile')].map(t => +t.dataset.face);
  $('#sgAll').onclick = async () => { const f = ids(); await post('/api/faces/assign', { face_ids: f, person_id: pid }).catch(fail); toast(`Added ${f.length} faces to ${p.name}`); refreshPeopleSoon(); openPerson(pid); };
  $('#sgNone').onclick = async () => { await post('/api/faces/reject', { face_ids: ids(), person_id: pid }).catch(fail); loadSuggestions(pid); };
}

function openCluster(cid) {
  const c = S.clusters.find(x => x.id === cid); if (!c) return;
  leaveTool(); S.mode = 'unknown'; S.clusterId = cid;
  selectLeft(); showLibrary();
  setHeader(`<img class="avatar" src="${faceUrl(c.faces[0])}"><div><h2>&lt;Unknown Person&gt;</h2>
    <div class="sub">${c.count} similar faces</div></div>`);
  const sg = c.suggest && c.suggest.name;
  $('#libExtra').innerHTML = `<div class="extra-head">
      <form id="nameForm" style="display:flex;gap:6px"><input id="nameInput" list="peopleNames" placeholder="Who is this?" autocomplete="off" required><button class="primary">Add name</button></form>
      ${sg ? `<span class="pill">Looks like <b>${esc(sg)}</b> <button id="sgYes" class="yes">✓ Yes</button></span>` : ''}
      <button id="ignoreSel" title="Hide the selected faces (not real faces, or people you don't want to name)">Ignore selected</button>
      <button id="toggleAll" title="Unselect every face, then click just the ones that are this person">Unselect all</button>
    </div><div class="muted">Click faces that are <b>not</b> this person to leave them out. Double-click a face to open its photo.</div>`;
  const grid = $('#grid');
  grid.className = 'faces';
  $('#empty').hidden = true;
  grid.innerHTML = c.faces.map(f => `<div class="ftile" data-face="${f}"><img loading="lazy" src="${faceUrl(f)}"></div>`).join('');
  $('#libInfo').textContent = `${c.count} faces`;
  const tiles = [...grid.querySelectorAll('.ftile')];
  const syncToggle = () => { $('#toggleAll').textContent = tiles.some(t => !t.classList.contains('off')) ? 'Unselect all' : 'Select all'; };
  $('#toggleAll').onclick = () => {
    const off = tiles.some(t => !t.classList.contains('off'));
    tiles.forEach(t => t.classList.toggle('off', off));
    syncToggle();
  };
  tiles.forEach(t => {
    t.onclick = () => { t.classList.toggle('off'); syncToggle(); };
    t.ondblclick = async () => {
      t.classList.remove('off'); syncToggle();
      const d = await api(`/api/faces/${t.dataset.face}`).catch(fail);
      if (d) openStandalone(d.media_id, d.id);
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

// The ✦ search: names from People ("Tze and Sam", "@Tze") pick whose photos to look in, "oldest"/"newest"
// sorts by date, and any description left over is ranked by an on-device image/text model (CLIP)
async function openSearch(q) {
  leaveTool(); S.mode = 'search'; S.query = q;
  selectLeft(); showLibrary();
  const head = sub => setHeader(`<div class="big-ico ai">✦</div>
    <div><h2>“${esc(q)}”</h2><div class="sub">${sub}</div></div>`);
  head('Searching…'); S.searchDone = false; S.searchByDate = false;
  $('#libExtra').innerHTML = '';
  setItems([]);
  let d;
  try { d = await api('/api/search?q=' + encodeURIComponent(q)); }
  catch (e) { if (S.mode === 'search' && S.query === q) { head(esc(e.message)); $('#empty').textContent = ''; } return; }
  if (S.mode !== 'search' || S.query !== q) return;
  const n = d.items.length;
  const si = S.searchIndex;
  const partial = d.description && si && si.done < si.total
    ? ` · still analyzing your library (${Math.round(100 * si.done / si.total)}% done), so more matches may appear later` : '';
  const ppl = d.people.map(esc);
  const who = ppl.length < 2 ? ppl.join('') : ppl.slice(0, -1).join(', ') + (d.match === 'any' ? ' or ' : ' and ') + ppl.at(-1);
  const what = [who && (d.match === 'any' || ppl.length < 2 ? `with ${who}` : `with ${who} together`),
                d.description && `that look like “${esc(d.description)}”`].filter(Boolean).join(' ');
  const order = { best: 'best first', oldest: 'oldest first', newest: 'newest first' }[d.order];
  S.searchDone = true; S.searchByDate = d.order !== 'best';
  head((n ? `${n} match${n === 1 ? '' : 'es'}${what ? ' ' + what : ''}, ${order}` : `No matches${what ? ' ' + what : ''}`) +
       partial + (d.note ? ` · ${esc(d.note)}` : ''));
  setItems(d.items);
}
$('#aiForm').onsubmit = e => {
  e.preventDefault();
  const q = $('#aiQuery').value.trim();
  if (q) openSearch(q);
};

function setHeader(html) { $('#libHeader').innerHTML = html; }

function setItems(items) {
  S.items = items;
  applyFilter();
}
function applyFilter() {
  const q = filterQuery();
  const items = S.kind === 'all' ? S.items : S.items.filter(m => m.kind === S.kind);
  if (!q) { S.shown = items; renderGrid(); return; }
  const byPath = new Map(S.folders.map(f => [f.path, f]));
  const folderHit = new Map();  // folder path -> does its name match?
  S.shown = items.filter(m => {
    if (m.name.toLowerCase().includes(q)) return true;
    if (!folderHit.has(m.folder)) {
      const f = byPath.get(m.folder);
      folderHit.set(m.folder, f ? folderMatches(f, q) : m.folder.toLowerCase().includes(q));
    }
    return folderHit.get(m.folder);
  });
  renderGrid();
}
const GRID_MODES = ['folder', 'person', 'search'];
$('#filter').addEventListener('input', () => { renderFolders(); if (GRID_MODES.includes(S.mode)) applyFilter(); });

// Photos / Videos switch: applies to every view and to the folder list
function setKind(k) {
  S.kind = k; store.set('kind', k);
  document.querySelectorAll('#kindSeg button').forEach(b => b.classList.toggle('on', b.dataset.k === k));
  renderFolders();
  if (GRID_MODES.includes(S.mode)) applyFilter();
  else if (S.mode === 'years') renderYears();
  else if (S.mode === 'dupes') renderDupes();
  else if (S.mode === 'junk') renderJunk();
}
$('#kindSeg').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setKind(b.dataset.k); });

function renderGrid() {
  const grid = $('#grid');
  // a person's photos come newest first: group them under year headings, with date and name under each picture.
  // Search results always get the date and name; year headings only when they are in date order.
  const byYear = S.mode === 'person' || (S.mode === 'search' && S.searchByDate);
  const captioned = byYear || S.mode === 'search';
  grid.className = captioned ? 'captioned' : '';
  const badges = m => (m.kind === 'video' ? '<span class="vid">▶ video</span>' : '') + (m.rotation || m.crop ? '<span class="edited">edited</span>' : '');
  let year = undefined;
  grid.innerHTML = S.shown.map((m, i) => {
    const title = `${esc(m.name)}${m.taken ? ' — ' + fmtDate(m.taken) : ''}`;
    if (!captioned) return `<div class="tile" data-i="${i}" title="${title}">
      <img loading="lazy" decoding="async" src="${thumbUrl(m)}" alt="">${badges(m)}</div>`;
    const y = m.taken ? new Date(m.taken * 1000).getFullYear() : null;
    let head = '';
    if (byYear && y !== year) {
      year = y;
      let n = 0; for (let j = i; j < S.shown.length && (S.shown[j].taken ? new Date(S.shown[j].taken * 1000).getFullYear() : null) === y; j++) n++;
      head = `<h3 class="ghead">${y ?? NO_YEAR} <span class="muted">${n}</span></h3>`;
    }
    return `${head}<div class="tile" data-i="${i}" title="${title}">
      <div class="timg"><img loading="lazy" decoding="async" src="${thumbUrl(m)}" alt="">${badges(m)}</div>
      <div class="cap"><div>${m.taken ? fmtDate(m.taken) : '&nbsp;'}</div><div class="cname">${esc(m.name)}</div></div></div>`;
  }).join('');
  const empty = $('#empty');
  empty.hidden = !!S.shown.length || !S.mode;
  empty.textContent = S.items.length ? (S.kind !== 'all' && !S.items.some(m => m.kind === S.kind)
      ? `No ${KIND_WORD[S.kind]} here. Showing ${KIND_WORD[S.kind]} only: choose All at the bottom to see everything.` : 'Nothing matches the filter.') :
    S.mode === 'search' && S.searchDone ? 'No photos match that search. Try different words.' : 'Loading…';
  const vids = S.shown.filter(m => m.kind === 'video').length;
  const hidden = S.items.length - S.shown.length;
  const photos = S.shown.length - vids;
  $('#libInfo').textContent = S.shown.length ? [photos && plural(photos, 'photo'), vids && plural(vids, 'video')].filter(Boolean).join(', ') +
    (hidden ? ` (${hidden} hidden)` : '') : '';
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

// open the viewer on a list that is not the library grid (year check / duplicates)
function openList(items, i) {
  V.standalone = { items };
  $('#library').hidden = true; $('#viewer').hidden = false;
  showItem(Math.max(0, i));
}

async function openStandalone(mediaId, faceId = null) {
  const d = await api(`/api/media/${mediaId}`).catch(fail);
  if (!d) return;
  V.standalone = { items: [d.media] };
  $('#library').hidden = true; $('#viewer').hidden = false;
  showItem(0, faceId);
}

function showItem(i, focusFace = null) {
  const list = viewerList();
  if (i < 0 || i >= list.length) return;
  exitCrop(); exitDraw(); closeShare();
  const m = list[i];
  V.focusFace = focusFace;
  V.idx = i; V.item = m; V.zoom = 1; V.panX = V.panY = 0; V.natW = V.natH = 0; V.faces = []; V.seekTo = null;
  const token = ++V.token;
  mediaBox.innerHTML = '';
  $('#faceList').innerHTML = '';
  $('#vName').textContent = m.name;
  $('#stageMsg').hidden = true;
  $('#vidBar').hidden = m.kind !== 'video';
  let el;
  if (m.kind === 'video') {
    el = document.createElement('video');
    el.preload = 'metadata'; el.playsInline = true;
    el.volume = clamp(store.get('volume', 1), 0, 1); el.muted = store.get('muted', false);
    el.onloadedmetadata = () => {
      if (token !== V.token) return;
      // HEVC without a decoder often "loads" with sound but no picture
      if (!el.videoWidth) return videoUnplayable(m, el, token);
      V.natW = el.videoWidth; V.natH = el.videoHeight; layout(); syncVidBar();
      if (V.seekTo != null) { el.currentTime = V.seekTo; V.seekTo = null; }
    };
    el.onerror = () => { if (token === V.token) videoUnplayable(m, el, token); };
    for (const ev of ['play', 'pause', 'timeupdate', 'durationchange', 'volumechange', 'ended']) el.addEventListener(ev, syncVidBar);
  } else {
    el = document.createElement('img');
    el.draggable = false;
    el.onload = () => { if (token === V.token) { V.natW = el.naturalWidth; V.natH = el.naturalHeight; layout(); } };
    el.onerror = () => { if (token === V.token) stageMsg('This image could not be displayed by the browser.'); };
  }
  el.src = m.kind === 'video' && m.converted ? `/api/media/${m.id}/play` : `/api/media/${m.id}/file`;
  mediaBox.appendChild(el);
  if (m.kind === 'video') syncVidBar();
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

// ---- video playback
const curVideo = () => $('#mediaBox video');
const fmtTime = t => { t = Math.max(0, Math.floor(t || 0)); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = String(t % 60).padStart(2, '0'); return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`; };

function syncVidBar() {
  const v = curVideo(); if (!v) return;
  const d = isFinite(v.duration) ? v.duration : 0;
  $('#vidPlay').textContent = v.paused || v.ended ? '▶' : '❚❚';
  $('#vidPlay').title = v.paused ? 'Play (Space)' : 'Pause (Space)';
  $('#vidTime').textContent = `${fmtTime(v.currentTime)} / ${fmtTime(d)}`;
  syncFaceBoxes();
  if (!vidSeeking) $('#vidSeek').value = d ? Math.round(1000 * v.currentTime / d) : 0;
  $('#vidMute').textContent = v.muted || !v.volume ? '🔇' : '🔊';
  $('#vidVol').value = v.muted ? 0 : v.volume;
  $('#vidSpeed').value = String(v.playbackRate);
}
let vidSeeking = false;
// video faces were found on one frame, so their boxes show only while paused on (about) that frame
function syncFaceBoxes() {
  const v = curVideo(); if (!v) return;
  const near = t => v.paused && Math.abs(v.currentTime - t) < 0.3;
  mediaBox.querySelectorAll('.fbox').forEach(b => b.classList.toggle('off', !near(+b.dataset.frame)));
}
function seekToFace(f) {
  const v = curVideo(); if (!v || f.frame == null) return;
  v.pause();
  if (v.readyState >= 1) v.currentTime = f.frame; else V.seekTo = f.frame;
}
function togglePlay() { const v = curVideo(); if (!v || !V.natW) return; if (v.paused || v.ended) v.play().catch(() => {}); else v.pause(); }
$('#vidPlay').onclick = togglePlay;
$('#vidSeek').oninput = e => { const v = curVideo(); vidSeeking = true; if (v && isFinite(v.duration)) { v.currentTime = e.target.value / 1000 * v.duration; $('#vidTime').textContent = `${fmtTime(v.currentTime)} / ${fmtTime(v.duration)}`; } };
$('#vidSeek').onchange = () => { vidSeeking = false; };
$('#vidMute').onclick = () => { const v = curVideo(); if (v) { v.muted = !v.muted; if (!v.muted && !v.volume) v.volume = 1; store.set('muted', v.muted); } };
$('#vidVol').oninput = e => { const v = curVideo(); if (v) { v.volume = +e.target.value; v.muted = !v.volume; store.set('volume', v.volume); store.set('muted', v.muted); } };
$('#vidSpeed').onchange = e => { const v = curVideo(); if (v) v.playbackRate = +e.target.value; };
$('#vidBar').addEventListener('pointerdown', e => e.stopPropagation());
$('#vidBar').addEventListener('dblclick', e => e.stopPropagation());
$('#vidBar').addEventListener('wheel', e => e.stopPropagation());

async function videoUnplayable(m, el, token) {
  el.removeAttribute('src'); el.load();
  if (m.converted) return stageMsg('This video could not be played, even after converting it.');
  if (!S.ffmpeg) return stageMsg('The browser cannot play this video format (often HEVC/H.265 .mov files from iPhones). ' +
    'Install ffmpeg support (pip install imageio-ffmpeg, then restart the app) to convert it automatically, or use Microsoft Edge with the HEVC Video Extensions.');
  stageMsg('Converting this video so the browser can play it…');
  let j = await post(`/api/media/${m.id}/convert`).catch(e => ({ state: 'error', error: e.message }));
  while (j.state === 'running' && token === V.token) {
    stageMsg(`Converting this video so the browser can play it… ${Math.round(100 * j.progress)}%`);
    await new Promise(r => setTimeout(r, 700));
    j = await api(`/api/media/${m.id}/convert`).catch(e => ({ state: 'error', error: e.message }));
  }
  if (token !== V.token) return;
  if (j.state !== 'done') return stageMsg('Could not convert this video: ' + (j.error || 'unknown error'));
  m.converted = true;
  $('#stageMsg').hidden = true;
  el.src = `/api/media/${m.id}/play`;
}

function updateNavButtons() {
  const n = viewerList().length;
  $('#vPrev').disabled = V.idx <= 0; $('#vNext').disabled = V.idx >= n - 1;
  const vid = V.item && V.item.kind === 'video';
  $('#vExport').disabled = vid && !S.ffmpeg; $('#vAddFace').disabled = vid;
  $('#vExport').title = vid && !S.ffmpeg ? 'Saving edited videos needs ffmpeg (pip install imageio-ffmpeg, then restart the app)' : 'Save an edited copy next to the original';
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
  e.preventDefault();
  const sx = e.clientX, sy = e.clientY, px = V.panX, py = V.panY, onVideo = e.target.tagName === 'VIDEO';
  let moved = false;
  stage.setPointerCapture(e.pointerId); stage.classList.add('panning');
  const move = ev => { if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > 4) moved = true; V.panX = px + ev.clientX - sx; V.panY = py + ev.clientY - sy; layout(); };
  const up = () => {
    stage.classList.remove('panning'); stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerup', up);
    if (onVideo && !moved) togglePlay(); // a click (not a drag) on a video plays/pauses it
  };
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
  if (V.item.kind === 'video') {
    // opened from a face, or from a person's photos: jump to where that face was seen
    const f = V.faces.find(x => x.id === V.focusFace) || (S.mode === 'person' && !V.standalone && V.faces.find(x => x.person_id === S.personId));
    if (f) seekToFace(f);
  }
  $('#fileInfo').innerHTML = `<div><b>${esc(d.media.name)}</b></div><div>${d.media.taken ? fmtDate(d.media.taken, { dateStyle: 'full' }) + ' ' + new Date(d.media.taken * 1000).toLocaleTimeString() : ''}</div>
     <div>${d.media.width && d.media.height ? `${d.media.width} × ${d.media.height}` : ''}</div><div>${esc(d.path)}</div>`;
}

function renderFaces() {
  mediaBox.querySelectorAll('.fbox').forEach(b => b.remove());
  const list = $('#faceList');
  const vid = V.item.kind === 'video';
  if (!V.faces.length) list.innerHTML = vid ? '<div class="muted">No faces found near the start or end of this video.</div>' :
    '<div class="muted">No faces found. Use <b>Tag a person</b> to draw a box around someone.</div>';
  else if (vid) list.innerHTML = '<div class="muted vidhint">Faces are looked for near the start and end of a video. Click a face to jump to it.</div>';
  else list.innerHTML = '';
  for (const f of V.faces) {
    const b = document.createElement('div');
    b.className = 'fbox' + (f.person_id ? ' named' : '');
    Object.assign(b.style, { left: f.x * 100 + '%', top: f.y * 100 + '%', width: f.w * 100 + '%', height: f.h * 100 + '%' });
    b.innerHTML = `<span class="flabel" style="transform:translateX(-50%) rotate(var(--unrot))">${esc(f.name || (f.suggest[0] ? f.suggest[0].name + '?' : '?'))}</span>`;
    b.dataset.face = f.id;
    if (vid) b.dataset.frame = f.frame;
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
      const act = ev.target.dataset.act;
      if (!act) { if (vid && !ev.target.closest('input')) seekToFace(f); return; }
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
  syncFaceBoxes();
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
  const m = V.item, what = m.kind === 'video' ? 'video' : 'photo';
  if (!m.rotation && !m.crop && !confirm(`This ${what} has no edits. Save an identical copy anyway?`)) return;
  const btn = $('#vExport'); btn.disabled = true;
  let r = await post(`/api/media/${m.id}/export`).catch(fail);
  while (r && r.state === 'running') {
    toast(`Saving edited video… ${Math.round(100 * r.progress)}%`, 2000);
    await new Promise(res => setTimeout(res, 1000));
    r = await api(`/api/media/${m.id}/export`).catch(fail);
  }
  if (V.item === m) updateNavButtons();
  if (!r) return;
  if (r.state !== 'done') return toast('Could not save the copy: ' + (r.error || 'unknown error'), 6000);
  toast('Saved copy: ' + r.path.split('/').pop(), 5000);
  if (S.mode === 'folder') api('/api/media?folder=' + encodeURIComponent(S.folder)).then(d => { S.items = d.items; gridDirty = true; });
};

// ------------------------------------------------------------------ share menu
const shareMenu = $('#shareMenu');
const SH = { key: null, blob: null };   // the email-size file for the current item, fetched when the menu opens
const shareUrl = (m, size, attach) => `/api/media/${m.id}/download?size=${size}&v=${m.ver}${attach ? '&attach=1' : ''}`;
const shareName = m => m.kind === 'image' ? m.name.replace(/\.[^.]+$/, '') + '.jpg' : m.name;

function shareBlob(m) {
  const key = `${m.id}:${m.ver}`;
  if (SH.key !== key) {
    SH.key = key;
    SH.blob = fetch(shareUrl(m, 'email')).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      return r.blob();
    });
    SH.blob.catch(() => { if (SH.key === key) SH.key = null; });
  }
  return SH.blob;
}

function closeShare() { shareMenu.hidden = true; $('#vShare').classList.remove('on'); }
function openShare() {
  const m = V.item; if (!m) return;
  const img = m.kind === 'image';
  const file = () => new File([], shareName(m), { type: img ? 'image/jpeg' : 'video/mp4' });
  const can = !!navigator.canShare && navigator.canShare({ files: [file()] });
  const btn = a => shareMenu.querySelector(`[data-act="${a}"]`);
  btn('share').hidden = !can;
  btn('copy').disabled = !img || !window.ClipboardItem;
  btn('email').disabled = !img;
  btn('share').querySelector('small').textContent = img ? 'Mail, Outlook, Nearby sharing and other apps (email size)' : 'Mail, Outlook, Nearby sharing and other apps';
  btn('copy').querySelector('small').textContent = img ? 'Then paste it into an email or chat (Ctrl+V)' : 'Only for photos';
  $('#sharePath').textContent = '';
  api(`/api/media/${m.id}/location`).then(d => { if (V.item === m) $('#sharePath').textContent = d.windows; }).catch(() => {});
  if (img || can) shareBlob(m).catch(() => {});   // start now so Share/Copy are instant when clicked
  shareMenu.hidden = false; $('#vShare').classList.add('on');
}
$('#vShare').onclick = e => { e.stopPropagation(); shareMenu.hidden ? openShare() : closeShare(); };
document.addEventListener('click', e => { if (!shareMenu.hidden && !e.target.closest('.menu-wrap')) closeShare(); });

function download(url) { const a = document.createElement('a'); a.href = url; a.download = ''; document.body.append(a); a.click(); a.remove(); }

async function toPng(blob) {
  const bmp = await createImageBitmap(blob);
  const c = document.createElement('canvas'); c.width = bmp.width; c.height = bmp.height;
  c.getContext('2d').drawImage(bmp, 0, 0);
  return new Promise((res, rej) => c.toBlob(b => (b ? res(b) : rej(new Error('could not convert the picture'))), 'image/png'));
}

shareMenu.addEventListener('click', async e => {
  const b = e.target.closest('button[data-act]'); if (!b || b.disabled) return;
  const m = V.item, act = b.dataset.act;
  closeShare();
  try {
    if (act === 'share') {
      const blob = await (m.kind === 'image' ? shareBlob(m) : fetch(shareUrl(m, 'full')).then(r => r.blob()));
      const file = new File([blob], shareName(m), { type: blob.type });
      try { await navigator.share({ files: [file], title: m.name }); }
      catch (err) {
        if (err.name === 'AbortError') return;   // closed the share window
        if (err.name === 'NotAllowedError') return toast('The file took a moment to get ready. Click Share again.', 5000);
        throw err;
      }
    } else if (act === 'copy') {
      // pass a promise so the browser keeps the click's permission while the picture is prepared
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': shareBlob(m).then(toPng) })]);
      toast('Picture copied. Paste it into your email or chat with Ctrl+V.', 4000);
    } else if (act === 'email' || act === 'full') {
      download(shareUrl(m, act, true));
      toast('Saving to your Downloads folder…');
    } else if (act === 'reveal') {
      await post(`/api/media/${m.id}/reveal`);
      toast('Opening File Explorer…');
    } else if (act === 'path') {
      const d = await api(`/api/media/${m.id}/location`);
      await navigator.clipboard.writeText(d.windows);
      toast('Copied: ' + d.windows, 5000);
    }
  } catch (err) { fail(err); }
});

document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
  if ($('#viewer').hidden) return;
  const k = e.key;
  if (k === 'Escape') { if (!shareMenu.hidden) closeShare(); else if (V.cropMode) exitCrop(); else if (V.drawMode) exitDraw(); else backToLibrary(); }
  else if (k === 'ArrowLeft' && !V.cropMode) showItem(V.idx - 1);
  else if (k === 'ArrowRight' && !V.cropMode) showItem(V.idx + 1);
  else if (k === '+' || k === '=') zoomCenter(1.25);
  else if (k === '-') zoomCenter(0.8);
  else if (k === '0') zoomFit();
  else if (k === '1') zoomActual();
  else if (k === ' ' && V.item && V.item.kind === 'video') { if (e.target.tagName === 'BUTTON') e.target.blur(); togglePlay(); }
  else if (k === '[') rotate(-1);
  else if (k === ']') rotate(1);
  else if (k === 'c' || k === 'C') V.cropMode ? exitCrop() : enterCrop();
  else if (k === 'Enter' && V.cropMode) $('#cropApply').click();
  else if (k === 's' || k === 'S') shareMenu.hidden ? openShare() : closeShare();
  else if (k === 'f' || k === 'F') { const c = $('#vShowFaces'); c.checked = !c.checked; c.onchange({ target: c }); }
  else return;
  e.preventDefault();
});

// ------------------------------------------------------------------ tidy-up tools (year check, duplicates)
const fmtBytes = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1e3)) + ' KB';
const plural = (n, w, ws = w + 's') => `${n.toLocaleString()} ${n === 1 ? w : ws}`;
const relPath = p => { const r = S.roots.find(x => p === x || p.startsWith(x.replace(/\/$/, '') + '/')); return r ? (p.slice(r.length).replace(/^\//, '') || r.split('/').pop()) : p; };
const kindOk = m => S.kind === 'all' || m.kind === S.kind;
const T = { years: null, dupes: null, junk: null, pollTimer: null, limit: 0 };
const ICON_CAL = '<svg viewBox="0 0 24 24" width="40" height="40"><rect x="3" y="5" width="18" height="16" rx="2" fill="#fff" stroke="#4a7fc1" stroke-width="1.6"/><path d="M3 9h18" stroke="#4a7fc1" stroke-width="1.6"/><path d="M8 3v4M16 3v4" stroke="#4a7fc1" stroke-width="1.6" stroke-linecap="round"/><path d="M8 15.5l2.5 2.5 5-5" fill="none" stroke="#2e8b3e" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_DUP = '<svg viewBox="0 0 24 24" width="40" height="40"><rect x="7.5" y="7.5" width="13" height="13" rx="1.5" fill="#cfe0f4" stroke="#4a7fc1" stroke-width="1.6"/><rect x="3.5" y="3.5" width="13" height="13" rx="1.5" fill="#fff" stroke="#4a7fc1" stroke-width="1.6"/><circle cx="10" cy="10" r="2.6" fill="#4a7fc1"/></svg>';

function enterTool(mode, title, icon, sub) {
  S.mode = mode; S.items = []; S.shown = [];
  selectLeft(); showLibrary();
  document.querySelectorAll('#btnYears, #btnDupes, #btnJunk').forEach(b => b.classList.remove('on'));
  $({ years: '#btnYears', dupes: '#btnDupes', junk: '#btnJunk' }[mode]).classList.add('on');
  setHeader(`<div class="big-ico tool">${icon}</div><div><h2>${title}</h2><div class="sub" id="toolSub">${sub}</div></div>
    <div class="actions"><button id="toolRerun">Check again</button></div>`);
  $('#libExtra').innerHTML = '';
  const grid = $('#grid'); grid.className = 'tool'; grid.innerHTML = ''; grid.scrollTop = 0;
  $('#empty').hidden = true; $('#libInfo').textContent = '';
}
function leaveTool() {
  document.querySelectorAll('#btnYears, #btnDupes, #btnJunk').forEach(b => b.classList.remove('on'));
  clearTimeout(T.pollTimer);
  const g = $('#grid'); g.onclick = g.ondblclick = g.onchange = null;
}

// Poll a background job until it finishes, showing progress in the header
async function runTool(mode, url, start, render) {
  clearTimeout(T.pollTimer);
  let d = start ? await post(url).catch(fail) : null;
  for (;;) {
    if (S.mode !== mode) return;
    d = await api(url).catch(e => ({ state: 'error', error: e.message }));
    if (S.mode !== mode) return;
    if (d.state === 'running') {
      const pct = d.total ? Math.round(100 * d.done / d.total) : null;
      $('#toolSub').innerHTML = `${esc(d.phase)}…${pct != null ? ` ${d.done.toLocaleString()} of ${d.total.toLocaleString()} <span class="bar"><i style="width:${pct}%"></i></span>` : ''}`;
      $('#toolRerun').disabled = true;
      await new Promise(r => (T.pollTimer = setTimeout(r, 800)));
      continue;
    }
    $('#toolRerun').disabled = false;
    if (d.state === 'idle' && !start) { start = true; await post(url).catch(fail); continue; }
    if (d.state === 'error') { $('#toolSub').textContent = 'Could not finish: ' + d.error; return; }
    render(d);
    return;
  }
}

// ---- year check
function openYears() {
  enterTool('years', 'Year check', ICON_CAL, 'Looking for photos and videos in the wrong year folder…');
  $('#toolRerun').onclick = () => runTool('years', '/api/years', true, gotYears);
  T.limit = 60;
  runTool('years', '/api/years', false, gotYears);
}
function gotYears(d) {
  T.years = { items: d.items, folders: d.folders || [], checked: d.checked, finished: d.finished,
    level: store.get('yearsLevel', 'medium'), fsel: new Set() };
  // pre-select the high-confidence file moves; folder moves and renames are bigger changes, so you choose them
  T.years.sel = new Set(d.items.filter(a => a.level === 'high').map(a => a.id));
  renderYears();
}
const LEVELS = { high: ['high'], medium: ['high', 'medium'], low: ['high', 'medium', 'low'] };
const pct = c => Math.round(c * 100) + '%';
const levelBadge = (level, c, title) => `<div class="conf ${level}" title="${title}">${level === 'none' ? 'No guess' : level[0].toUpperCase() + level.slice(1) + ' · ' + pct(c)}</div>`;

function folderRow(f, Y) {
  const canApply = !!f.target;
  const ev = f.evidence.map(e => `<li class="${e.agrees ? 'for' : e.year == null ? 'other' : 'against'}">${esc(e.label)}</li>`).join('');
  const yearsTxt = f.split ? f.split.map(([y, n]) => `${y} (${n})`).join(', ') : '';
  const action = f.kind === 'name' ? `Rename to <b>${esc(f.new_name)}</b>` :
    f.kind === 'split' ? `Sort into year folders by each file’s own date, as <span class="yto">${esc(relPath(f.target))}</span>: ${esc(yearsTxt)}` +
      (f.undated ? ` · <span class="muted">${plural(f.undated, 'file')} with no reliable date stay here</span>` : '') :
    canApply ? `Move to <span class="yto">${esc(relPath(f.target))}</span>` : '<span class="muted">Leave it where it is until there is more to go on</span>';
  const guess = f.kind === 'split' ? `<div class="fguess"><span class="yr new">${f.split[0][0]}–${f.split[f.split.length - 1][0]}</span></div>` :
    f.kind === 'name' ? `<div><span class="yr old">${esc(f.path.split('/').pop())}</span></div>` :
    f.year ? `<div class="fguess">${f.mtime_only ? '≈ ' : ''}<span class="yr new">${f.year}</span></div>` : '<div class="fguess"><span class="yr old">?</span></div>';
  return `<div class="yfrow" data-path="${esc(f.path)}">
    <input type="checkbox" ${canApply ? '' : 'disabled'} ${Y.fsel.has(f.path) ? 'checked' : ''} aria-label="Apply this suggestion">
    <div class="fthumbs">${f.sample.map(m => `<img loading="lazy" src="${thumbUrl(m)}" data-fopen="${m.id}" title="${esc(m.name)}">`).join('')}</div>
    <div class="yinfo"><div class="yname">${FOLDER_SVG} <a href="#" data-folder="${esc(f.path)}" title="Open this folder">${esc(relPath(f.path))}</a>
        <span class="muted">${plural(f.count, 'file')}${f.has_subfolders ? ' incl. subfolders' : ''}${f.era ? ` · folder name says ${esc(f.era)}` : ''}</span></div>
      <ul class="ev">${ev}</ul><div class="faction">${action}</div></div>
    <div class="yverdict">${guess}${f.kind === 'split' ? `<div class="conf medium" title="The files are from several years, so each goes to its own year. Best single guess: ${f.year} (${pct(f.confidence)})">Many years</div>` :
      levelBadge(f.level, f.confidence, 'How sure the guess is, from how many files agree and how strong their dates are')}</div>
  </div>`;
}

function renderYears() {
  const Y = T.years; if (!Y || S.mode !== 'years') return;
  const counts = { high: 0, medium: 0, low: 0 };
  for (const a of Y.items) if (kindOk(a.media)) counts[a.level]++;
  const shown = Y.items.filter(a => kindOk(a.media) && LEVELS[Y.level].includes(a.level));
  for (const id of Y.sel) if (!shown.some(a => a.id === id)) Y.sel.delete(id);
  const outside = Y.folders.filter(f => f.kind !== 'name'), named = Y.folders.filter(f => f.kind === 'name');
  for (const p of Y.fsel) if (!Y.folders.some(f => f.path === p)) Y.fsel.delete(p);
  $('#toolSub').textContent = `Checked ${plural(Y.checked, 'file')} in year folders` +
    (outside.length ? ` and ${plural(outside.length, 'folder')} outside them` : '') + ' · ' +
    (Y.items.length ? `${plural(Y.items.length, 'file')} may be in the wrong year` : 'no files in the wrong year') +
    (Y.finished ? ` · ${new Date(Y.finished * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : '');
  $('#libExtra').innerHTML = !Y.items.length ? '' : `<div class="extra-head">
      <label>Show files <select id="yLevel">
        <option value="high">High confidence (${counts.high})</option>
        <option value="medium">High and medium (${counts.high + counts.medium})</option>
        <option value="low">All suggestions (${counts.high + counts.medium + counts.low})</option></select></label>
      <button id="ySelPage" title="Only the files loaded on screen so far" hidden>Select all on screen</button>
      <button id="ySelAll" title="Every file in this list, including ones not loaded yet">Select all (${shown.length.toLocaleString()})</button><button id="ySelNone">Select none</button>
      <button id="yMove" class="primary" ${Y.sel.size ? '' : 'disabled'}>Move ${plural(Y.sel.size, 'selected file')}</button>
    </div><div class="muted">The suggestion comes from the date the camera recorded, any date in the file or folder name, and (weakly) when the file was last changed.
      Scanner dates and digital dates on pre-digital photos count for little: they say when a print was scanned, not taken.
      Files move to the suggested year folder, keeping any month and event folder below it. People, edits and search results move with them.</div>`;
  if (Y.items.length) {
    $('#yLevel').value = Y.level;
    $('#yLevel').onchange = e => { Y.level = e.target.value; store.set('yearsLevel', Y.level); T.limit = 60; renderYears(); };
    $('#ySelAll').onclick = () => { shown.forEach(a => Y.sel.add(a.id)); renderYears(); };
    $('#ySelNone').onclick = () => { Y.sel.clear(); renderYears(); };
    $('#yMove').onclick = moveYears;
  }
  const grid = $('#grid');
  let html = '';
  // whole folders: outside the year folders (best-guess year) and event folders whose name disagrees with their photos
  if (Y.folders.length) {
    html += `<div class="fbar"><b>${plural(Y.fsel.size, 'folder change')} selected</b>
      <button id="fApply" class="primary" ${Y.fsel.size ? '' : 'disabled'}>Apply folder changes</button>
      <span class="muted">Whole folders (with their subfolders) are moved or renamed on disk. Nothing is deleted.</span></div>`;
    if (outside.length) {
      const guessed = outside.filter(f => f.year).length;
      html += `<section class="ygroup fsec"><div class="yghead"><b>Folders that are not in a year folder</b>
        <span class="muted">${plural(outside.length, 'folder')} · best guess for ${guessed}</span>
        <button data-fsel="outside-high" title="Select the folders whose guess is high confidence">Select high-confidence</button></div>
        ${outside.map(f => folderRow(f, Y)).join('')}</section>`;
    }
    if (named.length) {
      html += `<section class="ygroup fsec"><div class="yghead"><b>Folder names that disagree with their photos</b>
        <span class="muted">${plural(named.length, 'folder')} · the files are in the right place, but the year or month in the name is not when the photos were taken</span>
        <button data-fsel="name-all">Select all</button></div>
        ${named.map(f => folderRow(f, Y)).join('')}</section>`;
    }
  }
  // single files in the wrong year folder, grouped by current folder: misfiled photos usually come in batches
  const groups = new Map();
  for (const a of shown) {
    const k = a.media.folder + '\n' + a.target;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(a);
  }
  if (!shown.length) {
    html += `<div class="tool-empty">${Y.items.length ? 'No file suggestions at this confidence level. Choose “All suggestions” to see weaker ones.' :
      'Every photo and video in a year folder (such as …/2021/…) matches its year. 🎉'}</div>`;
  } else if (Y.folders.length) html += '<h4 class="fsub">Files in the wrong year folder</h4>';
  let n = 0;
  const onScreen = [];
  for (const [, list] of groups) {
    if (n >= T.limit) break;
    onScreen.push(...list);
    const a0 = list[0], allSel = list.every(a => Y.sel.has(a.id));
    html += `<section class="ygroup"><div class="yghead"><label><input type="checkbox" data-group="${esc(a0.media.folder + '\n' + a0.target)}" ${allSel ? 'checked' : ''}>
      <span class="yfrom">${esc(relPath(a0.media.folder))}</span></label> <span class="arrow">→</span> <span class="yto">${esc(relPath(a0.target))}</span>
      <span class="muted">${plural(list.length, 'file')}</span></div>
      ${a0.note ? `<div class="ynote">${esc(a0.note)}</div>` : ''}`;
    for (const a of list) {
      n++;
      html += `<div class="yrow" data-id="${a.id}">
        <input type="checkbox" ${Y.sel.has(a.id) ? 'checked' : ''} aria-label="Move ${esc(a.media.name)}">
        <img loading="lazy" src="${thumbUrl(a.media)}" data-open="${a.id}" title="Open">
        <div class="yinfo"><div class="yname">${esc(a.media.name)}${a.media.kind === 'video' ? ' <span class="tag">video</span>' : ''}</div>
          <ul class="ev">${a.evidence.map(e => `<li class="${e.agrees ? 'for' : e.year === a.folder_year ? 'against' : 'other'}">${esc(e.label)}</li>`).join('')}</ul></div>
        <div class="yverdict"><div><span class="yr old">${a.folder_year}</span> → <span class="yr new">${a.year}</span></div>
          ${levelBadge(a.level, a.confidence, 'How sure the suggestion is, from how strong and consistent the evidence is')}</div>
      </div>`;
    }
    html += '</section>';
  }
  if (n < shown.length) html += `<div class="tool-more"><button id="yMore">Show more (${shown.length - n} left)</button></div>`;
  grid.innerHTML = html;
  $('#libInfo').textContent = `${plural(shown.length, 'file suggestion')} shown · ${Y.sel.size} selected` +
    (Y.folders.length ? ` · ${plural(Y.folders.length, 'folder suggestion')}` : '');
  const rerender = () => { const st = grid.scrollTop; renderYears(); grid.scrollTop = st; };
  const more = $('#yMore'); if (more) more.onclick = () => { T.limit += 200; rerender(); };
  const sp = $('#ySelPage');
  if (sp && n < shown.length) {
    sp.hidden = false; sp.textContent = `Select all on screen (${n.toLocaleString()})`;
    sp.onclick = () => { onScreen.forEach(a => Y.sel.add(a.id)); rerender(); };
  }
  const fa = $('#fApply'); if (fa) fa.onclick = applyFolders;
  grid.onchange = e => {
    const cb = e.target; if (cb.type !== 'checkbox') return;
    const fr = cb.closest('.yfrow');
    if (fr) { cb.checked ? Y.fsel.add(fr.dataset.path) : Y.fsel.delete(fr.dataset.path); }
    else if (cb.dataset.group != null) { for (const a of shown) if (a.media.folder + '\n' + a.target === cb.dataset.group) cb.checked ? Y.sel.add(a.id) : Y.sel.delete(a.id); }
    else { const id = +cb.closest('.yrow').dataset.id; cb.checked ? Y.sel.add(id) : Y.sel.delete(id); }
    rerender();
  };
  grid.onclick = e => {
    const fs = e.target.dataset.fsel;
    if (fs) {
      for (const f of Y.folders) if (f.target && (fs === 'name-all' ? f.kind === 'name' : f.kind === 'outside' && f.level === 'high')) Y.fsel.add(f.path);
      return rerender();
    }
    const fo = e.target.dataset.folder;
    if (fo) { e.preventDefault(); if (S.folders.some(f => f.path === fo)) openFolder(fo); else toast('That folder has no pictures of its own; its subfolders do.'); return; }
    const fid = e.target.dataset.fopen;
    if (fid) { const f = Y.folders.find(x => x.path === e.target.closest('.yfrow').dataset.path); return openList(f.sample, f.sample.findIndex(m => m.id === +fid)); }
    const id = e.target.dataset.open; if (!id) return;
    const list = shown.map(a => a.media);
    openList(list, list.findIndex(m => m.id === +id));
  };
}
async function applyFolders() {
  const Y = T.years, paths = [...Y.fsel];
  if (!paths.length) return;
  const fs = Y.folders.filter(f => Y.fsel.has(f.path));
  const nm = fs.filter(f => f.kind === 'name').length, sp = fs.filter(f => f.kind === 'split').length, mv = fs.length - nm - sp;
  if (!confirm(`${[mv && `Move ${plural(mv, 'folder')} into year folders`, sp && `Sort the files of ${plural(sp, 'folder')} into year folders`,
    nm && `Rename ${plural(nm, 'folder')}`].filter(Boolean).join(', ')}?\n\n` +
    'Folders are moved or renamed on disk, with everything in them. Nothing is deleted.')) return;
  $('#fApply').disabled = true;
  const r = await post('/api/years/folders', { paths }).catch(fail);
  if (!r) { renderYears(); return; }
  const ok = r.results.filter(x => x.ok), errs = r.results.filter(x => !x.ok);
  const done = new Set(ok.map(x => x.path));
  const under = (p, d) => p === d || p.startsWith(d + '/');
  Y.folders = Y.folders.filter(f => ![...done].some(d => under(f.path, d)));
  Y.items = Y.items.filter(a => ![...done].some(d => under(a.media.folder, d)));
  Y.fsel.clear();
  toast(`Done: ${plural(ok.length, 'folder')}` + (errs.length ? `. ${errs.length} could not be changed: ${errs[0].error}` : ''), errs.length ? 8000 : 4000);
  renderYears(); loadFolders();
}
async function moveYears() {
  const Y = T.years, ids = [...Y.sel];
  if (!ids.length) return;
  if (!confirm(`Move ${plural(ids.length, 'file')} to the suggested year folders?\n\nFiles are moved on disk (not copied). Nothing is deleted.`)) return;
  $('#yMove').disabled = true;
  const r = await post('/api/years/move', { ids }).catch(fail);
  if (!r) { $('#yMove').disabled = false; return; }
  const moved = new Set(r.results.filter(x => x.ok).map(x => x.id));
  const errs = r.results.filter(x => !x.ok);
  Y.items = Y.items.filter(a => !moved.has(a.id)); Y.sel.clear();
  toast(`Moved ${plural(moved.size, 'file')}` + (errs.length ? `. ${errs.length} could not be moved: ${errs[0].error}` : ''), errs.length ? 8000 : 4000);
  const st = $('#grid').scrollTop; renderYears(); $('#grid').scrollTop = st;
  loadFolders();
}

// ---- duplicates
function openDupes() {
  enterTool('dupes', 'Duplicates', ICON_DUP, 'Looking for duplicate photos and videos…');
  $('#toolRerun').onclick = () => runTool('dupes', '/api/duplicates', true, gotDupes);
  T.limit = 40;
  runTool('dupes', '/api/duplicates', false, gotDupes);
}
function gotDupes(d) {
  // byte-for-byte copies of the file to keep are pre-marked for removal; look-alikes are left for you to decide
  const remove = new Set();
  for (const g of d.groups) for (const it of g.items) if (it.id !== g.keep && it.same_as_keep) remove.add(it.id);
  T.dupes = { groups: d.groups, removed: d.removed, finished: d.finished, remove, show: store.get('dupesShow', 'both') };
  renderDupes();
}
function renderDupes() {
  const D = T.dupes; if (!D || S.mode !== 'dupes') return;
  const groups = D.groups.filter(g => (D.show === 'both' || D.show === g.kind) && g.items.some(it => kindOk(it.media)));
  const nExact = D.groups.filter(g => g.kind === 'exact').length, nSim = D.groups.length - nExact;
  const marked = D.groups.flatMap(g => g.items).filter(it => D.remove.has(it.id));
  const freed = marked.reduce((t, it) => t + (it.size || 0), 0);
  $('#toolSub').textContent = D.groups.length ? `${plural(nExact, 'set')} of exact copies · ${plural(nSim, 'set')} of look-alikes` : 'No duplicates found';
  const rm = D.removed;
  $('#libExtra').innerHTML = `${D.groups.length ? `<div class="extra-head">
      <label>Show <select id="dShow"><option value="both">Everything (${D.groups.length})</option>
        <option value="exact">Exact copies (${nExact})</option><option value="similar">Look-alikes (${nSim})</option></select></label>
      <button id="dMarkExact" title="In every set, mark the byte-for-byte copies of the file being kept">Mark all extra exact copies</button>
      <button id="dUnmark">Unmark all</button>
      <button id="dRemove" class="danger-btn" ${marked.length ? '' : 'disabled'}>Remove ${plural(marked.length, 'marked file')}${marked.length ? ` (${fmtBytes(freed)})` : ''}</button>
    </div><div class="muted"><b>Exact copies</b> are byte-for-byte identical. <b>Look-alikes</b> are the same picture saved differently (resized, re-saved or converted). They can also be burst shots, so check them before removing.
      Click a picture to mark it <span class="keep-word">keep</span> or <span class="rm-word">remove</span>. Double-click to open it.</div>` : ''}
    ${rm.count ? `<div class="removed-bar">${plural(rm.count, 'removed file')} (${fmtBytes(rm.bytes)}) are in the hidden <code>.removed-duplicates</code> folder inside your Pictures folder.
      <button id="dRestore">Put them all back</button><button id="dEmpty" class="danger-btn">Delete them permanently</button></div>` : ''}`;
  if (D.groups.length) {
    $('#dShow').value = D.show;
    $('#dShow').onchange = e => { D.show = e.target.value; store.set('dupesShow', D.show); T.limit = 40; renderDupes(); };
    $('#dMarkExact').onclick = () => { for (const g of D.groups) for (const it of g.items) if (it.id !== g.keep && it.same_as_keep) D.remove.add(it.id); rerenderDupes(); };
    $('#dUnmark').onclick = () => { D.remove.clear(); rerenderDupes(); };
    $('#dRemove').onclick = removeDupes;
  }
  if (rm.count) { $('#dRestore').onclick = restoreDupes; $('#dEmpty').onclick = emptyDupes; }
  const grid = $('#grid');
  if (!groups.length) {
    grid.innerHTML = `<div class="tool-empty">${D.groups.length ? `No duplicate ${S.kind === 'all' ? 'files' : KIND_WORD[S.kind]} of this type.` : 'No duplicate photos or videos found. 🎉'}</div>`;
    $('#libInfo').textContent = ''; return;
  }
  let html = '';
  for (const g of groups.slice(0, T.limit)) {
    const gi = D.groups.indexOf(g);
    html += `<section class="dgroup" data-g="${gi}"><div class="dghead"><span class="dkind ${g.kind}">${g.kind === 'exact' ? 'Exact copies' : 'Look-alikes'}</span>
      <span class="muted">${plural(g.items.length, 'file')} · ${fmtBytes(g.extra_bytes)} in extra copies</span></div><div class="dtiles">`;
    for (const it of g.items) {
      const m = it.media, rmv = D.remove.has(it.id);
      html += `<div class="dtile ${rmv ? 'rm' : 'keep'}" data-id="${it.id}" title="${esc(it.path)}">
        <div class="dimg"><img loading="lazy" src="${thumbUrl(m)}" alt=""><span class="dbadge">${rmv ? 'Remove' : 'Keep'}</span>${m.kind === 'video' ? '<span class="vid">▶ video</span>' : ''}</div>
        <div class="dname">${esc(m.name)}</div>
        <div class="dmeta">${esc(relPath(m.folder))}</div>
        <div class="dmeta">${m.width && m.height ? `${m.width}×${m.height} · ` : ''}${fmtBytes(it.size || 0)}${m.taken ? ' · ' + fmtDate(m.taken) : ''}</div>
        ${g.kind === 'similar' && it.id !== g.keep && it.same_as_keep ? '<div class="dmeta">identical to the first</div>' : ''}
      </div>`;
    }
    html += '</div></section>';
  }
  if (groups.length > T.limit) html += `<div class="tool-more"><button id="dMore">Show more (${groups.length - T.limit} sets left)</button></div>`;
  grid.innerHTML = html;
  $('#libInfo').textContent = `${plural(Math.min(groups.length, T.limit), 'set')} shown of ${groups.length} · ${marked.length} marked to remove`;
  const more = $('#dMore'); if (more) more.onclick = () => { T.limit += 100; rerenderDupes(); };
  let clickTimer;
  grid.onclick = e => {
    const t = e.target.closest('.dtile'); if (!t) return;
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => {  // wait to tell a click from a double-click
      const id = +t.dataset.id, g = D.groups[+t.closest('.dgroup').dataset.g];
      if (!D.remove.has(id) && g.items.filter(it => !D.remove.has(it.id)).length <= 1) {
        // clicking the only file kept: in a pair, keep the other one instead
        if (g.items.length !== 2) return toast('Keep at least one file from each set');
        for (const it of g.items) D.remove.add(it.id);
        D.remove.delete(g.items.find(it => it.id !== id).id);
      } else D.remove.has(id) ? D.remove.delete(id) : D.remove.add(id);
      rerenderDupes();
    }, 220);
  };
  grid.ondblclick = e => {
    const t = e.target.closest('.dtile'); if (!t) return;
    clearTimeout(clickTimer);
    const g = D.groups[+t.closest('.dgroup').dataset.g], list = g.items.map(it => it.media);
    openList(list, list.findIndex(m => m.id === +t.dataset.id));
  };
}
function rerenderDupes() { const g = $('#grid'), st = g.scrollTop; renderDupes(); g.scrollTop = st; }
async function removeDupes() {
  const D = T.dupes, ids = [...D.remove];
  const bytes = D.groups.flatMap(g => g.items).filter(it => D.remove.has(it.id)).reduce((t, it) => t + (it.size || 0), 0);
  if (!confirm(`Remove ${plural(ids.length, 'file')} (${fmtBytes(bytes)})?\n\nThey are moved to a hidden ".removed-duplicates" folder, so you can put them back until you delete them permanently.`)) return;
  $('#dRemove').disabled = true;
  const r = await post('/api/duplicates/remove', { ids }).catch(fail);
  if (!r) { rerenderDupes(); return; }
  const gone = new Set(r.results.filter(x => x.ok).map(x => x.id));
  const errs = r.results.filter(x => !x.ok);
  D.groups = D.groups.map(g => ({ ...g, items: g.items.filter(it => !gone.has(it.id)) })).filter(g => g.items.length >= 2);
  for (const id of gone) D.remove.delete(id);
  D.removed = r.removed;
  toast(`Removed ${plural(gone.size, 'file')}` + (errs.length ? `. ${errs.length} could not be removed: ${errs[0].error}` : ''), errs.length ? 8000 : 4000);
  rerenderDupes(); loadFolders();
}
async function restoreDupes() {
  const r = await post('/api/duplicates/restore').catch(fail);
  if (!r) return;
  toast(`Put back ${plural(r.restored, 'file')}` + (r.failed.length ? `. ${r.failed.length} could not be: ${r.failed[0]}` : ''), 6000);
  await post('/api/rescan').catch(() => {});
  setTimeout(() => { loadFolders(); if (S.mode === 'dupes') runTool('dupes', '/api/duplicates', true, gotDupes); }, 1500);
}
async function emptyDupes() {
  const rm = T.dupes.removed;
  if (!confirm(`Permanently delete ${plural(rm.count, 'removed file')} (${fmtBytes(rm.bytes)})?\n\nThis cannot be undone.`)) return;
  const r = await post('/api/duplicates/empty').catch(fail);
  if (!r) return;
  T.dupes.removed = r.removed;
  toast(`Deleted ${plural(r.deleted, 'file')} permanently`);
  rerenderDupes(); refreshPeopleSoon();
}

// ---- junk finder
const ICON_JUNK = '<svg viewBox="0 0 24 24" width="40" height="40"><path d="M5 7h14l-1.2 13a2 2 0 0 1-2 1.8H8.2a2 2 0 0 1-2-1.8z" fill="#fff" stroke="#4a7fc1" stroke-width="1.6"/><path d="M3 7h18M9 7V4h6v3" fill="none" stroke="#4a7fc1" stroke-width="1.6" stroke-linecap="round"/><path d="M10 11v7M14 11v7" stroke="#4a7fc1" stroke-width="1.4" stroke-linecap="round"/></svg>';
const JUNK_CATS = { screenshot: 'Screenshots', document: 'Documents, receipts & signs', graphic: 'Memes, logos & web graphics', book: 'Illustrations & book art' };
function openJunk() {
  enterTool('junk', 'Junk', ICON_JUNK, 'Looking for pictures that are not personal photos…');
  $('#toolRerun').onclick = () => runTool('junk', '/api/junk', true, gotJunk);
  T.limit = 120;
  runTool('junk', '/api/junk', false, gotJunk);
}
function gotJunk(d) {
  // only clear-cut screenshots and web graphics are pre-selected; paperwork and book art are for you to decide
  const sel = new Set(d.items.filter(a => a.level === 'high' && (a.category === 'screenshot' || a.category === 'graphic') && !a.marked).map(a => a.id));
  T.junk = { items: d.items, removed: d.removed, checked: d.checked, unanalyzed: d.unanalyzed, finished: d.finished, sel,
    cat: store.get('junkCat', 'screenshot'), level: store.get('junkLevel', 'medium') };
  renderJunk();
}
function renderJunk() {
  const J = T.junk; if (!J || S.mode !== 'junk') return;
  const n = c => J.items.filter(a => a.category === c && kindOk(a.media)).length;
  const shown = J.items.filter(a => a.category === J.cat && kindOk(a.media) && LEVELS[J.level].includes(a.level));
  const ids = new Set(J.items.map(a => a.id));
  for (const id of J.sel) if (!ids.has(id)) J.sel.delete(id);
  const nSel = J.sel.size, nJunk = J.items.filter(a => a.category !== 'book').length;
  $('#toolSub').textContent = `Looked at ${plural(J.checked, 'picture')} · ${plural(nJunk, 'possible junk picture')} · ${plural(n('book'), 'illustration')}` +
    (J.unanalyzed ? ` · ${plural(J.unanalyzed, 'picture')} not analyzed yet (check again later)` : '');
  const rm = J.removed;
  $('#libExtra').innerHTML = `<div class="extra-head">
      <div class="seg jcats">${Object.entries(JUNK_CATS).map(([c, t]) => `<button data-cat="${c}" class="${J.cat === c ? 'on' : ''}">${t} (${n(c)})</button>`).join('')}</div>
      <label>Show <select id="jLevel"><option value="high">High confidence</option><option value="medium">High and medium</option><option value="low">Everything found</option></select></label>
    </div>
    <div class="extra-head">
      ${shown.length > T.limit ? `<button id="jSelPage" title="Only the pictures loaded on screen so far">Select all on screen (${Math.min(shown.length, T.limit).toLocaleString()})</button>` : ''}
      <button id="jSelAll" title="Every picture in this list, including ones not loaded yet">Select all (${shown.length.toLocaleString()})</button><button id="jSelNone">Select none</button>
      <span class="sep"></span>
      <button id="jKeep" ${nSel ? '' : 'disabled'} title="Not junk: a personal photo. It will not be suggested again.">Keep (not junk)</button>
      <button id="jBook" ${nSel ? '' : 'disabled'} title="Book art: always kept, and listed under Illustrations & book art">Mark as book art</button>
      <button id="jRemove" class="danger-btn" ${nSel ? '' : 'disabled'}>Remove ${plural(nSel, 'selected picture')}</button>
    </div>
    <div class="muted">${J.cat === 'book' ? 'Drawings, paintings, book covers and other artwork: possibly illustrations for your books. They are never pre-selected. <b>Mark as book art</b> to protect them from removal for good.' :
      'Click a picture to select it, double-click to open it. Hover for why it was picked. Only clear-cut screenshots and web graphics are pre-selected. Removed pictures go to a hidden <code>.removed-junk</code> folder, so you can put them back.'}</div>
    ${rm.count ? `<div class="removed-bar">${plural(rm.count, 'removed picture')} (${fmtBytes(rm.bytes)}) are in the hidden <code>.removed-junk</code> folder.
      <button id="jRestore">Put them all back</button><button id="jEmpty" class="danger-btn">Delete them permanently</button></div>` : ''}`;
  $('#jLevel').value = J.level;
  $('#jLevel').onchange = e => { J.level = e.target.value; store.set('junkLevel', J.level); T.limit = 120; renderJunk(); };
  $('#libExtra .jcats').onclick = e => { const b = e.target.closest('button'); if (!b) return; J.cat = b.dataset.cat; store.set('junkCat', J.cat); T.limit = 120; renderJunk(); $('#grid').scrollTop = 0; };
  const selectJunk = list => { list.forEach(a => { if (a.marked !== 'book') J.sel.add(a.id); }); rerenderJunk(); };
  $('#jSelAll').onclick = () => selectJunk(shown);
  const sp = $('#jSelPage'); if (sp) sp.onclick = () => selectJunk(shown.slice(0, T.limit));
  $('#jSelNone').onclick = () => { J.sel.clear(); rerenderJunk(); };
  $('#jKeep').onclick = () => markJunk('keep');
  $('#jBook').onclick = () => markJunk('book');
  $('#jRemove').onclick = removeJunk;
  if (rm.count) { $('#jRestore').onclick = restoreJunk; $('#jEmpty').onclick = emptyJunk; }
  const grid = $('#grid');
  if (!shown.length) {
    grid.innerHTML = `<div class="tool-empty">${n(J.cat) ? 'Nothing at this confidence level. Choose “Everything found” to see weaker matches.' : `No ${JUNK_CATS[J.cat].toLowerCase()} found. 🎉`}</div>`;
    $('#libInfo').textContent = ''; return;
  }
  let html = '<div class="jtiles">';
  for (const a of shown.slice(0, T.limit)) {
    const m = a.media, on = J.sel.has(a.id), book = a.marked === 'book';
    html += `<div class="jtile${on ? ' on' : ''}${book ? ' book' : ''}" data-id="${a.id}" title="${esc(m.name + '\n' + relPath(m.folder) + '\n\n' + a.reasons.join('\n'))}">
      <div class="dimg"><img loading="lazy" src="${thumbUrl(m)}" alt=""><span class="jcheck">${on ? '✓' : ''}</span>${m.kind === 'video' ? '<span class="vid">▶ video</span>' : ''}${book ? '<span class="jbook">Book art</span>' : ''}</div>
      <div class="dname">${esc(m.name)}</div>
      <div class="dmeta">${esc(relPath(m.folder))}</div>
      ${levelBadge(a.level, a.confidence, a.reasons.join('\n'))}
    </div>`;
  }
  html += '</div>';
  if (shown.length > T.limit) html += `<div class="tool-more"><button id="jMore">Show more (${shown.length - T.limit} left)</button></div>`;
  grid.innerHTML = html;
  $('#libInfo').textContent = `${plural(Math.min(shown.length, T.limit), 'picture')} shown of ${shown.length} · ${nSel} selected`;
  const more = $('#jMore'); if (more) more.onclick = () => { T.limit += 300; rerenderJunk(); };
  let clickTimer;
  grid.onclick = e => {
    const t = e.target.closest('.jtile'); if (!t) return;
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => {  // wait to tell a click from a double-click
      const id = +t.dataset.id;
      if (t.classList.contains('book') && !J.sel.has(id)) return toast('Marked as book art, so it is kept. Select it from another list to change that.');
      J.sel.has(id) ? J.sel.delete(id) : J.sel.add(id);
      rerenderJunk();
    }, 220);
  };
  grid.ondblclick = e => {
    const t = e.target.closest('.jtile'); if (!t) return;
    clearTimeout(clickTimer);
    const list = shown.map(a => a.media);
    openList(list, list.findIndex(m => m.id === +t.dataset.id));
  };
}
function rerenderJunk() { const g = $('#grid'), st = g.scrollTop; renderJunk(); g.scrollTop = st; }
async function markJunk(mark) {
  const J = T.junk, ids = [...J.sel];
  if (!ids.length) return;
  const r = await post('/api/junk/mark', { ids, mark }).catch(fail);
  if (!r) return;
  const set = new Set(ids);
  if (mark === 'keep') J.items = J.items.filter(a => !set.has(a.id));
  else for (const a of J.items) if (set.has(a.id)) { a.marked = 'book'; a.category = 'book'; }
  J.sel.clear();
  toast(mark === 'keep' ? `${plural(ids.length, 'picture')} kept: they will not be suggested again` : `${plural(ids.length, 'picture')} marked as book art`);
  rerenderJunk();
}
async function removeJunk() {
  const J = T.junk, ids = [...J.sel];
  const items = J.items.filter(a => J.sel.has(a.id));
  const art = items.filter(a => a.category === 'book').length;
  if (!confirm(`Remove ${plural(ids.length, 'picture')}?` + (art ? `\n\n${plural(art, 'of them looks', 'of them look')} like an illustration: check it is not for one of your books.` : '') +
    '\n\nThey are moved to a hidden ".removed-junk" folder, so you can put them back until you delete them permanently.')) return;
  $('#jRemove').disabled = true;
  const r = await post('/api/junk/remove', { ids }).catch(fail);
  if (!r) { rerenderJunk(); return; }
  const gone = new Set(r.results.filter(x => x.ok).map(x => x.id));
  const errs = r.results.filter(x => !x.ok);
  J.items = J.items.filter(a => !gone.has(a.id));
  for (const id of gone) J.sel.delete(id);
  J.removed = r.removed;
  toast(`Removed ${plural(gone.size, 'picture')}` + (errs.length ? `. ${errs.length} could not be removed: ${errs[0].error}` : ''), errs.length ? 8000 : 4000);
  rerenderJunk(); loadFolders();
}
async function restoreJunk() {
  const r = await post('/api/junk/restore').catch(fail);
  if (!r) return;
  toast(`Put back ${plural(r.restored, 'picture')}` + (r.failed.length ? `. ${r.failed.length} could not be: ${r.failed[0]}` : ''), 6000);
  await post('/api/rescan').catch(() => {});
  setTimeout(() => { loadFolders(); if (S.mode === 'junk') runTool('junk', '/api/junk', true, gotJunk); }, 1500);
}
async function emptyJunk() {
  const rm = T.junk.removed;
  if (!confirm(`Permanently delete ${plural(rm.count, 'removed picture')} (${fmtBytes(rm.bytes)})?\n\nThis cannot be undone.`)) return;
  const r = await post('/api/junk/empty').catch(fail);
  if (!r) return;
  T.junk.removed = r.removed;
  toast(`Deleted ${plural(r.deleted, 'picture')} permanently`);
  rerenderJunk(); refreshPeopleSoon();
}

$('#btnYears').onclick = openYears;
$('#btnDupes').onclick = openDupes;
$('#btnJunk').onclick = openJunk;

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
    const pct = s.total ? Math.round(100 * s.scanned / s.total) : 100;
    $('#status').innerHTML = s.indexing ? 'Indexing folders…' :
      `${s.total.toLocaleString()} items · ${s.faces.toLocaleString()} faces` +
      (s.scanned < s.total ? ` · scanning ${pct}%<span class="bar"><i style="width:${pct}%"></i></span>` :
       s.search.enabled && s.search.done < s.search.total ? ` · analyzing for search ${Math.round(100 * s.search.done / s.search.total)}%` : '');
    S.searchIndex = s.search; S.ffmpeg = s.ffmpeg;
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
  setKind(store.get('kind', 'all'));
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
