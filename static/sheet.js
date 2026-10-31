// The "It's a match!" sheet arrives as an open <dialog>; make it modal (focus moves in,
// Escape closes it, screen readers treat it as a dialog). On load and after an htmx swap.
(function () {
  function modal() {
    document.querySelectorAll("dialog.cm-moment[open]").forEach(function (d) {
      if (d.matches(":modal")) return;
      d.close();
      d.showModal();
    });
  }
  modal();
  document.body.addEventListener("htmx:afterSwap", modal);
  // The poll for a new match (#moment-slot) skips a tab nobody is looking at; it asks
  // again the moment the tab is visible (hx-trigger "visibilitychange from:document").
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    if (document.hidden && e.target.id === "moment-slot") e.preventDefault();
  });
})();
