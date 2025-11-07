// Bridge to the extension service worker. Two transports:
// 1) postMessage relay via extension content script on http://localhost:3000/* (no ID needed)
// 2) direct chrome.runtime with NEXT_PUBLIC_EXTENSION_ID (optional fallback)

type TitlesItem = { id: string; title: string; ts: number; platform: string };
type ProfileHistogram = { t0: Record<string, number>; t1: Record<string, number>; updated_at?: number };
type Piece = { date?: string; title: string; source?: string; url?: string; image_url?: string; imageUrl?: string };

const EXTENSION_ID = process.env.NEXT_PUBLIC_EXTENSION_ID || '';
const PM_NAMESPACE_IN = 'thyself-bridge';
const PM_NAMESPACE_OUT = 'thyself-extension';

function canUseRuntime(): boolean {
  if (typeof window === 'undefined') return false;
  // In Chrome, pages don't typically have chrome.runtime, but some environments might expose it.
  // We also support chrome.runtime.sendMessage(extensionId, ...)
  const anyWin = window as any;
  return !!(anyWin.chrome && anyWin.chrome.runtime && typeof anyWin.chrome.runtime.sendMessage === 'function');
}

function pmSend(type: string, payload?: any, timeoutMs = 1500): Promise<any | null> {
  return new Promise((resolve) => {
    if (typeof window === 'undefined') return resolve(null);
    const requestId = Math.random().toString(36).slice(2);
    let done = false;
    const listener = (evt: MessageEvent) => {
      const data = evt.data;
      if (!data || data.namespace !== PM_NAMESPACE_OUT || data.requestId !== requestId) return;
      done = true; window.removeEventListener('message', listener);
      resolve(data.response || null);
    };
    window.addEventListener('message', listener);
    // Post to same window; the extension content script listens and relays
    window.postMessage({ namespace: PM_NAMESPACE_IN, action: type, payload: payload || {}, requestId }, '*');
    setTimeout(() => { if (!done) { window.removeEventListener('message', listener); resolve(null); } }, timeoutMs);
  });
}

async function runtimeSend(type: string, payload?: any): Promise<any | null> {
  if (!canUseRuntime() || !EXTENSION_ID) return null;
  const anyWin = window as any;
  return new Promise((resolve) => {
    try {
      anyWin.chrome.runtime.sendMessage(EXTENSION_ID, { type, ...payload }, (resp: any) => resolve(resp || null));
    } catch { resolve(null); }
  });
}

async function send(type: string, payload?: any): Promise<any | null> {
  // Prefer postMessage relay (no ID needed); fallback to runtime if configured
  const viaPM = await pmSend(type, payload);
  if (viaPM) return viaPM;
  return runtimeSend(type, payload);
}

export async function getTitles(): Promise<TitlesItem[] | null> {
  const resp = await send('GET_TITLES');
  if (resp && resp.ok && Array.isArray(resp.data)) return resp.data as TitlesItem[];
  return null;
}

export async function ackTitles(ids: string[]): Promise<boolean> {
  const resp = await send('ACK_TITLES', { ids });
  return !!(resp && resp.ok);
}

export async function setProfile(profile: ProfileHistogram): Promise<boolean> {
  const resp = await send('SET_PROFILE', { profile });
  return !!(resp && resp.ok);
}

export async function getProfile(): Promise<ProfileHistogram | null> {
  const resp = await send('GET_PROFILE');
  if (resp && resp.ok) return (resp.data || null) as ProfileHistogram | null;
  return null;
}

export async function appendPiece(piece: Piece): Promise<boolean> {
  const resp = await send('APPEND_PIECE', { piece });
  return !!(resp && resp.ok);
}

export async function getArchive(): Promise<Piece[] | null> {
  const resp = await send('GET_ARCHIVE');
  if (resp && resp.ok && Array.isArray(resp.data)) return resp.data as Piece[];
  return null;
}

export async function getStatus(): Promise<{ last_webapp_sync_at: number | null } | null> {
  const resp = await send('GET_STATUS');
  if (resp && resp.ok) return resp.data || { last_webapp_sync_at: null };
  return null;
}

export async function extensionAvailable(): Promise<boolean> {
  const resp = await send('GET_STATUS');
  return !!(resp && resp.ok);
}
