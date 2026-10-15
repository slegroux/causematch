// Chat page: keep the newest message in view, empty the box once a message is sent, and
// start over from the server's copy whenever the live connection was lost or refused,
// so a message sent while the phone was offline is never missing and a send that the
// server refused is never silently dropped.
(function () {
  var list = document.getElementById("messages");
  if (!list) return;

  var stick = true; // follow new messages unless the reader scrolled up into history
  function toEnd() {
    if (stick) list.scrollTop = list.scrollHeight;
  }
  list.scrollTop = list.scrollHeight;
  list.addEventListener("scroll", function () {
    stick = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  });

  // A page that reloads straight into another refusal must not loop.
  function reload() {
    var KEY = "chat-reloaded-at";
    try {
      var last = Number(sessionStorage.getItem(KEY) || 0);
      if (Date.now() - last < 5000) return;
      sessionStorage.setItem(KEY, String(Date.now()));
    } catch (e) {}
    location.reload();
  }

  var opens = 0;
  document.body.addEventListener("htmx:wsOpen", function () {
    opens += 1;
    document.body.dataset.ws = "open";
    if (opens > 1) reload(); // reconnected: history may have moved on
  });
  document.body.addEventListener("htmx:wsClose", function (e) {
    delete document.body.dataset.ws;
    var code = e.detail && e.detail.event && e.detail.event.code;
    if (code === 1008) reload(); // refused: the page now shows why
  });
  document.body.addEventListener("htmx:wsAfterMessage", toEnd);
  document.body.addEventListener("htmx:oobAfterSwap", toEnd);
  document.body.addEventListener("htmx:wsAfterSend", function (e) {
    var form = e.target;
    if (form && form.reset) form.reset();
  });
})();
