// "Add to Home Screen" hint: shown on iPhone Safari (not other iOS browsers or in-app browsers), in a tab (not the installed app),
// to anyone who has not dismissed it. The page renders the hint hidden; this only
// decides whether to show it.
(function () {
  var KEY = "a2hs-dismissed";
  var hint = document.getElementById("a2hs");
  if (!hint) return;

  var ua = navigator.userAgent;
  var iphone = /iPhone/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|Instagram|FBAN|FBAV|GSA\/|Ddg\/|Line\/|Snapchat|TikTok|MicroMessenger|; wv\)/.test(ua);
  var installed =
    navigator.standalone === true ||
    (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
  if (!iphone || installed) return;

  try {
    if (localStorage.getItem(KEY)) return;
  } catch (e) {}

  hint.hidden = false;
  var close = document.getElementById("a2hs-close");
  if (close)
    close.addEventListener("click", function () {
      hint.hidden = true;
      try {
        localStorage.setItem(KEY, "1");
      } catch (e) {}
    });
})();
