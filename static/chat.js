// Chat page: keep the newest message in view, and empty the box once a message is sent.
(function () {
  var list = document.getElementById("messages");
  if (!list) return;

  function toEnd() {
    list.scrollTop = list.scrollHeight;
  }
  toEnd();
  document.body.addEventListener("htmx:wsAfterMessage", toEnd);
  document.body.addEventListener("htmx:oobAfterSwap", toEnd);
  document.body.addEventListener("htmx:wsAfterSend", function (e) {
    var form = e.target;
    if (form && form.reset) form.reset();
  });
})();
