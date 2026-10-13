/** 数据导出工具: 将 K线 + 波峰波谷 导出为 CSV / JSON。 */

import { downloadBlob } from "./downloadBlob";

/** CSV 字段中的危险前缀 (Excel 公式注入) */
const FORMULA_PREFIXES = ["=", "+", "-", "@", "\t", "\r"] as const;

/** 安全转义 CSV 字段: 防逗号/引号/换行 + 防 Excel 公式注入 */
function escapeCsvField(value: unknown): string {
  if (value === null || value === undefined) return "";
  let s = typeof value === "string" ? value : String(value);

  // Excel 公式注入防护: 文本字段若以危险字符开头, 加单引号前缀
  // (Excel/GSheet 把它视为普通文本, 不执行)
  if (s.length > 0 && FORMULA_PREFIXES.includes(s[0] as typeof FORMULA_PREFIXES[number])) {
    s = "'" + s;
  }

  // RFC 4180: 含 ", , \n 时用双引号包裹, 内部 " 加双写
  if (/[",\n\r]/.test(s)) {
    s = '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

/** 触发浏览器下载一个 Blob — 共享实现见 utils/downloadBlob.ts (iter 88 dedup) */
function stamp(d: Date = new Date()): string {
  return (
    d.getFullYear().toString() +
    String(d.getMonth() + 1).padStart(2, "0") +
    String(d.getDate()).padStart(2, "0") +
    "_" +
    String(d.getHours()).padStart(2, "0") +
    String(d.getMinutes()).padStart(2, "0")
  );
}

/** 将 K线 + 分析导出为 CSV (UTF-8 BOM, Excel 可直接打开) */
export function exportHistoryAsCSV(
  code: string,
  name: string,
  // iter 133: period 收紧到 Period 联合类型 — 与 useAppStore.period 对齐, 调用点
  // 永远是 'daily'|'weekly'|'monthly', 之前 `string` 接受任何字符串让 contract 失真。
  period: import("../types").Period,
  data: import("../types").KLineData[],
  segments?: import("../types").PeakTroughSegment[],
  // iter 157: source 参数透传 — 让导出的 CSV 头部告诉读者数据来源 (akshare / yfinance / mock / mock_fallback)。
  // 放在 segments 之后保持向后兼容 (旧调用点只传 5 个参数, source 默认 undefined)。
  source?: string
): void {
  const lines: string[] = [];
  // iter 92: 单一 now — 之前 `new Date()` 在头部注释和 stamp() 中各创建一次,
  // 跨秒边界时文件名时间戳和 ISO 时间会不一致。
  const now = new Date();

  // 头部元数据 (注释行, 多数 CSV 工具会忽略)
  lines.push(`# Stock: ${code} ${name}`);
  lines.push(`# Period: ${period}`);
  if (source) {
    lines.push(`# Source: ${source}`);
  }
  lines.push(`# Generated: ${now.toISOString()}`);
  lines.push(`# Rows: ${data.length}`);
  lines.push("");

  // OHLCV 表
  lines.push("date,open,high,low,close,volume,amount,change_percent");
  for (const bar of data) {
    // date 是文本字段, 需要公式注入防护 + 转义; 数值字段无需公式防护
    lines.push(
      [
        escapeCsvField(bar.date),
        escapeCsvField(bar.open),
        escapeCsvField(bar.high),
        escapeCsvField(bar.low),
        escapeCsvField(bar.close),
        escapeCsvField(bar.volume),
        escapeCsvField(bar.amount),
        escapeCsvField(bar.change_percent),
      ].join(",")
    );
  }

  // 如果有 segments, 追加分析表
  if (segments && segments.length > 0) {
    lines.push("");
    lines.push("# Peak/Trough segments");
    lines.push("type,from_date,to_date,start_price,end_price,change_pct,from_peak");
    for (const s of segments) {
      lines.push(
        [
          escapeCsvField(s.type),
          escapeCsvField(s.from_date),
          escapeCsvField(s.to_date),
          escapeCsvField(s.start_price),
          escapeCsvField(s.end_price),
          escapeCsvField(s.change_pct),
          escapeCsvField(s.from_peak),
        ].join(",")
      );
    }
  }

  // 加 UTF-8 BOM, 让 Excel 正确识别中文
  const BOM = "﻿";
  const blob = new Blob([BOM + lines.join("\n")], {
    type: "text/csv;charset=utf-8",
  });
  const safeName = name.replace(/[\\/:*?"<>|]/g, "_");
  // 复用同一个 now, filename 与 ISO 注释保持一致
  downloadBlob(blob, `${safeName}_${code}_${period}_${stamp(now)}.csv`);
}

/** 将完整分析结果导出为 JSON */
export function exportAnalysisAsJSON(
  analysis: import("../types").PeakTroughResponse,
  data: import("../types").KLineData[],
  // iter 157: 可选 source — JSON 顶层加 source 字段, 与 CSV 的 # Source 注释行保持一致。
  // 不传时省略, 不影响旧调用点。
  source?: string
): void {
  const now = new Date();
  const payload = {
    exported_at: now.toISOString(),
    ...(source ? { source } : {}),
    analysis,
    history: data,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const safeName = analysis.stock_name.replace(/[\\/:*?"<>|]/g, "_");
  downloadBlob(
    blob,
    `${safeName}_${analysis.stock_code}_analysis_${stamp(now)}.json`
  );
}