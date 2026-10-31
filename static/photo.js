// Photo uploads (forms marked data-photo-form). On choosing a picture, show it in the
// form's data-photo-preview image; with data-shrink, redraw it on a canvas no larger than
// that many pixels and upload the JPEG instead of the original, so a 12 MB phone photo
// goes up as a few hundred KB. The server re-encodes whatever arrives. If the browser
// can't read the picture, the form posts the original as it would without this script.
// A form with data-auto sends the picture as soon as it is shrunk and shown.
(function () {
  var TOO_LARGE = "That photo is too large. Choose one under 6 MB.";
  var UNREADABLE = "We couldn't read that picture. Choose a JPEG, PNG or WebP photo.";
  var OFFLINE = "Couldn't upload. Check your connection and try again.";

  function shrink(file, max) {
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bmp) {
      var scale = Math.min(1, max / Math.max(bmp.width, bmp.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * scale);
      canvas.height = Math.round(bmp.height * scale);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      bmp.close();
      return new Promise(function (resolve) {
        canvas.toBlob(resolve, "image/jpeg", 0.85);
      });
    });
  }

  function setUp(form) {
    var input = form.querySelector('input[type="file"]');
    var card = form.closest("[data-photo-card]") || form.parentNode;
    var preview = card.querySelector("[data-photo-preview]");
    var current = card.querySelector("[data-photo-current]");
    var status = form.querySelector("[data-photo-status]");
    var max = parseInt(form.getAttribute("data-shrink"), 10);
    var blob = null;
    var url = null;

    function say(text) {
      status.textContent = text;
      status.hidden = !text;
    }

    function show(b) {
      if (!preview) return;
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(b);
      preview.src = url;
      preview.hidden = false;
      if (current) current.classList.add("hidden"); // a class: it may be display: grid
    }

    input.addEventListener("change", function () {
      blob = null;
      say("");
      var file = input.files[0];
      if (!file) return;
      if (!max) return show(file);
      shrink(file, max).then(function (b) {
        if (input.files[0] !== file || !b) return;
        blob = b;
        show(b);
        if (form.hasAttribute("data-auto")) form.requestSubmit(); // one tap, no Save
      }, function () {
        blob = null; // not decodable here: the original goes up, and the server decides
      });
    });

    form.addEventListener("submit", function (e) {
      if (!blob) return;
      e.preventDefault();
      var body = new FormData();
      body.append(input.name, blob, "photo.jpg");
      var buttons = form.querySelectorAll("button");
      buttons.forEach(function (b) { b.disabled = true; });
      fetch(form.action, { method: "POST", body: body, credentials: "same-origin" })
        .then(function (r) {
          if (r.ok && r.redirected) return window.location.assign(r.url);
          say(r.status === 413 ? TOO_LARGE : UNREADABLE);
        }, function () {
          say(OFFLINE);
        })
        .finally(function () {
          buttons.forEach(function (b) { b.disabled = false; });
        });
    });
  }

  document.querySelectorAll("form[data-photo-form]").forEach(setUp);
})();
