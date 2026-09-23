import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const outputDir = path.dirname(new URL(import.meta.url).pathname.replace(/^\/(?=[A-Za-z]:)/, ""));
const source = await fs.readFile(path.join(outputDir, "results.jsonl"), "utf8");
const records = source.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
const rows = [...new Map(records.map((item) => [item.row, item])).values()].sort((a, b) => a.row - b.row);
if (rows.length !== 81) throw new Error("Expected 81 unique benchmark rows");

const completed = rows.filter((x) => x.status === "completed");
const hit = completed.filter((x) => x.target_in_references).length;
const oldHit = completed.filter((x) => x.old_ref_ok === "是").length;
const improved = completed.filter((x) => x.old_ref_ok === "否" && x.target_in_references);
const regressed = completed.filter((x) => x.old_ref_ok === "是" && !x.target_in_references);
const maxTurns = rows.filter((x) => String(x.error || "").includes("max_turns")).length;
const balance = rows.filter((x) => String(x.error || "").includes("Insufficient Balance")).length;
const audit = new Map([
  [15, ["错误", "开头称40/100㎡机房不计入高度，后文又正确算出40%>25%并称应计入；结论自相矛盾。", "GB55031-2022 3.2.6"]],
  [26, ["错误", "称GB55031未规定双车道宽度；实际4.3.6明确不应小于6.0m。", "GB55031-2022 4.3.6"]],
  [27, ["错误", "称8m距离没有对应高度；地下车库/地下室污染性排风口朝向人员活动场所时，距离<10m要求口底≥2.5m。需先确认排风口场景。", "GB55031-2022 4.5.1"]],
  [28, ["错误", "同时称1m轮椅坡道必须分段，又称1:20时可单段；现行通用规范规定每段提升≤750mm。", "GB55019-2021 2.3.1；GB55031-2022 5.2.1"]],
  [30, ["错误", "开头称260mm可以，正文和结尾却称主入口踏步宽应≥300mm、260mm不合规。", "GB55031-2022 5.2.2"]],
  [31, ["错误", "将公共建筑台阶踏步宽写成0.8m并解释成平台尺寸；GB50352正式文本为0.3m，法规库该处疑有录入错误。", "GB50352-2019 6.7.1；GB55031-2022 5.2.2"]],
  [41, ["错误", "称楼梯间门距踏步边缘无统一数值；正对公共楼梯梯段的门应≥0.60m。", "GB55031-2022 5.3.6"]],
  [55, ["错误", "称高层旅馆电梯无统一硬性台数；高层公共建筑电梯不应少于2台。", "GB55031-2022 5.4.2"]],
  [58, ["错误", "先称厨房楼上可布卫生间；厨房食品加工/贮存区属严格卫生要求房间，直接上层不得设公共厕所。", "GB55031-2022 5.6.2；GB50352-2019 6.6.1"]],
  [60, ["错误", "称0.9×1.4m只有内开门+蹲便才可；外开门坐便/蹲便也满足尺寸下限。", "GB55031-2022 5.6.4"]],
  [62, ["错误", "核心1.30/1.10m正确，但附加称GB50352无更严格数值；隔间对面洗手盆、外开门净距为1.50m。", "GB50352-2019 6.6.5"]],
  [72, ["错误", "未给出所问高度；向公共走道开启的窗扇底面距走道地面不应小于2.00m。", "GB55031-2022 6.5.4"]],
  [75, ["错误", "开头称窗台1m多数情况仍需设防，表格和结尾又称通常可不设；结论自相矛盾。", "GB55031-2022 6.5.6"]],
  [78, ["错误", "结尾称六层及以下住宅阳台可做1.05m；通用规范对阳台临空栏杆要求≥1.10m。", "GB55031-2022 6.6.1"]],
  [37, ["待复核", "称每股按0.70m时更早触发三股人流扶手要求，推理方向不清，需结合适用规范核对。", "GB55031-2022 5.3.2、5.3.4；GB50352-2019 6.8.3"]],
  [39, ["待复核", "结论1.1m平台不合规正确，但又说1.1m小于梯段净宽1.1m，存在表述矛盾。", "GB55031-2022 5.3.5"]],
]);
const answerErrors = rows.filter((x) => audit.get(x.row)?.[0] === "错误");
const answerReview = rows.filter((x) => audit.get(x.row)?.[0] === "待复核");
const missing = rows.filter((x) => !x.target_in_references);

