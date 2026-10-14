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
  timeout: 30000, // ★ Round 13：默认 30s（长任务 endpoint 单独覆盖为 0）
})

// ★ Round 13：全局 response interceptor — 5xx/401 友好提示
// ★ 本轮修复：抽成具名函数，http 与 httpLong 各自 use()，避免复制
//   `http.interceptors.response.handlers[0]`（axios 类型上 handlers 可能 undefined）
function handleResponseError(err: unknown): Promise<never> {
  const e = err as { response?: { status?: number; data?: { detail?: string } }; message?: string }
  const status = e?.response?.status
  if (status === 401) {
    e.message = `未授权: ${e?.response?.data?.detail || '需要登录'}`
  } else if (status === 429) {
    e.message = '请求过于频繁，请稍后再试'
  } else if (status && status >= 500) {
    e.message = `服务器错误 (${status})，请稍后再试`
  }
  return Promise.reject(err)
}

http.interceptors.response.use(
  (resp) => resp,
  (err) => handleResponseError(err),
)

// ★ Round 13：长任务 endpoint（不超时）
export const httpLong: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 0,
})
httpLong.interceptors.response.use(
  (resp) => resp,
  (err) => handleResponseError(err),
)

// ★ 技术分析使用 /api/v2 前缀（独立于 /api/v1 的四榜分析）
const V2_API_BASE = '/api/v2'
// ★ 修复(H2)：导出 v2Http/v2HttpLong 供组件复用（AdvancedViz/TechnicalAnalysisView
//   各自硬编码 axios.create({baseURL:'/api/v2'}) 无统一错误拦截器）。
//   旧实现 401/429/5xx 时 e.message 保持 axios 原文，限流提示不友好。
export const v2Http: AxiosInstance = axios.create({
  baseURL: V2_API_BASE,
  timeout: 30000,
})
v2Http.interceptors.response.use(
  (resp) => resp,
  (err) => handleResponseError(err),
)
export const v2HttpLong: AxiosInstance = axios.create({
  baseURL: V2_API_BASE,
  timeout: 0,
})
v2HttpLong.interceptors.response.use(
  (resp) => resp,
  (err) => handleResponseError(err),
)

