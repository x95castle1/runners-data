/* The route on a real map.

   Tiles come from OpenStreetMap, which is the one thing in this app that talks
   to the outside world: opening a run tells that server roughly where you ran.
   The route line itself is vector data drawn locally, so it still renders if
   the tiles never arrive. */

(function () {
  const el = document.getElementById("route-map");
  const raw = document.getElementById("route-data");
  if (!el || !raw || typeof L === "undefined") return;

  let points;
  try {
    points = JSON.parse(raw.textContent);
  } catch (e) {
    return;
  }
  if (!points || points.length < 2) return;

  const token = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const map = L.map(el, {
    scrollWheelZoom: false, // don't hijack the page scroll
    zoomControl: true,
    attributionControl: true,
    // Integer zoom steps double each level, so snapping down can leave a route
    // filling a quarter of the frame. Fractional zoom fits it properly.
    zoomSnap: 0,
    zoomDelta: 0.5,
  });

  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  const line = L.polyline(points, {
    color: token("--series-1"),
    weight: 4,
    opacity: 0.9,
    lineJoin: "round",
    lineCap: "round",
  }).addTo(map);

  const endpoint = (position, color) =>
    L.circleMarker(position, {
      radius: 6,
      color: token("--surface-1"), // a surface ring, as the charts use
      weight: 2,
      fillColor: color,
      fillOpacity: 1,
    }).addTo(map);

  endpoint(points[0], token("--series-3")).bindPopup("Start");
  endpoint(points[points.length - 1], token("--series-2")).bindPopup("Finish");

  map.fitBounds(line.getBounds(), { padding: [20, 20], maxZoom: 17 });

  // Scroll-zoom only once the map has been clicked, so the page stays scrollable.
  map.on("click", () => map.scrollWheelZoom.enable());
  map.on("mouseout", () => map.scrollWheelZoom.disable());
})();
