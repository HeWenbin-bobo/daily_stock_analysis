<script setup lang="ts">
/**
 * 历史记录视图
 *
 * Phase 2: 查看历史分析记录、删除、查看详情
 */
import { ref, onMounted, h } from 'vue'
import {
  NCard,
  NDataTable,
  NButton,
  NSpace,
  NText,
  NTag,
  NEmpty,
  NSpin,
  NPopconfirm,
  NModal,
  type DataTableColumns,
} from 'naive-ui'
import {
  getAnalysisHistory,
  deleteAnalysisRun,
  type AnalysisHistoryItem,
  type HotAnalysisStockResult,
} from '../api/hotAnalysis'
import { scoreColor, adviceType, passReasonText, formatDate, extractErrorMessage } from '../utils/formatters'

const emit = defineEmits<{
  (e: 'back'): void
  (e: 'compare', runId: number): void
}>()

const loading = ref(false)
const items = ref<AnalysisHistoryItem[]>([])
const total = ref(0)
const error = ref<string | null>(null)
const page = ref(1)
const pageSize = 20

// 详情弹窗
const showDetail = ref(false)
const detailItem = ref<AnalysisHistoryItem | null>(null)

async function loadHistory() {
  loading.value = true
  error.value = null
  try {
    const data = await getAnalysisHistory(page.value, pageSize)
    items.value = data.items
    total.value = data.total
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '加载历史记录失败')
  } finally {
    loading.value = false
  }
}

async function handleDelete(runId: number) {
  try {
    await deleteAnalysisRun(runId)
    await loadHistory()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '删除失败')
  }
}

function viewDetail(item: AnalysisHistoryItem) {
  detailItem.value = item
  showDetail.value = true
}

onMounted(loadHistory)

const columns: DataTableColumns<AnalysisHistoryItem> = [
  {
    title: 'ID',
    key: 'id',
    width: 60,
  },
  {
    title: '时间',
    key: 'started_at',
    width: 170,
    render: (row) => {
      const d = new Date(row.started_at)
      return `${formatDate(d)} ${d.toLocaleTimeString()}`
    },
  },
  {
    title: '状态',
    key: 'status',
    width: 80,
    render: (row) => {
      // ★ 修复(H6)：cancelled 是用户主动触发的正常终态，不应标红（旧实现落入 error）。
      //   timeout 是超时（warning）；cancelled 用 warning 与"已取消"黄色警示一致。
      const type = row.status === 'ok' ? 'success'
        : (row.status === 'timeout' || row.status === 'cancelled') ? 'warning'
        : 'error'
      return h(NTag, { type, size: 'small', bordered: false }, () => row.status)
    },
  },
  {
    title: '交集',
    key: 'raw_intersection_count',
    width: 80,
  },
  {
    title: '分析',
    key: 'analyzed_stock_count',
    width: 70,
  },
  {
    title: '通过',
    key: 'passed_stock_count',
    width: 70,
    render: (row) =>
      h(
        'span',
        { style: `color: ${row.passed_stock_count > 0 ? '#16a34a' : '#94a3b8'}; font-weight: 600` },
        String(row.passed_stock_count),
      ),
  },
  {
    title: '耗时',
    key: 'duration_seconds',
    width: 80,
    render: (row) => `${Math.round(row.duration_seconds)}s`,
  },
  {
    title: '操作',
    key: 'actions',
    width: 200,
    render: (row) =>
      h(NSpace, { size: 4 }, () => [
        h(
          NButton,
          { size: 'tiny', class: 'btn-apply', 'aria-label': `查看历史记录 #${row.id} 的详情`, onClick: () => viewDetail(row), style: 'border-radius: 6px' },
          () => '📊 详情',
        ),
        h(
          NButton,
          { size: 'tiny', class: 'btn-compare', 'aria-label': `以历史记录 #${row.id} 作为对比基准`, onClick: () => emit('compare', row.id), style: 'border-radius: 6px' },
          () => '🔄 对比',
        ),
        h(
          NPopconfirm,
          { onPositiveClick: () => handleDelete(row.id) },
          {
            trigger: () =>
              h(NButton, { size: 'tiny', class: 'btn-danger', 'aria-label': `删除历史记录 #${row.id}`, style: 'border-radius: 6px' }, () => '🗑 删除'),
            default: () => '确定删除此记录？',
          },
        ),
      ]),
  },
]

// 详情表格列
const detailColumns: DataTableColumns<HotAnalysisStockResult> = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name', width: 110 },
  {
    title: '均分',
    key: 'avg_score',
    width: 80,
    render: (r) =>
      h('span', { style: `color: ${scoreColor(r.avg_score)}; font-weight: 600` }, String(r.avg_score)),
  },
  {
    title: '建议',
    key: 'final_advice',
    width: 100,
    render: (r) =>
      h(NTag, { type: adviceType(r.final_advice), bordered: false, size: 'small' }, () => r.final_advice || '-'),
  },
  { title: '命中榜', key: 'in_sources', render: (r) => r.in_sources.join('+') },
  {
    title: '通过原因',
    key: 'pass_reason',
    width: 100,
    render: (r) => passReasonText(r.pass_reason),
  },
]

// h is imported at the top of the script
</script>

<template>
  <n-card
    :bordered="false"
    style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06)"
  >
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px">
      <n-space align="center">
        <n-button class="btn-back" aria-label="返回分析主页" @click="$emit('back')" style="border-radius: 8px">← 返回</n-button>
        <n-text style="font-size: 16px; font-weight: 600">📚 历史记录</n-text>
        <n-text depth="3">共 {{ total }} 条</n-text>
      </n-space>
    </div>

    <n-alert v-if="error" type="error" :show-icon="true" style="margin-bottom: 12px" closable @close="error = null" role="alert">{{ error }}</n-alert>
    <n-spin :show="loading" role="status" aria-live="polite">
      <n-empty v-if="!loading && items.length === 0" description="暂无历史记录" />

      <n-data-table
        v-else
        :columns="columns"
        :data="items"
        :row-key="(r: AnalysisHistoryItem) => r.id"
        :pagination="total > pageSize ? { page, pageSize, itemCount: total, onUpdatePage: (p: number) => { page = p; loadHistory() } } : false"
        size="small"
      />
    </n-spin>

    <!-- 详情弹窗 -->
    <n-modal
      v-model:show="showDetail"
      preset="card"
      title="分析详情"
      style="width: 90%; max-width: 1000px; max-height: 80vh; overflow: auto"
    >
      <div v-if="detailItem" style="margin-bottom: 16px">
        <n-space>
          <n-text>ID: {{ detailItem.id }}</n-text>
          <n-text>状态: {{ detailItem.status }}</n-text>
          <n-text>耗时: {{ detailItem.duration_seconds != null ? Math.round(detailItem.duration_seconds) : '-' }}s</n-text>
          <n-text>通过: {{ detailItem.passed_stock_count }}/{{ detailItem.analyzed_stock_count }}</n-text>
        </n-space>
      </div>
      <n-data-table
        v-if="detailItem"
        :columns="detailColumns"
        :data="detailItem.results"
        :row-key="(r: HotAnalysisStockResult) => r.code"
        :pagination="false"
        size="small"
      />
    </n-modal>
  </n-card>
</template>