// ★ Round 4：导出供组件复用（统一 baseURL / timeout / 拦截器）
export { http }

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
  // ★ Round 16：并发分析股票数（1=串行）
  max_concurrency?: number
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
  const { data } = await httpLong.post<HotAnalysisRunResponse>('/hot-analysis/run', {
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
    max_concurrency: req.max_concurrency ?? 1,
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
  // ★ Round 16：并发分析股票数（1=串行）
  max_concurrency?: number
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

/** 阶段 1：仅抓榜 + 求交集 + 预估时间（~5-15s）
 *  ★ Round 19 修复(H1)：支持 AbortSignal，让"取消预览"能真正中断请求，
 *    避免四榜抓取阻塞时 loading 无限锁死配置面板 */
export async function previewHotAnalysis(
  req: HotAnalysisPreviewRequest = {},
  signal?: AbortSignal,
): Promise<HotAnalysisPreviewResponse> {
  const { data } = await httpLong.post<HotAnalysisPreviewResponse>('/hot-analysis/preview', {
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
    max_concurrency: req.max_concurrency ?? 1,
  }, { signal })
  return data
}

// ---- 单股分析 (Phase 1) ----

export interface SingleStockRequest {
  code: string
  rounds?: number
  simple_report?: boolean
  llm_timeout_seconds?: number
  // ★ 修复(H5)：单股分析也透传轮间间隔（此前忽略用户配置）
  inter_round_sleep?: number
}

export interface SingleStockResponse {
  status: string
  code: string
  name: string
  analyses: AnalysisRunItem[]
  avg_score: number
  final_advice: string
  pass_reason: string
  // ★ Round 26：真实名称/行业 + 汇总操作建议
  industry?: string
  summary?: string
  // ★ Round 27：最近股价 + 最新涨跌幅（前端单独展示）
  price?: number | null
  change_pct?: number | null
  error: string | null
  error_code?: string | null
}

/** 单股分析（不走四榜交集，直接分析指定股票） */
export async function analyzeSingleStock(
  req: SingleStockRequest,
  signal?: AbortSignal,
): Promise<SingleStockResponse> {
  // ★ Round 13：单股分析可能 30s+，用 httpLong（无超时）
  const { data } = await httpLong.post<SingleStockResponse>('/hot-analysis/analyze-single', {
    code: req.code,
    rounds: req.rounds ?? 3,
    simple_report: req.simple_report ?? true,
    llm_timeout_seconds: req.llm_timeout_seconds ?? 180,
    // ★ 修复：单股分析也透传轮间间隔。此前前端 API 层丢弃 inter_round_sleep
    //   （composable 已传但这里未转发），用户配置的轮间间隔对单股分析不生效
    inter_round_sleep: req.inter_round_sleep ?? 0.6,
  }, { signal })
  return data
}

// ============================================================
// ★ v3 融合 ai-berkshire：深度研究（四大师价值投资框架）
// ============================================================
export interface DeepResearchScore {
  dimension: string
  score: number
  reason: string
  confidence: string
}
export interface DeepResearchResult {
  code: string
  name: string
  info_richness: { grade: string; note: string; rules?: string }
  scores: DeepResearchScore[]
  conclusion: string
  advice: Record<string, string>
  price_range: { buy_zone?: string; overvalued_zone?: string }
  risks: string[]
  confidence: string
  analysis_confidence_note?: string
  verify_list?: string[]
  quote: { price?: number; change_pct?: number; source?: string }
  cap_verify?: Record<string, unknown>
  masters?: Array<{ key: string; name: string; dimension: string }>
  provider?: string
  mocked?: boolean
  disclaimer?: string
}

/** 深度研究（四大师综合分析，可能 30-120s，用 httpLong） */
export async function runDeepResearch(
  code: string,
  name?: string,
): Promise<DeepResearchResult> {
  const { data } = await v2HttpLong.post<DeepResearchResult>('/deep-research', {
    code,
    name: name ?? '',
  })
  return data
}

/** 深度研究 Markdown 报告（导出用） */
export async function getDeepResearchReport(code: string): Promise<{ code: string; markdown: string; mocked: boolean }> {
  const { data } = await v2HttpLong.get<{ code: string; markdown: string; mocked: boolean }>(
    `/deep-research/${code}/report`,
  )
  return data
}

/** ai-berkshire 集成工具健康检查 */
export async function getBerkshireToolsHealth(): Promise<{ tools_dir: string; files: Record<string, boolean>; financial_rigor_importable: boolean }> {
  const { data } = await v2Http.get('/berkshire/tools-health')
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
    // ★ 迭代3 修复：区分"命名的 error 事件"（后端任务失败，带 data）与"连接错误"（无 data）。
    //   此前命名的 error 事件被忽略（readyState 非 CLOSED），任务失败详情丢失。
    const evt = e as MessageEvent
    if (evt.data) {
      try {
        const payload = JSON.parse(String(evt.data))
        onError?.(payload)
      } catch { /* ignore */ }
      return  // 命名 error 不关闭连接（等待 complete/done 或超时兜底）
    }
    // ★ Round 19 修复(C2)：仅当 EventSource 真正关闭时才调 onError + close；
    //   瞬时网络抖动时 readyState 为 CONNECTING，浏览器会自动重连，
    //   无条件 es.close() 会把可自愈的抖动升级成 SSE_FALLBACK → 二次完整分析。
    if (es.readyState === EventSource.CLOSED) {
      es.close()
      onError?.(e)
    }
  })

  return es
}

// ---- 后台任务启动 ----

export interface AsyncTaskResponse {
  task_id: string
  status: string
  message: string
}

/** 任务状态查询响应（GET /hot-analysis/tasks/{task_id}） */
export interface TaskStatusResponse {
  task_id: string
  status: string | null
  phase: string | null
  total_stocks: number
  completed_stocks: number
  current_stock: string
  current_round: number
  passed_count: number
  error?: string
  started_at?: string
  elapsed_seconds?: number
  result?: HotAnalysisRunResponse | null
  persist_failed?: boolean
  persist_error?: string
}

