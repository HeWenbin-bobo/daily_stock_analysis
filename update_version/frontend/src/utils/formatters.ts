/**
 * UI 格式化工具函数
 */

/** 评分颜色 */
export function scoreColor(score: number | null | undefined): string {
  if (score == null) return '#94a3b8'
  if (score >= 80) return '#16a34a'
  if (score >= 60) return '#22c55e'
  if (score >= 40) return '#f59e0b'
  return '#ef4444'
}

/** 建议标签类型 */
export function adviceType(
  advice: string | null | undefined,
): 'success' | 'warning' | 'error' | 'default' {
  if (!advice) return 'default'
  if (advice.includes('买入') || advice.includes('加仓')) return 'success'
  if (advice.includes('持有') || advice.toLowerCase() === 'hold') return 'success'
  if (advice.includes('卖出') || advice.includes('减仓')) return 'error'
  if (advice.includes('观望') || advice.includes('等待')) return 'warning'
  return 'default'
}

/** 通过原因文本 */
export function passReasonText(reason: string): string {
  const map: Record<string, string> = {
    score: '评分均≥60',
    advice: '建议均为买/持',
    'score+advice': '评分+建议',
    incomplete: '数据不全',
    rejected_sell: '出现卖出',
    rejected_filter: '未达条件',
  }
  return map[reason] || reason
}

/** 通过原因标签类型 */
export function passReasonType(
  reason: string,
): 'success' | 'default' | 'warning' | 'error' {
  if (['score', 'advice', 'score+advice'].includes(reason)) return 'success'
  if (reason === 'rejected_sell') return 'error'
  return 'warning'
}

/** 榜单状态徽章元数据 */
export function statusType(
  status: string | undefined,
): { color: string; label: string; icon: string } {
  if (status === 'ok') return { color: '#16a34a', label: '正常', icon: '✓' }
  if (status === 'fallback') return { color: '#f59e0b', label: '兜底', icon: '⚠' }
  if (status === 'failed') return { color: '#ef4444', label: '失败', icon: '✗' }
  return { color: '#94a3b8', label: '未知', icon: '?' }
}

/** 日期格式化 */
export function formatDate(d: Date): string {
  // ★ 迭代4 修复(L4)：无效日期（Invalid Date）输出 "-" 而非 "NaN 年 NaN 月 NaN 日"
  if (!(d instanceof Date) || isNaN(d.getTime())) return '-'
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`
}

/** 时间格式化 */
export function formatTime(d: Date): string {
  if (!(d instanceof Date) || isNaN(d.getTime())) return '-'
  const pad2 = (n: number) => (n < 10 ? `0${n}` : `${n}`)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

/**
 * 从 axios 抛出的未知错误中提取可展示的字符串。
 *
 * 替代各 Vue 组件中重复的 `e?.response?.data?.detail || e?.message || '兜底'`
 * 模式，避免 `catch (e: any)` 绕过 TS 类型检查。
 */
export function extractErrorMessage(e: unknown, fallback: string): string {
  if (!e) return fallback
  // 优先按 Error 实例处理（包括 axios 抛的 AxiosError）
  if (e instanceof Error) {
    const anyE = e as Error & {
      response?: { data?: { detail?: unknown; message?: unknown; error?: unknown } }
    }
    const detail = anyE.response?.data?.detail
    const message = anyE.response?.data?.message
    // ★ round 7：当 detail 是 FastAPI 422 验证错误的数组时，提取每个字段名 + msg
    if (Array.isArray(detail) && detail.length > 0) {
      const parts = detail.map((d: Record<string, unknown>) => {
        const loc = Array.isArray(d.loc) ? d.loc.filter((x: unknown) => x !== 'body').join('.') : ''
        const msg = typeof d.msg === 'string' ? d.msg : String(d)
        return loc ? `${loc}: ${msg}` : msg
      })
      return parts.length === 1 ? parts[0] : parts.join('; ')
    }
    if (typeof detail === 'string' && detail) return detail
    // message 是通用"请求参数验证失败"时不要直接用，因为会遮蔽 detail 里的字段信息
    if (typeof message === 'string' && message && !message.includes('请求参数验证失败')) return message
    // 网络错误（后端不可达）：明确给出兜底，让用户知道要检查后端
    if (!anyE.response) return fallback
    if (typeof anyE.message === 'string' && anyE.message) return anyE.message
    return fallback
  }
  // SSE Event 对象等非 Error 形态的 object 兜底
  if (typeof e === 'object' && e !== null) {
    const anyE = e as {
      response?: { data?: { detail?: unknown; message?: unknown; error?: unknown } }
      message?: unknown
    }
    const detail = anyE.response?.data?.detail
    const message = anyE.response?.data?.message
    if (Array.isArray(detail) && detail.length > 0) {
      const parts = detail.map((d: Record<string, unknown>) => {
        const loc = Array.isArray(d.loc) ? d.loc.filter((x: unknown) => x !== 'body').join('.') : ''
        const msg = typeof d.msg === 'string' ? d.msg : String(d)
        return loc ? `${loc}: ${msg}` : msg
      })
      return parts.length === 1 ? parts[0] : parts.join('; ')
    }
    if (typeof detail === 'string' && detail) return detail
    if (typeof message === 'string' && message && !message.includes('请求参数验证失败')) return message
    if (typeof anyE.message === 'string' && anyE.message) return anyE.message
  }
  if (typeof e === 'string') return e
  return fallback
}

/** 分析轮次提示文本 */
export function buildAnalysisTip(a: {
  round: number
  error?: string | null
  sentiment_score?: number | null
  operation_advice?: string | null
  trend_prediction?: string | null
  model_used?: string | null
  timestamp?: string | null
}): string {
  if (a.error) return `第 ${a.round} 轮失败：${a.error}`
  return [
    `第 ${a.round} 轮`,
    `评分: ${a.sentiment_score}`,
    `建议: ${a.operation_advice}`,
    `趋势: ${a.trend_prediction || '-'}`,
    `模型: ${a.model_used || '-'}`,
    `时间: ${a.timestamp || '-'}`,
  ].join('\n')
}

/** 构建纯文本导出内容 */
export function buildPlainText(
  result: {
    hot_lists_summary: Record<string, number>
    raw_intersection_count: number
    filtered_intersection_count: number
    passed_stock_count: number
    duration_seconds: number
    status: string
  },
  stocks: Array<{
    code: string
    name: string
    avg_score: number
    final_advice: string
    in_sources: string[]
    analyses: Array<{
      round: number
      error?: string | null
      sentiment_score?: number | null
      operation_advice?: string | null
    }>
  }>,
): string {
  const lines: string[] = []
  lines.push('# 四榜交集分析结果')
  lines.push(`# 抓取: ${JSON.stringify(result.hot_lists_summary)}`)
  lines.push(
    `# 交集: raw=${result.raw_intersection_count} filtered=${result.filtered_intersection_count} 通过=${result.passed_stock_count}`,
  )
  lines.push(
    `# 用时: ${result.duration_seconds}s | 状态: ${result.status}`,
  )
  lines.push('')
  for (const s of stocks) {
    lines.push(
      `${s.code} ${s.name} | 均分=${s.avg_score} | 建议=${s.final_advice} | 命中榜=${s.in_sources.join('+')}`,
    )
    for (const a of s.analyses) {
      const err = a.error ? ` | ERR: ${a.error}` : ''
      lines.push(
        `  - 第${a.round}轮: score=${a.sentiment_score ?? '-'} advice=${a.operation_advice ?? '-'}${err}`,
      )
    }
    lines.push('')
  }
  return lines.join('\n')
}
