<template>
  <div class="hyperopt-view">
    <n-card title="⚙️ 策略参数优化 (Hyperopt)" size="small" class="hyperopt-card">
      <n-alert type="info" :show-icon="true" class="mb-3">
        <b>样本外验证提示：</b>优化只在 <b>train 段（前 80% 时间）</b> 进行，<b>test 段（后 20%）</b> 单独报告。
        优化结果存在过拟合风险，样本外表现可能明显衰减 —— 请以样本外结果为准。
      </n-alert>

      <n-form label-placement="left" label-width="90" inline class="hyperopt-form">
        <n-form-item label="股票代码">
          <n-input v-model:value="symbol" placeholder="如 600519" style="width: 120px" />
        </n-form-item>
        <n-form-item label="策略">
          <n-select
            v-model:value="strategyId"
            :options="strategyOptions"
            placeholder="选择策略"
            style="width: 320px"
            filterable
            clearable
          />
        </n-form-item>
        <n-form-item label="epochs">
          <n-input-number v-model:value="epochs" :min="10" :max="1000" :step="10" style="width: 110px" />
        </n-form-item>
        <n-form-item label="loss 函数">
          <n-select v-model:value="lossFn" :options="lossOptions" style="width: 140px" />
        </n-form-item>
        <n-form-item>
          <n-button type="primary" :loading="running" @click="onRun" :disabled="running">
            🚀 开始优化
          </n-button>
        </n-form-item>
      </n-form>
    </n-card>

    <n-card title="📋 历史优化记录" size="small" class="hyperopt-card">
      <div class="mb-2">
        <n-button size="small" quaternary @click="() => loadRuns()">🔄 刷新</n-button>
        <n-button size="small" quaternary @click="() => loadRuns(userId)">我的记录</n-button>
      </div>
      <n-data-table
        :columns="runColumns"
        :data="runs"
        :bordered="false"
        :max-height="420"
        size="small"
      />
    </n-card>

    <n-card v-if="detail" title="🔍 最优参数详情" size="small" class="hyperopt-card">
      <div class="detail-actions">
        <n-button
          type="primary"
          size="small"
          :loading="applying"
          :disabled="!detail?.best_params || detail?.status !== 'completed'"
          @click="onApply"
        >
          ✅ 应用此参数到策略
        </n-button>
        <n-text depth="3" style="font-size: 12px">原 DSL 会自动备份到策略描述，可手工恢复</n-text>
      </div>
      <n-descriptions bordered size="small" :column="2" label-placement="left">
        <n-descriptions-item label="best_loss">{{ detail.best_loss?.toFixed?.(4) ?? '-' }}</n-descriptions-item>
        <n-descriptions-item label="样本外 loss">{{ detail.oos_loss?.toFixed?.(4) ?? '-' }}</n-descriptions-item>
        <n-descriptions-item label="train 段">{{ detail.train_start }} ~ {{ detail.train_end }}</n-descriptions-item>
        <n-descriptions-item label="test 段">{{ detail.test_start }} ~ {{ detail.test_end }}</n-descriptions-item>
      </n-descriptions>

      <div class="section-title">🏆 最优参数</div>
      <n-code :code="paramsText" language="json" :word-wrap="true" class="code-block" />

      <div class="section-title">📈 样本外指标（关键！）</div>
      <!-- ★ 迭代3：样本外无交易 → 醒目警告（防误导） -->
      <n-alert v-if="detail.oos_no_trades" type="warning" :show-icon="true" class="mb-3">
        ⚠️ 样本外 0 笔交易 —— 策略在样本外完全失效（过拟合信号），收益 0% 不代表"持平"，
        请勿应用此参数。建议缩小参数范围或更换策略。
      </n-alert>
      <n-grid :cols="3" x-gap="8" y-gap="8" v-if="detail.oos_metrics">
        <n-grid-item v-for="(v, k) in detail.oos_metrics" :key="k">
          <n-statistic :label="String(k)" :value="fmtStat(v)" />
        </n-grid-item>
      </n-grid>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h } from 'vue'
import axios from 'axios'
import {
  NCard, NAlert, NForm, NFormItem, NInput, NInputNumber, NSelect,
  NButton, NDataTable, NDescriptions, NDescriptionsItem, NCode,
  NGrid, NGridItem, NStatistic, NText,
} from 'naive-ui'

const V2_API_BASE = '/api/v2'
const http = axios.create({ baseURL: V2_API_BASE, timeout: 30000 })
const httpLong = axios.create({ baseURL: V2_API_BASE, timeout: 0 })

const symbol = ref('600519')
const strategyId = ref<number | null>(null)
const strategyOptions = ref<{ label: string; value: number }[]>([])
const epochs = ref(100)
const lossFn = ref('sharpe')
const running = ref(false)
const applying = ref(false)
const runs = ref<any[]>([])
const detail = ref<any>(null)
const userId = ref('')

const lossOptions = [
  { label: 'sharpe（默认）', value: 'sharpe' },
  { label: 'sortino（下行风险）', value: 'sortino' },
  { label: 'multi_metric（综合）', value: 'multi_metric' },
]

