<script setup lang="ts">
/**
 * 组合总览 Dashboard — 复用主项目 src/services/portfolio_service（P0 组合域）
 * 端点: GET/POST /api/v2/portfolio-p0/*
 */
import { ref, onMounted } from 'vue'
import {
  NCard, NButton, NAlert, NTable, NTag, NInput, NInputNumber,
  NSelect, NModal, NForm, NFormItem, NSpace, NStatistic, NGrid, NGi,
} from 'naive-ui'
import axios from 'axios'

const http = axios.create({ baseURL: '/api/v2' })

interface Account {
  id: number
  name: string
  broker: string | null
  market: string
  base_currency: string
  is_active?: boolean
}
interface Snapshot {
  as_of: string
  cost_method: string
  currency: string
  account_count: number
  total_cash: number | null
  total_market_value: number | null
  total_equity: number | null
  realized_pnl?: number | null
  positions?: any[]
}

const accounts = ref<Account[]>([])
const snapshot = ref<Snapshot | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)

const showCreate = ref(false)
const form = ref({ name: '', broker: '', market: 'cn', base_currency: 'CNY' })
const marketOptions = [
  { label: 'A 股 (cn)', value: 'cn' },
  { label: '港股 (hk)', value: 'hk' },
  { label: '美股 (us)', value: 'us' },
]

const showTrade = ref(false)
const tradeForm = ref({
  account_id: null as number | null,
  symbol: '',
  trade_date: new Date().toISOString().slice(0, 10),
  side: 'buy',
  quantity: 100,
  price: 0,
  fee: 0,
  tax: 0,
  note: '',
})
const sideOptions = [
  { label: '买入 (buy)', value: 'buy' },
  { label: '卖出 (sell)', value: 'sell' },
]

