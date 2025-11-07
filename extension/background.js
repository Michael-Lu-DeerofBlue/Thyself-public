// Background service worker: title-only pipeline + storage contract for web app bridge
// - Captures raw events (feed_video, shorts_video) into IndexedDB 'events'
// - Maintains chrome.storage.local keys:
//   titles_batch: [{ id, title, ts, platform }]
//   profile_histogram: { t0: Record<string,number>, t1: Record<string,number>, updated_at }
//   pieces_archive: [{ date, title, source, url }]
//   last_webapp_sync_at: epoch ms (set on SET_PROFILE/APPEND_PIECE)

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('local-logger', 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('events')) {
        const store = db.createObjectStore('events', { keyPath: 'id', autoIncrement: true });
        store.createIndex('by_ts', 'ts');
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function addEvent(evt) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('events', 'readwrite');
    tx.objectStore('events').add(evt);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}
async function getRecent(limit = 200) {
  const db = await openDB();
  return new Promise((resolve) => {
    const out = [];
    const tx = db.transaction('events', 'readonly');
    const idx = tx.objectStore('events').index('by_ts');
    const req = idx.openCursor(null, 'prev');
    req.onsuccess = e => {
      const cur = e.target.result;
      if (cur && out.length < limit) { out.push(cur.value); cur.continue(); } else resolve(out);
    };
  });
}
async function clearAll() {
  const db = await openDB();
  return new Promise((resolve) => {
    const tx = db.transaction('events', 'readwrite');
    tx.objectStore('events').clear();
    tx.oncomplete = () => resolve();
  });
}

// Only keep whitelisted types originating from YouTube harvesting
const ALLOWED_TYPES = new Set(['feed_video', 'shorts_video']);
function sanitizeEvent(p) {
  try {
    if (!p || !ALLOWED_TYPES.has(p.type)) return null;
    const title = (p.title || '').trim();
    if (!title) return null;
    return {
      ts: p.ts || Date.now(),
      type: p.type,
      title,
      videoId: p.videoId,
      href: p.href,
      page: p.page,
      platform: p.platform || 'youtube'
    };
  } catch { return null; }
}

// ---------------- Schema helpers in chrome.storage.local ----------------
const BATCH_RETENTION_DAYS = 14; // rolling window
const ARCHIVE_MAX = 365;

function normalizeTitle(t) {
  return String(t || '')
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '') // strip diacritics
    .replace(/[\s\t\n\r]+/g, ' ')
    .replace(/["'`]+/g, '')
    .trim();
}

function hashStr(s) {
  // djb2
  let h = 5381;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) + h) + s.charCodeAt(i);
    h |= 0;
  }
  // convert to unsigned hex
  return (h >>> 0).toString(16);
}

async function getStore(keys) {
  return new Promise(resolve => chrome.storage.local.get(keys, resolve));
}
async function setStore(obj) {
  return new Promise(resolve => chrome.storage.local.set(obj, resolve));
}

async function updateTitlesBatchFromEvent(evt) {
  const now = Date.now();
  const { titles_batch = [] } = await getStore(['titles_batch']);
  const keepAfter = now - BATCH_RETENTION_DAYS * 24 * 60 * 60 * 1000;
  const cleaned = Array.isArray(titles_batch) ? titles_batch.filter(x => (x && x.ts >= keepAfter)) : [];
  const norm = normalizeTitle(evt.title);
  if (!norm) {
    await setStore({ titles_batch: cleaned });
    return;
  }
  const id = hashStr(norm);
  let exists = false;
  for (let i = 0; i < cleaned.length; i++) {
    if (cleaned[i].id === id) {
      // refresh timestamp to the latest sighting
      cleaned[i] = { ...cleaned[i], ts: Math.max(cleaned[i].ts || 0, evt.ts || now) };
      exists = true;
      break;
    }
  }
  if (!exists) cleaned.push({ id, title: evt.title, ts: evt.ts || now, platform: evt.platform || 'youtube' });
  await setStore({ titles_batch: cleaned });
}

// Ensure titles_batch updates happen sequentially to avoid lost updates under concurrency
let __titlesQueue = Promise.resolve();
function enqueueTitlesUpdate(evt) {
  __titlesQueue = __titlesQueue.then(() => updateTitlesBatchFromEvent(evt)).catch(() => {});
  return __titlesQueue;
}

async function getTitlesBatch() {
  const { titles_batch = [] } = await getStore(['titles_batch']);
  return Array.isArray(titles_batch) ? titles_batch : [];
}

async function clearTitlesBatch() {
  await setStore({ titles_batch: [] });
}

async function setProfileHistogram(profile) {
  const payload = { ...profile, updated_at: Date.now() };
  await setStore({ profile_histogram: payload, last_webapp_sync_at: Date.now() });
}

async function getProfileHistogram() {
  const { profile_histogram = null } = await getStore(['profile_histogram']);
  return profile_histogram;
}

