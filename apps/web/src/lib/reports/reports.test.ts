import { describe, expect, it } from 'vitest';

import {
  customRangeIssue,
  dailySeries,
  elapsedHours,
  faultsByCode,
  isoDay,
  periodRange,
  rangeDays,
  REPORT_COLUMNS,
  reportRows,
  reportTable,
  reportTotals,
  type ReportDay,
} from './aggregate';
import { BOM, CSV_DIALECTS, csvField, csvFileName, csvNumber, toCsv, toTsv } from './csv';

const BYTE_ORDER_MARK = String.fromCharCode(0xfeff);
const NOW = new Date('2026-10-10T06:00:00Z');

const day = (date: string, extra: Partial<ReportDay> = {}): ReportDay => ({
  day: date,
  blade_hours: 1,
  mowing_hours: 1.5,
  distance_m: 1000,
  jobs: 1,
  online_hours: 6,
  errors: {},
  ...extra,
});

describe('csv', () => {
  it('starts with a byte order mark and ends lines with CRLF', () => {
    expect(BOM).toBe(BYTE_ORDER_MARK);
    const csv = toCsv(
      [
        ['a', 'b'],
        [1, 2.5],
      ],
      CSV_DIALECTS.en,
    );
    expect(csv).toBe(`${BYTE_ORDER_MARK}a,b\r\n1,2.5\r\n`);
    expect(toCsv([['x']], CSV_DIALECTS.en, false)).toBe('x\r\n');
  });

  it('uses semicolons and decimal commas in the German dialect', () => {
    expect(toCsv([[1.256, 'Wiese; hinten', null]], CSV_DIALECTS.de, false)).toBe(
      '1,26;"Wiese; hinten";\r\n',
    );
  });

  it('quotes separators, quotes and line breaks', () => {
    expect(csvField('plain', ',')).toBe('plain');
    expect(csvField('a,b', ',')).toBe('"a,b"');
    expect(csvField('a,b', ';')).toBe('a,b');
    expect(csvField('say "hi"', ';')).toBe('"say ""hi"""');
    expect(csvField('two\nlines', ';')).toBe('"two\nlines"');
  });

  it('formats numbers without grouping', () => {
    expect(csvNumber(1234.5678, CSV_DIALECTS.en)).toBe('1234.57');
    expect(csvNumber(1234.5678, CSV_DIALECTS.de, 1)).toBe('1234,6');
    expect(csvNumber(-0.001, CSV_DIALECTS.en)).toBe('0');
    expect(csvNumber(Number.NaN, CSV_DIALECTS.en)).toBe('');
  });

  it('builds a tab separated table for the clipboard', () => {
    expect(
      toTsv(
        [
          ['a\tb', 1.5],
          ['x\ny', null],
        ],
        CSV_DIALECTS.de,
      ),
    ).toBe('a b\t1,5\nx y\t');
  });

  it('names the file after scope and range', () => {
    expect(csvFileName('Halle Süd / West', '2026-10-01', '2026-10-07')).toBe(
      'mowdeck-report-halle-sud-west-2026-10-01_2026-10-07.csv',
    );
    expect(csvFileName('***', '2026-10-01', '2026-10-01')).toBe(
      'mowdeck-report-all-2026-10-01_2026-10-01.csv',
    );
  });
});

describe('periods', () => {
  it('ranges end today in UTC days', () => {
    expect(isoDay(NOW)).toBe('2026-10-10');
    expect(periodRange('today', NOW)).toEqual({ from: '2026-10-10', to: '2026-10-10' });
    expect(periodRange('7d', NOW)).toEqual({ from: '2026-10-04', to: '2026-10-10' });
    expect(periodRange('30d', NOW)).toEqual({ from: '2026-09-11', to: '2026-10-10' });
    expect(periodRange('custom', NOW, { from: '2026-10-05', to: '2026-10-01' })).toEqual({
      from: '2026-10-01',
      to: '2026-10-05',
    });
    expect(periodRange('custom', NOW)).toEqual({ from: '2026-10-10', to: '2026-10-10' });
    expect(rangeDays(periodRange('30d', NOW))).toBe(30);
  });

  it('counts the elapsed hours of the range', () => {
    expect(elapsedHours(periodRange('today', NOW), NOW)).toBe(6);
    expect(elapsedHours({ from: '2026-10-08', to: '2026-10-09' }, NOW)).toBe(48);
    expect(elapsedHours({ from: '2026-10-11', to: '2026-10-12' }, NOW)).toBe(0);
  });

  it('checks a custom range', () => {
    expect(customRangeIssue({ from: '', to: '2026-10-01' }, NOW)).toBe('missing');
    expect(customRangeIssue({ from: '2026-10-05', to: '2026-10-01' }, NOW)).toBe('order');
    expect(customRangeIssue({ from: '2025-01-01', to: '2026-10-01' }, NOW)).toBe('too_long');
    expect(customRangeIssue({ from: '2026-11-01', to: '2026-11-02' }, NOW)).toBe('future');
    expect(customRangeIssue({ from: '2026-10-01', to: '2026-10-05' }, NOW)).toBeNull();
  });
});

