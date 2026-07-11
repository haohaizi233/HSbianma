import fs from "node:fs/promises";
import path from "node:path";

import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [payloadPath, outputPath, previewDir] = process.argv.slice(2);
if (!payloadPath || !outputPath) {
  throw new Error("usage: build_hs_workbook.mjs <payload.json> <output.xlsx> [preview-dir]");
}

const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
const { columns, chapters, records, sheet_order: sheetOrder } = payload;
if (!Array.isArray(columns) || !Array.isArray(chapters) || !Array.isArray(records)) {
  throw new Error("invalid workbook payload");
}
if (sheetOrder?.[0] !== "总表") {
  throw new Error("the first worksheet must be 总表");
}

function columnName(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function typedValue(column, value) {
  if (value === "" || value === null || value === undefined) return null;
  if (column.kind === "percent" && typeof value === "number") return value / 100;
  if (column.kind === "date" && /^\d{4}-\d{2}-\d{2}$/.test(String(value))) {
    return new Date(`${value}T00:00:00Z`);
  }
  if (column.kind === "datetime" && !Number.isNaN(Date.parse(String(value)))) {
    return new Date(String(value));
  }
  return value;
}

function dataMatrix(rows) {
  return [
    columns.map((column) => column.header),
    ...rows.map((record) => columns.map((column) => typedValue(column, record[column.key]))),
  ];
}

function setColumnWidth(sheet, columnKey, rowCount, width) {
  const index = columns.findIndex((column) => column.key === columnKey);
  if (index >= 0) {
    sheet.getRangeByIndexes(0, index, rowCount, 1).format.columnWidth = width;
  }
}

function styleSheet(sheet, rows, sheetIndex) {
  const matrix = dataMatrix(rows);
  const rowCount = matrix.length;
  const columnCount = columns.length;
  const lastColumn = columnName(columnCount - 1);
  const used = sheet.getRangeByIndexes(0, 0, rowCount, columnCount);
  used.values = matrix;
  used.format.columnWidth = 14;
  used.format.rowHeight = 20;

  const header = sheet.getRangeByIndexes(0, 0, 1, columnCount);
  header.format = {
    fill: "#17324D",
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    rowHeight: 30,
    borders: { preset: "outside", style: "thin", color: "#17324D" },
  };

  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(3);

  if (rows.length > 0) {
    const table = sheet.tables.add(`A1:${lastColumn}${rowCount}`, true, `HSData${sheetIndex}`);
    table.style = "TableStyleMedium2";
    table.showFilterButton = true;

    const percentageColumns = columns
      .map((column, index) => [column, index])
      .filter(([column]) => column.kind === "percent");
    for (const [, index] of percentageColumns) {
      sheet.getRangeByIndexes(1, index, rows.length, 1).format.numberFormat = "0.00%";
    }

    for (let index = 0; index < columns.length; index += 1) {
      const column = columns[index];
      const body = sheet.getRangeByIndexes(1, index, rows.length, 1);
      if (["identifier", "text", "json"].includes(column.kind)) body.format.numberFormat = "@";
      if (column.kind === "date") body.format.numberFormat = "yyyy-mm-dd";
      if (column.kind === "datetime") body.format.numberFormat = "yyyy-mm-dd hh:mm:ss";
    }

    const statusIndex = columns.findIndex((column) => column.key === "code_status");
    if (statusIndex >= 0) {
      sheet.getRangeByIndexes(1, statusIndex, rows.length, 1).conditionalFormats.add(
        "containsText",
        {
          text: "过期",
          format: { fill: "#FDE7E7", font: { color: "#A61B1B", bold: true } },
        },
      );
    }
  }

  for (const column of columns) {
    setColumnWidth(sheet, column.key, rowCount, column.width);
  }
}

const workbook = Workbook.create();
const sheetData = [
  ["总表", records],
  ...chapters.map((chapter) => [chapter, records.filter((row) => row.hs_code?.startsWith(chapter))]),
];

for (let index = 0; index < sheetData.length; index += 1) {
  const [name, rows] = sheetData[index];
  const sheet = workbook.worksheets.add(name);
  styleSheet(sheet, rows, index + 1);
}

const keyRows = Math.min(records.length + 1, 12);
const keyCols = Math.min(columns.length, 12);
const keyLastColumn = columnName(keyCols - 1);
const inspection = await workbook.inspect({
  kind: "table",
  range: `总表!A1:${keyLastColumn}${keyRows}`,
  include: "values,formulas",
  tableMaxRows: keyRows,
  tableMaxCols: keyCols,
  maxChars: 4000,
});
const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});

if (previewDir) {
  await fs.mkdir(previewDir, { recursive: true });
  for (let index = 0; index < sheetData.length; index += 1) {
    const [name, rows] = sheetData[index];
    const previewRows = Math.min(rows.length + 1, 12);
    const previewCols = Math.min(columns.length, 12);
    const previewLastColumn = columnName(previewCols - 1);
    const blob = await workbook.render({
      sheetName: name,
      range: `A1:${previewLastColumn}${previewRows}`,
      scale: 1,
      format: "png",
    });
    const safeName = name.replace(/[^\p{L}\p{N}._-]+/gu, "_");
    await fs.writeFile(
      path.join(previewDir, `${String(index + 1).padStart(3, "0")}-${safeName}.png`),
      new Uint8Array(await blob.arrayBuffer()),
    );
  }
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({
  sheets: sheetData.map(([name]) => name),
  records: records.length,
  inspection: inspection.ndjson,
  formulaErrors: formulaErrors.ndjson,
}));