async function appendPiece(piece) {
  const { pieces_archive = [] } = await getStore(['pieces_archive']);
  const arr = Array.isArray(pieces_archive) ? pieces_archive.slice() : [];
  // normalize date string if absent
  const d = piece.date || new Date().toISOString().slice(0, 10).replaceAll('-', '/');
  const img = piece.image_url || piece.imageUrl || '';
  arr.unshift({
    date: d,
    title: piece.title || '',
    source: piece.source || '',
    url: piece.url || '',
    image_url: img,
    imageUrl: img // store camelCase mirror for forward-compat
  });
  const cropped = arr.slice(0, ARCHIVE_MAX);
  await setStore({ pieces_archive: cropped, last_webapp_sync_at: Date.now() });
}

async function getArchive() {
  const { pieces_archive = [] } = await getStore(['pieces_archive']);
  const arr = Array.isArray(pieces_archive) ? pieces_archive : [];
  // Ensure both image_url and imageUrl are present on return for older entries
  const mapped = arr.map((p) => {
    try {
      const img = (p && (p.image_url || p.imageUrl)) || '';
      return img && (!p.imageUrl || !p.image_url)
        ? { ...p, image_url: p.image_url || img, imageUrl: p.imageUrl || img }
        : p;
    } catch { return p; }
  });
  return mapped;
}

async function clearProfileAndArchive() {
  await setStore({ profile_histogram: null, pieces_archive: [], last_webapp_sync_at: Date.now() });
}

async function getStatus() {
  const st = await getStore(['last_webapp_sync_at']);
  return { last_webapp_sync_at: st.last_webapp_sync_at || null };
}

async function ackTitles(ids) {
  if (!Array.isArray(ids) || !ids.length) return;
  const { titles_batch = [] } = await getStore(['titles_batch']);
  if (!Array.isArray(titles_batch) || !titles_batch.length) return;
  const drop = new Set(ids);
  const kept = titles_batch.filter(x => x && !drop.has(x.id));
  await setStore({ titles_batch: kept });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg && msg.type === 'LCL_ADD_EVENT') {
        const compact = sanitizeEvent(msg.payload);
        if (compact) {
          await addEvent(compact);
          // also reflect into titles_batch with dedupe + retention (queued to avoid races)
          await enqueueTitlesUpdate(compact);
          sendResponse({ ok: true, stored: true });
        } else {
          sendResponse({ ok: true, stored: false });
        }
      // legacy/event debug endpoints
      } else if (msg && msg.type === 'LCL_GET_RECENT') {
        const data = await getRecent(msg.limit || 200);
        sendResponse({ ok: true, data });
      } else if (msg && msg.type === 'LCL_CLEAR') {
        await clearAll();
        sendResponse({ ok: true });

      // -------- Web app bridge contract --------
      } else if (msg && msg.type === 'GET_TITLES') {
        const data = await getTitlesBatch();
        sendResponse({ ok: true, data });
      } else if (msg && msg.type === 'ACK_TITLES') {
        await ackTitles((msg.ids || msg.payload || {}).ids || msg.ids || []);
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'CLEAR_TITLES') {
        await clearTitlesBatch();
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'SET_PROFILE') {
        await setProfileHistogram(msg.profile || msg.payload || {});
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'GET_PROFILE') {
        const prof = await getProfileHistogram();
        sendResponse({ ok: true, data: prof });
      } else if (msg && msg.type === 'APPEND_PIECE') {
        await appendPiece(msg.piece || msg.payload || {});
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'GET_ARCHIVE') {
        const data = await getArchive();
        sendResponse({ ok: true, data });
      } else if (msg && msg.type === 'GET_STATUS') {
        const data = await getStatus();
        sendResponse({ ok: true, data });
      } else if (msg && msg.type === 'CLEAR_PROFILE_ARCHIVE') {
        await clearProfileAndArchive();
        sendResponse({ ok: true });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true; // keep message channel open for async
});

// Allow external web pages (when permitted via externally_connectable) to call the same API
chrome.runtime.onMessageExternal?.addListener((msg, sender, sendResponse) => {
  // Delegate to the same handler by invoking onMessage directly
  // Note: duplicate code avoided by calling the same logic via a function, but for brevity we inline minimal handling
  (async () => {
    try {
      // Only allow a small set of bridge methods externally
      if (msg && msg.type === 'GET_TITLES') {
        const data = await getTitlesBatch();
        sendResponse({ ok: true, data });
      } else if (msg && msg.type === 'ACK_TITLES') {
        await ackTitles((msg.ids || msg.payload || {}).ids || msg.ids || []);
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'CLEAR_TITLES') {
        await clearTitlesBatch();
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'SET_PROFILE') {
        await setProfileHistogram(msg.profile || msg.payload || {});
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'GET_PROFILE') {
        const prof = await getProfileHistogram();
        sendResponse({ ok: true, data: prof });
      } else if (msg && msg.type === 'APPEND_PIECE') {
        await appendPiece(msg.piece || msg.payload || {});
        sendResponse({ ok: true });
      } else if (msg && msg.type === 'GET_ARCHIVE') {
        const data = await getArchive();
        sendResponse({ ok: true, data });
      } else if (msg && msg.type === 'GET_STATUS') {
        const data = await getStatus();
        sendResponse({ ok: true, data });
      } else {
        sendResponse({ ok: false, error: 'unsupported' });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true;
});
