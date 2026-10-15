// MonsterUI 1.0.47's theme script (Theme.blue.headers()), moved out of the page so the CSP
// can forbid inline scripts. Loaded without defer, so the classes are set before first paint.
(function () {
  var html = document.documentElement;
  var saved = {};
  try {
    saved = JSON.parse(localStorage.getItem("__FRANKEN__") || "{}");
  } catch (e) {}
  if (
    saved.mode === "dark" ||
    (!saved.mode && window.matchMedia("(prefers-color-scheme: dark)").matches)
  ) {
    html.classList.add("dark");
  } else {
    html.classList.remove("dark");
  }
  html.classList.add(saved.theme || "uk-theme-blue");
  html.classList.add(saved.radii || "uk-radii-sm");
  html.classList.add(saved.shadows || "uk-shadows-sm");
  html.classList.add(saved.font || "uk-font-sm");
})();
