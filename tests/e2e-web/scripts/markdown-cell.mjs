// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/** Text for one cell of a Markdown table: pipes, backslashes and backticks are escaped, line breaks become spaces. */
export function markdownCell(text) {
  return String(text)
    .replaceAll('\\', '\\\\')
    .replaceAll('|', '\\|')
    .replaceAll('`', '\\`')
    .replace(/\r?\n/g, ' ');
}
