// dct web viewer — client glue.
// v1: intentionally empty; the SSE refresh wiring lives inline in base.html.
// Task 8 appends the uPlot chart initialization below.

// --- Velocity charts (uPlot) -------------------------------------------------
function dctInitVelocity() {
  var mount = document.getElementById("velocity-chart");
  var dataEl = document.getElementById("velocity-data");
  if (!mount || !dataEl || typeof uPlot === "undefined") return;
  if (mount.dataset.rendered === "1") return;

  var v;
  try {
    v = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }

  // Merge created/resolved per-week series onto a shared, sorted week axis.
  var byWeek = {};
  (v.created_per_week || []).forEach(function (r) {
    (byWeek[r.week] = byWeek[r.week] || {}).created = r.count;
  });
  (v.resolved_per_week || []).forEach(function (r) {
    (byWeek[r.week] = byWeek[r.week] || {}).resolved = r.count;
  });

  var weeks = Object.keys(byWeek).sort();
  if (!weeks.length) {
    mount.dataset.rendered = "1";
    return;
  }

  var xs = weeks.map(function (_, i) { return i; });
  var ysCreated = weeks.map(function (w) { return byWeek[w].created || 0; });
  var ysResolved = weeks.map(function (w) { return byWeek[w].resolved || 0; });

  var opts = {
    width: mount.clientWidth || 720,
    height: 260,
    scales: { x: { time: false } },
    axes: [
      {
        values: function (u, vals) {
          return vals.map(function (i) { return weeks[i] || ""; });
        }
      },
      {}
    ],
    series: [
      {},
      { label: "created", stroke: "#d97706" },
      { label: "resolved", stroke: "#2563eb" }
    ]
  };

  new uPlot(opts, [xs, ysCreated, ysResolved], mount);
  mount.dataset.rendered = "1";
}

document.addEventListener("DOMContentLoaded", dctInitVelocity);
document.addEventListener("htmx:afterSwap", dctInitVelocity);
