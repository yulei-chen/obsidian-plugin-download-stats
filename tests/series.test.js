/**
 * Covers the three parts of the series math most likely to be wrong: spreading
 * increments across gaps, clamping drops in the cumulative count, and bucket
 * alignment for weeks and months.
 *
 * Run with: node --test tests/*.test.js
 */
const test = require('node:test');
const assert = require('node:assert');
const { toDaily, weekStart, monthStart, buildSeries } = require('../site/app.js');

test('consecutive snapshots are a plain difference', () => {
  const { days, values, anomalies } = toDaily(100, [10, 15, 22, 30]);
  assert.deepStrictEqual(days, [101, 102, 103]);
  assert.deepStrictEqual(values, [5, 7, 8]);
  assert.strictEqual(anomalies, 0);
});

test('missing days are spread evenly, not dumped on the last day', () => {
  // No snapshot on days 101 and 102; by day 103 the total has grown by 30
  const { days, values } = toDaily(100, [10, null, null, 40]);
  assert.deepStrictEqual(days, [101, 102, 103]);
  assert.deepStrictEqual(values, [10, 10, 10]);
});

test('a drop in the cumulative count is clamped to zero and counted', () => {
  const { values, anomalies } = toDaily(100, [50, 30, 35]);
  assert.deepStrictEqual(values, [0, 5]);
  assert.strictEqual(anomalies, 1);
});

test('a single data point yields no increments', () => {
  const { days, values } = toDaily(100, [42]);
  assert.deepStrictEqual(days, []);
  assert.deepStrictEqual(values, []);
});

test('weeks align to Monday', () => {
  // Epoch day 20690 is 2026-08-25, a Tuesday; its Monday is 2026-08-24 = 20689
  assert.strictEqual(weekStart(20690), 20689);
  assert.strictEqual(new Date(20689 * 86400000).getUTCDay(), 1);
  // Days within one week must land in the same bucket
  assert.strictEqual(weekStart(20689), weekStart(20695));
  assert.notStrictEqual(weekStart(20695), weekStart(20696));
});

test('months align to the first of the month', () => {
  const start = monthStart(20690);
  assert.strictEqual(new Date(start * 86400000).toISOString().slice(0, 10), '2026-08-01');
});

test('weekly buckets sum the daily increments', () => {
  // Eight consecutive days starting Monday 2026-08-24
  const plugin = { start: 20689, totals: [0, 1, 2, 3, 4, 5, 6, 7, 100] };
  const { xs, ys } = buildSeries(plugin, 'weekly', 'delta', 0);
  assert.strictEqual(xs.length, 2);
  // Week one is Tuesday to Sunday at +1 a day; week two is +1 then +93
  assert.deepStrictEqual(ys, [6, 94]);
});

test('cumulative mode takes the last known value in each bucket', () => {
  // 20689 is a Monday, so the first week covers the first 7 points (up to 70)
  const plugin = { start: 20689, totals: [10, 20, 30, 40, 50, 60, 70, 80, 999] };
  const { ys } = buildSeries(plugin, 'weekly', 'total', 0);
  assert.deepStrictEqual(ys, [70, 999]);
});

test('the range filter keeps only the trailing N days', () => {
  const totals = Array.from({ length: 200 }, (_, i) => i * 10);
  const { xs } = buildSeries({ start: 20500, totals }, 'daily', 'delta', 30);
  assert.strictEqual(xs.length, 31);
});
