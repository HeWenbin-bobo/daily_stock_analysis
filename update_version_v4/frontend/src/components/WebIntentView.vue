<script setup lang="ts">
/**
 * Web Chat 意图识别调试台 — 复用主项目 src/agent/web_intent_resolver
 * 端点: POST /api/v2/web-intent/resolve
 */
import { ref } from 'vue'
import { NCard, NButton, NInput, NAlert, NTag, NSpace, NTable } from 'naive-ui'
import axios from 'axios'

const http = axios.create({ baseURL: '/api/v2', timeout: 120_000 })

interface StockRef { code: string; name: string; market: string }
interface Task {
  intent: string
  confidence: number
  source: string
  stocks: StockRef[]
  sectors: string[]
  candidates: StockRef[]
  unresolved_names: string[]
  unverified_codes: string[]
  needs_confirmation: boolean
  reason: string
  source_request: string
  inherited_stock_code: string
}

const message = ref('')
const sessionCtx = ref('')
const groups = ref<Task[][]>([])
const running = ref(false)
const error = ref<string | null>(null)

const intentLabels: Record<string, string> = {
  stock_analysis: '📊 股票分析',
  sector_analysis: '🏭 板块分析',
  portfolio_analysis: '💼 持仓分析',
  quote_lookup: '💱 行情查询',
  general_chat: '💬 闲聊',
}
const sourceLabels: Record<string, string> = {
  rule: '规则',
  llm: 'LLM',
  llm_failed: 'LLM失败回退',
  context: '上下文继承',
  confirmation: '用户确认',
}

function sourceType(s: string): 'success' | 'info' | 'warning' | 'default' {
  if (s === 'rule') return 'success'
  if (s === 'llm') return 'info'
  if (s === 'llm_failed') return 'warning'
  return 'default'
}

function parseCtx(): any {
  const raw = sessionCtx.value.trim()
  if (!raw) return undefined
  try {
    return JSON.parse(raw)
  } catch {
    return undefined
  }
}

async function onResolve() {
  running.value = true
  error.value = null
  groups.value = []
  try {
    const r = await http.post('/web-intent/resolve', {
      message: message.value,
      session_context: parseCtx(),
    })
    groups.value = r.data.groups
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '解析失败'
  } finally {
    running.value = false
  }
}
</script>

<template>
  <div class="intent-view">
    <n-card title="🧭 Web Chat 意图识别" size="small">
      <n-alert type="info" :show-icon="true" style="margin-bottom: 12px">
        把一条自然语言消息解析为任务组（消费型标点/连接词切分子消息，组内平权任务）。
        规则高置信直达；规则不完备时走主项目 LLM 兜底（来源列可见判定路径）。
      </n-alert>
      <n-input
        v-model:value="message"
        type="textarea"
        placeholder="如：分析一下600519，顺便查查000858的行情"
        :rows="2"
      />
      <n-input
        v-model:value="sessionCtx"
        placeholder='会话上下文 JSON（可选），如 {"recent_stocks": ["600519"]}'
        style="margin-top: 8px"
      />
      <n-space style="margin-top: 10px">
        <n-button type="primary" :loading="running" @click="onResolve" :disabled="!message.trim()">
          解析
        </n-button>
      </n-space>
    </n-card>

    <n-alert v-if="error" type="error" style="margin-top: 12px" closable @close="error = null">{{ error }}</n-alert>

    <template v-if="groups.length">
      <n-card
        v-for="(group, gi) in groups"
        :key="gi"
        :title="`子消息 ${groups.length > 1 ? gi + 1 : ''}`.trim()"
        size="small"
        style="margin-top: 12px"
      >
        <n-table size="small">
          <thead>
            <tr><th>意图</th><th>置信</th><th>来源</th><th>标的</th><th>板块</th><th>备注</th></tr>
          </thead>
          <tbody>
            <tr v-for="(t, ti) in group" :key="ti">
              <td>{{ intentLabels[t.intent] || t.intent }}</td>
              <td>{{ (t.confidence * 100).toFixed(0) }}%</td>
              <td><n-tag :type="sourceType(t.source)" size="small">{{ sourceLabels[t.source] || t.source }}</n-tag></td>
              <td>
                <n-tag v-for="s in t.stocks" :key="s.code" size="small" style="margin-right: 4px">
                  {{ s.code }}{{ s.name ? ` ${s.name}` : '' }}
                </n-tag>
                <span v-if="!t.stocks.length && t.inherited_stock_code">继承: {{ t.inherited_stock_code }}</span>
                <span v-if="!t.stocks.length && !t.inherited_stock_code">—</span>
              </td>
              <td>{{ t.sectors.join('、') || '—' }}</td>
              <td>
                <n-tag v-if="t.needs_confirmation" type="warning" size="small">需确认: {{ t.reason }}</n-tag>
                <span v-else-if="t.unresolved_names.length">未解析: {{ t.unresolved_names.join('、') }}</span>
                <span v-else-if="t.candidates.length">候选: {{ t.candidates.map(c => c.code).join('、') }}</span>
                <span v-else>—</span>
              </td>
            </tr>
          </tbody>
        </n-table>
        <p v-if="group.some(t => t.source_request)" style="font-size: 12px; color: #888; margin: 8px 0 0">
          来源子消息: {{ group.map(t => t.source_request).filter(Boolean).join(' | ') }}
        </p>
      </n-card>
    </template>
  </div>
</template>

<style scoped>
.intent-view { max-width: 960px; }
</style>
