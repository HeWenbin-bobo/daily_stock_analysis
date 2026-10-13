<script setup lang="ts">
/**
 * 四榜交集分析 - 主页面
 *
 * 功能：过滤规则、单股分析、配置记忆、历史记录、SSE 进度、
 * CSV/Excel 导出、对比分析、暗色模式、PDF 报告、自选股、模板、定时任务
 */
import {
  NConfigProvider,
  NCard,
  NButton,
  NSpin,
  NAlert,
  NSpace,
  NText,
  NStatistic,
  NGrid,
  NGi,
  NProgress,
  NTabs,
  NTabPane,
  NCollapse,
  NCollapseItem,
  NTag,
  NTooltip,
  NScrollbar,
  darkTheme,
} from 'naive-ui'
import { zhCN, dateZhCN } from 'naive-ui'
import { ref, computed } from 'vue'
import { useHotAnalysis, type ViewMode } from './composables/useHotAnalysis'
import { useCountdown } from './composables/useCountdown'
import { formatDate, formatTime, scoreColor, adviceType, passReasonText, passReasonType } from './utils/formatters'
import { generatePDFReport } from './api/hotAnalysis'
import ConfigPanel from './components/ConfigPanel.vue'
import PreviewCard from './components/PreviewCard.vue'
import StatusBadges from './components/StatusBadges.vue'
import ResultTable from './components/ResultTable.vue'
import SingleStockInput from './components/SingleStockInput.vue'
import HistoryView from './components/HistoryView.vue'
import CompareView from './components/CompareView.vue'
import WatchlistView from './components/WatchlistView.vue'
import TemplatesView from './components/TemplatesView.vue'
import ScheduledTasksView from './components/ScheduledTasksView.vue'
import MarketReviewView from './components/MarketReviewView.vue'
import AdvancedViz from './components/AdvancedViz.vue'
import TechnicalAnalysisView from './components/TechnicalAnalysisView.vue'
import DeepResearchView from './components/DeepResearchView.vue'

const WEEK_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

// 暗色模式 — ★ Round 4 修复：localStorage 在隐私模式 / 配额满时抛错会导致整个 App 白屏；
//   包 try/catch，并支持系统偏好（prefers-color-scheme）作为兜底
const isDark = ref(false)
function _readStoredDark(): boolean {
  try {
    const v = localStorage.getItem('hot-analysis-dark')
    if (v === 'true') return true
    if (v === 'false') return false
  } catch (e) {
    if (import.meta.env.DEV) console.warn('localStorage read failed:', e)
  }
  // 兜底：跟随系统偏好
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  }
  return false
}
function _writeStoredDark(v: boolean): void {
  try {
    localStorage.setItem('hot-analysis-dark', String(v))
  } catch (e) {
    if (import.meta.env.DEV) console.warn('localStorage write failed:', e)
  }
}
isDark.value = _readStoredDark()
const theme = computed(() => isDark.value ? darkTheme : undefined)
function toggleDark() {
  isDark.value = !isDark.value
  _writeStoredDark(isDark.value)
}

const {
  phase, loading, error, result, partialResult, cancelledWithPartial, preview, progressTip,
  viewMode,
  sortKey, sortOrder, sortedResults, toggleSort,
  failedSources,
  selectedMaxRuntime, selectedMaxIntersection,
  rounds, interRoundSleep,
  minScore, allowedAdvice, excludeKeywords, requireAllRounds,
  llmTimeout,
  maxConcurrency,
  taskProgress,
  // 单股分析（并发）
  singleTasks, singleConcurrency, submitSingle, cancelSingleTask, cancelAllSingle, removeSingleTask, clearFinishedSingle,
  copyOk, exportOk, csvOk, excelOk,
  onCopy, onExport, onExportCSV, onExportExcel,
  onRun, onConfirmAnalysis, onCancelAnalysis, onCancelPreview,
  setViewMode,
} = useHotAnalysis()

