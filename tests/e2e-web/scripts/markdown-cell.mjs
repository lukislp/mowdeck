/** Text for one cell of a Markdown table: pipes, backslashes and backticks are escaped, line breaks become spaces. */
export function markdownCell(text) {
  return String(text)
    .replaceAll('\\', '\\\\')
    .replaceAll('|', '\\|')
    .replaceAll('`', '\\`')
    .replace(/\r?\n/g, ' ');
}
