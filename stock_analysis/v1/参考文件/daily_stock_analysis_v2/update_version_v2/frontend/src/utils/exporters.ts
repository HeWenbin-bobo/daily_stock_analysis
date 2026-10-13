/**
 * 导出工具 —— CSV 导出
 *
 * CSV 使用纯浏览器 API，无需额外依赖。
 * 可用 Excel 直接打开 CSV 文件。
 */
import type { HotAnalysisStockResult, HotAnalysisRunResponse } from '../api/hotAnalysis'

/** UTF-8 BOM 头，让 Excel for Windows 直接识别 UTF-8 编码（不乱码） */
const UTF8_BOM = '﻿'

/** CRLF 是 RFC 4180 推荐的 CSV 行终止符，跨平台兼容性最好 */
const LINE_END = '\r\n'

/**
 * 将分析结果导出为标准 CSV（逗号分隔 + UTF-8 BOM + CRLF）
 *
 * 适合：直接打开、命令行处理（awk/grep 等）、跨平台兼容。
 *
 * ★ Round 6：try/catch 包裹，避免 iOS 私有模式 / 老浏览器 Blob 异常静默
 */
export function exportCSV(
  _result: HotAnalysisRunResponse,
  stocks: HotAnalysisStockResult[],
): void {
  try {
    const headers = [
      '代码', '名称', '平均分', '最终建议', '通过原因', '命中榜',
      'R1评分', 'R1建议', 'R2评分', 'R2建议', 'R3评分', 'R3建议',
    ]

    const rows = stocks.map((s) => [
      s.code,
      s.name,
      String(s.avg_score),
      s.final_advice,
      s.pass_reason,
      s.in_sources.join('+'),
      ...buildRoundCols(s.analyses),
    ])

    const csvContent = [
      headers.join(','),
      ...rows.map((r) => r.map(escapeCSV).join(',')),
    ].join(LINE_END)

    downloadBlob(csvContent, `hot-analysis-${timestamp()}.csv`, 'text/csv;charset=utf-8')
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e)
    if (import.meta.env.DEV) console.warn('exportCSV failed:', e)
    // 抛出由上层捕获并显示给用户
    throw new Error(`CSV 导出失败：${msg}`)
  }
}

/**
 * 导出为 Excel 友好的 CSV：与 exportCSV 相比，分隔符使用分号（Excel 中文/欧版默认），
 * 且摘要信息以独立行写入，让用户在 Excel 打开后能直接看到抓榜和过滤统计。
 *
 * 为什么不直接生成 xlsx：
 *  - 避免引入 `xlsx`（~500KB）+ 维护成本；
 *  - UTF-8 BOM + 分号分隔符已能保证 Excel for Windows / macOS 直接打开不乱码。
 */
export async function exportCSVForExcel(
  result: HotAnalysisRunResponse,
  stocks: HotAnalysisStockResult[],
): Promise<void> {
  // 让上层 await 拿到真实微任务
  await Promise.resolve()

  const summary = buildSummaryBlock(result)
  const headerLine = [
    '代码', '名称', '平均分', '最终建议', '通过原因', '命中榜',
    'R1评分', 'R1建议', 'R2评分', 'R2建议', 'R3评分', 'R3建议',
  ].join(';')
  const dataRows = stocks.map((s) => [
    s.code,
    s.name,
    String(s.avg_score),
    s.final_advice,
    s.pass_reason,
    s.in_sources.join('+'),
    ...buildRoundCols(s.analyses),
  ].map(escapeCSV).join(';'))

  const csvContent = [
    summary,
    headerLine,
    ...dataRows,
  ].join(LINE_END)

  downloadBlob(csvContent, `hot-analysis-excel-${timestamp()}.csv`, 'text/csv;charset=utf-8')
}

/** 抓榜 + 过滤摘要（前 3 行作为 Excel 顶部元数据） */
function buildSummaryBlock(result: HotAnalysisRunResponse): string {
  const lines = [
    `# 抓榜;${JSON.stringify(result.hot_lists_summary ?? {})}`,
    `# 交集;raw=${result.raw_intersection_count};filtered=${result.filtered_intersection_count};通过=${result.passed_stock_count}`,
    `# 用时;${result.duration_seconds}s;状态;${result.status}`,
  ]
  return lines.join(LINE_END)
}

// ---- 辅助函数 ----

function buildRoundCols(analyses: HotAnalysisStockResult['analyses']): string[] {
  const cols: string[] = []
  for (let i = 0; i < 3; i++) {
    const a = analyses[i]
    if (a) {
      cols.push(String(a.sentiment_score ?? '-'))
      cols.push(a.operation_advice ?? '-')
    } else {
      cols.push('', '')
    }
  }
  return cols
}

function escapeCSV(val: string): string {
  if (val.includes(';') || val.includes(',') || val.includes('"') || val.includes('\n') || val.includes('\r')) {
    return `"${val.replace(/"/g, '""')}"`
  }
  return val
}

function timestamp(): string {
  return new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
}

function downloadBlob(content: string, filename: string, mimeType: string): void {
  // ★ Round 6：try/catch — 浏览器环境异常（无 createObjectURL）抛到上层
  const blob = new Blob([UTF8_BOM + content], { type: mimeType })
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
  } finally {
    URL.revokeObjectURL(url)
  }
}
