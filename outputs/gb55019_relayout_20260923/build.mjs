import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const sourcePath = "C:\\Users\\zwsoft\\Desktop\\GB55019-2021.xlsx";
const outputDir = path.dirname(new URL(import.meta.url).pathname.replace(/^\/(?=[A-Za-z]:)/, ""));
const outputPath = path.join(outputDir, "GB55019-2021_评测表.xlsx");

const source = await SpreadsheetFile.importXlsx(await FileBlob.load(sourcePath));
const sourceSheet = source.worksheets.getItemAt(0);
const sourceRows = sourceSheet.getRange("A1:Z109").values;
const pairs = [
  [13, 14], // N/O: 问题1 / 答案1
  [16, 17], // Q/R: 问题2 / 答案2
  [19, 20], // T/U: 问题3 / 答案3
  [24, 25], // Y/Z: 未命名的问题 / 答案列
];

const textOf = (value) => value == null ? "" : String(value).trim();
const outputRows = [];
const slotCounts = [0, 0, 0, 0];
for (let i = 1; i < sourceRows.length; i += 1) {
  const row = sourceRows[i];
  const fullClause = textOf(row[5]);
  const clauseMatch = fullClause.match(/第\s*([0-9]+(?:\.[0-9]+)+)\s*条/);
  for (let slot = 0; slot < pairs.length; slot += 1) {
    const [questionCol, answerCol] = pairs[slot];
    const question = textOf(row[questionCol]);
    const requirement = textOf(row[answerCol]);
    if (!question) {
      if (requirement) throw new Error(`Source row ${i + 1} has an answer without a question in slot ${slot + 1}`);
      continue;
    }
    if (!clauseMatch) throw new Error(`Source row ${i + 1} has a question but no parseable clause: ${fullClause}`);
    if (!requirement) throw new Error(`Source row ${i + 1} has a question but no requirement in slot ${slot + 1}`);
    outputRows.push([question, `第${clauseMatch[1]}条`, requirement, null, null, null, null, null, null]);
    slotCounts[slot] += 1;
  }
}
if (outputRows.length !== 120 || slotCounts.join(",") !== "45,24,7,44") {
  throw new Error(`Unexpected source counts: total=${outputRows.length}, slots=${slotCounts.join(",")}`);
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("GB55019-2021 评测结果");
sheet.showGridLines = true;
sheet.freezePanes.freezeRows(1);
const lastRow = outputRows.length + 1;
sheet.getRange("A1:I1").values = [[
  "问题", "需要引用的条文", "回答要求", "AI回答", "条文是否引用正确",
  "回答是否符合要求", "tokens", "耗时s", "实际引用条款",
]];
sheet.getRange(`A2:I${lastRow}`).values = outputRows;

const all = sheet.getRange(`A1:I${lastRow}`);
all.format.font = { name: "宋体", size: 11 };
all.format.verticalAlignment = "center";
const header = sheet.getRange("A1:I1");
header.format = {
  fill: "#4472C4",
  font: { name: "宋体", size: 11, bold: true, color: "#FFFFFF" },
  verticalAlignment: "center",
  wrapText: true,
};
header.format.rowHeight = 27;
sheet.getRange(`A2:A${lastRow}`).format.wrapText = true;
sheet.getRange(`B2:B${lastRow}`).format.verticalAlignment = "center";
sheet.getRange(`C2:C${lastRow}`).format.wrapText = true;
sheet.getRange(`D2:D${lastRow}`).format.wrapText = true;
sheet.getRange(`D2:D${lastRow}`).format.verticalAlignment = "top";
sheet.getRange(`E2:F${lastRow}`).format.horizontalAlignment = "center";
sheet.getRange(`I2:I${lastRow}`).format.wrapText = true;
sheet.getRange(`A2:I${lastRow}`).format.rowHeight = 40;

for (const [column, width] of Object.entries({
  A: 28, B: 16, C: 18, D: 90, E: 14, F: 14, G: 9, H: 8, I: 34,
})) {
  sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}

workbook.recalculate();
const inspection = await workbook.inspect({
  kind: "region", sheetId: sheet.name, range: "A1:I6", maxChars: 4000,
});
await fs.writeFile(path.join(outputDir, "verification.json"), JSON.stringify({
  sourceRows: sourceRows.length - 1,
  questionRows: outputRows.length,
  slotCounts,
  first: outputRows[0].slice(0, 3),
  last: outputRows.at(-1).slice(0, 3),
  inspection,
}, null, 2), "utf8");

const preview = await workbook.render({ sheetName: sheet.name, range: "A1:I7", scale: 1, format: "png" });
await fs.writeFile(path.join(outputDir, "preview.png"), new Uint8Array(await preview.arrayBuffer()));
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
console.log(JSON.stringify({ outputPath, questionRows: outputRows.length, slotCounts }));
