// The opening form (app/routes/org.py): a ZIP code fills an empty Neighborhood, which the
// coordinator sees and can change before posting. The server fills it the same way if it
// is still empty when posted (app/geo.py zip_neighborhood).
(function () {
  document.addEventListener("input", function (e) {
    if (e.target.name !== "zip" || !e.target.form) return;
    var hood = e.target.form.querySelector("[name=neighborhood]");
    var zip = e.target.value.trim();
    if (!hood || hood.value.trim() || !/^[0-9]{5}$/.test(zip)) return;
    fetch("/org/zip?zip=" + zip, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.neighborhood && !hood.value.trim()) hood.value = d.neighborhood;
      })
      .catch(function () {});
  });
})();
