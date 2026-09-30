/* Runs before first paint (external file: CSP forbids inline script). Applies the saved theme. */
(function () {
  try {
    var t = localStorage.getItem("aij.theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow system */ }
})();
