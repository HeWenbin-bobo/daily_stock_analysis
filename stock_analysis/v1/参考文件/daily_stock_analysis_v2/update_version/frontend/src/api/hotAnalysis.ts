/**
 * 四榜交集分析 API 客户端
 *
 * Phase 1: 新增单股分析、过滤规则配置
 * Phase 2: 新增历史记录、SSE 进度推送
 * Phase 3: 新增对比分析、Webhook 通知
 */
import axios, { type AxiosInstance } from 'axios'

const API_BASE = (import.meta.env.VITE_API_BASE as string) || '/api/v1'

const http: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 0, // 不超时（长任务）
})

// ============================================================
// 公共类型
// ============================================================

export interface AnalysisRunItem {
  round: number
  sentiment_score: number | null
  operation_advice: string | null
  decision_type: string | null
  trend_prediction: string | null
  model_used: string | null
  timestamp: string | null
  error: string | null
}

export interface HotAnalysisStockResult {
  code: string
  name: string
  in_sources: string[]
  analyses: AnalysisRunItem[]
  pass_reason: string
  avg_score: number
  final_advice: string
}

/** 过滤规则配置 */
export interface FilterConfig {
  min_score: number
  allowed_advice: string[]
  exclude_keywords: string[]
  require_all_rounds: boolean
}

// ============================================================
// Phase 1: 预览 + 运行 + 单股分析
// ============================================================

export interface HotAnalysisRunRequest {
  hot_list_n?: number
  max_intersection?: number
  exclude_kechuang?: boolean
  rounds?: number
  max_runtime_seconds?: number
  inter_round_sleep?: number
  simple_report?: boolean
  llm_timeout_seconds?: number
  // 过滤规则
  min_score?: number
  allowed_advice?: string[]
  exclude_keywords?: string[]
  require_all_rounds?: boolean
  // 通知配置
  webhook_url?: string
}

export interface HotAnalysisRunResponse {
  status: 'ok' | 'failed' | 'timeout' | 'cancelled'
  started_at: string
  finished_at: string
  duration_seconds: number
  hot_lists_summary: Record<string, number>
  hot_lists_status?: Record<string, string>
  hot_lists_notes?: Record<string, string>
  raw_intersection_count: number
  filtered_intersection_count: number
  analyzed_stock_count: number
  passed_stock_count: number
  results: HotAnalysisStockResult[]
  filtered_stocks?: FilteredStockItem[]
  error: string | null
  filter_config?: Record<string, unknown>
  notification_sent?: boolean
}

/** 触发一次完整流程（支持 AbortSignal 取消） */
export async function runHotAnalysis(
  req: HotAnalysisRunRequest = {},
  signal?: AbortSignal,
): Promise<HotAnalysisRunResponse> {
  const { data } = await http.post<HotAnalysisRunResponse>('/hot-analysis/run', {
    hot_list_n: req.hot_list_n ?? 200,
    max_intersection: req.max_intersection ?? 10,
    exclude_kechuang: req.exclude_kechuang ?? true,
    rounds: req.rounds ?? 3,
    max_runtime_seconds: req.max_runtime_seconds ?? 600,
    inter_round_sleep: req.inter_round_sleep ?? 0.6,
    simple_report: req.simple_report ?? true,
    llm_timeout_seconds: req.llm_timeout_seconds ?? 180,
    min_score: req.min_score ?? 60,
    allowed_advice: req.allowed_advice ?? ['买入', '加仓', '持有', 'BUY', 'HOLD', 'ADD'],
    exclude_keywords: req.exclude_keywords ?? ['卖出', '减仓', 'SELL', 'REDUCE'],
    require_all_rounds: req.require_all_rounds ?? true,
    webhook_url: req.webhook_url,
  }, { signal })
  return data
}

// ---- 预览 ----

export interface HotAnalysisPreviewRequest {
  hot_list_n?: number
  max_intersection?: number
  exclude_kechuang?: boolean
  rounds?: number
  inter_round_sleep?: number
  simple_report?: boolean
  llm_timeout_seconds?: number
  min_score?: number
  allowed_advice?: string[]
  exclude_keywords?: string[]
  require_all_rounds?: boolean
}

