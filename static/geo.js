// "Use my location" on /me/preferences (app/routes/prefs.py): the device's position,
// rounded here to 2 decimals (about 1 km), goes into the place's hidden fields; the server
// rounds it again. Nothing leaves the page until the form is saved.
(function () {
  if (!("geolocation" in navigator)) return;

  function field(place, name) {
    return document.getElementById(place + "_" + name);
  }

  document.querySelectorAll("[data-locate-box]").forEach(function (box) {
    box.hidden = false;
  });

  document.addEventListener("click", function (e) {
    var button = e.target.closest && e.target.closest("[data-locate]");
    if (!button) return;
    var place = button.getAttribute("data-locate");
    var status = field(place, "status");
    status.textContent = "Finding you…";
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        field(place, "lat").value = pos.coords.latitude.toFixed(2);
        field(place, "lon").value = pos.coords.longitude.toFixed(2);
        field(place, "where").value = "My location";
        status.textContent = "Got it: we keep an area about 1 km around you. Save to use it.";
      },
      function () {
        status.textContent = "Couldn't get your location. Type a neighborhood or ZIP instead.";
      },
      { enableHighAccuracy: false, timeout: 15000, maximumAge: 600000 }
    );
  });

  // Typing a place by hand drops a location found earlier.
  document.addEventListener("input", function (e) {
    var place = e.target.getAttribute && e.target.getAttribute("data-where");
    if (!place || e.target.value === "My location") return;
    field(place, "lat").value = "";
    field(place, "lon").value = "";
  });
})();