async function load() {
  loading.value = true
  error.value = null
  try {
    const [a, s] = await Promise.all([
      http.get('/portfolio-p0/accounts'),
      http.get('/portfolio-p0/snapshot'),
    ])
    accounts.value = a.data.accounts || []
    snapshot.value = s.data
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  error.value = null
  try {
    await http.post('/portfolio-p0/accounts', {
      name: form.value.name,
      broker: form.value.broker || undefined,
      market: form.value.market,
      base_currency: form.value.base_currency,
    })
    showCreate.value = false
    form.value = { name: '', broker: '', market: 'cn', base_currency: 'CNY' }
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '创建失败'
  }
}

async function onRecordTrade() {
  error.value = null
  try {
    await http.post('/portfolio-p0/trades', {
      account_id: tradeForm.value.account_id,
      symbol: tradeForm.value.symbol,
      trade_date: tradeForm.value.trade_date,
      side: tradeForm.value.side,
      quantity: tradeForm.value.quantity,
      price: tradeForm.value.price,
      fee: tradeForm.value.fee,
      tax: tradeForm.value.tax,
      note: tradeForm.value.note || undefined,
    })
    showTrade.value = false
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '记录失败'
  }
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

onMounted(load)
</script>

<template>
  <div class="pf-view">
    <n-card title="💼 组合总览 (P0 组合域)" size="small">
      <n-space>
        <n-button size="small" quaternary @click="load" :loading="loading">🔄 刷新</n-button>
        <n-button size="small" type="primary" @click="showCreate = true">＋ 新建账户</n-button>
        <n-button size="small" @click="showTrade = true" :disabled="!accounts.length">📝 记录交易</n-button>
      </n-space>
    </n-card>

    <n-alert v-if="error" type="error" style="margin-top: 12px" closable @close="error = null">{{ error }}</n-alert>

    <n-grid v-if="snapshot" :cols="4" :x-gap="12" responsive="screen" style="margin-top: 12px" item-responsive>
      <n-gi span="4 m:1"><n-card size="small"><n-statistic label="账户数" :value="snapshot.account_count" /></n-card></n-gi>
      <n-gi span="4 m:1"><n-card size="small"><n-statistic label="总现金" :value="fmt(snapshot.total_cash)" /></n-card></n-gi>
      <n-gi span="4 m:1"><n-card size="small"><n-statistic label="总市值" :value="fmt(snapshot.total_market_value)" /></n-card></n-gi>
      <n-gi span="4 m:1"><n-card size="small"><n-statistic label="总权益" :value="fmt(snapshot.total_equity)" /></n-card></n-gi>
    </n-grid>

    <n-card title="账户" size="small" style="margin-top: 12px">
      <n-table v-if="accounts.length" size="small">
        <thead><tr><th>ID</th><th>名称</th><th>券商</th><th>市场</th><th>币种</th></tr></thead>
        <tbody>
          <tr v-for="a in accounts" :key="a.id">
            <td>{{ a.id }}</td><td>{{ a.name }}</td><td>{{ a.broker || '—' }}</td>
            <td><n-tag size="small">{{ a.market }}</n-tag></td><td>{{ a.base_currency }}</td>
          </tr>
        </tbody>
      </n-table>
      <n-alert v-else type="default">暂无账户，点击「新建账户」开始。</n-alert>
    </n-card>

    <n-card v-if="snapshot?.positions?.length" title="持仓" size="small" style="margin-top: 12px">
      <n-table size="small">
        <thead>
          <tr>
            <th v-for="k in Object.keys(snapshot.positions[0])" :key="k">{{ k }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(p, i) in snapshot.positions" :key="i">
            <td v-for="k in Object.keys(snapshot.positions[0])" :key="k">{{ (p as any)[k] }}</td>
          </tr>
        </tbody>
      </n-table>
    </n-card>

    <n-modal v-model:show="showCreate" preset="card" title="新建组合账户" style="max-width: 460px">
      <n-form label-placement="left" label-width="80">
        <n-form-item label="名称"><n-input v-model:value="form.name" placeholder="如：主账户" /></n-form-item>
        <n-form-item label="券商"><n-input v-model:value="form.broker" placeholder="可选" /></n-form-item>
        <n-form-item label="市场"><n-select v-model:value="form.market" :options="marketOptions" /></n-form-item>
        <n-form-item label="币种"><n-input v-model:value="form.base_currency" placeholder="CNY" /></n-form-item>
        <n-space justify="end"><n-button type="primary" @click="onCreate" :disabled="!form.name">创建</n-button></n-space>
      </n-form>
    </n-modal>

    <n-modal v-model:show="showTrade" preset="card" title="记录交易" style="max-width: 520px">
      <n-form label-placement="left" label-width="80">
        <n-form-item label="账户">
          <n-select
            v-model:value="tradeForm.account_id"
            :options="accounts.map(a => ({ label: `#${a.id} ${a.name}`, value: a.id }))"
            placeholder="选择账户"
          />
        </n-form-item>
        <n-form-item label="代码"><n-input v-model:value="tradeForm.symbol" placeholder="如 600519" /></n-form-item>
        <n-form-item label="日期"><n-input v-model:value="tradeForm.trade_date" placeholder="YYYY-MM-DD" /></n-form-item>
        <n-form-item label="方向"><n-select v-model:value="tradeForm.side" :options="sideOptions" /></n-form-item>
        <n-form-item label="数量"><n-input-number v-model:value="tradeForm.quantity" :min="1" style="width: 100%" /></n-form-item>
        <n-form-item label="价格"><n-input-number v-model:value="tradeForm.price" :min="0.0001" :step="0.01" style="width: 100%" /></n-form-item>
        <n-form-item label="费用"><n-input-number v-model:value="tradeForm.fee" :min="0" :step="0.01" style="width: 100%" /></n-form-item>
        <n-form-item label="税"><n-input-number v-model:value="tradeForm.tax" :min="0" :step="0.01" style="width: 100%" /></n-form-item>
        <n-form-item label="备注"><n-input v-model:value="tradeForm.note" /></n-form-item>
        <n-space justify="end">
          <n-button type="primary" @click="onRecordTrade" :disabled="!tradeForm.account_id || !tradeForm.symbol || !tradeForm.price">记录</n-button>
        </n-space>
      </n-form>
    </n-modal>
  </div>
</template>

<style scoped>
.pf-view { max-width: 1080px; }
</style>