export interface IntersectionStock {
  code: string
  name: string
  in_sources: string[]
}

export interface HotAnalysisPreviewResponse {
  status: 'ok' | 'failed'
  started_at: string
  finished_at: string
  duration_seconds: number
  hot_lists_summary: Record<string, number>
  hot_lists_status?: Record<string, string>
  hot_lists_notes?: Record<string, string>
  raw_intersection_count: number
  filtered_intersection_count: number
  target_count: number
  estimated_seconds: number
  estimated_human: string
  rounds: number
  error: string | null
  intersection_stocks?: IntersectionStock[]
}

/** 阶段 1：仅抓榜 + 求交集 + 预估时间（~5-15s） */
export async function previewHotAnalysis(
  req: HotAnalysisPreviewRequest = {},
): Promise<HotAnalysisPreviewResponse> {
  const { data } = await http.post<HotAnalysisPreviewResponse>('/hot-analysis/preview', {
    hot_list_n: req.hot_list_n ?? 200,
    max_intersection: req.max_intersection ?? 10,
    exclude_kechuang: req.exclude_kechuang ?? true,
    rounds: req.rounds ?? 3,
    inter_round_sleep: req.inter_round_sleep ?? 0.6,
    simple_report: req.simple_report ?? true,
    llm_timeout_seconds: req.llm_timeout_seconds ?? 180,
    min_score: req.min_score ?? 60,
    allowed_advice: req.allowed_advice ?? ['买入', '加仓', '持有', 'BUY', 'HOLD', 'ADD'],
    exclude_keywords: req.exclude_keywords ?? ['卖出', '减仓', 'SELL', 'REDUCE'],
    require_all_rounds: req.require_all_rounds ?? true,
  })
  return data
}

// ---- 单股分析 (Phase 1) ----

export interface SingleStockRequest {
  code: string
  rounds?: number
  simple_report?: boolean
  llm_timeout_seconds?: number
}

export interface SingleStockResponse {
  status: string
  code: string
  name: string
  analyses: AnalysisRunItem[]
  avg_score: number
  final_advice: string
  pass_reason: string
  error: string | null
  error_code?: string | null
}

/** 单股分析（不走四榜交集，直接分析指定股票） */
export async function analyzeSingleStock(
  req: SingleStockRequest,
  signal?: AbortSignal,
): Promise<SingleStockResponse> {
  const { data } = await http.post<SingleStockResponse>('/hot-analysis/analyze-single', {
    code: req.code,
    rounds: req.rounds ?? 3,
    simple_report: req.simple_report ?? true,
    llm_timeout_seconds: req.llm_timeout_seconds ?? 180,
  }, { signal })
  return data
}

// ============================================================
// Phase 2: 历史记录 + SSE 进度
// ============================================================

export interface AnalysisHistoryItem {
  id: number
  started_at: string
  finished_at: string
  duration_seconds: number
  status: string
  hot_lists_summary: Record<string, number>
  hot_lists_status?: Record<string, string>
  hot_lists_notes?: Record<string, string>
  raw_intersection_count: number
  filtered_intersection_count: number
  analyzed_stock_count: number
  passed_stock_count: number
  filter_config?: Record<string, unknown>
  results: HotAnalysisStockResult[]
}

export interface AnalysisHistoryResponse {
  total: number
  page: number
  page_size: number
  items: AnalysisHistoryItem[]
}

/** 查询历史记录（分页） */
export async function getAnalysisHistory(
  page: number = 1,
  pageSize: number = 20,
): Promise<AnalysisHistoryResponse> {
  const { data } = await http.get<AnalysisHistoryResponse>('/hot-analysis/history', {
    params: { page, page_size: pageSize },
  })
  return data
}

/** 查询单次历史详情 */
export async function getAnalysisRun(runId: number): Promise<AnalysisHistoryItem> {
  const { data } = await http.get<AnalysisHistoryItem>(`/hot-analysis/history/${runId}`)
  return data
}

