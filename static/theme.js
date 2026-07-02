/* Color-mode control. Three preferences: "auto" (follow the browser/OS), "light",
 * "dark". Stored in localStorage; applied by setting data-theme on <html>.
 *
 * For "auto" the attribute is REMOVED so the CSS @media (prefers-color-scheme) rule
 * governs (this is also the no-JS default). "light"/"dark" set the attribute, which
 * overrides the media query. This file is loaded synchronously in <head> (CSP is
 * script-src 'self', so no inline scripts) to set the theme before first paint.
 */
(function () {
  "use strict";
  var KEY = "theme";
  var ORDER = ["auto", "light", "dark"];
  var LABEL = { auto: "◐ Auto", light: "☀ Light", dark: "☾ Dark" };

  function pref() {
    try {
      var v = localStorage.getItem(KEY);
      return ORDER.indexOf(v) >= 0 ? v : "auto";
    } catch (e) {
      return "auto";
    }
  }

  function apply(p) {
    var root = document.documentElement;
    if (p === "light" || p === "dark") root.setAttribute("data-theme", p);
    else root.removeAttribute("data-theme");
  }

  // Apply immediately, before the body paints.
  apply(pref());

  function wire() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    function refresh() {
      var p = pref();
      btn.textContent = LABEL[p];
      btn.setAttribute("aria-label", "Color theme: " + p + ". Click to change.");
      btn.setAttribute("title", "Color theme: " + p + " (click to change)");
    }
    refresh();
    btn.addEventListener("click", function () {
      var next = ORDER[(ORDER.indexOf(pref()) + 1) % ORDER.length];
      try { localStorage.setItem(KEY, next); } catch (e) { /* ignore */ }
      apply(next);
      refresh();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
