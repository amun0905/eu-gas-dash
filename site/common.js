// Shared helpers for every page: data loading, chart defaults, tables, source credits.
(() => {
  const cfg = window.GASDASH_CONFIG || {};
  const params = new URLSearchParams(location.search);
  const BASE = (params.get("data") || cfg.DATA_BASE || "").replace(/\/$/, "");

  // ---- energy <-> volume units -------------------------------------------------------------
  // Continental data (ENTSOG, GIE) is published in energy; 10.55 kWh/m3 is the common European
  // reference. GB data (National Gas) was converted from mcm at 10.97, so dividing by 10.97
  // gives back National Gas's own published volumes.
  const KWH_PER_M3 = { eu: 10.55, gb: 10.97 };
  let VOL = false;
  try { VOL = localStorage.getItem("gasdash-unit") === "volume"; } catch (e) { /* storage blocked */ }
  const c = (v, src = "eu") => (v == null || !VOL ? v : v / KWH_PER_M3[src]);        // GWh/d->mcm/d, TWh->bcm, GWh->mcm
  const ca = (arr, src = "eu") => (VOL ? arr.map((v) => c(v, src)) : arr);
  const uF = () => (VOL ? "mcm/d" : "GWh/d"), uS = () => (VOL ? "bcm" : "TWh"), uG = () => (VOL ? "mcm" : "GWh");
  const dp = (d) => (VOL ? d + 1 : d);                                                // one more decimal in volume
  const nf = (v, d = 0, src = "eu") => num(c(v, src), dp(d));
  const sf = (v, d = 0, src = "eu") => signed(c(v, src), dp(d));
  function applyUnits() {
    document.querySelectorAll("[data-u]").forEach((el) => { el.textContent = { F: uF(), S: uS(), G: uG() }[el.dataset.u]; });
  }
  let rerender = null;

  const PAGES = [["index.html", "Storage"], ["lng.html", "LNG"], ["flows.html", "Flows"],
                 ["events.html", "Events"], ["auctions.html", "Auctions"], ["weather.html", "Weather"], ["sources.html", "Sources"]];

  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  // Categorical slots in fixed order (validated palette); colour follows the entity, never its rank.
  const SLOTS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
  const slot = (i) => css(SLOTS[i % SLOTS.length]);

  const fmtDay = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "2-digit", month: "short" });
  const fmtDayY = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "2-digit", month: "short", year: "numeric" });
  const fmtDT = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const num = (v, d = 1) => v == null || Number.isNaN(v) ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const signed = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), d);
  const esc = (s) => s == null ? "" : String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function header(active) {
    const nav = PAGES.map(([href, name]) => `<a href="${href}${location.search}"${href === active ? ' aria-current="page"' : ""}>${name}</a>`).join("");
    const el = document.querySelector("header");
    el.insertAdjacentHTML("afterbegin", `<h1>European Gas</h1><nav class="tabs" aria-label="Pages">${nav}</nav>`);
    if (!["auctions.html", "sources.html", "weather.html"].includes(active)) {
      el.insertAdjacentHTML("beforeend", `<div class="seg" role="group" aria-label="Units" id="unit-toggle" title="Volumes use ${KWH_PER_M3.eu} kWh/m³ (GB: National Gas published volumes)">
        <button data-v="energy" aria-pressed="${!VOL}">GWh</button><button data-v="volume" aria-pressed="${VOL}">mcm</button></div>`);
      document.getElementById("unit-toggle").addEventListener("click", (e) => {
        const b = e.target.closest("button"); if (!b) return;
        VOL = b.dataset.v === "volume";
        try { localStorage.setItem("gasdash-unit", b.dataset.v); } catch (err) { /* ignore */ }
        document.querySelectorAll("#unit-toggle button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        applyUnits();
        if (rerender) rerender();
      });
    }
    applyUnits();
  }

  async function load(name) {
    const url = `${BASE}/web/${name}.json`;
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  function banner(msg) {
    const b = document.getElementById("banner");
    b.textContent = msg; b.style.display = msg ? "block" : "none";
  }

  // Credits under the page, plus "generated at". GIE requires a visible credit.
  function footer(data, extra = "") {
    const srcs = (data.sources || []).map((s) => `<a href="${s.url}" target="_blank" rel="noopener">${esc(s.text)}</a>`).join(" · ");
    const gen = data.generated_at ? `Built ${fmtDT.format(new Date(data.generated_at))} (UK time).` : "";
    const power = cfg.POWER_DASH_URL ? ` · <a href="${cfg.POWER_DASH_URL}">GB power dashboard</a>` : "";
    document.getElementById("footer").innerHTML =
      `<p>${srcs}</p><p>${gen} ${extra} Public data, shown as published; check the original sources before relying on it.` +
      ` <a href="sources.html${location.search}">All sources &amp; data freshness</a>${power}</p>`;
  }

  function base(opts = {}) {
    const ink2 = css("--text-secondary"), grid = css("--grid"), muted = css("--text-muted");
    return {
      animation: false,
      textStyle: { fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif", color: ink2 },
      grid: { left: 56, right: 18, top: opts.legend === false ? 14 : 40, bottom: 28 },
      legend: opts.legend === false ? { show: false } : { top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 12, textStyle: { color: ink2, fontSize: 12 } },
      tooltip: { trigger: "axis", backgroundColor: css("--surface-1"), borderColor: css("--border"), textStyle: { color: css("--text-primary"), fontSize: 12 },
                 axisPointer: { type: "line", lineStyle: { color: muted, width: 1 } }, confine: true },
      xAxis: { type: "time", axisLine: { lineStyle: { color: grid } }, axisTick: { show: false },
               axisLabel: { color: muted, fontSize: 11, hideOverlap: true, formatter: (v) => fmtDay.format(v) }, splitLine: { show: false } },
      yAxis: { type: "value", axisLine: { show: false }, axisTick: { show: false }, scale: !!opts.scale,
               axisLabel: { color: muted, fontSize: 11 }, splitLine: { lineStyle: { color: grid } } },
    };
  }

  const line = (name, data, color, extra = {}) => ({ name, type: "line", data, showSymbol: false, symbolSize: 8, connectNulls: false,
    lineStyle: { width: 2, color }, itemStyle: { color }, emphasis: { focus: "series" }, ...extra });
  const area = (name, data, color, stack = "s") => ({ name, type: "line", stack, data, showSymbol: false, symbolSize: 8,
    lineStyle: { width: 0 }, areaStyle: { color, opacity: 0.9 }, itemStyle: { color }, emphasis: { focus: "series" } });
  const pairs = (t, arr) => t.map((x, i) => [x, arr[i] == null ? null : arr[i]]);

  function axisTooltip(unit, d = 1) {
    return (ps) => {
      if (!ps.length) return "";
      const t = ps[0].axisValue;
      const rows = ps.filter((p) => p.value && p.value[1] != null)
        .map((p) => `<div style="display:flex;gap:10px;justify-content:space-between"><span>${p.marker}${esc(p.seriesName)}</span><b>${num(p.value[1], typeof unit === "function" ? dp(d) : d)}</b></div>`).join("");
      return `<div style="min-width:180px"><div style="margin-bottom:4px;color:${css("--text-secondary")}">Gas day ${fmtDayY.format(t)}</div>${rows}<div style="color:${css("--text-muted")};margin-top:4px">${typeof unit === "function" ? unit() : unit}</div></div>`;
    };
  }

  const charts = [];
  // Legends wrap on narrow cards; push the plot down by the number of legend rows so they never overlap.
  function fitLegend(el, o) {
    if (!o.legend || o.legend.show === false || !o.series) return;
    const names = (o.legend.data || o.series.map((s) => s.name)).filter((n) => n && n !== "band-lo");
    const w = Math.max(200, el.clientWidth - 10);
    let rows = 1, x = 0;
    names.forEach((n) => { const iw = 22 + String(n).length * 6.6 + 12; if (x + iw > w) { rows++; x = 0; } x += iw; });
    o.grid = { ...(o.grid || {}), top: 16 + rows * 22 };
  }
  function chart(id) {
    const el = document.getElementById(id);
    const c = echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: "svg" });
    if (!charts.includes(c)) {
      charts.push(c);
      const set = c.setOption.bind(c);
      c.setOption = (o, ...rest) => { fitLegend(el, o); return set(o, ...rest); };
    }
    return c;
  }
  window.addEventListener("resize", () => charts.forEach((c) => c.resize()));

  // Table toggle on cards: [data-toggle-table] switches chart <-> table (table built by fillTable).
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-toggle-table]");
    if (!b) return;
    const card = b.closest(".card");
    card.classList.toggle("show-table");
    b.textContent = card.classList.contains("show-table") ? "Chart" : "Table";
  });

  function table(cols, rows) {
    const th = cols.map((c) => `<th class="${c.cls || ""}">${esc(c.label)}</th>`).join("");
    const tr = rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.cls || ""}">${c.html ? c.html(r) : esc(c.fmt ? c.fmt(r[c.key], r) : r[c.key])}</td>`).join("")}</tr>`).join("");
    return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
  }

  function seriesTable(t, named, d = 1) {
    // named: [{name, data:[...]}] aligned with t; newest first
    const cols = [{ label: "Gas day", key: "t", fmt: (v) => fmtDayY.format(v) }, ...named.map((s, i) => ({ label: s.name, key: "c" + i, fmt: (v) => num(v, d) }))];
    const rows = t.map((x, j) => Object.fromEntries([["t", x], ...named.map((s, i) => ["c" + i, s.data[j]])])).reverse();
    return table(cols, rows.filter((r) => named.some((_, i) => r["c" + i] != null)));
  }

  function fillTable(cardSel, html) {
    const w = document.querySelector(`${cardSel} .table-wrap`);
    if (w) w.innerHTML = html;
  }

  function seg(id, onChange) {
    const g = document.getElementById(id);
    g.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      g.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      onChange(b.dataset.v);
    });
    return () => g.querySelector('[aria-pressed="true"]')?.dataset.v;
  }

  async function boot(active, name, render) {
    header(active);
    let last = null;
    rerender = () => { if (last) { applyUnits(); render(last); } };
    const run = async () => {
      try {
        const data = await load(name);
        last = data;
        banner("");
        render(data);
        applyUnits();
        footer(data, VOL ? `Volumes at ${KWH_PER_M3.eu} kWh/m³ (GB: as published by National Gas).` : "");
      } catch (err) {
        console.error(err);
        banner(`Could not load data (${err.message}). The pipeline may not have run yet.`);
      }
    };
    await run();
    if (cfg.REFRESH_MINUTES) setInterval(run, cfg.REFRESH_MINUTES * 60e3);
  }

  window.GD = { c, ca, uF, uS, uG, nf, sf, dp, isVol: () => VOL, KWH_PER_M3, BASE, css, slot, num, signed, esc, fmtDay, fmtDayY, fmtDT, base, line, area, pairs, axisTooltip, chart,
                table, seriesTable, fillTable, seg, boot, load, banner, footer };
})();
