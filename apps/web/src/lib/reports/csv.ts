/**
 * CSV and "copy as table" for the reports page, built in the browser (no export endpoint).
 *
 * CSV is RFC 4180 with CRLF line ends and a UTF-8 byte order mark, so spreadsheet programs
 * detect the encoding. Two dialects: `en` (comma, decimal point) and `de` (semicolon, decimal
 * comma, as German Excel expects). Dates stay ISO 8601 (`YYYY-MM-DD`) in both. The table for the
 * clipboard is tab separated: spreadsheets and most editors paste it as columns.
 */

export type Cell = string | number | null | undefined;

export interface CsvDialect {
  separator: ',' | ';';
  decimal: '.' | ',';
}

export const CSV_DIALECTS: Record<'en' | 'de', CsvDialect> = {
  en: { separator: ',', decimal: '.' },
  de: { separator: ';', decimal: ',' },
};

/** The byte order mark that tells spreadsheet programs the file is UTF-8. */
export const BOM = '\u{FEFF}';

/** A number with at most `digits` decimals, without grouping, in the dialect's decimal mark. */
export function csvNumber(value: number, dialect: CsvDialect, digits = 2): string {
  if (!Number.isFinite(value)) return '';
  const factor = 10 ** digits;
  const rounded = Math.round(value * factor) / factor;
  const text = String(Object.is(rounded, -0) ? 0 : rounded);
  return dialect.decimal === ',' ? text.replace('.', ',') : text;
}

function cellText(cell: Cell, dialect: CsvDialect): string {
  if (cell === null || cell === undefined) return '';
  return typeof cell === 'number' ? csvNumber(cell, dialect) : cell;
}

/** One CSV field: quoted when it holds the separator, a quote or a line break. */
export function csvField(text: string, separator: string): string {
  return text.includes(separator) || /["\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The whole CSV document (with the BOM unless `bom` is false). */
export function toCsv(rows: readonly (readonly Cell[])[], dialect: CsvDialect, bom = true): string {
  const body = rows
    .map((row) =>
      row
        .map((cell) => csvField(cellText(cell, dialect), dialect.separator))
        .join(dialect.separator),
    )
    .join('\r\n');
  return (bom ? BOM : '') + body + '\r\n';
}

/** Tab separated rows for the clipboard (tabs and line breaks inside cells become spaces). */
export function toTsv(rows: readonly (readonly Cell[])[], dialect: CsvDialect): string {
  return rows
    .map((row) => row.map((cell) => cellText(cell, dialect).replace(/[\t\r\n]+/g, ' ')).join('\t'))
    .join('\n');
}

/** The download name: `mowdeck-report-<scope>-<from>_<to>.csv` with a file-safe scope. */
export function csvFileName(scope: string, from: string, to: string): string {
  const safe =
    scope
      .normalize('NFKD')
      .replace(/\p{M}/gu, '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || 'all';
  return `mowdeck-report-${safe}-${from}_${to}.csv`;
}
