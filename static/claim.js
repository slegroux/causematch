// The claim page (app/routes/claim.py): typing a name searches IRS records through
// /org/claim/search, and picking a result fills the EIN and name. The search field stays
// hidden without this script, and typing the EIN works either way.
(function () {
  var box = document.getElementById("org-search");
  if (!box) return;
  var form = box.form;
  var list = document.getElementById("org-results");
  var note = document.getElementById("org-search-note");
  var timer = null;
  var asked = 0;
  document.getElementById("org-search-field").hidden = false;

  function say(text) {
    note.textContent = text;
  }

  function clear() {
    list.textContent = "";
    list.hidden = true;
  }

  function pick(hit) {
    form.elements.ein.value = hit.ein;
    form.elements.org_name.value = hit.name;
    box.value = hit.name;
    clear();
    say("Picked " + hit.name + ", EIN " + hit.ein + ".");
  }

  function line(text, cls) {
    var span = document.createElement("span");
    span.className = cls;
    span.textContent = text; // text, never HTML
    return span;
  }

  function show(hits) {
    clear();
    hits.forEach(function (hit) {
      var b = document.createElement("button");
      b.type = "button";
      b.className =
        "block w-full min-h-11 text-left rounded-xl border border-line bg-white px-3 py-2 hover:border-brand";
      var place = [hit.city, hit.state].filter(Boolean).join(", ");
      if (hit.outside_la) place += (place ? " · " : "") + "outside LA County";
      b.appendChild(line(hit.name, "block font-semibold text-ink"));
      b.appendChild(
        line((place ? place + " · " : "") + "EIN " + hit.ein, "block text-sm text-ink-muted")
      );
      b.addEventListener("click", function () {
        pick(hit);
      });
      var li = document.createElement("li");
      li.appendChild(b);
      list.appendChild(li);
    });
    list.hidden = !hits.length;
  }

  function search() {
    var q = box.value.trim();
    var mine = ++asked;
    if (q.length < 3) {
      clear();
      say("");
      return;
    }
    fetch("/org/claim/search?q=" + encodeURIComponent(q), { credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : Promise.reject(r.status);
      })
      .then(function (d) {
        if (mine !== asked) return; // a newer search is on its way
        show(d.results);
        say(d.results.length ? "" : "Nothing found. Try another word, or type the EIN below.");
      })
      .catch(function () {
        if (mine !== asked) return;
        clear();
        say("Search isn't working right now. Type the EIN below instead.");
      });
  }

  box.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(search, 400);
  });
  box.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault(); // Enter searches; it doesn't send the request yet
      clearTimeout(timer);
      search();
    }
  });
})();
