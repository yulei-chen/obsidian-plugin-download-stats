'use strict';

const DAY = 86400;
const state = { index: null, plugin: null, granularity: 'daily', mode: 'delta', range: 0 };
let chart = null;

const $ = (id) => document.getElementById(id);
const fmt = (n) => (n === null || n === undefined || Number.isNaN(n) ? '—' : Math.round(n).toLocaleString('en-US'));
const dayToDate = (day) => new Date(day * DAY * 1000);

/* ---------- Series math ---------- */

/**
 * Convert cumulative download counts into per-day increments.
 *
 * Two realities have to be handled here. Snapshots can be missing for a day or
 * more (a failed workflow run, or no upstream commit), so an increment spanning
 * N days is spread evenly across them instead of being dumped on the last day,
 * which would show up as a phantom spike. And the cumulative count occasionally
 * drops, because Obsidian prunes inflated numbers and delisted plugins can come
 * back; negative increments are clamped to zero and counted so the UI can say so.
 */
function toDaily(start, totals) {
  const points = [];
  for (let i = 0; i < totals.length; i++) {
    if (totals[i] !== null) points.push([start + i, totals[i]]);
  }

  const days = [];
  const values = [];
  let anomalies = 0;

  for (let k = 1; k < points.length; k++) {
    const [prevDay, prevTotal] = points[k - 1];
    const [day, total] = points[k];
    let diff = total - prevTotal;
    if (diff < 0) {
      anomalies++;
      diff = 0;
    }
    const perDay = diff / (day - prevDay);
    for (let d = prevDay + 1; d <= day; d++) {
      days.push(d);
      values.push(perDay);
    }
  }
  return { days, values, anomalies };
}

/** Weeks start on Monday. Epoch day 4 is 1970-01-05, which was a Monday. */
const weekStart = (day) => 4 + Math.floor((day - 4) / 7) * 7;

function monthStart(day) {
  const date = dayToDate(day);
  return Math.floor(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1) / 1000 / DAY);
}

function bucketSum(days, values, keyOf) {
  const buckets = new Map();
  for (let i = 0; i < days.length; i++) {
    const key = keyOf(days[i]);
    buckets.set(key, (buckets.get(key) || 0) + values[i]);
  }
  const keys = [...buckets.keys()].sort((a, b) => a - b);
  return { days: keys, values: keys.map((k) => buckets.get(k)) };
}

/** Cumulative mode: take the last known total within each bucket. */
function bucketLast(start, totals, keyOf) {
  const buckets = new Map();
  for (let i = 0; i < totals.length; i++) {
    if (totals[i] === null) continue;
    buckets.set(keyOf(start + i), totals[i]);
  }
  const keys = [...buckets.keys()].sort((a, b) => a - b);
  return { days: keys, values: keys.map((k) => buckets.get(k)) };
}

function buildSeries(plugin, granularity, mode, rangeDays) {
  const { start, totals } = plugin;
  const keyOf = granularity === 'weekly' ? weekStart : granularity === 'monthly' ? monthStart : (d) => d;

  let series;
  let anomalies = 0;
  if (mode === 'total') {
    series = granularity === 'daily'
      ? { days: totals.map((_, i) => start + i).filter((_, i) => totals[i] !== null),
          values: totals.filter((v) => v !== null) }
      : bucketLast(start, totals, keyOf);
  } else {
    const daily = toDaily(start, totals);
    anomalies = daily.anomalies;
    series = granularity === 'daily' ? daily : bucketSum(daily.days, daily.values, keyOf);
  }

  if (rangeDays > 0) {
    const cutoff = start + totals.length - 1 - rangeDays;
    const from = series.days.findIndex((d) => d >= cutoff);
    if (from > 0) {
      series = { days: series.days.slice(from), values: series.values.slice(from) };
    }
  }
  return { xs: series.days.map((d) => d * DAY), ys: series.values, anomalies };
}

/** Downloads gained over the last N days, for the summary cards. */
function recentSum(plugin, n) {
  const { days, values } = toDaily(plugin.start, plugin.totals);
  if (!days.length) return null;
  const cutoff = days[days.length - 1] - n + 1;
  let sum = 0;
  for (let i = days.length - 1; i >= 0 && days[i] >= cutoff; i--) sum += values[i];
  return sum;
}

/* ---------- Chart ---------- */

const tooltipPlugin = () => {
  let el;
  return {
    hooks: {
      init: (u) => {
        el = document.createElement('div');
        el.className = 'u-tooltip';
        el.style.display = 'none';
        u.over.appendChild(el);
      },
      setCursor: (u) => {
        const { idx, left, top } = u.cursor;
        if (idx === null || idx === undefined || u.data[1][idx] === null) {
          el.style.display = 'none';
          return;
        }
        const date = new Date(u.data[0][idx] * 1000);
        const label = state.granularity === 'monthly'
          ? date.toISOString().slice(0, 7)
          : date.toISOString().slice(0, 10);
        const prefix = state.granularity === 'weekly' ? 'Week of ' : '';
        el.textContent = `${prefix}${label} · ${fmt(u.data[1][idx])}`;
        el.style.display = '';
        el.style.left = `${Math.min(left + 12, u.over.clientWidth - el.offsetWidth - 4)}px`;
        el.style.top = `${top + 12}px`;
      },
    },
  };
};