/** 删除历史记录 */
export async function deleteAnalysisRun(runId: number): Promise<void> {
  await http.delete(`/hot-analysis/history/${runId}`)
}

// ---- SSE 进度推送 ----

export interface TaskProgress {
  task_id: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  phase: 'fetching' | 'analyzing' | 'completed'
  total_stocks: number
  completed_stocks: number
  current_stock: string
  current_round: number
  passed_count: number
  error?: string
  started_at: string
  elapsed_seconds: number
}

/**
 * 创建 SSE 连接监听分析进度
 *
 * 用法：
 *   const es = createProgressStream(taskId, (progress) => { ... })
 *   // 关闭: es.close()
 */
export function createProgressStream(
  taskId: string,
  onProgress: (p: TaskProgress) => void,
  onError?: (err: Event) => void,
  onComplete?: (result: HotAnalysisRunResponse, isPartial: boolean) => void,
): EventSource {
  const es = new EventSource(`${API_BASE}/hot-analysis/run/${taskId}/stream`)

  es.addEventListener('progress', (e) => {
    try {
      onProgress(JSON.parse(e.data) as TaskProgress)
    } catch { /* ignore parse errors */ }
  })

  es.addEventListener('partial', (e) => {
    try {
      const result = JSON.parse(e.data) as HotAnalysisRunResponse
      onComplete?.(result, true)
    } catch { /* ignore */ }
    es.close()
  })

  es.addEventListener('complete', (e) => {
    try {
      const result = JSON.parse(e.data) as HotAnalysisRunResponse
      onComplete?.(result, false)
    } catch { /* ignore */ }
    es.close()
  })

  es.addEventListener('error', (e) => {
    onError?.(e)
    es.close()
  })

  return es
}

// ---- 后台任务启动 ----

export interface AsyncTaskResponse {
  task_id: string
  status: string
  message: string
}

/** 启动后台分析任务（非阻塞，返回 task_id） */
export async function startAsyncAnalysis(
  req: HotAnalysisRunRequest = {},
): Promise<AsyncTaskResponse> {
  const { data } = await http.post<AsyncTaskResponse>('/hot-analysis/run-async', {
    hot_list_n: req.hot_list_n ?? 200,
    max_intersection: req.max_intersection ?? 10,
    exclude_kechuang: req.exclude_kechuang ?? true,
    rounds: req.rounds ?? 3,
    max_runtime_seconds: req.max_runtime_seconds ?? 600,
    inter_round_sleep: req.inter_round_sleep ?? 0.6,
    simple_report: req.simple_report ?? true,
    llm_timeout_seconds: req.llm_timeout_seconds ?? 180,
    min_score: req.min_score ?? 60,
    allowed_advice: req.allowed_advice ?? ['买入', '加仓', '持有', 'BUY', 'HOLD', 'ADD'],
    exclude_keywords: req.exclude_keywords ?? ['卖出', '减仓', 'SELL', 'REDUCE'],
    require_all_rounds: req.require_all_rounds ?? true,
    webhook_url: req.webhook_url,
  })
  return data
}

// ============================================================
// Phase 3: 对比分析
// ============================================================

export interface CompareResult {
  run_1: AnalysisHistoryItem
  run_2: AnalysisHistoryItem
  new_stocks: string[]
  removed_stocks: string[]
  score_changes: Array<{
    code: string
    name: string
    old_score: number
    new_score: number
    old_advice: string
    new_advice: string
  }>
}

/** 对比两次分析结果 */
export async function compareRuns(
  runId1: number,
  runId2: number,
): Promise<CompareResult> {
  const { data } = await http.post<CompareResult>('/hot-analysis/compare', {
    run_id_1: runId1,
    run_id_2: runId2,
  })
  return data
}

// ============================================================
// Phase: 自选股跟踪
// ============================================================

export interface WatchlistItem {
  id: number
  code: string
  name: string
  notes: string
  created_at: string
}

export interface WatchlistResponse {
  items: WatchlistItem[]
}

