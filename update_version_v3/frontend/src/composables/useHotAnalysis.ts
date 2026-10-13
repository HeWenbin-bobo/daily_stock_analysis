/**
 * 热分析核心逻辑 composable
 *
 * Phase 1: 过滤规则、单股分析、配置记忆
 * Phase 2: SSE 进度推送、CSV/Excel 导出
 * Phase 3: 后台任务模式
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import {
  runHotAnalysis,
  previewHotAnalysis,
  analyzeSingleStock,
  startAsyncAnalysis,
  createProgressStream,
  cancelTask,
  type HotAnalysisRunResponse,
  type HotAnalysisPreviewResponse,
  type HotAnalysisStockResult,
  type FilterConfig,
  type TaskProgress,
  type SingleStockResponse,
} from '../api/hotAnalysis'
import { buildPlainText, extractErrorMessage } from '../utils/formatters'
import { exportCSV, exportCSVForExcel } from '../utils/exporters'
import { loadConfig, saveConfig } from '../utils/config'

// ★ 迭代3 修复(M1)：统一判断"用户取消"（axios abort 抛 CanceledError 而非 DOMException AbortError，
//   此前取消同步分析会误报"后端未启动"）
function _isAbortError(e: unknown): boolean {
  if (e instanceof DOMException && e.name === 'AbortError') return true
  const anyE = e as { name?: string; code?: string; message?: string }
  return anyE?.code === 'ERR_CANCELED' || anyE?.name === 'CanceledError' || anyE?.name === 'AbortError'
}

// 阶段类型
export type Phase = 'idle' | 'previewing' | 'previewed' | 'analyzing' | 'done'

// 排序类型
export type SortKey = 'code' | 'name' | 'avg_score' | 'final_advice'

// 视图模式
export type ViewMode = 'analysis' | 'history' | 'compare' | 'single' | 'watchlist' | 'templates' | 'scheduled' | 'review' | 'viz' | 'technical' | 'deepresearch'

/** 单股分析任务（并发） */
export interface SingleTask {
  id: string
  code: string  // 用户输入（可重复）
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  result: SingleStockResponse | null
  error: string | null
  controller: AbortController
}