function render() {
  const { xs, ys, anomalies } = buildSeries(state.plugin, state.granularity, state.mode, state.range);

  $('notice').hidden = anomalies === 0;
  if (anomalies) {
    const times = anomalies === 1 ? 'once' : `${anomalies} times`;
    $('notice').textContent =
      `The cumulative count dropped ${times} (Obsidian pruning inflated numbers, or the plugin being delisted). ` +
      'Those intervals are counted as zero new downloads.';
  }

  if (chart) chart.destroy();
  const width = $('chart').clientWidth - 16;
  chart = new uPlot({
    width,
    height: 360,
    padding: [12, 12, 0, 0],
    cursor: { y: false, points: { size: 6 } },
    scales: { x: { time: true } },
    axes: [
      { stroke: '#8b93a7', grid: { stroke: '#232833' }, ticks: { stroke: '#232833' } },
      {
        stroke: '#8b93a7',
        grid: { stroke: '#232833' },
        ticks: { stroke: '#232833' },
        size: 62,
        values: (u, ticks) => ticks.map((v) => (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : v)),
      },
    ],
    series: [
      {},
      {
        label: 'Downloads',
        stroke: '#a882ff',
        width: 2,
        fill: 'rgba(168,130,255,0.12)',
        points: { show: xs.length < 60 },
      },
    ],
    plugins: [tooltipPlugin()],
  }, [xs, ys], $('chart'));
}

/* ---------- Views ---------- */

async function showPlugin(id) {
  let plugin;
  try {
    const response = await fetch(`./data/plugins/${encodeURIComponent(id)}.json`);
    if (!response.ok) throw new Error(String(response.status));
    plugin = await response.json();
  } catch {
    $('empty').hidden = false;
    $('detail').hidden = true;
    $('empty').querySelector('p').textContent = `No data found for plugin “${id}”.`;
    return;
  }

  state.plugin = plugin;
  $('empty').hidden = true;
  $('detail').hidden = false;

  $('plugin-name').textContent = plugin.name;
  $('plugin-desc').textContent = plugin.description || '';
  $('plugin-id').textContent = plugin.id;
  $('plugin-author').textContent = plugin.author ? `by ${plugin.author}` : '';
  const repo = $('plugin-repo');
  repo.hidden = !plugin.repo;
  if (plugin.repo) repo.href = `https://github.com/${plugin.repo}`;

  const latest = [...plugin.totals].reverse().find((v) => v !== null);
  const last30 = recentSum(plugin, 30);
  $('stat-total').textContent = fmt(latest);
  $('stat-7').textContent = fmt(recentSum(plugin, 7));
  $('stat-30').textContent = fmt(last30);
  $('stat-avg').textContent = last30 === null ? '—' : fmt(last30 / 30);

  document.title = `${plugin.name} · Obsidian Plugin Downloads`;
  render();
}

function renderSuggestions(matches) {
  const list = $('suggestions');
  list.innerHTML = '';
  if (!matches.length) {
    list.hidden = true;
    return;
  }
  for (const [id, name, author, downloads] of matches) {
    const item = document.createElement('li');
    item.innerHTML =
      `<span class="s-name"></span><span class="s-id"></span><span class="s-dl">${fmt(downloads)}</span>`;
    item.querySelector('.s-name').textContent = name;
    item.querySelector('.s-id').textContent = id;
    item.addEventListener('mousedown', (event) => {
      event.preventDefault();
      select(id);
    });
    list.appendChild(item);
  }
  list.hidden = false;
}

function search(term) {
  const needle = term.trim().toLowerCase();
  if (!needle) return [];
  const starts = [];
  const contains = [];
  for (const row of state.index.plugins) {
    const [id, name, author] = row;
    if (id.toLowerCase().startsWith(needle) || name.toLowerCase().startsWith(needle)) {
      starts.push(row);
    } else if (
      id.toLowerCase().includes(needle) ||
      name.toLowerCase().includes(needle) ||
      author.toLowerCase().includes(needle)
    ) {
      contains.push(row);
    }
    if (starts.length >= 30) break;
  }
  return starts.concat(contains).slice(0, 30);
}

function select(id) {
  $('query').value = '';
  $('suggestions').hidden = true;
  location.hash = encodeURIComponent(id);
}

function bindSegmented(elementId, key) {
  $(elementId).addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    for (const sibling of event.currentTarget.children) sibling.classList.remove('active');
    button.classList.add('active');
    state[key] = key === 'range' ? Number(button.dataset.value) : button.dataset.value;
    if (state.plugin) render();
  });
}

function route() {
  const id = decodeURIComponent(location.hash.replace(/^#\/?/, ''));
  if (id) {
    showPlugin(id);
  } else {
    state.plugin = null;
    $('detail').hidden = true;
    $('empty').hidden = false;
    document.title = 'Obsidian Plugin Download Stats';
  }
}

async function main() {
  state.index = await (await fetch('./data/index.json')).json();
  $('generated').textContent = `Data through ${state.index.lastDate}`;

  const popular = $('popular');
  for (const [id, name] of state.index.plugins.slice(0, 12)) {
    const chip = document.createElement('button');
    chip.textContent = name;
    chip.addEventListener('click', () => select(id));
    popular.appendChild(chip);
  }

  const input = $('query');
  input.addEventListener('input', () => renderSuggestions(search(input.value)));
  input.addEventListener('blur', () => setTimeout(() => ($('suggestions').hidden = true), 120));
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const first = $('suggestions').querySelector('li');
    if (first) first.dispatchEvent(new MouseEvent('mousedown'));
  });

  bindSegmented('granularity', 'granularity');
  bindSegmented('mode', 'mode');
  bindSegmented('range', 'range');

  window.addEventListener('hashchange', route);
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => state.plugin && render(), 150);
  });

  route();
}

if (typeof document !== 'undefined') {
  main();
}

// Exposed so tests/series.test.js can exercise the aggregation logic under Node
if (typeof module !== 'undefined') {
  module.exports = { toDaily, weekStart, monthStart, bucketSum, bucketLast, buildSeries };
}