describe('aggregation', () => {
  const mowers = [
    { id: 'b', name: 'Yuka', site_id: 's2' },
    { id: 'a', name: 'Luba', site_id: 's1' },
    { id: 'c', name: 'Neu', site_id: 's1' },
  ];
  const days = new Map<string, ReportDay[]>([
    [
      'a',
      [
        day('2026-10-09', { errors: { E12: 1 } }),
        day('2026-10-10', { errors: { E12: 1, E7: 2 }, online_hours: 4 }),
      ],
    ],
    ['b', [day('2026-10-10', { online_hours: 40 })]],
  ]);

  it('sums days per mower, keeps mowers without data and caps availability', () => {
    const rows = reportRows(mowers, days, 30);
    expect(rows.map((r) => r.name)).toEqual(['Luba', 'Neu', 'Yuka']);
    const [luba, neu, yuka] = rows;
    expect(luba).toMatchObject({
      mowingHours: 3,
      bladeHours: 2,
      distanceM: 2000,
      jobs: 2,
      faults: 4,
      errors: { E12: 2, E7: 2 },
      onlineHours: 10,
      days: 2,
    });
    expect(luba?.availability).toBeCloseTo(1 / 3, 9);
    expect(neu).toMatchObject({ jobs: 0, faults: 0, availability: 0, days: 0 });
    expect(yuka?.availability).toBe(1);
    expect(reportRows(mowers, days, 0)[0]?.availability).toBe(0);
  });

  it('totals the rows and lists faults by code', () => {
    const rows = reportRows(mowers, days, 30);
    const totals = reportTotals(rows);
    expect(totals).toMatchObject({ mowers: 3, mowingHours: 4.5, jobs: 3, faults: 4 });
    expect(totals.availability).toBeCloseTo((1 / 3 + 0 + 1) / 3, 9);
    expect(reportTotals([]).availability).toBe(0);
    expect(faultsByCode(rows)).toEqual([
      { code: 'E12', count: 2 },
      { code: 'E7', count: 2 },
    ]);
  });

  it('sums a metric per day over the chosen mowers', () => {
    const series = dailySeries(
      { from: '2026-10-08', to: '2026-10-10' },
      days,
      ['a', 'b'],
      (d) => d.jobs,
    );
    expect(series).toEqual({ days: ['2026-10-08', '2026-10-09', '2026-10-10'], values: [0, 1, 2] });
    expect(
      dailySeries({ from: '2026-10-10', to: '2026-10-10' }, days, ['c'], (d) => d.jobs),
    ).toEqual({
      days: ['2026-10-10'],
      values: [0],
    });
  });

  it('builds the export table with a stable header and a totals line', () => {
    const rows = reportRows(mowers, days, 30);
    const table = reportTable(
      rows,
      reportTotals(rows),
      { from: '2026-10-09', to: '2026-10-10' },
      (id) => (id === 's1' ? 'Hof' : 'Halle'),
      'Summe',
      CSV_DIALECTS.de,
    );
    expect(table[0]).toEqual([...REPORT_COLUMNS]);
    expect(table[1]).toEqual([
      'a',
      'Luba',
      'Hof',
      '2026-10-09',
      '2026-10-10',
      '3',
      '2',
      '2000',
      2,
      4,
      '33,33',
      'E12=2 E7=2',
    ]);
    expect(table.at(-1)?.slice(0, 2)).toEqual(['total', 'Summe']);
    expect(table).toHaveLength(5);
    const csv = toCsv(table, CSV_DIALECTS.de);
    expect(csv.split('\r\n')[0]).toBe(
      `${BYTE_ORDER_MARK}mower_id;mower;site;from;to;mowing_hours;blade_hours;distance_m;jobs;faults;availability_percent;faults_by_code`,
    );
  });
});
