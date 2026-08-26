/* Live flight map: OSM tiles via Leaflet, trail + aircraft from /track. */
(function () {
  var el = document.getElementById("map");
  if (!el || typeof L === "undefined") return;

  var flightId = el.dataset.flightId;
  var map = L.map(el, { scrollWheelZoom: false, worldCopyJump: true });
  // Leaflet must have a center/zoom BEFORE any layer is added, otherwise those
  // layers project against an undefined view and render as a degenerate "M0 0"
  // path. fitBounds() later replaces this placeholder view.
  map.setView([30, 0], 2);
  var statusEl = document.getElementById("map-status");

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 11,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);

  var layers = L.layerGroup().addTo(map);
  var hasFittedOnce = false;

  function airportMarker(point, code, label) {
    return L.circleMarker([point.lat, point.lon], {
      radius: 6, color: "#4aa3ff", fillColor: "#4aa3ff", fillOpacity: 0.9, weight: 2,
    }).bindTooltip(code + (label ? " — " + label : ""), { direction: "top" });
  }

  function aircraftMarker(fix) {
    var icon = L.divIcon({
      className: "plane-icon",
      html: '<div class="plane" style="transform: rotate(' +
            ((fix.track_deg || 0) - 45) + 'deg)">✈</div>',
      iconSize: [26, 26],
      iconAnchor: [13, 13],
    });
    var bits = [];
    if (fix.altitude_ft) bits.push(fix.altitude_ft.toLocaleString() + " ft");
    if (fix.ground_speed_kt) bits.push(fix.ground_speed_kt + " kt");
    return L.marker([fix.lat, fix.lon], { icon: icon })
      .bindTooltip(bits.join(" · ") || "position", { direction: "top" });
  }

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = "map-status" + (kind ? " map-status-" + kind : "");
  }

  function render(data) {
    layers.clearLayers();
    var bounds = [];

    var dep = data.departure, arr = data.arrival;
    var depOk = dep && dep.lat != null && dep.lon != null;
    var arrOk = arr && arr.lat != null && arr.lon != null;

    if (depOk) { airportMarker(dep, dep.iata, dep.name).addTo(layers); bounds.push([dep.lat, dep.lon]); }
    if (arrOk) { airportMarker(arr, arr.iata, arr.name).addTo(layers); bounds.push([arr.lat, arr.lon]); }

    // Planned route: dashed great-circle-ish line between the airports.
    if (depOk && arrOk) {
      L.polyline([[dep.lat, dep.lon], [arr.lat, arr.lon]], {
        color: "#8b98a5", weight: 1.5, dashArray: "6 6", opacity: 0.7,
      }).addTo(layers);
    }

    // Flown trail.
    var trail = data.trail || [];
    if (trail.length > 1) {
      L.polyline(trail.map(function (p) { return [p.lat, p.lon]; }), {
        color: "#4aa3ff", weight: 3, opacity: 0.9,
      }).addTo(layers);
    }
    trail.forEach(function (p) { bounds.push([p.lat, p.lon]); });

    if (data.latest) aircraftMarker(data.latest).addTo(layers);

    if (bounds.length && !hasFittedOnce) {
      map.fitBounds(bounds, { padding: [30, 30], maxZoom: 7 });
      hasFittedOnce = true;
    }
    // The container can be laid out after Leaflet initialised (fonts, images);
    // recomputing the size keeps tiles and vectors aligned.
    map.invalidateSize();

    // Say plainly why there is no aircraft, rather than showing an empty map.
    if (data.latest) {
      var when = data.last_position_at ? new Date(data.last_position_at) : null;
      var ago = when ? Math.round((Date.now() - when.getTime()) / 1000) : null;
      setStatus(
        "Live via " + (data.position_source || "adsb.lol") +
        (ago != null ? " · updated " + ago + "s ago" : "") +
        (data.callsign_derived ? " · callsign " + data.callsign + " (derived, unconfirmed)" : ""),
        data.callsign_derived ? "warn" : "ok"
      );
    } else if (!data.airborne) {
      setStatus("Not airborne — live position starts once the flight departs.", null);
    } else if (data.position_error) {
      setStatus(data.position_error, "warn");
    } else {
      setStatus("Airborne, waiting for the first ADS-B fix…", null);
    }
  }

  function load() {
    fetch("/api/flights/" + flightId + "/track", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(render)
      .catch(function (err) { setStatus("Could not load position data (" + err + ").", "warn"); });
  }

  load();
  setInterval(load, 30000);
})();