const { now, elapsedSec, startElapsed, stopElapsed } = useCountdown()

// 对比
const compareRunId = ref<number | null>(null)
function onCompareFromHistory(runId: number) {
  compareRunId.value = runId
  setViewMode('compare')
}

// PDF 下载
const pdfLoading = ref(false)
const pdfError = ref<string | null>(null)
async function onPDF() {
  pdfLoading.value = true
  pdfError.value = null
  try {
    const resp = await generatePDFReport()
    if (resp.status === 'ok' && resp.pdf_base64) {
      const bin = atob(resp.pdf_base64)
      const arr = new Uint8Array(bin.length)
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
      const blob = new Blob([arr], { type: 'application/pdf' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = resp.filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } else {
      // ★ Round 13：不静默失败；展示后端 error
      pdfError.value = (resp as { error?: string }).error || 'PDF 生成失败'
    }
  } catch (e) {
    // ★ Round 13：替换 alert() 为 inline 错误显示
    pdfError.value = 'PDF 生成失败：' + (e instanceof Error ? e.message : String(e))
  } finally {
    pdfLoading.value = false
  }
}

// 模板加载
function onApplyTemplate(config: Record<string, unknown>) {
  // ★ 修复：运行时校验避免脏模板污染 ref（之前直接 as 强转，错类型值会穿透 axios）
  if (typeof config.min_score === 'number') minScore.value = config.min_score
  if (typeof config.max_intersection === 'number') selectedMaxIntersection.value = config.max_intersection
  if (Array.isArray(config.allowed_advice)) {
    allowedAdvice.value = config.allowed_advice.filter((x): x is string => typeof x === 'string')
  }
  if (Array.isArray(config.exclude_keywords)) {
    excludeKeywords.value = config.exclude_keywords.filter((x): x is string => typeof x === 'string')
  }
  if (typeof config.require_all_rounds === 'boolean') requireAllRounds.value = config.require_all_rounds
  // ★ 功能升级(L2)：模板应用更多配置（轮数/LLM 超时/并发/最大运行时）
  if (typeof config.rounds === 'number') rounds.value = config.rounds
  if (typeof config.llm_timeout === 'number') llmTimeout.value = config.llm_timeout
  if (typeof config.max_concurrency === 'number') maxConcurrency.value = config.max_concurrency
  if (typeof config.max_runtime === 'number') selectedMaxRuntime.value = config.max_runtime
  setViewMode('analysis')
}

// ★ Round 26 UI 重构：颜色统一走 CSS 设计变量（--page-bg / --color-card / --text-primary / --text-secondary），
//   不再内联硬编码色值，保证与全局 design system 一致（单强调色 + 深色模式同步）
const bodyBg = 'var(--page-bg)'
const cardBg = 'var(--color-card)'
const textPrimary = 'var(--text-primary)'
const textSecondary = 'var(--text-secondary)'
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme="theme">
    <div :data-theme="isDark ? 'dark' : 'light'" :style="`min-height: 100vh; background: ${bodyBg}; transition: background 0.3s`">
    <div style="max-width: 1200px; margin: 0 auto; padding: 32px clamp(16px, 4vw, 32px)">
      <!-- 标题 + 暗色切换 -->
      <header style="text-align: center; margin-bottom: 28px">
        <div style="display: flex; justify-content: center; align-items: center; gap: 12px">
          <h1 :style="`margin: 0; font-size: 28px; color: ${textPrimary}`">
            📈 四榜交集分析
          </h1>
          <n-button quaternary circle size="small" :aria-label="isDark ? '切换到亮色模式' : '切换到暗色模式'" @click="toggleDark" :title="isDark ? '切换亮色' : '切换暗色'" style="font-size: 18px; width: 36px; height: 36px; transition: transform 0.3s ease" @mouseenter="($event.currentTarget as HTMLElement).style.transform='rotate(180deg)'" @mouseleave="($event.currentTarget as HTMLElement).style.transform='rotate(0deg)'" @focusin="($event.currentTarget as HTMLElement).style.transform='rotate(180deg)'" @focusout="($event.currentTarget as HTMLElement).style.transform='rotate(0deg)'">
            {{ isDark ? '☀️' : '🌙' }}
          </n-button>
        </div>
        <p :style="`color: ${textSecondary}; margin-top: 8px; font-size: 14px`">
          东方财富 · 雪球 · 同花顺 · 全市场活跃 四榜求交，剔科创板，三轮 LLM 分析过滤
        </p>
        <div :style="`
          margin-top: 14px;
          display: inline-flex;
          align-items: center;
          gap: 14px;
          padding: 7px 18px;
          border-radius: 999px;
          background: var(--color-elevated);
          border: 1px solid var(--color-border);
          font-size: 13px;
          color: var(--text-secondary);
        `">
          <span style="display: inline-flex; align-items: center; gap: 6px">
            <span style="font-size: 14px">📅</span>
            {{ formatDate(now) }} · {{ WEEK_LABELS[now.getDay()] }}
          </span>
          <span style="width: 1px; height: 14px; background: rgba(148,163,184,0.3)"></span>
          <span style="
            display: inline-flex; align-items: center; gap: 6px;
            font-family: 'SF Mono', Menlo, Consolas, monospace;
            font-variant-numeric: tabular-nums; font-weight: 600;
            color: ${textPrimary}; letter-spacing: 0.5px;
          ">
            <span style="font-size: 14px">🕐</span>
            {{ formatTime(now) }}
          </span>
        </div>
      </header>

      <!-- 导航标签 -->
      <n-tabs
        :value="viewMode"
        @update:value="setViewMode($event as ViewMode)"
        type="segment"
        style="margin-bottom: 20px"
      >
        <n-tab-pane name="analysis" tab="📊 分析" />
        <n-tab-pane name="single" tab="🔍 单股" />
        <n-tab-pane name="technical" tab="📐 技术" />
        <n-tab-pane name="review" tab="🗞 大盘复盘" />
        <n-tab-pane name="viz" tab="📈 可视化" />
        <n-tab-pane name="history" tab="📚 历史" />
        <n-tab-pane name="compare" tab="🔄 对比" />
        <n-tab-pane name="watchlist" tab="⭐ 自选" />
        <n-tab-pane name="templates" tab="📋 模板" />
        <n-tab-pane name="scheduled" tab="⏰ 定时" />
        <n-tab-pane name="deepresearch" tab="🧠 深度研究" />
      </n-tabs>

      <!-- ========== 分析视图 ========== -->
      <div v-if="viewMode === 'analysis'">
        <n-card :bordered="false" :style="`border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,${isDark ? 0.3 : 0.06}); background: ${cardBg}`">
          <ConfigPanel
            :selected-max-runtime="selectedMaxRuntime"
            :selected-max-intersection="selectedMaxIntersection"
            :rounds="rounds"
            :inter-round-sleep="interRoundSleep"
            :loading="loading"
            :min-score="minScore"
            :allowed-advice="allowedAdvice"
            :exclude-keywords="excludeKeywords"
            :require-all-rounds="requireAllRounds"
            :llm-timeout="llmTimeout"
            :max-concurrency="maxConcurrency"
            @update:selected-max-runtime="selectedMaxRuntime = $event"
            @update:selected-max-intersection="selectedMaxIntersection = $event"
            @update:rounds="rounds = $event"
            @update:inter-round-sleep="interRoundSleep = $event"
            @update:min-score="minScore = $event ?? 60"
            @update:allowed-advice="allowedAdvice = $event"
            @update:exclude-keywords="excludeKeywords = $event"
            @update:require-all-rounds="requireAllRounds = $event"
            @update:llm-timeout="llmTimeout = $event"
            @update:max-concurrency="maxConcurrency = $event"
          />

          <n-space justify="space-between" align="center" wrap>
            <n-space align="center">
              <n-button
                size="large"
                :class="['btn-primary-action', phase === 'idle' ? 'btn-pulse' : '']"
                :loading="phase === 'previewing' || phase === 'analyzing'"
                :disabled="loading || phase === 'previewing' || phase === 'analyzing'"
                @click="onRun"
                aria-label="开始四榜交集分析"
                style="min-width: 180px; height: 52px; font-size: 18px; border-radius: 12px"
              >
                <template #icon>🚀</template>
                {{ phase === 'previewing' ? '抓榜中…' : phase === 'previewed' ? '已预览' : phase === 'analyzing' ? '分析中…' : phase === 'done' ? '重新分析' : '开始分析' }}
              </n-button>
              <n-text depth="3">{{ progressTip }}</n-text>
            </n-space>

            <n-space v-if="result">
              <n-tooltip><template #trigger><n-button @click="onCopy" :type="copyOk ? 'success' : 'default'" size="small" class="btn-export" :aria-label="copyOk ? '已复制到剪贴板' : '复制分析结果为纯文本'" :style="copyOk ? 'background: var(--success)!important' : ''">{{ copyOk ? '✓ 已复制' : '📋 复制' }}</n-button></template>复制纯文本</n-tooltip>
              <n-tooltip><template #trigger><n-button @click="onExport" :type="exportOk ? 'success' : 'default'" size="small" class="btn-export" :aria-label="exportOk ? '已导出 TXT' : '导出为 TXT 文本'" :style="exportOk ? 'background: var(--success)!important' : ''">{{ exportOk ? '✓ 已导出' : '📥 TXT' }}</n-button></template>下载 TXT</n-tooltip>
              <n-tooltip><template #trigger><n-button @click="onExportCSV" :type="csvOk ? 'success' : 'default'" size="small" class="btn-export" :aria-label="csvOk ? '已导出 CSV' : '导出为 CSV 文件'" :style="csvOk ? 'background: var(--success)!important' : ''">{{ csvOk ? '✓ 已导出' : '📊 CSV' }}</n-button></template>下载 CSV</n-tooltip>
              <!-- ★ 修复：Excel 兼容 CSV 导出按钮（功能在 composable 已实现但 UI 缺失——
                    onExportExcel/excelOk 之前从未被模板引用，用户无法使用） -->
              <n-tooltip><template #trigger><n-button @click="onExportExcel" :type="excelOk ? 'success' : 'default'" size="small" class="btn-export" :aria-label="excelOk ? '已导出 Excel CSV' : '导出为 Excel 兼容 CSV'" :style="excelOk ? 'background: var(--success)!important' : ''">{{ excelOk ? '✓ 已导出' : '📗 Excel' }}</n-button></template>下载 Excel 兼容 CSV（带 BOM，中文不乱码）</n-tooltip>
              <n-tooltip><template #trigger><n-button @click="onPDF" :loading="pdfLoading" size="small" class="btn-pdf" aria-label="生成并下载 PDF 分析报告">📄 PDF</n-button></template>生成 PDF 分析报告</n-tooltip>
              <n-alert v-if="pdfError" type="error" :show-icon="true" style="margin-top: 8px" closable @close="pdfError = null">
                {{ pdfError }}
              </n-alert>
            </n-space>
          </n-space>

          <!-- 加载中 -->
          <div v-if="loading" role="status" aria-live="polite" aria-busy="true" style="margin-top: 18px; text-align: center">
            <n-spin size="large" />
            <div :style="`margin-top: 10px; color: ${textSecondary}; font-size: 13px`">
              已等待 {{ elapsedSec }} 秒 · 最多 {{ selectedMaxRuntime }} 分钟
            </div>
            <div v-if="taskProgress" style="margin-top: 12px; max-width: 400px; margin-left: auto; margin-right: auto">
              <n-progress type="line" :percentage="taskProgress.total_stocks > 0 ? Math.round(taskProgress.completed_stocks / taskProgress.total_stocks * 100) : 0" :indicator-placement="'inside'" :status="taskProgress.status === 'failed' ? 'error' : 'default'" />
              <div style="margin-top: 6px; font-size: 12px; color: #64748b">
                {{ taskProgress.current_stock }}（{{ taskProgress.completed_stocks }}/{{ taskProgress.total_stocks }}）· 通过: {{ taskProgress.passed_count }}
              </div>
            </div>
            <!-- ★ 修复(H1)：预览阶段（previewing）取消应调用 onCancelPreview（清 loading 回 idle），
                 分析阶段（analyzing）才用 onCancelAnalysis（保留 partial 结果）。
                 旧实现统一 onCancelAnalysis → 抓榜中取消会 abort 预览请求 → onRun catch
                 AbortError → 红色"分析失败：预览已取消或超时"，把用户主动取消当失败。 -->
            <n-button
              v-if="phase === 'previewing'"
              size="small"
              class="btn-danger"
              style="margin-top: 10px; border-radius: 8px"
              @click="onCancelPreview"
              aria-label="取消预览"
            >✕ 取消预览</n-button>
            <n-button
              v-else
              size="small"
              class="btn-danger"
              style="margin-top: 10px; border-radius: 8px"
              @click="onCancelAnalysis"
              aria-label="取消分析"
            >✕ 取消分析</n-button>
          </div>
        </n-card>

        <n-alert v-if="error" type="error" title="分析失败" style="margin-top: 18px" :show-icon="true" role="alert" aria-live="assertive">{{ error }}</n-alert>

        <n-alert v-if="cancelledWithPartial" type="warning" title="分析已取消" style="margin-top: 18px" :show-icon="true">
          已展示本次取消前已完成的分析结果。可继续复制 / 导出 / 查看历史。
        </n-alert>

        <PreviewCard v-if="phase === 'previewed' && preview" :preview="preview" @confirm="() => onConfirmAnalysis(startElapsed, stopElapsed)" @cancel="onCancelPreview" />

        <StatusBadges v-if="result" :hot-lists-summary="result.hot_lists_summary" :hot-lists-status="result.hot_lists_status" :hot-lists-notes="result.hot_lists_notes" />

        <!-- 摘要统计 -->
        <n-grid v-if="result && result.status !== 'failed'" :cols="4" :x-gap="14" :y-gap="14" responsive="screen" style="margin-top: 20px">
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: var(--accent-soft); border: 1px solid var(--accent-border)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">📡</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">榜单抓取</n-text>
                  <!-- ★ 修复(M6)：补上第 4 源"全市场活跃"，与 StatusBadges 的 4 源一致 -->
                  <n-text strong style="font-size: 18px; color: var(--accent)">
                    {{ (result.hot_lists_summary['东方财富'] ?? 0) }} / {{ (result.hot_lists_summary['雪球'] ?? 0) }} / {{ (result.hot_lists_summary['同花顺'] ?? 0) }} / {{ (result.hot_lists_summary['全市场活跃'] ?? 0) }}
                  </n-text>
                </div>
              </div>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: #f0fdf4; border: 1px solid rgba(22,163,74,0.15)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">🎯</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">交集 / 剔688</n-text>
                  <n-text strong style="font-size: 18px; color: var(--success)">
                    {{ result.raw_intersection_count }} / {{ result.filtered_intersection_count }}
                  </n-text>
                </div>
              </div>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: var(--color-elevated); border: 1px solid var(--color-border)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">📊</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">分析 / 通过</n-text>
                  <n-text strong style="font-size: 18px; color: var(--text-primary)">
                    {{ result.analyzed_stock_count }} / {{ result.passed_stock_count }}
                  </n-text>
                </div>
              </div>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: #fff7ed; border: 1px solid rgba(217,119,6,0.15)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">⏱</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">总耗时</n-text>
                  <n-text strong style="font-size: 18px; color: var(--warning)">
                    {{ result.duration_seconds }}s
                  </n-text>
                </div>
              </div>
            </n-card>
          </n-gi>
        </n-grid>

        <!-- 通过的结果 -->
        <n-card v-if="result" :bordered="false" :style="`margin-top: 20px; border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,${isDark ? 0.3 : 0.06}); background: ${cardBg}`">
          <ResultTable :results="sortedResults" :sort-key="sortKey" :sort-order="sortOrder" @toggle-sort="toggleSort" />
        </n-card>

        <!-- 未通过的股票（折叠） -->
        <n-card v-if="result && result.filtered_stocks?.length" :bordered="false" :style="`margin-top: 16px; border-radius: 14px; background: rgba(220,38,38,0.05); border: 1px solid rgba(220,38,38,0.15)`">
          <n-collapse>
            <n-collapse-item :title="`🚫 未通过过滤（${result.filtered_stocks.length} 只）`" name="filtered">
              <n-scrollbar style="max-height: 400px">
                <div v-for="s in result.filtered_stocks" :key="s.code" :style="`padding: 10px 12px; margin-bottom: 8px; border-radius: 8px; background: var(--color-card); border: 1px solid var(--color-border)`">
                  <n-space align="center" wrap>
                    <n-text strong style="font-family: monospace">{{ s.code }}</n-text>
                    <n-text>{{ s.name }}</n-text>
                    <n-tag :type="passReasonType(s.filter_reason)" size="small" :bordered="false">
                      {{ passReasonText(s.filter_reason) }}
                    </n-tag>
                    <n-text :style="`color: ${scoreColor(s.avg_score)}; font-weight: 600`">{{ s.avg_score }}分</n-text>
                    <n-tag :type="adviceType(s.final_advice)" size="small" :bordered="false">{{ s.final_advice || '-' }}</n-tag>
                    <n-space :size="4">
                      <n-tag v-for="src in s.in_sources" :key="src" size="tiny" type="info" :bordered="false">{{ src }}</n-tag>
                    </n-space>
                  </n-space>
                </div>
              </n-scrollbar>
            </n-collapse-item>
          </n-collapse>
        </n-card>
      </div>

      <!-- ========== 其他视图 ========== -->
      <div v-if="viewMode === 'single'">
        <SingleStockInput
          :tasks="singleTasks"
          :concurrency="singleConcurrency"
          @update:concurrency="singleConcurrency = $event"
          @submit="submitSingle"
          @cancel="cancelSingleTask"
          @cancel-all="cancelAllSingle"
          @remove="removeSingleTask"
          @clear-finished="clearFinishedSingle"
        />
      </div>
      <div v-if="viewMode === 'technical'">
        <TechnicalAnalysisView />
      </div>
      <div v-if="viewMode === 'review'">
        <MarketReviewView @back="setViewMode('analysis')" />
      </div>
      <div v-if="viewMode === 'viz'">
        <AdvancedViz />
      </div>
      <div v-if="viewMode === 'history'">
        <HistoryView @back="setViewMode('analysis')" @compare="onCompareFromHistory" />
      </div>
      <div v-if="viewMode === 'compare'">
        <CompareView :initial-run-id="compareRunId" @back="setViewMode('analysis')" />
      </div>
      <div v-if="viewMode === 'watchlist'">
        <WatchlistView @back="setViewMode('analysis')" />
      </div>
      <div v-if="viewMode === 'templates'">
        <TemplatesView @back="setViewMode('analysis')" @apply="onApplyTemplate" />
      </div>
      <div v-if="viewMode === 'scheduled'">
        <ScheduledTasksView @back="setViewMode('analysis')" />
      </div>
      <div v-if="viewMode === 'deepresearch'">
        <DeepResearchView />
      </div>
    </div>
    </div>
  </n-config-provider>
</template>

<style scoped>
header h1 { letter-spacing: 0.5px; }
</style>
