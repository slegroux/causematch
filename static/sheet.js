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
})();
