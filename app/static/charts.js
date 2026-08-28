/* Charts read their data from <script type="application/json"> blocks the
   templates emit, and their colors from the CSS custom properties -- so a theme
   switch is a re-read, not a second palette. */

(function () {
  const token = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const theme = () => ({
    series1: token("--series-1"),
    series2: token("--series-2"),
    series3: token("--series-3"),
    series5: token("--series-5"),
    surface: token("--surface-1"),
    text: token("--text-primary"),
    secondary: token("--text-secondary"),
    muted: token("--muted"),
    grid: token("--grid"),
    axis: token("--axis"),
  });

  const data = (id) => {
    const el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  };

  const mmss = (seconds) => {
    if (seconds == null) return "-";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  const shortDate = (iso) =>
    new Date(iso + "T00:00:00").toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });

  function baseOptions(t) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false }, // every chart here is a single series
        tooltip: {
          backgroundColor: t.surface,
          titleColor: t.text,
          bodyColor: t.secondary,
          borderColor: t.axis,
          borderWidth: 1,
          padding: 10,
          cornerRadius: 8,
          displayColors: false,
          titleFont: { family: "system-ui, sans-serif", weight: "600" },
          bodyFont: { family: "system-ui, sans-serif" },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: t.axis },
          ticks: {
            color: t.muted,
            font: { family: "system-ui, sans-serif", size: 11 },
            maxRotation: 0,
            autoSkipPadding: 16,
          },
        },
        y: {
          beginAtZero: true,
          grid: { color: t.grid, drawTicks: false }, // solid hairline, never dashed
          border: { display: false },
          ticks: {
            color: t.muted,
            font: { family: "system-ui, sans-serif", size: 11 },
            padding: 8,
          },
        },
      },
    };
  }

  /* Selective direct labels: the biggest week and the current one, nothing else. */
  const labelPeaks = {
    id: "labelPeaks",
    afterDatasetsDraw(chart, _args, opts) {
      const meta = chart.getDatasetMeta(0);
      const values = chart.data.datasets[0].data;
      if (!values.length) return;
      const peak = values.indexOf(Math.max(...values));
      const marked = new Set([peak, values.length - 1]);
      const ctx = chart.ctx;
      ctx.save();
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.fillStyle = opts.color;
      ctx.textAlign = "center";
      marked.forEach((i) => {
        const bar = meta.data[i];
        if (!bar || !values[i]) return;
        const text = opts.format(values[i]);
        // Keep the label inside the plot: the last bar sits against the right
        // edge, and a wide value would otherwise be cut off there.
        const half = ctx.measureText(text).width / 2;
        const area = chart.chartArea;
        const x = Math.min(Math.max(bar.x, area.left + half), area.right - half);
        ctx.fillText(text, x, bar.y - 6);
      });
      ctx.restore();
    },
  };

  function weeklyChart(t) {
    const rows = data("weekly-data");
    const el = document.getElementById("weekly-chart");
    if (!rows || !el) return null;
    return new Chart(el, {
      type: "bar",
      data: {
        labels: rows.map((r) => shortDate(r.week_start)),
        datasets: [
          {
            data: rows.map((r) => r.miles),
            backgroundColor: t.series1,
            borderRadius: 4,           // rounded data-end...
            borderSkipped: "bottom",   // ...anchored to the baseline
            barPercentage: 0.86,       // leaves the 2px surface gap between bars
            categoryPercentage: 0.9,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        plugins: {
          ...baseOptions(t).plugins,
          labelPeaks: { color: t.secondary, format: (v) => `${v} mi` },
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            callbacks: {
              title: (items) => `Week of ${items[0].label}`,
              label: (item) => {
                const row = rows[item.dataIndex];
                const pace = row.avg_pace ? ` · ${mmss(row.avg_pace)}/mi` : "";
                return `${row.miles} mi · ${row.runs} run${row.runs === 1 ? "" : "s"}${pace}`;
              },
            },
          },
        },
        scales: {
          ...baseOptions(t).scales,
          y: {
            ...baseOptions(t).scales.y,
            title: { display: true, text: "miles", color: t.muted,
                     font: { size: 11, family: "system-ui, sans-serif" } },
          },
        },
      },
      plugins: [labelPeaks],
    });
  }

  function loadChart(t) {
    const rows = data("load-data");
    const el = document.getElementById("load-chart");
    if (!rows || !el) return null;
    return new Chart(el, {
      type: "line",
      data: {
        labels: rows.map((r) => r.date),
        datasets: [
          {
            data: rows.map((r) => r.miles),
            borderColor: t.series1,
            backgroundColor: t.series1 + "1f",
            borderWidth: 2,
            fill: true,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: t.series1,
            pointHoverBorderColor: t.surface,
            pointHoverBorderWidth: 2, // 2px surface ring
          },
        ],
      },
      options: {
        ...baseOptions(t),
        scales: {
          ...baseOptions(t).scales,
          x: {
            ...baseOptions(t).scales.x,
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback(value) {
                return shortDate(this.getLabelForValue(value));
              },
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            title: { display: true, text: "miles in prior 7 days", color: t.muted,
                     font: { size: 11, family: "system-ui, sans-serif" } },
          },
        },
        plugins: {
          ...baseOptions(t).plugins,
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            callbacks: {
              title: (items) => shortDate(items[0].label),
              label: (item) => `${item.parsed.y} mi over the prior 7 days`,
            },
          },
        },
      },
    });
  }

  function paceChart(t) {
    const rows = data("pace-data");
    const trend = data("trend-data");
    const el = document.getElementById("pace-chart");
    if (!rows || !el || !rows.length) return null;

    // A straight line needs only its two ends; the category axis places them on
    // the matching dates and draws between.
    const trendSet = trend && {
      label: "Trend",
      type: "line",
      data: [
        { x: trend.start_date, y: trend.start_pace },
        { x: trend.end_date, y: trend.end_pace },
      ],
      borderColor: t.series2,
      borderWidth: 2,
      pointRadius: 0,
      pointHitRadius: 0,
      fill: false,
      order: 0,          // drawn over the dots
    };

    return new Chart(el, {
      type: "scatter",
      data: {
        datasets: [
          ...(trendSet ? [trendSet] : []),
          {
            label: "Each run",
            order: 1,
            data: rows.map((r) => ({
              x: r.date,
              y: r.pace_sec_per_mi,
              distance: r.distance_mi,
              type: r.workout_type,
            })),
            backgroundColor: t.series1,
            borderColor: t.surface, // 2px surface ring keeps overlapping dots legible
            borderWidth: 2,
            pointRadius: 5,
            pointHoverRadius: 7,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        interaction: { mode: "nearest", intersect: false },
        scales: {
          x: {
            ...baseOptions(t).scales.x,
            type: "category",
            labels: [...new Set(rows.map((r) => r.date))].sort(),
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback(value) {
                return shortDate(this.getLabelForValue(value));
              },
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            beginAtZero: false,
            reverse: true, // faster runs sit higher, the way every run log shows it
            title: { display: true, text: "pace per mile — faster is higher",
                     color: t.muted,
                     font: { size: 11, family: "system-ui, sans-serif" } },
            ticks: {
              ...baseOptions(t).scales.y.ticks,
              stepSize: 60, // land ticks on whole minutes per mile
              callback: (value) => mmss(value),
            },
          },
        },
        plugins: {
          ...baseOptions(t).plugins,
          legend: {
            display: !!trendSet,   // two series: never identify by colour alone
            position: "top",
            align: "end",
            labels: {
              boxWidth: 10,
              boxHeight: 10,
              usePointStyle: true,
              color: t.secondary,
              font: { family: "system-ui, sans-serif", size: 12 },
            },
          },
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            filter: (item) => item.datasetIndex !== 0 || !trendSet,
            callbacks: {
              title: (items) => shortDate(items[0].raw.x),
              label: (item) => {
                const p = item.raw;
                if (p.distance === undefined) return null;
                const type = p.type ? ` · ${p.type}` : "";
                return `${mmss(p.y)}/mi · ${p.distance.toFixed(2)} mi${type}`;
              },
            },
          },
        },
      },
    });
  }

  function hrChart(t) {
    const rows = data("hr-data");
    const el = document.getElementById("hr-chart");
    if (!rows || !el || !rows.length) return null;
    const mmssFromSec = (s) =>
      `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
    return new Chart(el, {
      type: "line",
      data: {
        labels: rows.map((r) => r.offset_sec),
        datasets: [
          {
            data: rows.map((r) => r.bpm),
            borderColor: t.series2,
            backgroundColor: t.series2 + "1a",
            borderWidth: 2,
            fill: true,
            tension: 0.2,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: t.series2,
            pointHoverBorderColor: t.surface,
            pointHoverBorderWidth: 2,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        scales: {
          ...baseOptions(t).scales,
          x: {
            ...baseOptions(t).scales.x,
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback(value) {
                return mmssFromSec(this.getLabelForValue(value));
              },
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            beginAtZero: false,
            title: { display: true, text: "bpm", color: t.muted,
                     font: { size: 11, family: "system-ui, sans-serif" } },
          },
        },
        plugins: {
          ...baseOptions(t).plugins,
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            callbacks: {
              title: (items) => `${mmssFromSec(items[0].label)} into the run`,
              label: (item) => `${item.parsed.y} bpm`,
            },
          },
        },
      },
    });
  }

  function vo2Chart(t) {
    const rows = data("vo2-data");
    const trend = data("vo2-trend");
    const el = document.getElementById("vo2-chart");
    if (!rows || !el || !rows.length) return null;

    const trendSet = trend && {
      label: "Trend",
      type: "line",
      data: [
        { x: trend.start_date, y: trend.start_value },
        { x: trend.end_date, y: trend.end_value },
      ],
      borderColor: t.series2,
      borderWidth: 2,
      pointRadius: 0,
      pointHitRadius: 0,
      fill: false,
      order: 0,
    };

    return new Chart(el, {
      type: "scatter",
      data: {
        datasets: [
          ...(trendSet ? [trendSet] : []),
          {
            label: "Daily estimate",
            order: 1,
            data: rows.map((r) => ({ x: r.date, y: r.value })),
            backgroundColor: t.series1,
            borderColor: t.surface,
            borderWidth: 2,
            pointRadius: 4,
            pointHoverRadius: 6,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        interaction: { mode: "nearest", intersect: false },
        scales: {
          x: {
            ...baseOptions(t).scales.x,
            type: "category",
            labels: [...new Set(rows.map((r) => r.date))].sort(),
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback(value) {
                return shortDate(this.getLabelForValue(value));
              },
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            beginAtZero: false,
            title: { display: true, text: "mL/min·kg", color: t.muted,
                     font: { size: 11, family: "system-ui, sans-serif" } },
          },
        },
        plugins: {
          ...baseOptions(t).plugins,
          legend: {
            display: !!trendSet,
            position: "top",
            align: "end",
            labels: {
              boxWidth: 10, boxHeight: 10, usePointStyle: true,
              color: t.secondary,
              font: { family: "system-ui, sans-serif", size: 12 },
            },
          },
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            filter: (item) => item.datasetIndex !== 0 || !trendSet,
            callbacks: {
              title: (items) => shortDate(items[0].raw.x),
              label: (item) => `${item.parsed.y.toFixed(1)} mL/min·kg`,
            },
          },
        },
      },
    });
  }

  function elevationChart(t) {
    const rows = data("elevation-data");
    const el = document.getElementById("elevation-chart");
    if (!rows || !el || !rows.length) return null;
    const mmssFromSec = (s) =>
      `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
    return new Chart(el, {
      type: "line",
      data: {
        labels: rows.map((r) => r.offset_sec),
        datasets: [
          {
            data: rows.map((r) => r.altitude_ft),
            borderColor: t.series3,
            backgroundColor: t.series3 + "1a",
            borderWidth: 2,
            fill: true,
            tension: 0.3,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: t.series3,
            pointHoverBorderColor: t.surface,
            pointHoverBorderWidth: 2,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        scales: {
          ...baseOptions(t).scales,
          x: {
            ...baseOptions(t).scales.x,
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback(value) {
                return mmssFromSec(this.getLabelForValue(value));
              },
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            // A 70ft roll over an 800ft course is invisible from zero.
            beginAtZero: false,
            title: { display: true, text: "ft", color: t.muted,
                     font: { size: 11, family: "system-ui, sans-serif" } },
          },
        },
        plugins: {
          ...baseOptions(t).plugins,
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            callbacks: {
              title: (items) => `${mmssFromSec(items[0].label)} into the run`,
              label: (item) => `${Math.round(item.parsed.y)} ft`,
            },
          },
        },
      },
    });
  }

  function cadenceChart(t) {
    const rows = data("cadence-data");
    const el = document.getElementById("cadence-chart");
    if (!rows || !el || !rows.length) return null;
    const mmssFromSec = (s) =>
      `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
    return new Chart(el, {
      type: "line",
      data: {
        labels: rows.map((r) => r.offset_sec),
        datasets: [
          {
            data: rows.map((r) => r.spm),
            borderColor: t.series5,
            backgroundColor: t.series5 + "1a",
            borderWidth: 2,
            fill: true,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: t.series5,
            pointHoverBorderColor: t.surface,
            pointHoverBorderWidth: 2,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        scales: {
          ...baseOptions(t).scales,
          x: {
            ...baseOptions(t).scales.x,
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback(value) {
                return mmssFromSec(this.getLabelForValue(value));
              },
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            beginAtZero: false,
            title: { display: true, text: "spm", color: t.muted,
                     font: { size: 11, family: "system-ui, sans-serif" } },
          },
        },
        plugins: {
          ...baseOptions(t).plugins,
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            callbacks: {
              title: (items) => `${mmssFromSec(items[0].label)} into the run`,
              label: (item) => `${item.parsed.y} steps per minute`,
            },
          },
        },
      },
    });
  }

  /* Weekly steps and calories: the same bar treatment as weekly mileage, so the
     three read as one family. */
  function weeklyBars(id, t, field, colour, format) {
    const rows = data("weekly-data");
    const el = document.getElementById(id);
    if (!rows || !el) return null;
    const base = baseOptions(t);
    return new Chart(el, {
      type: "bar",
      data: {
        labels: rows.map((r) => shortDate(r.week_start)),
        datasets: [
          {
            data: rows.map((r) => r[field] || 0),
            backgroundColor: colour,
            borderRadius: 4,
            borderSkipped: "bottom",
            barPercentage: 0.86,
            categoryPercentage: 0.9,
          },
        ],
      },
      options: {
        ...base,
        plugins: {
          ...base.plugins,
          labelPeaks: { color: t.secondary, format },
          tooltip: {
            ...base.plugins.tooltip,
            callbacks: {
              title: (items) => `Week of ${items[0].label}`,
              label: (item) => format(item.parsed.y),
            },
          },
        },
        scales: {
          ...base.scales,
          y: {
            ...base.scales.y,
            ticks: {
              ...base.scales.y.ticks,
              callback: (value) =>
                value >= 1000 ? `${Math.round(value / 1000)}k` : value,
            },
          },
        },
      },
      plugins: [labelPeaks],
    });
  }

  let charts = [];
  function render() {
    charts.forEach((c) => c && c.destroy());
    const t = theme();
    charts = [weeklyChart(t), loadChart(t), paceChart(t), hrChart(t), vo2Chart(t), elevationChart(t), cadenceChart(t),
      weeklyBars("steps-chart", t, "steps", t.series3,
                 (v) => `${v.toLocaleString()} steps`),
      weeklyBars("calories-chart", t, "calories", t.series2,
                 (v) => `${v.toLocaleString()} cal`)];
  }

  document.addEventListener("DOMContentLoaded", render);
  document.addEventListener("themechange", render);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (!document.documentElement.dataset.theme) render();
  });
})();
