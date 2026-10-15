// Drag the opening card sideways to Like (right) or Pass (left). A drag past the
// threshold clicks the same form button a tap would, so there is one code path.
(function () {
  var THRESHOLD = 0.3; // fraction of the card width
  var SETTLE_MS = 400; // taps right after a swap are the tail of a double tap
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var drag = null;
  var busy = false; // a swipe request is in flight
  var quietUntil = 0;

  function card(el) {
    return el.closest && el.closest("[data-swipe-card]");
  }

  function button(c, liked) {
    var form = c.parentNode.querySelector("form");
    return form && form.querySelector('button[name="liked"][value="' + liked + '"]');
  }

  function move(c, dx) {
    if (reduce.matches) {
      // No motion: a coloured outline says which way the release will go.
      c.style.outline = Math.abs(dx) > c.offsetWidth * THRESHOLD
        ? "4px solid " + (dx > 0 ? "#16a34a" : "#dc2626") : "";
      return;
    }
    c.style.transform = "translateX(" + dx + "px) rotate(" + dx / 20 + "deg)";
  }

  function reset(c) {
    c.style.outline = "";
    c.style.transition = reduce.matches ? "" : "transform 0.15s";
    c.style.transform = "";
  }

  function ready() {
    return !busy && Date.now() >= quietUntil;
  }

  document.addEventListener("pointerdown", function (e) {
    var c = card(e.target);
    if (!c || !ready() || (e.pointerType === "mouse" && e.button !== 0)) return;
    drag = { c: c, id: e.pointerId, x: e.clientX, y: e.clientY, dx: 0, live: false };
    c.style.transition = "";
  });

  document.addEventListener("pointermove", function (e) {
    if (!drag || e.pointerId !== drag.id) return;
    drag.dx = e.clientX - drag.x;
    var dy = e.clientY - drag.y;
    if (!drag.live && Math.abs(drag.dx) > 8 && Math.abs(drag.dx) > Math.abs(dy)) {
      drag.live = true;
      try { drag.c.setPointerCapture(e.pointerId); } catch (_) {}
    }
    if (drag.live) move(drag.c, drag.dx);
  });

  document.addEventListener("pointerup", function (e) {
    if (!drag || e.pointerId !== drag.id) return;
    var d = drag;
    drag = null;
    var far = d.live && Math.abs(d.dx) > d.c.offsetWidth * THRESHOLD;
    var b = far && ready() && button(d.c, d.dx > 0 ? "1" : "0");
    if (b) b.click(); // a real click, so htmx sees which button submitted
    else reset(d.c);
  });

  document.addEventListener("pointercancel", function (e) {
    if (drag && e.pointerId === drag.id) {
      reset(drag.c);
      drag = null;
    }
  });

  // Ignore taps that land on a card just swapped in (the second tap of a double tap).
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("#deck-area button") && !ready()) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);

  document.body.addEventListener("htmx:beforeRequest", function (e) {
    if (e.detail.elt.closest("#deck-area")) busy = true;
  });

  document.body.addEventListener("htmx:afterRequest", function (e) {
    busy = false;
    if (e.detail.successful) {
      quietUntil = Date.now() + SETTLE_MS;
    } else {
      // Failed or never sent: put the card back and say so.
      var c = document.querySelector("[data-swipe-card]");
      if (c) reset(c);
      var msg = document.getElementById("swipe-error");
      if (msg) msg.hidden = false;
    }
  });
})();
