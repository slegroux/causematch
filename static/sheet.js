// The "It's a match!" sheet arrives as an open <dialog>; make it modal (focus moves in,
// Escape closes it, screen readers treat it as a dialog). On load and after an htmx swap.
(function () {
  function modal() {
    var open = Array.prototype.slice.call(
      document.querySelectorAll("dialog.cm-moment[open]")
    );
    var fresh = open.filter(function (d) {
      return !d.matches(":modal");
    });
    if (!fresh.length) return;
    // a newer sheet replaces one still open; of two that arrive together (a poll and a
    // swipe answered at once) only the first stays
    open.forEach(function (d) {
      if (d !== fresh[0]) d.close();
    });
    fresh[0].close();
    fresh[0].showModal();
  }
  modal();
  document.body.addEventListener("htmx:afterSwap", modal);
  // The poll for a new match (#moment-slot) skips a tab nobody is looking at; it asks
  // again the moment the tab is visible (hx-trigger "visibilitychange from:document").
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    if (document.hidden && e.target.id === "moment-slot") e.preventDefault();
  });
})();
