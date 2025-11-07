// ---- Background message helpers ----
function sendMsg(type, payload) {
  return new Promise(resolve => chrome.runtime.sendMessage({ type, ...payload }, resp => resolve(resp)));
}
function getRecent(limit=200) {
  return new Promise(resolve => {
    chrome.runtime.sendMessage({ type: 'LCL_GET_RECENT', limit }, resp => {
      resolve((resp && resp.ok && resp.data) ? resp.data : []);
    });
  });
}
function clearEvents() {
  return new Promise(resolve => {
    chrome.runtime.sendMessage({ type: 'LCL_CLEAR' }, () => resolve());
  });
}
async function getTitlesBatch() {
  const resp = await sendMsg('GET_TITLES');
  return (resp && resp.ok && Array.isArray(resp.data)) ? resp.data : [];
}
async function clearTitles() {
  await sendMsg('CLEAR_TITLES');
}
async function getProfile() {
  const resp = await sendMsg('GET_PROFILE');
  return (resp && resp.ok) ? (resp.data || null) : null;
}
async function getArchive() {
  const resp = await sendMsg('GET_ARCHIVE');
  return (resp && resp.ok && Array.isArray(resp.data)) ? resp.data : [];
}
async function getStatus() {
  const resp = await sendMsg('GET_STATUS');
  return (resp && resp.ok) ? (resp.data || {}) : {};
}
async function clearProfileArchive() {
  await sendMsg('CLEAR_PROFILE_ARCHIVE');
}

// ---- UI render ----
async function render() {
  const [events, titles, profile, archive, status] = await Promise.all([
    getRecent(200),
    getTitlesBatch(),
    getProfile(),
    getArchive(),
    getStatus()
  ]);

  // Status and counts
  const lastSync = status.last_webapp_sync_at ? new Date(status.last_webapp_sync_at).toLocaleString() : 'never';
  document.getElementById('status').textContent = `Web app sync: ${lastSync}`;
  document.getElementById('titlesCount').textContent = `${titles.length} titles`;
  document.getElementById('eventsCount').textContent = `${events.length} events`;

  // Today's piece and archive
  const todayWrap = document.getElementById('todayPiece');
  todayWrap.textContent = '—';
  if (archive && archive.length) {
    const first = archive[0];
    const a = document.createElement('a');
    a.href = first.url || '#'; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = first.title || '(untitled)';
    const meta = document.createElement('div');
    meta.className = 'muted';
    meta.textContent = `${first.date || ''} · ${first.source || ''}`;
    todayWrap.innerHTML = '';
    todayWrap.appendChild(a);
    todayWrap.appendChild(meta);
  }
}

// ---- Buttons ----
document.getElementById('refresh').onclick = render;

document.getElementById('exportTitles').onclick = async () => {
  const userId = 'local-user';
  const batch = await getTitlesBatch();
  const titles = [];
  const seen = new Set();
  for (const it of batch) {
    const t = (it && it.title && String(it.title).trim()) || '';
    if (!t) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    titles.push(t);
  }
  const out = { user_id: userId, titles };
  const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: 'titles.json' });
  a.click();
};

document.getElementById('clearTitles').onclick = async () => { await clearTitles(); render(); };
document.getElementById('clearEvents').onclick = async () => { await clearEvents(); render(); };
document.getElementById('clearProfileArchive').onclick = async () => { await clearProfileArchive(); render(); };

render();