export async function getWatchlist(): Promise<WatchlistResponse> {
  const { data } = await http.get<WatchlistResponse>('/hot-analysis/watchlist')
  return data
}

export async function addToWatchlist(code: string, name: string = '', notes: string = ''): Promise<void> {
  await http.post('/hot-analysis/watchlist', { code, name, notes })
}

export async function removeFromWatchlist(code: string): Promise<void> {
  await http.delete(`/hot-analysis/watchlist/${code}`)
}

// ============================================================
// Phase: 分析模板
// ============================================================

export interface AnalysisTemplate {
  id: number
  name: string
  description: string
  config: Record<string, unknown>
  is_default: boolean
  created_at: string
  updated_at: string
}

export interface TemplateListResponse {
  items: AnalysisTemplate[]
}

export async function getTemplates(): Promise<TemplateListResponse> {
  const { data } = await http.get<TemplateListResponse>('/hot-analysis/templates')
  return data
}

export async function saveTemplate(req: {
  name: string
  description?: string
  config: Record<string, unknown>
  is_default?: boolean
}): Promise<{ id: number }> {
  const { data } = await http.post('/hot-analysis/templates', req)
  return data
}

export async function deleteTemplate(templateId: number): Promise<void> {
  await http.delete(`/hot-analysis/templates/${templateId}`)
}

// ============================================================
// Phase: 定时任务
// ============================================================

export interface ScheduledTaskItem {
  id: number
  name: string
  enabled: boolean
  cron_expr: string
  config: Record<string, unknown>
  last_run_at: string | null
  last_status: string | null
  last_result_id: number | null
  created_at: string
}

export interface ScheduledTaskListResponse {
  items: ScheduledTaskItem[]
}

export async function getScheduledTasks(): Promise<ScheduledTaskListResponse> {
  const { data } = await http.get<ScheduledTaskListResponse>('/hot-analysis/scheduled-tasks')
  return data
}

export async function saveScheduledTask(req: {
  name: string
  cron_expr: string
  config?: Record<string, unknown>
  enabled?: boolean
  task_id?: number
}): Promise<{ id: number }> {
  const { data } = await http.post('/hot-analysis/scheduled-tasks', req)
  return data
}

export async function deleteScheduledTask(taskId: number): Promise<void> {
  await http.delete(`/hot-analysis/scheduled-tasks/${taskId}`)
}

export async function runScheduledTask(taskId: number): Promise<void> {
  await http.post(`/hot-analysis/scheduled-tasks/${taskId}/run`)
}

// ============================================================
// Phase: 行情数据
// ============================================================

export interface MarketDataItem {
  code: string
  name: string
  price: number
  change_pct: number
  volume: number
  turnover: number
  high: number
  low: number
  open: number
  market_cap: number
}

export interface MarketDataResponse {
  items: MarketDataItem[]
  updated_at: string
}

export async function getMarketData(codes: string[]): Promise<MarketDataResponse> {
  const { data } = await http.get<MarketDataResponse>('/hot-analysis/market-data', {
    params: { codes: codes.join(',') },
  })
  return data
}

// ============================================================
// Phase: PDF 报告
// ============================================================

export interface PDFReportResponse {
  status: string
  filename: string
  pdf_base64: string
  size_bytes: number
  error?: string
}

export async function generatePDFReport(runId?: number): Promise<PDFReportResponse> {
  const { data } = await http.post<PDFReportResponse>('/hot-analysis/pdf-report', {
    run_id: runId,
  })
  return data
}

// ============================================================
// 过滤后股票（未通过）
// ============================================================

export interface FilteredStockItem {
  code: string
  name: string
  in_sources: string[]
  filter_reason: string
  avg_score: number
  final_advice: string
  analyses: AnalysisRunItem[]
}

// ============================================================
// 健康检查
// ============================================================

/** 取消后台任务 */
export async function cancelTask(taskId: string): Promise<void> {
  await http.post(`/hot-analysis/run/${taskId}/cancel`)
}

