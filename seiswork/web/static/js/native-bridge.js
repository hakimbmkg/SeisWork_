/* Native (pywebview) bridge.
 *
 * In `seiswork gui --native` the app runs inside WKWebView (macOS) / WebKitGTK,
 * which — unlike a real browser — silently ignores BOTH `window.open(...)` and
 * `<a target="_blank">`. So the "open full-page" buttons (Waveform, Station Map,
 * Catalog Map, Pipeline Flow) do nothing: no new page ever appears.
 *
 * When the pywebview JS API is present we route those actions to the Python
 * bridge (`open_window`), which spawns a REAL native window loading that URL.
 * In a normal browser this file is inert (window.pywebview is undefined), so
 * the standard target="_blank" / window.open behaviour is left untouched.
 */
(function () {
  var patched = false;

  function nativeOpen(url) {
    try { window.pywebview.api.open_window(url); } catch (e) { /* ignore */ }
  }

  function nativeClose() {
    try { window.pywebview.api.close_window(); } catch (e) { /* ignore */ }
  }

  function wire() {
    if (patched) return;
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.open_window) return;
    patched = true;

    // window.open(url, ...) → native window
    var _open = window.open;
    window.open = function (url) {
      if (url) { nativeOpen(url); return null; }
      return _open.apply(window, arguments);
    };

    // window.close() → destroy this native window (WKWebView ignores it otherwise,
    // so the full-page views' × Close button does nothing in --native mode).
    window.close = function () { nativeClose(); };

    // <a target="_blank"> clicks → native window (capture phase, before default)
    document.addEventListener('click', function (e) {
      var a = e.target && e.target.closest ? e.target.closest('a[target="_blank"]') : null;
      if (!a) return;
      var href = a.getAttribute('href');
      if (!href || href === '#') return;
      e.preventDefault();
      nativeOpen(a.href);   // a.href is already an absolute URL
    }, true);
  }

  // The API may already be injected, or arrive later via the ready event.
  if (window.pywebview && window.pywebview.api) wire();
  window.addEventListener('pywebviewready', wire);
})();
