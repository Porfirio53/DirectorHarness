import { Fragment } from "react";
import ReactMarkdown from "react-markdown";

function transformGfmTables(content: string) {
  const lines = content.split(/\r?\n/);
  const result: string[] = [];
  let index = 0;

  while (index < lines.length) {
    const header = lines[index];
    const separator = lines[index + 1];

    if (!header || !separator || !header.includes("|") || !isTableSeparator(separator)) {
      result.push(header);
      index += 1;
      continue;
    }

    const headerCells = splitTableRow(header);
    const separatorCells = splitTableRow(separator);

    if (!headerCells.length || headerCells.length !== separatorCells.length) {
      result.push(header);
      index += 1;
      continue;
    }

    const alignments = separatorCells.map(getAlignment);
    const bodyRows: string[] = [];
    let bodyIndex = index + 2;

    while (bodyIndex < lines.length) {
      const row = lines[bodyIndex];
      if (!row || !row.includes("|")) break;
      const cells = splitTableRow(row);
      if (cells.length !== headerCells.length) break;
      bodyRows.push(toHtmlTableRow(cells, "td", alignments));
      bodyIndex += 1;
    }

    if (!bodyRows.length) {
      result.push(header);
      index += 1;
      continue;
    }

    result.push("<table>");
    result.push("<thead>");
    result.push(toHtmlTableRow(headerCells, "th", alignments));
    result.push("</thead>");
    result.push("<tbody>");
    result.push(...bodyRows);
    result.push("</tbody>");
    result.push("</table>");
    index = bodyIndex;
  }

  return result.join("\n");
}

function escapeHeadingId(value: string) {
  return value
    .toLowerCase()
    .replace(/[`*_[\](){}]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

function splitTableRow(row: string) {
  return row
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isTableSeparator(row: string) {
  const cells = splitTableRow(row);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function getAlignment(cell: string) {
  if (cell.startsWith(":") && cell.endsWith(":")) return "center";
  if (cell.endsWith(":")) return "right";
  if (cell.startsWith(":")) return "left";
  return "left";
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function toHtmlTableRow(cells: string[], tagName: "th" | "td", alignments: string[]) {
  const columns = cells
    .map((cell, index) => `<${tagName} align=\"${alignments[index] || "left"}\">${escapeHtml(cell)}</${tagName}>`)
    .join("");
  return `<tr>${columns}</tr>`;
}

interface MarkdownPreviewProps {
  content: string;
  compact?: boolean;
}

function stripHtml(value: string) {
  return value.replace(/<[^>]+>/g, "").replace(/&nbsp;/g, " ").trim();
}

function renderHtmlTable(tableMarkup: string, key: number) {
  const rows = [...tableMarkup.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)].map((match) => match[1]);
  const parsedRows = rows.map((row) => [...row.matchAll(/<(th|td)\b[^>]*>([\s\S]*?)<\/\1>/gi)].map((cell) => ({ tag: cell[1].toLowerCase(), value: stripHtml(cell[2]) })));
  if (!parsedRows.length || !parsedRows[0].length) return null;
  const hasHeader = parsedRows[0].some((cell) => cell.tag === "th");
  const header = hasHeader ? parsedRows[0] : null;
  const body = hasHeader ? parsedRows.slice(1) : parsedRows;
  return (
    <div className="markdown-table-wrap" key={`table-${key}`}>
      <table>
        {header ? <thead><tr>{header.map((cell, index) => <th key={index}>{cell.value}</th>)}</tr></thead> : null}
        <tbody>{body.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell.value}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function renderContent(content: string) {
  const fragments = content.split(/(<table\b[^>]*>[\s\S]*?<\/table>)/gi);
  return fragments.map((fragment, index) => {
    if (/^<table\b/i.test(fragment)) return renderHtmlTable(fragment, index);
    if (!fragment) return null;
    return <ReactMarkdown key={`markdown-${index}`} components={{
      h1: ({ children, ...props }) => <h1 id={escapeHeadingId(String(children))} {...props}>{children}</h1>,
      h2: ({ children, ...props }) => <h2 id={escapeHeadingId(String(children))} {...props}>{children}</h2>,
      h3: ({ children, ...props }) => <h3 id={escapeHeadingId(String(children))} {...props}>{children}</h3>,
    }}>{transformGfmTables(fragment)}</ReactMarkdown>;
  });
}

export function MarkdownPreview({ content, compact = false }: MarkdownPreviewProps) {
  return (
    <div className={`markdown-preview ${compact ? "markdown-preview--compact" : ""}`}>
      <Fragment>{renderContent(content)}</Fragment>
    </div>
  );
}