/** 轮询查询后台任务状态（刷新后恢复运行中任务用） */
export async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  const { data } = await http.get<TaskStatusResponse>(`/hot-analysis/tasks/${taskId}`)
  return data
}

/** 启动后台分析任务（非阻塞，返回 task_id） */
export async function startAsyncAnalysis(
  req: HotAnalysisRunRequest = {},
): Promise<AsyncTaskResponse> {
  const { data } = await httpLong.post<AsyncTaskResponse>('/hot-analysis/run-async', {
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
    max_concurrency: req.max_concurrency ?? 1,
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
  const { data } = await httpLong.post<PDFReportResponse>('/hot-analysis/pdf-report', {
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
  error: string | null   // ★ Round 13：可空
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
  const { data } = await httpLong.post<MarketReviewItem>('/hot-analysis/market-review/generate', {
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
// ★ 修复(C1)：后端 /hot-analysis/market-review/batch 直接返回 MarketReviewBatchResult[]
//   （market_review_service.batch_generate_recent_reviews 是 List[dict]），
//   旧类型 MarketReviewBatchResponse 声明为对象 → 前端表格永远空白。
export type MarketReviewBatchResponse = MarketReviewBatchResult[]
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

// ============================================================
// 技术分析（波峰波谷 / 指标 / AI 解读）—— 从 stock_analysis/v1 移植
// ============================================================

export interface PeakTroughSegment {
  type: 'decline' | 'rebound'
  from_date: string
  to_date: string
  start_price: number
  end_price: number
  change_pct: number
  from_peak: boolean
}

export interface PeakTroughSummary {
  max_decline_pct: number
  max_rebound_pct: number
  current_vs_high_pct: number
  current_vs_year_start_pct: number
  current_price: number
  year_start_price: number
  segment_count: number
  decline_count: number
  rebound_count: number
}

export interface PeakTroughResponse {
  stock_code: string
  stock_name: string
  period: string
  year_high: { date: string; price: number }
  year_start_date: string
  analysis_date: string
  segments: PeakTroughSegment[]
  summary: PeakTroughSummary
  effective_window: number
  is_mock?: boolean
}

export interface IndicatorPoint {
  time: string
  value: number
}

export interface IndicatorResponse {
  indicator: 'MACD' | 'BOLL' | 'KDJ'
  params: Record<string, number>
  series: Record<string, IndicatorPoint[]>
  code: string
  period: string
  is_mock?: boolean
}

export interface LLMInsightResponse {
  provider: string
  model: string
  text: string
  mocked: boolean
  is_mock?: boolean
}

/** 波峰波谷分析 */
export async function analyzePeakTrough(req: {
  code: string
  period?: string
  days?: number
  window?: number
}): Promise<PeakTroughResponse> {
  const { data } = await v2HttpLong.post<PeakTroughResponse>('/technical/peak-trough', {
    code: req.code,
    period: req.period ?? 'daily',
    days: req.days ?? 365,
    window: req.window ?? 3,
  })
  return data
}

/** 技术指标计算 */
export async function getIndicator(
  code: string,
  indicator: 'MACD' | 'BOLL' | 'KDJ',
  period: string = 'daily',
  days: number = 120,
): Promise<IndicatorResponse> {
  const { data } = await v2Http.get<IndicatorResponse>('/technical/indicators', {
    params: { code, indicator, period, days },
  })
  return data
}

/** AI 解读（波峰波谷 + LLM） */
export async function getLLMInsight(req: {
  code: string
  period?: string
  days?: number
  window?: number
  extra_prompt?: string
}): Promise<LLMInsightResponse> {
  const { data } = await v2HttpLong.post<LLMInsightResponse>('/technical/llm-insight', {
    code: req.code,
    period: req.period ?? 'daily',
    days: req.days ?? 365,
    window: req.window ?? 3,
    extra_prompt: req.extra_prompt,
  })
  return data
}