const paramsText = computed(() => JSON.stringify(detail.value?.best_params ?? {}, null, 2))

function fmtStat(v: unknown): string {
  if (typeof v === 'number') return v.toFixed(4)
  return String(v ?? '-')
}

const runColumns = [
  { title: 'ID', key: 'id', width: 60 },
  { title: '策略', key: 'strategy_id', width: 70 },
  { title: '代码', key: 'symbol', width: 90 },
  { title: 'loss_fn', key: 'loss_fn', width: 90 },
  { title: 'epochs', key: 'epochs', width: 70 },
  { title: '状态', key: 'status', width: 80 },
  {
    title: 'best_loss', key: 'best_loss', width: 90,
    render: (row: any) => (row.best_loss != null ? Number(row.best_loss).toFixed(4) : '-'),
  },
  {
    title: '样本外收益%', key: 'oos_return', width: 110,
    render: (row: any) => {
      const oos = row.oos_metrics_json ? JSON.parse(row.oos_metrics_json) : null
      // ★ 迭代3：样本外无交易 → 明确标记（防误导用户以为持平）
      if (oos && !(oos.total_trades || 0)) return '⚠️ 0 笔交易'
      return oos?.total_return_pct != null ? `${Number(oos.total_return_pct).toFixed(2)}%` : '-'
    },
  },
  {
    title: '操作', key: 'actions', width: 80,
    render: (row: any) => h(NButton, { size: 'small', quaternary: true, onClick: () => loadDetail(row.id) }, { default: () => '查看' }),
  },
]

async function loadStrategies() {
  try {
    const { data } = await http.get('/backtest/strategies')
    const list = data.strategies ?? []
    // 显示 ID + 名称，有自然语言则附上前缀
    strategyOptions.value = list.map((s: any) => ({
      value: s.id,
      label: `#${s.id} ${s.name || ''}${s.natural_language ? ` · ${String(s.natural_language).slice(0, 24)}` : ''}`,
    }))
    // 默认选中第一条
    if (strategyOptions.value.length > 0 && !strategyId.value) {
      strategyId.value = strategyOptions.value[0].value
    }
  } catch (e: any) {
    alert(`❌ 加载策略列表失败: ${e?.message || e}`)
  }
}

async function onRun() {
  if (!strategyId.value) { alert('请先选择策略'); return }
  running.value = true
  try {
    const { data } = await httpLong.post('/backtest/hyperopt', {
      user_id: userId.value || 'api_test_user',
      strategy_id: strategyId.value,
      symbol: symbol.value,
      epochs: epochs.value,
      loss_fn: lossFn.value,
    })
    alert(`✅ 优化完成！run_id=${data.run_id}\nbest_loss=${Number(data.best_loss).toFixed(4)}\n样本外收益=${data.oos_metrics?.total_return_pct ?? '-'}%${data.oos_no_trades ? '\n\n⚠️ 注意：样本外 0 笔交易！策略可能过拟合，请勿直接应用！' : ''}`)
    detail.value = data
    await loadRuns()
  } catch (e: any) {
    alert(`❌ 优化失败: ${e?.response?.data?.detail || e?.message || e}`)
  } finally {
    running.value = false
  }
}

async function loadRuns(uid?: string) {
  try {
    const { data } = await http.get('/backtest/hyperopt/runs', {
      params: { user_id: uid, limit: 20 },
    })
    runs.value = data.runs
  } catch (e: any) {
    alert(`❌ 加载记录失败: ${e?.message || e}`)
  }
}

async function loadDetail(id: number) {
  try {
    const { data } = await http.get(`/backtest/hyperopt/runs/${id}`)
    detail.value = data
  } catch (e: any) {
    alert(`❌ 加载详情失败: ${e?.message || e}`)
  }
}

// ★ P0：应用最优参数到策略（闭环最后一环）
async function onApply() {
  if (!detail.value?.id) return
  if (!confirm('将把最优参数写回策略的 DSL（原 DSL 备份到策略描述），确认应用？')) return
  applying.value = true
  try {
    const { data } = await http.post(`/backtest/hyperopt/runs/${detail.value.id}/apply`, {
      user_id: userId.value || 'api_test_user',
    })
    alert(`✅ 已应用！策略 #${data.strategy_id} 参数已更新\n${data.backup_hint}`)
    await loadStrategies()  // 刷新策略下拉（DSL 已变）
  } catch (e: any) {
    alert(`❌ 应用失败: ${e?.response?.data?.detail || e?.message || e}`)
  } finally {
    applying.value = false
  }
}

onMounted(() => {
  loadStrategies()
  loadRuns()
})
</script>

<style scoped>
.hyperopt-view { display: flex; flex-direction: column; gap: 12px; }
.hyperopt-card { max-width: 1100px; }
.hyperopt-form { flex-wrap: wrap; }
.mb-2 { margin-bottom: 8px; }
.mb-3 { margin-bottom: 12px; }
.section-title { font-weight: 600; margin: 12px 0 6px; }
.code-block { max-height: 240px; overflow: auto; }
.detail-actions { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
</style>
