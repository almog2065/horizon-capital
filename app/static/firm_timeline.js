/**
 * Firm state timeline charts (Chart.js) — expects window.HORIZON_TIMELINE payload.
 */
(function () {
  const payload = window.HORIZON_TIMELINE;
  if (!payload || !payload.has_data) return;

  const policy = payload.policy || {};
  const metrics = payload.metrics || [];
  const events = payload.events || [];
  const sectors = payload.sector_mix || [];

  const labels = metrics.map((m) => m.label);
  const nav = metrics.map((m) => m.nav_usd);
  const cash = metrics.map((m) => m.cash_pct);
  const invested = metrics.map((m) => m.invested_pct);
  const positions = metrics.map((m) => m.positions_count);

  const chartFont = { family: "'Inter', system-ui, sans-serif", size: 11 };

  const navEl = document.getElementById("chart-nav");
  if (navEl) {
    new Chart(navEl, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "NAV (USD)",
            data: nav,
            borderColor: "#007aff",
            backgroundColor: "rgba(0,122,255,0.08)",
            fill: true,
            tension: 0.25,
            pointRadius: 3,
            yAxisID: "y",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label(ctx) {
                const m = metrics[ctx.dataIndex];
                const parts = [`NAV $${Number(ctx.raw).toLocaleString()}`];
                if (m.posture) parts.push(`posture: ${m.posture}`);
                if (m.source) parts.push(m.source);
                return parts;
              },
            },
          },
        },
        scales: {
          x: { ticks: { font: chartFont, maxRotation: 45 } },
          y: {
            ticks: {
              font: chartFont,
              callback: (v) => "$" + Number(v).toLocaleString(),
            },
          },
        },
      },
    });
  }

  const allocEl = document.getElementById("chart-allocation");
  if (allocEl) {
    new Chart(allocEl, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Invested %",
            data: invested,
            borderColor: "#34c759",
            tension: 0.25,
            pointRadius: 2,
          },
          {
            label: "Cash %",
            data: cash,
            borderColor: "#ff9500",
            tension: 0.25,
            pointRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "bottom", labels: { font: chartFont } },
          annotation: false,
        },
        scales: {
          x: { ticks: { font: chartFont, maxRotation: 45 } },
          y: {
            min: 0,
            max: 100,
            ticks: { font: chartFont, callback: (v) => v + "%" },
          },
        },
      },
      plugins: [
        {
          id: "policyBands",
          beforeDraw(chart) {
            const { ctx, chartArea, scales } = chart;
            if (!chartArea || !scales.y) return;
            const y = scales.y;
            const bands = [
              { pct: policy.target_invested_pct, color: "rgba(52,199,89,0.12)" },
              { pct: policy.cash_target_pct, color: "rgba(255,149,0,0.1)" },
            ];
            bands.forEach((b) => {
              if (b.pct == null) return;
              const py = y.getPixelForValue(b.pct);
              ctx.save();
              ctx.strokeStyle = "rgba(0,0,0,0.15)";
              ctx.setLineDash([4, 4]);
              ctx.beginPath();
              ctx.moveTo(chartArea.left, py);
              ctx.lineTo(chartArea.right, py);
              ctx.stroke();
              ctx.restore();
            });
          },
        },
      ],
    });
  }

  const posEl = document.getElementById("chart-positions");
  if (posEl) {
    new Chart(posEl, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Positions",
            data: positions,
            backgroundColor: "rgba(88,86,214,0.65)",
            borderRadius: 4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { font: chartFont, maxRotation: 45 } },
          y: {
            beginAtZero: true,
            ticks: { font: chartFont, stepSize: 1 },
          },
        },
      },
    });
  }

  const sectorEl = document.getElementById("chart-sectors");
  if (sectorEl && sectors.length) {
    new Chart(sectorEl, {
      type: "doughnut",
      data: {
        labels: sectors.map((s) => s.sector),
        datasets: [
          {
            data: sectors.map((s) => s.pct),
            backgroundColor: [
              "#007aff",
              "#34c759",
              "#ff9500",
              "#af52de",
              "#ff2d55",
              "#5ac8fa",
              "#ffcc00",
              "#8e8e93",
              "#5856d6",
              "#30b0c7",
              "#a2845e",
              "#64d2ff",
            ],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: "right",
            labels: { font: chartFont, boxWidth: 12 },
          },
          tooltip: {
            callbacks: {
              label(ctx) {
                return `${ctx.label}: ${ctx.raw}% NAV`;
              },
            },
          },
        },
      },
    });
  }

  const eventEl = document.getElementById("chart-events");
  if (eventEl && events.length) {
    const kindOrder = { run: 0, trade: 1, hitl: 2 };
    const colors = { run: "#007aff", trade: "#34c759", hitl: "#ff9500" };
    const yFor = (k) => kindOrder[k] ?? 0;

    new Chart(eventEl, {
      type: "scatter",
      data: {
        datasets: ["run", "trade", "hitl"].map((kind) => ({
          label: kind.charAt(0).toUpperCase() + kind.slice(1),
          data: events
            .filter((e) => e.kind === kind)
            .map((e) => ({
              x: e.ts * 1000,
              y: yFor(kind),
              label: e.label,
              detail: e.detail,
            })),
          backgroundColor: colors[kind],
          pointRadius: 6,
          pointHoverRadius: 8,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        plugins: {
          legend: { position: "bottom", labels: { font: chartFont } },
          tooltip: {
            callbacks: {
              title(items) {
                const raw = items[0]?.raw;
                if (!raw || !raw.x) return "";
                return new Date(raw.x).toLocaleString();
              },
              label(ctx) {
                const raw = ctx.raw;
                return [raw.label, raw.detail].filter(Boolean);
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            ticks: {
              font: chartFont,
              maxTicksLimit: 8,
              callback(v) {
                return new Date(v).toLocaleString(undefined, {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                });
              },
            },
          },
          y: {
            min: -0.5,
            max: 2.5,
            ticks: {
              font: chartFont,
              stepSize: 1,
              callback(v) {
                return ["Runs", "Trades", "HITL"][v] || "";
              },
            },
          },
        },
      },
    });
  }

  const listEl = document.getElementById("timeline-event-list");
  if (listEl) {
    const recent = [...events].reverse().slice(0, 24);
    recent.forEach((e) => {
      const li = document.createElement("li");
      li.className = "timeline-event-item timeline-event-" + e.kind;
      const when = new Date(e.ts * 1000).toLocaleString();
      li.innerHTML =
        `<span class="timeline-event-time">${when}</span>` +
        `<span class="timeline-event-kind">${e.kind}</span>` +
        `<strong>${e.label}</strong>` +
        (e.detail ? `<span class="muted"> — ${e.detail}</span>` : "");
      listEl.appendChild(li);
    });
  }
})();