const wb = Workbook.create();
const summary = wb.worksheets.add("汇总");
const detail = wb.worksheets.add("逐题结果");
summary.showGridLines = false;
detail.showGridLines = false;
summary.tabColor = "#1F4E78";

summary.getRange("A2").values = [["GB55031 题库复测：描述更新后"]];
summary.getRange("A2").format.font = { name: "Arial", size: 14, bold: true, color: "#243247" };
summary.getRange("A4:B12").values = [
  ["题目总数", 81],
  ["成功完成", completed.length],
  ["成功题目中命中目标条文", hit],
  ["本次命中率（仅成功题目）", completed.length ? hit / completed.length : null],
  ["旧结果命中数（相同成功题目）", oldHit],
  ["相同题目旧命中率", completed.length ? oldHit / completed.length : null],
  ["由未命中变为命中", improved.length],
  ["由命中变为未命中", regressed.length],
  ["无法完成：轮数超限 / 模型余额不足", `${maxTurns} / ${balance}`],
];
summary.getRange("A4:A12").format.font = { name: "Arial", size: 10, color: "#243247" };
summary.getRange("B4:B12").format.font = { name: "Arial", size: 10, bold: true, color: "#243247" };
summary.getRange("B7").setNumberFormat("0.0%");
summary.getRange("B9").setNumberFormat("0.0%");
summary.getRange("A4:B4").format.borders = { bottom: { style: "thin", color: "#C9D4E0" } };
summary.getRange("A14").values = [["解读"]];
summary.getRange("A14").format.font = { name: "Arial", size: 11, bold: true, color: "#243247" };
summary.getRange("A15").values = [["本次与旧结果是两次独立模型运行，命中差异不能单独归因于描述更新。"]];
summary.getRange("A16").values = [[`本次成功完成 ${completed.length} 题；${balance} 题余额不足、${maxTurns} 题轮数超限，未完成题不计入命中率。`]];
summary.getRange("A17").values = [["命中目标条文以系统实际引用列表为准；不等同于回答内容完全正确。"]];
summary.getRange("A18").values = [["Excel 第 57 行连续两次达到 8 轮上限，补跑时单独提高至 12 轮；其余题为 8 轮。"]];
summary.getRange("A15:A18").format.font = { name: "Arial", size: 10, color: "#5A6573" };
summary.getRange("E4:F8").values = [["快速核查", "题数"], ["目标条文未命中", missing.length], ["发现答案错误", answerErrors.length], ["答案待复核", answerReview.length], ["未命中且答案错误", missing.filter((x) => audit.get(x.row)?.[0] === "错误").length]];
summary.getRange("E4:F4").format = { fill: "#1F4E78", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" } };
summary.getRange("E5:F8").format.font = { name: "Arial", size: 10, color: "#243247" };
summary.getRange("E1:E10").format.columnWidth = 25;
summary.getRange("F1:F10").format.columnWidth = 12;
summary.getRange("E10").values = [["初审标记；未发现错误不等于全面正确。"]];
summary.getRange("E10").format.font = { name: "Arial", size: 10, color: "#5A6573" };
summary.getRange("A19:C19").values = [["变化", "Excel 行", "问题"]];
summary.getRange("A19:C19").format = { fill: "#1F4E78", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" } };
const changes = [...improved.map((x) => ["改善", x.row, x.question]), ...regressed.map((x) => ["退步", x.row, x.question])];
if (changes.length) summary.getRangeByIndexes(19, 0, changes.length, 3).values = changes;
summary.getRange("A1:A35").format.columnWidth = 39;
summary.getRange("B1:B35").format.columnWidth = 15;
summary.getRange("C1:C35").format.columnWidth = 48;
summary.getRange("A2:C35").format.rowHeight = 22;

const headers = ["Excel 行", "问题", "目标条文", "旧引用正确", "新执行状态", "新目标命中", "回答提及目标", "实际引用条文", "新回答", "异常", "总 tokens", "耗时（秒）", "回答要求", "检索轮数上限", "答案核查", "问题说明", "核查依据"];
detail.getRange("A4:Q4").values = [headers];
detail.getRange("A4:Q4").format = { fill: "#1F4E78", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" } };
detail.getRange("A4:Q4").format.horizontalAlignment = "center";
const values = rows.map((x) => {
  const status = x.status === "completed" ? "完成" : String(x.error || "").includes("max_turns") ? "轮数超限" : String(x.error || "").includes("Insufficient Balance") ? "余额不足" : "异常";
  const refs = (x.references || []).map((r) => `${r.spec_no || ""} ${r.clause_no || ""}`).join("；");
  return [x.row, x.question, `GB55031-2022 ${x.target_clause}`, x.old_ref_ok, status,
    x.status === "completed" ? (x.target_in_references ? "是" : "否") : "未测",
    x.status === "completed" ? (x.target_in_answer ? "是" : "否") : "未测",
    refs, x.answer || "", x.error || "", x.usage?.total_tokens ?? null,
    x.elapsed_seconds ?? null, x.answer_requirement || "", x.max_turns ?? 8,
    audit.get(x.row)?.[0] || "未发现明显错误", audit.get(x.row)?.[1] || "", audit.get(x.row)?.[2] || ""];
});
detail.getRangeByIndexes(4, 0, values.length, headers.length).values = values;
detail.getRange("A5:Q85").format.font = { name: "Arial", size: 10, color: "#243247" };
detail.getRange("A5:Q85").format.rowHeight = 55;
detail.getRange("A5:A85").format.horizontalAlignment = "center";
detail.getRange("C5:G85").format.horizontalAlignment = "center";
detail.getRange("K5:L85").format.horizontalAlignment = "right";
for (const col of ["B", "H", "I", "J", "M", "P", "Q"]) detail.getRange(`${col}5:${col}85`).format.wrapText = true;
detail.getRange("L5:L85").setNumberFormat("0.0");
detail.getRange("F5:F85").conditionalFormats.add("containsText", { text: "否", format: { fill: "#FCE8E6", font: { color: "#B42318", bold: true } } });
detail.getRange("O5:O85").conditionalFormats.addCustom('=$O5="错误"', { fill: "#FCE8E6", font: { color: "#B42318", bold: true } });
detail.getRange("O5:O85").conditionalFormats.addCustom('=$O5="待复核"', { fill: "#FFF3CD", font: { color: "#8A5700", bold: true } });
for (const [col, width] of Object.entries({ A: 9, B: 44, C: 23, D: 13, E: 14, F: 13, G: 15, H: 55, I: 76, J: 44, K: 14, L: 15, M: 35, N: 16, O: 18, P: 70, Q: 42 })) {
  detail.getRange(`${col}1:${col}85`).format.columnWidth = width;
}
detail.freezePanes.freezeRows(4);
wb.recalculate();
const checked = await wb.inspect({ kind: "table", range: "汇总!A4:B12", include: "values", tableMaxRows: 12, tableMaxCols: 2, maxChars: 3000 });
console.log(checked.ndjson);
const errors = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 100 }, maxChars: 1000 });
console.log(errors.ndjson);
const preview = await wb.render({ sheetName: "汇总", range: "A1:F33", scale: 1.2, format: "png" });
await fs.writeFile(path.join(outputDir, "summary_preview.png"), new Uint8Array(await preview.arrayBuffer()));
const detailPreview = await wb.render({ sheetName: "逐题结果", range: "A4:H10", scale: 1, format: "png" });
await fs.writeFile(path.join(outputDir, "detail_preview.png"), new Uint8Array(await detailPreview.arrayBuffer()));
const flagsPreview = await wb.render({ sheetName: "逐题结果", range: "N28:Q35", scale: 1.2, format: "png" });
await fs.writeFile(path.join(outputDir, "flags_preview.png"), new Uint8Array(await flagsPreview.arrayBuffer()));
const xlsx = await SpreadsheetFile.exportXlsx(wb);
const outputPath = path.join(outputDir, "GB55031_评测_未命中与答案错误标记.xlsx");
await xlsx.save(outputPath);
console.log(outputPath);
