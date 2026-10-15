// Drag the opening card sideways to Like (right) or Pass (left). A drag past the
// threshold submits the same form button a tap would, so there is one code path.
(function () {
  var THRESHOLD = 0.3; // fraction of the card width
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var drag = null;

  function card(el) {
    return el.closest && el.closest("[data-swipe-card]");
  }

  function button(c, liked) {
    var form = c.parentNode.querySelector("form");
    return form && form.querySelector('button[name="liked"][value="' + liked + '"]');
  }

  function move(c, dx) {
    if (reduce.matches) return;
    c.style.transform = "translateX(" + dx + "px) rotate(" + dx / 20 + "deg)";
  }

  function reset(c) {
    c.style.transition = reduce.matches ? "" : "transform 0.15s";
    c.style.transform = "";
  }

  document.addEventListener("pointerdown", function (e) {
    var c = card(e.target);
    if (!c || (e.pointerType === "mouse" && e.button !== 0)) return;
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

  function end(e) {
    if (!drag || e.pointerId !== drag.id) return;
    var d = drag;
    drag = null;
    var far = d.live && Math.abs(d.dx) > d.c.offsetWidth * THRESHOLD;
    var b = far && button(d.c, d.dx > 0 ? "1" : "0");
    if (b) b.click(); // a real click, so htmx sees which button submitted
    else reset(d.c);
  }

  document.addEventListener("pointerup", end);
  document.addEventListener("pointercancel", function (e) {
    if (drag && e.pointerId === drag.id) {
      reset(drag.c);
      drag = null;
    }
  });
})();