/** 健康检查 */
export async function checkHealth(): Promise<{ status: string; akshare_available: boolean }> {
  const { data } = await http.get('/hot-analysis/health')
  return data
}

// ============================================================
// 大盘复盘（每日报告）
// ============================================================
export interface MarketReviewItem {
  id: number
  review_date: string
  region: string
  status: string
  summary: string
  breadth: Record<string, number>
  indices: Record<string, Record<string, unknown>>
  sectors: Record<string, unknown>
  signals: Record<string, unknown>
  warnings: string[]
  report_markdown: string
  duration_seconds: number
  error: string
  created_at: string
  stocks_fetch_seconds: number
}

export interface MarketReviewListResponse {
  items: MarketReviewItem[]
  total: number
  page: number
  page_size: number
}

/** 同步生成一次大盘复盘（akshare 抓指数 + 宽度 + 板块）。 */
export async function generateMarketReview(
  review_date?: string,
  region: string = 'cn',
): Promise<MarketReviewItem> {
  const { data } = await http.post<MarketReviewItem>('/hot-analysis/market-review/generate', {
    review_date,
    region,
    persist: true,
  })
  return data
}

/** 分页获取历史复盘记录。 */
export async function listMarketReviews(
  page: number = 1,
  page_size: number = 20,
  region?: string,
): Promise<MarketReviewListResponse> {
  const { data } = await http.get<MarketReviewListResponse>('/hot-analysis/market-review/list', {
    params: { page, page_size, region },
  })
  return data
}

/** 获取最近一次复盘。 */
export async function getLatestMarketReview(region?: string): Promise<MarketReviewItem> {
  const { data } = await http.get<MarketReviewItem>('/hot-analysis/market-review/latest', {
    params: region ? { region } : {},
  })
  return data
}

/** 按日期获取复盘。 */
export async function getMarketReviewByDate(review_date: string): Promise<MarketReviewItem> {
  const { data } = await http.get<MarketReviewItem>(`/hot-analysis/market-review/${review_date}`)
  return data
}

/** 删除某日复盘。 */
export async function deleteMarketReview(review_date: string): Promise<void> {
  await http.delete(`/hot-analysis/market-review/${review_date}`)
}

// ============================================================
// round 6 新功能：导出 / 对比 / 批量生成
// ============================================================

/** 导出某日复盘为 markdown 文件（浏览器下载） */
export function getMarketReviewExportUrl(review_date: string): string {
  return `${API_BASE}/hot-analysis/market-review/${review_date}/export`
}

/** 导出某日复盘为 PDF 文件（round 8 新增；复用后端 reportlab） */
export function getMarketReviewPdfUrl(review_date: string): string {
  return `${API_BASE}/hot-analysis/market-review/${review_date}/pdf`
}

/** 对比两个日期的复盘：返回 diff 摘要 + 详细字段差 */
export interface MarketReviewComparison {
  date_a: string
  date_b: string
  diff: Array<{
    path: string
    from: unknown
    to: unknown
    delta?: number | null
    type?: string
  }>
  summary: Record<string, { from: unknown; to: unknown; delta?: number }>
}
export async function compareMarketReviews(
  date_a: string,
  date_b: string,
): Promise<MarketReviewComparison> {
  const { data } = await http.get<MarketReviewComparison>(
    '/hot-analysis/market-review/compare',
    { params: { date_a, date_b } },
  )
  return data
}

/** 批量生成最近 N 天复盘（同步） */
export interface MarketReviewBatchResult {
  review_date: string
  status: 'ok' | 'failed' | 'skipped'
  summary: string
}
export interface MarketReviewBatchResponse {
  status: string
  requested_days: number
  completed: number
  failed: number
  results: MarketReviewBatchResult[]
}
export async function batchGenerateMarketReviews(
  days: number = 5,
  region: string = 'cn',
): Promise<MarketReviewBatchResponse> {
  const { data } = await http.post<MarketReviewBatchResponse>(
    '/hot-analysis/market-review/batch',
    { days, region, persist: true },
  )
  return data
}