export function useHotAnalysis() {
  // ---- 状态 ----
  const phase = ref<Phase>('idle')
  const loading = ref(false)
  const error = ref<string | null>(null)
  const result = ref<HotAnalysisRunResponse | null>(null)
  const partialResult = ref<HotAnalysisRunResponse | null>(null)
  const cancelledWithPartial = ref(false)
  const preview = ref<HotAnalysisPreviewResponse | null>(null)
  const progressTip = ref('点击下方按钮开始全流程分析')

  // 视图模式
  const viewMode = ref<ViewMode>('analysis')

  // 排序
  const sortKey = ref<SortKey>('avg_score')
  const sortOrder = ref<'asc' | 'desc'>('desc')

  // 复制/导出反馈
  const copyOk = ref(false)
  const exportOk = ref(false)
  const csvOk = ref(false)
  const excelOk = ref(false)

  // 基础配置
  const selectedMaxRuntime = ref<number>(180)
  const selectedMaxIntersection = ref<number>(10)

  // 分析轮数 & 轮间间隔（之前硬编码 rounds=3 / inter_round_sleep=0.6，用户不能调）
  const rounds = ref<number>(3)
  const interRoundSleep = ref<number>(0.6)

  // 过滤规则
  const minScore = ref<number>(60)
  const allowedAdvice = ref<string[]>(['买入', '加仓', '持有', 'BUY', 'HOLD', 'ADD'])
  const excludeKeywords = ref<string[]>(['卖出', '减仓', 'SELL', 'REDUCE'])
  const requireAllRounds = ref<boolean>(true)

  // LLM 超时
  const llmTimeout = ref<number>(180)

  // ★ Round 16：市场分析并发分析股票数（1=串行）
  const maxConcurrency = ref<number>(2)

  // SSE 进度
  const taskProgress = ref<TaskProgress | null>(null)
  const taskId = ref<string | null>(null)
  let eventSource: EventSource | null = null

  // 单股分析：支持并发
  // 每只股票一个独立任务，可独立取消；总并发数受 singleConcurrency 限制
  // ★ 修复（round 3 audit）：之前用 once-only flag 启动 N 个 worker 然后让其在
  //   队列空时永久退出，第二次提交会被静默丢弃。现改为按需补齐：
  //   每次 submitSingle 调用 ensureSingleWorkers 都会计算"差量"并启动缺的 worker。
  // ★ 修复（round 5 audit）：worker 永真循环在组件卸载/HMR 时不会停止，会持续 poll
  //   模块级 singleQueue 并跨实例抢任务；改为带停止标志的循环，onBeforeUnmount 触发
  //   全部 worker 退出；同时把模块级 singleQueue 改为实例级 ref 之外的纯数组（仍是
  //   闭包内私有），避免 HMR 跨实例共享导致任务丢失。
  const singleTasks = ref<SingleTask[]>([])
  const singleConcurrency = ref<number>(3)  // 同时最多 N 只股票 LLM 调用，避免限流（用户可调）
  const singleQueue: SingleTask[] = []  // 闭包内私有队列
  let runningWorkers = 0  // 当前活跃 worker 数（含"等待队列空但仍存活"的）
  let workersStopped = false  // onBeforeUnmount 时翻 true，worker 立刻退出循环

  function ensureSingleWorkers() {
    // 单并发上限随用户拖动 slider 实时变；补齐差量；不杀现存
    if (workersStopped) return  // 已卸载，禁止再启动
    const target = Math.max(1, singleConcurrency.value)
    while (runningWorkers < target) {
      runningWorkers += 1
      void runSingleWorker().finally(() => {
        runningWorkers -= 1
      })
    }
  }

  async function runSingleWorker() {
    // ★ 迭代1 修复(M2)：队列空超过 3s → worker 自行退出（runningWorkers 由调用方 .finally 递减），
    //   下次 submitSingle 会自动补齐。此前空闲 worker 每 200ms 无限空转轮询，且降低并发不回收多余 worker。
    let idleSince = 0
    // worker 检查 workersStopped：true 时立刻退出循环（HMR / 卸载时不留 phantom worker）
    while (!workersStopped) {
      // ★ 修复(M3)：用户把并发调小（如 3→1）时，现存 worker 在空闲状态应提前退出，
      //   否则继续 3 路并发直到 3s 空闲超时，可能触发 LLM 限流。
      //   注意只在队列空时检查（运行中的任务不打断，避免任务被丢弃）。
      if (singleQueue.length === 0 && runningWorkers > Math.max(1, singleConcurrency.value)) {
        return
      }
      const next = singleQueue.shift()
      if (!next) {
        if (!idleSince) idleSince = Date.now()
        if (Date.now() - idleSince > 3000) return
        await new Promise<void>((resolve) => setTimeout(resolve, 200))
        continue
      }
      idleSince = 0
      // 拿到任务后也再 check 一次：避免已卸载后还跑了 LLM
      if (workersStopped) return
      await runSingleTask(next)
    }
  }

  function parseCodes(input: string): string[] {
    return input
      .split(/[\s,，;；\n\r\t]+/)
      .map((s) => s.trim())
      .filter(Boolean)
  }

  function makeSingleTask(code: string): SingleTask {
    return {
      id: `single_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      code,
      status: 'pending',
      result: null,
      error: null,
      controller: new AbortController(),
    }
  }

  // ★ Round 5 a11y/响应式：单股任务用不可变 map() 替换数组，保证 Vue3 响应式
  //   一定触发（过去直接 live.status = ... 在某些时序下不更新）
  function getSingleTask(id: string): SingleTask | undefined {
    return singleTasks.value.find((t) => t.id === id)
  }
  function patchSingleTask(id: string, patch: Partial<SingleTask>): void {
    if (!singleTasks.value.some((t) => t.id === id)) return
    singleTasks.value = singleTasks.value.map((t) =>
      t.id === id ? { ...t, ...patch } : t,
    )
  }

  async function runSingleTask(task: SingleTask) {
    // 防御：进入函数时再查一次，防止 cancelAll / removeSingle 在 push 之后已经把它清掉
    const initial = getSingleTask(task.id)
    if (!initial) return  // 已被移除，跳过
    patchSingleTask(task.id, { status: 'running' })
    try {
      const data = await analyzeSingleStock(
        {
          code: initial.code,
          rounds: rounds.value,
          simple_report: true,
          // ★ 修复(H5)：旧实现只传 code/rounds/simple_report，后端默认 llm_timeout=180、
          //   无 inter_round_sleep → 用户把 LLM 超时调成 300s 对单股完全不生效。
          llm_timeout_seconds: llmTimeout.value,
          inter_round_sleep: interRoundSleep.value,
        },
        initial.controller.signal,
      )
      // 取消可能在请求过程中触发；如已取消，丢弃结果（用 initial 锁住 signal 引用）
      if (initial.controller.signal.aborted) {
        patchSingleTask(task.id, { status: 'cancelled' })
        return
      }
      const okStatus = data.status === 'failed' ? 'failed' : 'done'
      patchSingleTask(task.id, {
        result: data,
        status: okStatus,
        error: data.status === 'failed' ? data.error || '分析失败' : null,
      })
    } catch (e: unknown) {
      if (_isAbortError(e)) {
        patchSingleTask(task.id, { status: 'cancelled' })
        return
      }
      const err = e as { message?: string; response?: { data?: { detail?: string } } }
      patchSingleTask(task.id, {
        status: 'failed',
        error:
          err?.response?.data?.detail ||
          err?.message ||
          '请求失败，请确认后端已启动（端口 8001）',
      })
    }
  }

  async function submitSingle(codes: string[]) {
    if (!codes.length) return
    // 去重：同 code 若已 pending/running/done（成功），不重复加入
    const existingCodes = new Set(
      singleTasks.value
        .filter((t) => t.status === 'pending' || t.status === 'running' || t.status === 'done')
        .map((t) => t.code),
    )
    const newCodes = codes.filter((c) => !existingCodes.has(c))
    if (!newCodes.length) return
    const newTasks = newCodes.map(makeSingleTask)
    // 加入响应式列表（UI 可见），并 push 到全局 worker 队列
    singleTasks.value = [...singleTasks.value, ...newTasks]
    for (const t of newTasks) {
      singleQueue.push(t)
    }
    // 幂等启动：只跑一次
    ensureSingleWorkers()
  }

  function cancelSingleTask(id: string) {
    const t = getSingleTask(id)
    if (!t) return
    if (t.status === 'pending' || t.status === 'running') {
      t.controller.abort()
      patchSingleTask(id, { status: 'cancelled' })
    }
  }

  function removeSingleTask(id: string) {
    cancelSingleTask(id)
    singleTasks.value = singleTasks.value.filter((x) => x.id !== id)
  }

  function cancelAllSingle() {
    // 不可变 map：保证列表内每个 pending/running 都翻成 cancelled，触发表格刷新
    let touched = false
    const next = singleTasks.value.map((t) => {
      if (t.status === 'pending' || t.status === 'running') {
        t.controller.abort()
        touched = true
        return { ...t, status: 'cancelled' as const }
      }
      return t
    })
    if (touched) singleTasks.value = next
  }

  function clearFinishedSingle() {
    singleTasks.value = singleTasks.value.filter(
      (t) => t.status === 'pending' || t.status === 'running',
    )
  }

  // AbortController
  let abortController: AbortController | null = null
  // 缓存最近一次启动的 stopTimer，使 onCancelAnalysis 的 5s 兜底也能关闭计时器
  let currentStopTimer: (() => void) | null = null

  // ★ 迭代6 修复：持久化最近一次市场分析结果到 localStorage，刷新后自动恢复。
  //   否则 Vite HMR 全量刷新 / 浏览器标签页内存回收等会导致结果丢失（用户"没及时查看就没了"）。
  const LAST_RESULT_KEY = 'hot-analysis-last-result'
  const LAST_RESULT_TTL_MS = 24 * 60 * 60 * 1000  // 24h 内可恢复

  function _saveLastResult(data: HotAnalysisRunResponse): void {
    try {
      localStorage.setItem(LAST_RESULT_KEY, JSON.stringify({ result: data, savedAt: Date.now() }))
    } catch { /* 隐私模式 / 配额满时忽略 */ }
  }
  function _loadLastResult(): { result: HotAnalysisRunResponse; savedAt: number } | null {
    try {
      const raw = localStorage.getItem(LAST_RESULT_KEY)
      if (!raw) return null
      const parsed = JSON.parse(raw)
      if (!parsed?.result || Date.now() - (parsed.savedAt || 0) > LAST_RESULT_TTL_MS) return null
      return parsed
    } catch { return null }
  }
  function _clearLastResult(): void {
    try { localStorage.removeItem(LAST_RESULT_KEY) } catch { /* ignore */ }
  }

  // ---- 初始化：从 localStorage 加载配置 ----
  onMounted(() => {
    const saved = loadConfig()
    if (saved) {
      if (saved.maxRuntime) selectedMaxRuntime.value = saved.maxRuntime
      if (saved.maxIntersection) selectedMaxIntersection.value = saved.maxIntersection
      if (saved.minScore != null) minScore.value = saved.minScore
      if (saved.allowedAdvice) allowedAdvice.value = saved.allowedAdvice
      if (saved.excludeKeywords) excludeKeywords.value = saved.excludeKeywords
      if (saved.requireAllRounds != null) requireAllRounds.value = saved.requireAllRounds
      if (saved.llmTimeout) llmTimeout.value = saved.llmTimeout
      if (saved.rounds) rounds.value = saved.rounds
      if (saved.interRoundSleep != null) interRoundSleep.value = saved.interRoundSleep
      if (saved.singleConcurrency) singleConcurrency.value = saved.singleConcurrency
      if (saved.maxConcurrency) maxConcurrency.value = saved.maxConcurrency
    }
    // ★ 迭代6：恢复上次持久化的市场分析结果（Vite HMR / 标签页回收等刷新后不丢）
    const savedResult = _loadLastResult()
    if (savedResult && savedResult.result && savedResult.result.status !== 'failed') {
      result.value = savedResult.result
      phase.value = 'done'
      hasFinalized = true
      const savedTime = new Date(savedResult.savedAt)
      const timeStr = `${String(savedTime.getHours()).padStart(2, '0')}:${String(savedTime.getMinutes()).padStart(2, '0')}`
      progressTip.value =
        savedResult.result.passed_stock_count > 0
          ? `上次分析结果（${timeStr}）：${savedResult.result.passed_stock_count} 只股票通过`
          : `上次分析结果（${timeStr}）：本轮未发现符合条件的股票`
    }
  })

  // ★ Round 16：单股并发设置移到单股 tab 后，在此持久化到 localStorage
  watch(singleConcurrency, (v) => saveConfig({ singleConcurrency: v }))

  // ---- 过滤配置 ----
  const filterConfig = computed<FilterConfig>(() => ({
    min_score: minScore.value,
    allowed_advice: allowedAdvice.value,
    exclude_keywords: excludeKeywords.value,
    require_all_rounds: requireAllRounds.value,
  }))

  // ---- 排序后的结果 ----
  const sortedResults = computed(() => {
    if (!result.value) return []
    const list = [...result.value.results]
    const dir = sortOrder.value === 'asc' ? 1 : -1
    return list.sort((a, b) => {
      let av: number | string = 0
      let bv: number | string = 0
      if (sortKey.value === 'code') {
        av = a.code; bv = b.code
      } else if (sortKey.value === 'name') {
        av = a.name; bv = b.name
      } else if (sortKey.value === 'avg_score') {
        av = a.avg_score; bv = b.avg_score
      } else if (sortKey.value === 'final_advice') {
        av = a.final_advice; bv = b.final_advice
      }
      if (typeof av === 'string' && typeof bv === 'string') {
        return av.localeCompare(bv) * dir
      }
      // 数值相减需用括号保证整体被 dir 缩放（避免被解析为 av - bv * dir）
      return ((av as number) - (bv as number)) * dir
    })
  })

  // ---- 失败的榜单 ----
  const failedSources = computed(() => {
    const list = result.value?.hot_lists_status ?? {}
    return Object.entries(list)
      .filter(([, s]) => s === 'failed')
      .map(([k]) => k)
  })

  // ---- 排序切换 ----
  function toggleSort(key: SortKey) {
    if (sortKey.value === key) {
      sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
    } else {
      sortKey.value = key
      sortOrder.value = key === 'avg_score' ? 'desc' : 'asc'
    }
  }

  // ---- 阶段 1：预览 ----
  async function onRun(): Promise<void> {
    // loading 同时覆盖 previewing / analyzing：避免点"重新分析"或重复触发
    if (loading.value || phase.value === 'previewing') return
    loading.value = true
    phase.value = 'previewing'
    preview.value = null
    result.value = null
    partialResult.value = null
    cancelledWithPartial.value = false
    error.value = null
    _clearLastResult()  // ★ 迭代6：开始新预览时清掉上次持久化结果
    progressTip.value = '正在抓取四榜（东方财富 / 雪球 / 同花顺 / 全市场活跃）…'

    // ★ Round 19 修复(H1)：预览请求支持取消 + 硬超时（四榜抓取慢时最坏 180s 兜底），
    //   避免 loading 无限锁死配置面板且无法中断
    abortController = new AbortController()
    const previewTimeout = setTimeout(() => abortController?.abort(), 180000)

    try {
      const data = await previewHotAnalysis({
        hot_list_n: 200,
        max_intersection: selectedMaxIntersection.value,
        rounds: rounds.value,
        inter_round_sleep: interRoundSleep.value,
        simple_report: true,
        max_concurrency: maxConcurrency.value,
        ...filterConfig.value,
      }, abortController.signal)
      if (data.status === 'failed') {
        error.value = data.error || '预览失败'
        progressTip.value = '抓榜失败'
        phase.value = 'idle'
        return
      }
      preview.value = data
      phase.value = 'previewed'
      progressTip.value = `准备分析 ${data.target_count} 只股票`
    } catch (e: unknown) {
      if (_isAbortError(e)) {
        error.value = '预览已取消或超时（180s），请重试'
        progressTip.value = '抓榜已取消'
      } else {
        error.value = extractErrorMessage(e, '请求失败，请确认后端已启动（端口 8001）')
        progressTip.value = '抓榜失败'
      }
      phase.value = 'idle'
    } finally {
      clearTimeout(previewTimeout)
      loading.value = false
      abortController = null
    }
  }

  const cancelled = ref(false)
  let cancelFallbackTimer: ReturnType<typeof setTimeout> | null = null
  let hasFinalized = false  // 防止 partial / complete / catch / finally 重复覆盖已显示结果
  // ★ Round 18：代数计数器。每次启动新任务 +1，SSE 回调捕获启动时的 gen；
  //   gen 变了说明已有新任务，旧任务的 progress/partial/complete 一律忽略，
  //   避免"取消后再次开始"时旧任务 partial 覆盖新任务 UI（续着上一次任务）。
  let runGeneration = 0

  function resetFinalizedFlag() {
    hasFinalized = false
  }

  // 统一的应用入口：partial 与 complete 共享同一路径，避免多事件相互覆盖
  function applyAnalysisResult(
    data: HotAnalysisRunResponse,
    isPartial: boolean,
  ) {
    // ★ 修复：若已完成（complete 或 partial）已应用，stale partial/complete 事件一律不再覆盖，
    //  否则会出现"分析完成" → "分析已取消"的状态回退
    if (hasFinalized) {
      // complete 已经应用，partial 不再覆盖
      return
    }
    if (isPartial) {
      partialResult.value = data
      cancelledWithPartial.value = true
      // 用 partial 覆盖 result，模板中 v-if="result" 即可渲染（保持向后兼容）
      result.value = data
      _saveLastResult(data)  // ★ 迭代6：持久化，刷新后恢复
      // ★ 清掉 onProgress 留下的"正在分析 通富微电(...)" 文案，避免用户看到冲突信息
      progressTip.value =
        data.passed_stock_count > 0
          ? `分析已取消：已展示 ${data.passed_stock_count} 只已完成股票`
          : '分析已取消：尚未有股票通过过滤'
    } else {
      // 完整结果：清掉 cancelled 标记，避免"分析已取消"警告条误显示
      partialResult.value = null
      cancelledWithPartial.value = false
      result.value = data
      _saveLastResult(data)  // ★ 迭代6：持久化，刷新后恢复
      if (data.status === 'failed') {
        error.value = data.error || '后端返回 failed 状态'
      }
      // cancelled / timeout 也算一种"完成"——不归为 failed，但保留 result 让用户看到已抓取部分
      if (data.status === 'ok') {
        progressTip.value =
          data.passed_stock_count > 0
            ? `分析完成：共 ${data.passed_stock_count} 只股票通过过滤`
            : '分析完成，但本轮未发现符合条件的股票'
      }
    }
    phase.value = 'done'
    loading.value = false
    hasFinalized = true
  }

  function clearCancelFallback() {
    if (cancelFallbackTimer) {
      clearTimeout(cancelFallbackTimer)
      cancelFallbackTimer = null
    }
  }

  // ---- 阶段 2：确认分析（SSE 模式） ----
  async function onConfirmAnalysis(
    startTimer?: () => void,
    stopTimer?: () => void,
  ): Promise<void> {
    // ★ 去重：避免重复连点"确认开始分析"启多个后台任务
    if (loading.value || phase.value === 'analyzing' || !preview.value) return
    // ★ Round 18：再次开始时废弃上一次的 SSE 连接与回调——取消后旧任务仍在后台跑，
    //   若不隔离，其 partial 会覆盖新任务 UI（续着上一次任务继续）。
    runGeneration += 1
    const gen = runGeneration
    if (eventSource) {
      eventSource.close()  // close 幂等；置 null 交给 finally 统一处理（避免 TS 窄化成 never）
    }
    // 容错：模板里如果没传 timer，使用 no-op
    const safeStart = startTimer ?? (() => undefined)
    const safeStop = stopTimer ?? (() => undefined)
    cancelled.value = false
    cancelledWithPartial.value = false
    partialResult.value = null
    resetFinalizedFlag()
    clearCancelFallback()
    phase.value = 'analyzing'
    result.value = null
    error.value = null
    loading.value = true
    _clearLastResult()  // ★ 迭代6：开始新分析时清掉上次持久化结果
    safeStart()
    currentStopTimer = safeStop
    progressTip.value = `正在对 ${preview.value.target_count} 只股票做三轮 LLM 分析…`
    try {
      // 先尝试 SSE 后台任务模式
      const taskResp = await startAsyncAnalysis({
        hot_list_n: 200,
        max_intersection: selectedMaxIntersection.value,
        rounds: rounds.value,
        max_runtime_seconds: selectedMaxRuntime.value * 60,
        inter_round_sleep: interRoundSleep.value,
        simple_report: true,
        llm_timeout_seconds: llmTimeout.value,
        max_concurrency: maxConcurrency.value,
        ...filterConfig.value,
      })

      taskId.value = taskResp.task_id
      progressTip.value = '后台任务已启动，实时推送进度…'

      // 监听 SSE 进度（带超时保护）
      // ★ 迭代5 修复：无进展超时改为固定 5 分钟（此前 = maxRuntime 分钟，最高 30h → 兜底实际失效）。
      //   正常分析每只股票/每轮都会推 progress，5 分钟无进展说明 SSE 卡死/后端异常。
      const sseTimeoutMs = 5 * 60 * 1000
      let lastProgressTime = Date.now()
      const runResult = await new Promise<HotAnalysisRunResponse>((resolve, reject) => {
        const timeoutTimer = setInterval(() => {
          if (gen !== runGeneration) {
            // 已有新任务，旧 timer 退出（不再关掉新任务的 eventSource / reject 旧 Promise）
            clearInterval(timeoutTimer)
            return
          }
          if (Date.now() - lastProgressTime > sseTimeoutMs) {
            clearInterval(timeoutTimer)
            if (eventSource) { eventSource.close(); eventSource = null }
            reject(new Error('SSE_TIMEOUT'))
          }
        }, 1000)

        eventSource = createProgressStream(
          taskResp.task_id,
          (progress) => {
            if (gen !== runGeneration) return
            lastProgressTime = Date.now()
            taskProgress.value = progress
            if (hasFinalized) return  // 防止 partial/complete 之后 progress 事件再覆盖 progressTip
            if (progress.phase === 'analyzing') {
              progressTip.value = `正在分析 ${progress.current_stock}（${progress.completed_stocks}/${progress.total_stocks}）`
            } else if (progress.phase === 'fetching') {
              progressTip.value = '正在抓取四榜数据…'
            }
          },
          (err) => {
            if (gen !== runGeneration) return
            clearInterval(timeoutTimer)
            // ★ round 6 audit：保留 warn 用于本地排障，但生产无 devtools 时用户不可见；
            //   SSE 失败回退到同步模式是设计行为，不需要弹窗
            if (import.meta.env.DEV) console.warn('SSE 连接失败，回退到同步模式', err)
            reject(new Error('SSE_FALLBACK'))
          },
          (completeResult, isPartial) => {
            if (gen !== runGeneration) return
            clearInterval(timeoutTimer)
            if (isPartial) {
              applyAnalysisResult(completeResult, true)
              safeStop()
              resolve(completeResult)
            } else {
              // complete: 仅在尚未 finalize（partial 还没到）时应用
              if (!hasFinalized) {
                applyAnalysisResult(completeResult, false)
              }
              safeStop()
              resolve(completeResult)
            }
          },
        )
      })

      // 兜底：防止 complete 已 resolve 但 applyAnalysisResult 没跑到（极端 race）
      if (!hasFinalized) {
        applyAnalysisResult(runResult, runResult.status === 'cancelled' || runResult.status === 'timeout')
        // cancelled 走 partial 文案；timeout 同理
        if (runResult.status === 'cancelled' || runResult.status === 'timeout') {
          cancelledWithPartial.value = true
          partialResult.value = runResult
        }
      }
    } catch (e: unknown) {
      // SSE 失败时回退到同步模式
      if (e instanceof Error && (e.message === 'SSE_FALLBACK' || e.message === 'SSE_TIMEOUT')) {
        if (e.message === 'SSE_TIMEOUT') {
          progressTip.value = 'SSE 连接超时，回退到同步模式…'
        }
        await runSyncAnalysis(safeStart, safeStop)
        return
      }
      if (_isAbortError(e)) {
        // 同步 abort 模式：后端不会推 partial，只能拿到已抓取的 progressTip
        if (!hasFinalized) {
          progressTip.value = '已取消分析'
          loading.value = false
          safeStop()
          phase.value = 'idle'
        }
      } else if (e instanceof Error && e.message === 'CANCELLED') {
        // 兜底 reject（CANCELLED path）：不应在 SSE 模式下触发，触发则走兜底
        if (taskId.value) {
          try { await cancelTask(taskId.value) } catch { /* ignore */ }
        }
        if (!hasFinalized) {
          progressTip.value = '已取消分析'
          loading.value = false
          safeStop()
          phase.value = 'idle'
        }
      } else {
        error.value = extractErrorMessage(e, '请求失败，请确认后端已启动（端口 8001）')
        progressTip.value = '分析失败'
      }
    } finally {
      loading.value = false
      abortController = null
      taskProgress.value = null
      taskId.value = null
      if (eventSource) {
        eventSource.close()
        eventSource = null
      }
      safeStop()
      clearCancelFallback()
    }
  }

  // ---- 同步分析回退 ----
  async function runSyncAnalysis(startTimer: () => void, stopTimer: () => void): Promise<void> {
    abortController = new AbortController()
    progressTip.value = '使用同步模式分析…'

    try {
      const data = await runHotAnalysis(
        {
          hot_list_n: 200,
          max_intersection: selectedMaxIntersection.value,
          rounds: rounds.value,
          max_runtime_seconds: selectedMaxRuntime.value * 60,
          inter_round_sleep: interRoundSleep.value,
          simple_report: true,
          llm_timeout_seconds: llmTimeout.value,
          max_concurrency: maxConcurrency.value,
          ...filterConfig.value,
        },
        abortController.signal,
      )
      applyAnalysisResult(data, false)
      stopTimer()
    } catch (e: unknown) {
      if (_isAbortError(e)) {
        if (!hasFinalized) {
          // ★ 修复(M1)：同步模式取消后旧实现只置 progressTip + phase=idle，
          //   无"已取消"提示条（与 SSE 取消的 UX 不一致）。补 cancelledWithPartial。
          progressTip.value = '已取消分析'
          cancelledWithPartial.value = true
          phase.value = 'idle'
        }
      } else {
        error.value = extractErrorMessage(e, '请求失败，请确认后端已启动（端口 8001）')
        progressTip.value = '分析失败'
      }
    } finally {
      loading.value = false
      abortController = null
      stopTimer()
      clearCancelFallback()
    }
  }

  // ---- 单股分析：占位符，实际实现见 submitSingle / cancelSingleTask / cancelAllSingle ----

  // ---- 取消分析 ----
  async function onCancelAnalysis() {
    // ★ 重要：取消时绝对不能递增 runGeneration。后端 _run_analysis_background
    //   在循环边界 break 后会专门推一条 partial 事件给前端，SSE 监听者
    //   onComplete(_, isPartial=true) → applyAnalysisResult(data, true) →
    //   cancelledWithPartial=true → 模板渲染"分析已取消"警告条 + 已完成结果卡片。
    //   若在此递增 runGeneration，SSE onComplete 开头的
    //   `if (gen !== runGeneration) return` 会把后端的 partial 静默丢弃，
    //   用户等待数分钟的成果不可见。防"晚到 partial 覆盖已 finalize 结果"
    //   靠的是 applyAnalysisResult 开头的 `if (hasFinalized) return` 守卫，
    //   与 runGeneration 无关。
    //   "取消后立刻重启新分析"场景下，旧任务晚到 partial 不污染新任务 UI
    //   靠 onConfirmAnalysis 中的 runGeneration += 1（gen 变了 → 旧回调丢弃）。
    cancelled.value = true
    if (abortController) {
      // 同步模式：把请求 abort；后端会忽略但客户端不会等
      abortController.abort()
    }
    // SSE 模式：不要在这里 close eventSource。
    // 后端 _run_analysis_background 会在循环边界 break → 推 partial → 推 done，
    // SSE 监听者 onComplete(_, isPartial=true) 调用 applyAnalysisResult 进入 done 阶段。
    if (taskId.value) {
      try { await cancelTask(taskId.value) } catch { /* ignore */ }
    }
    // 兜底：仅当 SSE 因网络/异常断开、且 5s 内后端未能成功推 partial 时，
    // 给用户"已取消"提示。不要重置已经 finalize 的 result/partialResult，
    // 也不要把 phase 切回 idle（会清掉已展示的 summary 卡片）。
    clearCancelFallback()
    cancelFallbackTimer = setTimeout(() => {
      if (hasFinalized) return
      if (!loading.value) return  // 已自然结束
      progressTip.value = '已取消分析'
      loading.value = false
      currentStopTimer?.()
      phase.value = 'idle'
      cancelFallbackTimer = null
    }, 5000)
  }

  // ---- 取消预览 ----
  function onCancelPreview() {
    // ★ Round 19 修复(H1)：真正 abort 预览请求，否则后端四榜抓取仍继续、前端 loading 卡住
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    preview.value = null
    phase.value = 'idle'
    loading.value = false
    progressTip.value = '点击下方按钮开始全流程分析'
  }

  // ★ Round 6：收集所有 setTimeout feedback id 用于 unmount 清理
  let _feedbackTimers: ReturnType<typeof setTimeout>[] = []
  function _flashFeedback(ref: { value: boolean }, ms = 1800): void {
    ref.value = true
    const t = setTimeout(() => {
      ref.value = false
      _feedbackTimers = _feedbackTimers.filter((x) => x !== t)
    }, ms)
    _feedbackTimers.push(t)
  }

  // ---- 复制 ----
  async function onCopy() {
    if (!result.value) return
    const text = buildPlainText(result.value, sortedResults.value)
    if (!text) return
    let ok = false
    try {
      await navigator.clipboard.writeText(text)
      ok = true
    } catch {
      // 兜底：execCommand（iOS 私有模式可能也失败 → catch 到这里）
      try {
        const ta = document.createElement('textarea')
        ta.value = text
        document.body.appendChild(ta)
        ta.select()
        ok = document.execCommand('copy')
        document.body.removeChild(ta)
      } catch (e: unknown) {
        if (import.meta.env.DEV) console.warn('Copy fallback failed:', e)
      }
    }
    if (ok) {
      _flashFeedback(copyOk)
    } else {
      error.value = '复制失败：浏览器拒绝访问剪贴板'
    }
  }

  // ---- 导出 TXT ----
  function onExport() {
    if (!result.value) return
    const text = buildPlainText(result.value, sortedResults.value)
    if (!text) return
    try {
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      try {
        const a = document.createElement('a')
        a.href = url
        a.download = `hot-analysis-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
      } finally {
        URL.revokeObjectURL(url)
      }
      _flashFeedback(exportOk)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      if (import.meta.env.DEV) console.warn('onExport failed:', e)
      error.value = `TXT 导出失败：${msg}`
    }
  }

  // ---- 导出 CSV ----
  function onExportCSV() {
    if (!result.value) return
    try {
      exportCSV(result.value, sortedResults.value)
      _flashFeedback(csvOk)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      if (import.meta.env.DEV) console.warn('onExportCSV failed:', e)
      error.value = `CSV 导出失败：${msg}`
    }
  }

  // ---- 导出 CSV (Excel 兼容) ----
  async function onExportExcel() {
    if (!result.value) return
    try {
      await exportCSVForExcel(result.value, sortedResults.value)
      _flashFeedback(excelOk)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      if (import.meta.env.DEV) console.warn('onExportExcel failed:', e)
      error.value = `Excel CSV 导出失败：${msg}`
    }
  }

  // ---- 切换视图 ----
  function setViewMode(mode: ViewMode) {
    viewMode.value = mode
    // ★ 修复：不再清掉 error，避免切换 tab 时丢失失败信息；
    // 用户可通过明确的 dismiss 行为 / 下一次分析触发清理
    if (mode !== 'analysis') {
      // 切视图时不重置 result/partialResult（避免失去已显示的取消后部分结果），
      // 但清掉"分析已取消"警告条标记——离开分析页后该标记失效
      cancelledWithPartial.value = false
    }
  }

  // ★ 修复：组件卸载时清理 SSE / interval / 异步任务，避免 HMR / 路由切换后泄漏
  onBeforeUnmount(() => {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    if (cancelFallbackTimer) {
      clearTimeout(cancelFallbackTimer)
      cancelFallbackTimer = null
    }
    // ★ Round 6：清理 export feedback timers（避免 unmount 后 setTimeout 写已 unmount ref）
    _feedbackTimers.forEach((t) => clearTimeout(t))
    _feedbackTimers = []
    currentStopTimer = null
    // ★ round 5 audit：翻停止标志让所有 runSingleWorker 退出；同时取消未完成任务
    workersStopped = true
    singleTasks.value.forEach((t) => {
      try { t.controller.abort() } catch { /* ignore */ }
    })
    // ★ Round 5：immutable update 触发响应式
    singleTasks.value = singleTasks.value.map((t) =>
      t.status === 'pending' || t.status === 'running'
        ? { ...t, status: 'cancelled' as const }
        : t,
    )
    // 清空闭包内队列，避免新挂载实例意外消费旧任务
    singleQueue.length = 0
  })

  return {
    // 状态
    phase,
    loading,
    error,
    result,
    partialResult,
    cancelledWithPartial,
    preview,
    progressTip,
    viewMode,
    // 排序
    sortKey,
    sortOrder,
    sortedResults,
    toggleSort,
    // 失败源
    failedSources,
    // 基础配置
    selectedMaxRuntime,
    selectedMaxIntersection,
    // 分析轮数 / 轮间间隔（用户可调）
    rounds,
    interRoundSleep,
    // 过滤规则
    minScore,
    allowedAdvice,
    excludeKeywords,
    requireAllRounds,
    filterConfig,
    // LLM 超时
    llmTimeout,
    // ★ Round 16：市场分析并发分析股票数
    maxConcurrency,
    // SSE 进度
    taskProgress,
    taskId,
    // 单股分析（并发）
    singleTasks,
    singleConcurrency,
    submitSingle,
    cancelSingleTask,
    cancelAllSingle,
    removeSingleTask,
    clearFinishedSingle,
    // 复制/导出
    copyOk,
    exportOk,
    csvOk,
    excelOk,
    onCopy,
    onExport,
    onExportCSV,
    onExportExcel,
    // 操作
    onRun,
    onConfirmAnalysis,
    onCancelAnalysis,
    onCancelPreview,
    setViewMode,
  }
}
