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
import { formatDate, formatTime, scoreColor, adviceType, passReasonText } from './utils/formatters'
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

const WEEK_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

// 暗色模式
const isDark = ref(false)
const storedDark = localStorage.getItem('hot-analysis-dark')
if (storedDark === 'true') isDark.value = true
const theme = computed(() => isDark.value ? darkTheme : undefined)
function toggleDark() {
  isDark.value = !isDark.value
  localStorage.setItem('hot-analysis-dark', String(isDark.value))
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
async function onPDF() {
  pdfLoading.value = true
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
    }
  } catch (e) {
    // ★ round 6 audit：用户可见提示而非 console（生产无 devtools）
    pdfLoading.value = false
    // 触发全局错误提示（n-message 已在 useHotAnalysis 暴露 error.value）
    alert('PDF 生成失败：' + (e instanceof Error ? e.message : String(e)))
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
  setViewMode('analysis')
}

const bodyBg = computed(() => isDark.value ? '#1a1a2e' : '#f8fafc')
const cardBg = computed(() => isDark.value ? '#16213e' : '#ffffff')
const textPrimary = computed(() => isDark.value ? '#e2e8f0' : '#0f172a')
const textSecondary = computed(() => isDark.value ? '#94a3b8' : '#64748b')
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme="theme">
    <div :data-theme="isDark ? 'dark' : 'light'" :style="`min-height: 100vh; background: ${bodyBg}; transition: background 0.3s`">
    <div style="max-width: 1200px; margin: 0 auto; padding: 32px 24px">
      <!-- 标题 + 暗色切换 -->
      <header style="text-align: center; margin-bottom: 28px">
        <div style="display: flex; justify-content: center; align-items: center; gap: 12px">
          <h1 :style="`margin: 0; font-size: 28px; color: ${textPrimary}`">
            📈 四榜交集分析
          </h1>
          <n-button quaternary circle size="small" @click="toggleDark" :title="isDark ? '切换亮色' : '切换暗色'" style="font-size: 18px; width: 36px; height: 36px; transition: transform 0.3s ease" @mouseenter="($event.currentTarget as HTMLElement).style.transform='rotate(180deg)'" @mouseleave="($event.currentTarget as HTMLElement).style.transform='rotate(0deg)'">
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
          padding: 8px 18px;
          border-radius: 999px;
          background: ${isDark ? 'rgba(148,163,184,0.1)' : 'linear-gradient(135deg, #f1f5f9 0%, #e2e8f0 100%)'};
          font-size: 13px;
          color: ${textSecondary};
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
        <n-tab-pane name="review" tab="🗞 大盘复盘" />
        <n-tab-pane name="history" tab="📚 历史" />
        <n-tab-pane name="compare" tab="🔄 对比" />
        <n-tab-pane name="watchlist" tab="⭐ 自选" />
        <n-tab-pane name="templates" tab="📋 模板" />
        <n-tab-pane name="scheduled" tab="⏰ 定时" />
      </n-tabs>

      <!-- ========== 分析视图 ========== -->
      <div v-if="viewMode === 'analysis'">
        <n-card :bordered="false" :style="`border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,${isDark ? 0.3 : 0.06}); background: ${cardBg}`">
          <ConfigPanel
            :selected-max-runtime="selectedMaxRuntime"
            :selected-max-intersection="selectedMaxIntersection"
            :rounds="rounds"
            :inter-round-sleep="interRoundSleep"
            :single-concurrency="singleConcurrency"
            :loading="loading"
            :min-score="minScore"
            :allowed-advice="allowedAdvice"
            :exclude-keywords="excludeKeywords"
            :require-all-rounds="requireAllRounds"
            :llm-timeout="llmTimeout"
            @update:selected-max-runtime="selectedMaxRuntime = $event"
            @update:selected-max-intersection="selectedMaxIntersection = $event"
            @update:rounds="rounds = $event"
            @update:inter-round-sleep="interRoundSleep = $event"
            @update:min-score="minScore = $event ?? 60"
            @update:allowed-advice="allowedAdvice = $event"
            @update:exclude-keywords="excludeKeywords = $event"
            @update:require-all-rounds="requireAllRounds = $event"
            @update:llm-timeout="llmTimeout = $event"
            @update:single-concurrency="singleConcurrency = $event"
          />

          <n-space justify="space-between" align="center" wrap>
            <n-space align="center">
              <n-button
                size="large"
                :class="['btn-primary-action', phase === 'idle' ? 'btn-pulse' : '']"
                :loading="phase === 'previewing' || phase === 'analyzing'"
                :disabled="loading || phase === 'previewing' || phase === 'analyzing'"
                @click="onRun"
                style="min-width: 180px; height: 52px; font-size: 18px; border-radius: 12px"
              >
                <template #icon>🚀</template>
                {{ phase === 'previewing' ? '抓榜中…' : phase === 'previewed' ? '已预览' : phase === 'analyzing' ? '分析中…' : phase === 'done' ? '重新分析' : '开始分析' }}
              </n-button>
              <n-text depth="3">{{ progressTip }}</n-text>
            </n-space>

            <n-space v-if="result">
              <n-tooltip><template #trigger><n-button @click="onCopy" :type="copyOk ? 'success' : 'default'" size="small" class="btn-export" :style="copyOk ? 'background: linear-gradient(135deg,#10b981,#059669)!important' : ''">{{ copyOk ? '✓ 已复制' : '📋 复制' }}</n-button></template>复制纯文本</n-tooltip>
              <n-tooltip><template #trigger><n-button @click="onExport" :type="exportOk ? 'success' : 'default'" size="small" class="btn-export" :style="exportOk ? 'background: linear-gradient(135deg,#10b981,#059669)!important' : ''">{{ exportOk ? '✓ 已导出' : '📥 TXT' }}</n-button></template>下载 TXT</n-tooltip>
              <n-tooltip><template #trigger><n-button @click="onExportCSV" :type="csvOk ? 'success' : 'default'" size="small" class="btn-export" :style="csvOk ? 'background: linear-gradient(135deg,#10b981,#059669)!important' : ''">{{ csvOk ? '✓ 已导出' : '📊 CSV' }}</n-button></template>下载 CSV</n-tooltip>
              <n-tooltip><template #trigger><n-button @click="onPDF" :loading="pdfLoading" size="small" class="btn-pdf">📄 PDF</n-button></template>生成 PDF 分析报告</n-tooltip>
            </n-space>
          </n-space>

          <!-- 加载中 -->
          <div v-if="loading" style="margin-top: 18px; text-align: center">
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
            <n-button size="small" class="btn-danger" style="margin-top: 10px; border-radius: 8px" @click="onCancelAnalysis">✕ 取消分析</n-button>
          </div>
        </n-card>

        <n-alert v-if="error" type="error" title="分析失败" style="margin-top: 18px" :show-icon="true">{{ error }}</n-alert>

        <n-alert v-if="cancelledWithPartial" type="warning" title="分析已取消" style="margin-top: 18px" :show-icon="true">
          已展示本次取消前已完成的分析结果。可继续复制 / 导出 / 查看历史。
        </n-alert>

        <PreviewCard v-if="phase === 'previewed' && preview" :preview="preview" @confirm="() => onConfirmAnalysis(startElapsed, stopElapsed)" @cancel="onCancelPreview" />

        <StatusBadges v-if="result" :hot-lists-summary="result.hot_lists_summary" :hot-lists-status="result.hot_lists_status" :hot-lists-notes="result.hot_lists_notes" />

        <!-- 摘要统计 -->
        <n-grid v-if="result && result.status !== 'failed'" :cols="4" :x-gap="14" :y-gap="14" responsive="screen" style="margin-top: 20px">
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: linear-gradient(135deg, #eff6ff, #dbeafe); border: 1px solid rgba(59,130,246,0.1)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">📡</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">榜单抓取</n-text>
                  <n-text strong style="font-size: 18px; color: #2563eb">
                    {{ (result.hot_lists_summary['东方财富'] ?? 0) }} / {{ (result.hot_lists_summary['雪球'] ?? 0) }} / {{ (result.hot_lists_summary['同花顺'] ?? 0) }}
                  </n-text>
                </div>
              </div>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: linear-gradient(135deg, #f0fdf4, #dcfce7); border: 1px solid rgba(16,185,129,0.1)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">🎯</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">交集 / 剔688</n-text>
                  <n-text strong style="font-size: 18px; color: #059669">
                    {{ result.raw_intersection_count }} / {{ result.filtered_intersection_count }}
                  </n-text>
                </div>
              </div>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: linear-gradient(135deg, #faf5ff, #f3e8ff); border: 1px solid rgba(139,92,246,0.1)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">📊</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">分析 / 通过</n-text>
                  <n-text strong style="font-size: 18px; color: #7c3aed">
                    {{ result.analyzed_stock_count }} / {{ result.passed_stock_count }}
                  </n-text>
                </div>
              </div>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card size="small" style="border-radius: 12px; background: linear-gradient(135deg, #fff7ed, #ffedd5); border: 1px solid rgba(245,158,11,0.1)">
              <div style="display: flex; align-items: center; gap: 10px">
                <span style="font-size: 24px">⏱</span>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block">总耗时</n-text>
                  <n-text strong style="font-size: 18px; color: #d97706">
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
        <n-card v-if="result && result.filtered_stocks?.length" :bordered="false" :style="`margin-top: 16px; border-radius: 14px; background: ${isDark ? '#1e293b' : '#fef2f2'}; border: 1px solid ${isDark ? '#334155' : '#fecaca'}`">
          <n-collapse>
            <n-collapse-item :title="`🚫 未通过过滤（${result.filtered_stocks.length} 只）`" name="filtered">
              <n-scrollbar style="max-height: 400px">
                <div v-for="s in result.filtered_stocks" :key="s.code" :style="`padding: 10px 12px; margin-bottom: 8px; border-radius: 8px; background: ${isDark ? '#0f172a' : '#fff'}; border: 1px solid ${isDark ? '#334155' : '#e5e7eb'}`">
                  <n-space align="center" wrap>
                    <n-text strong style="font-family: monospace">{{ s.code }}</n-text>
                    <n-text>{{ s.name }}</n-text>
                    <n-tag :type="s.filter_reason === 'rejected_sell' ? 'error' : 'warning'" size="small" :bordered="false">
                      {{ s.filter_reason === 'rejected_sell' ? '出现卖出' : s.filter_reason === 'incomplete' ? '数据不全' : '未达条件' }}
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
          @submit="submitSingle"
          @cancel="cancelSingleTask"
          @cancel-all="cancelAllSingle"
          @remove="removeSingleTask"
          @clear-finished="clearFinishedSingle"
        />
      </div>
      <div v-if="viewMode === 'review'">
        <MarketReviewView @back="setViewMode('analysis')" />
      </div>
      <div v-if="viewMode === 'history'">
        <HistoryView @back="setViewMode('analysis')" @compare="onCompareFromHistory" />
      </div>
      <div v-if="viewMode === 'compare'">
        <CompareView @back="setViewMode('analysis')" />
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
    </div>
    </div>
  </n-config-provider>
</template>

<style scoped>
header h1 { letter-spacing: 0.5px; }
</style>
