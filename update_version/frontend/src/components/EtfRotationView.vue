<script setup lang="ts">
/**
 * ETF 双动量轮动 — 复用主项目 src/services/etf_rotation_service
 * 端点: POST /api/v2/etf-rotation/run（限流 2 次/5 分钟，as_of 同日进程内缓存）
 *       GET  /api/v2/etf-rotation/history[/{date}]（主项目 reports/ 落盘报告）
 */
import { ref, onMounted } from 'vue'
import { NCard, NButton, NAlert, NSpin, NTable, NTag, NList, NListItem } from 'naive-ui'
import axios from 'axios'

const http = axios.create({ baseURL: '/api/v2', timeout: 600_000 })

const running = ref(false)
const error = ref<string | null>(null)
const result = ref<{
  as_of: string | null
  target_weights: Record<string, number>
  failed_codes: Record<string, string>
  warnings: string[]
  markdown: string
  cached?: boolean
} | null>(null)

interface HistoryItem { date: string; file: string; size: number }
const history = ref<HistoryItem[]>([])
const historyDetail = ref<{ date: string; markdown: string } | null>(null)
const loadingDetail = ref(false)

async function onRun(force = false) {
  running.value = true
  error.value = null
  result.value = null
  try {
    const r = await http.post(`/etf-rotation/run${force ? '?force=true' : ''}`)
    result.value = r.data
    void loadHistory()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '请求失败'
  } finally {
    running.value = false
  }
}

async function loadHistory() {
  try {
    const r = await http.get('/etf-rotation/history')
    history.value = r.data.reports || []
  } catch {
    history.value = []
  }
}

async function openDetail(date: string) {
  loadingDetail.value = true
  historyDetail.value = null
  try {
    const r = await http.get(`/etf-rotation/history/${date}`)
    historyDetail.value = r.data
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '读取失败'
  } finally {
    loadingDetail.value = false
  }
}

function pct(v: number): string {
  return `${(v * 100).toFixed(1)}%`
}

function fmtSize(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`
}

onMounted(loadHistory)
</script>

<template>
  <div class="etf-view">
    <n-card title="🔄 ETF 双动量轮动" size="small">
      <n-alert type="info" :show-icon="true" style="margin-bottom: 12px">
        基于动量排名的规则化轮动（不调用 LLM）。配置见主项目环境变量
        <code>ETF_ROTATION_POOL</code> / <code>ETF_ROTATION_SAFE_ASSET</code> 等。
        数据抓取较重（约 1-3 分钟）；当日结果自动缓存，限流 2 次/5 分钟。
      </n-alert>
      <n-space>
        <n-button type="primary" :loading="running" @click="onRun(false)" :disabled="running">
          🚀 运行轮动信号
        </n-button>
        <n-button quaternary :loading="running" @click="onRun(true)" :disabled="running">
          强制重跑
        </n-button>
      </n-space>
    </n-card>

    <n-spin v-if="running" style="margin-top: 16px" size="large">
      <template #description>抓取数据并回测中，请稍候…</template>
      <div style="min-height: 80px" />
    </n-spin>

    <n-alert v-if="error" type="error" title="执行失败" style="margin-top: 16px" closable @close="error = null">
      {{ error }}
    </n-alert>

    <template v-if="result">
      <n-card title="📌 最新信号" size="small" style="margin-top: 16px">
        <n-alert v-if="result.cached" type="success" :show-icon="true" style="margin-bottom: 10px">
          ⚡ 当日缓存命中（数据截至 {{ result.as_of }}）。如需最新数据请点「强制重跑」。
        </n-alert>
        <p v-if="result.as_of">数据截至：<b>{{ result.as_of }}</b></p>
        <n-table v-if="Object.keys(result.target_weights).length" size="small" style="margin-top: 8px">
          <thead><tr><th>标的</th><th>目标权重</th></tr></thead>
          <tbody>
            <tr v-for="(w, code) in result.target_weights" :key="code">
              <td>{{ code }}</td>
              <td><n-tag type="success">{{ pct(w as number) }}</n-tag></td>
            </tr>
          </tbody>
        </n-table>
        <n-alert v-else type="warning" style="margin-top: 8px">本次无持仓信号（可能全现金防守）</n-alert>
        <n-alert
          v-for="(msg, code) in result.failed_codes" :key="code"
          type="warning" style="margin-top: 8px"
        >
          {{ code }} 数据获取失败: {{ msg }}
        </n-alert>
      </n-card>

      <n-card title="📄 完整报告" size="small" style="margin-top: 16px">
        <pre class="report-pre">{{ result.markdown }}</pre>
      </n-card>
    </template>

    <n-card title="📚 历史报告（主项目 reports/）" size="small" style="margin-top: 16px">
      <n-list v-if="history.length" hoverable clickable>
        <n-list-item v-for="h in history" :key="h.date" @click="openDetail(h.date)">
          <n-space justify="space-between" align="center">
            <span>📅 {{ h.date }}</span>
            <n-tag size="small" :bordered="false">{{ fmtSize(h.size) }}</n-tag>
          </n-space>
        </n-list-item>
      </n-list>
      <n-alert v-else type="default">暂无历史报告（运行一次后会落盘到主项目 reports/etf_rotation_*.md）</n-alert>
    </n-card>

    <n-card v-if="loadingDetail" title="报告详情" size="small" style="margin-top: 16px">
      <n-spin size="small" />
    </n-card>
    <n-card v-else-if="historyDetail" :title="`📅 ${historyDetail.date} 报告全文`" size="small" style="margin-top: 16px" closable @close="historyDetail = null">
      <pre class="report-pre">{{ historyDetail.markdown }}</pre>
    </n-card>
  </div>
</template>

<style scoped>
.etf-view { max-width: 960px; }
.report-pre {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
  font-family: 'Cascadia Code', Consolas, monospace;
}
</style>
