// Content script bridge for web app ↔ extension communication without exposing extension ID.
// Listens for window messages with namespace 'thyself-bridge' and forwards them to the
// extension background via chrome.runtime.sendMessage. Replies are posted back to window
// with namespace 'thyself-extension' and matching requestId.

(function () {
  const NAMESPACE_IN = 'thyself-bridge';
  const NAMESPACE_OUT = 'thyself-extension';

  function isValidOrigin(evt) {
    try {
      const url = new URL(evt.origin);
      return (
        (url.hostname === 'localhost' || url.hostname === '127.0.0.1') && url.port === '3000'
      );
    } catch { return false; }
  }

  async function forward(type, payload) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type, ...payload }, (resp) => resolve(resp));
      } catch (e) {
        resolve({ ok: false, error: String(e) });
      }
    });
  }

  window.addEventListener('message', async (evt) => {
    const data = evt.data;
    if (!data || data.namespace !== NAMESPACE_IN) return;
    if (!isValidOrigin(evt)) return;

    const { action, payload, requestId } = data;
    const resp = await forward(action, payload || {});
    window.postMessage({ namespace: NAMESPACE_OUT, requestId, response: resp }, '*');
  });
})();
