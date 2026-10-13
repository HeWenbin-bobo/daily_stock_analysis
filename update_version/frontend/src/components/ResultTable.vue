<script setup lang="ts">
/**
 * 分析结果表格
 *
 * Phase 2: 新增可展开行（显示完整轮次详情）
 */
import { computed, ref, h } from 'vue'
import {
  NTag,
  NDataTable,
  NText,
  NEmpty,
  NSpace,
  NTooltip,
  NCard,
  NGrid,
  NGi,
  NStatistic,
  type DataTableColumns,
} from 'naive-ui'
import type { HotAnalysisStockResult, AnalysisRunItem } from '../api/hotAnalysis'
import {
  scoreColor,
  adviceType,
  passReasonText,
  passReasonType,
  buildAnalysisTip,
} from '../utils/formatters'
import type { SortKey } from '../composables/useHotAnalysis'

const props = defineProps<{
  results: HotAnalysisStockResult[]
  sortKey: SortKey
  sortOrder: 'asc' | 'desc'
}>()

const emit = defineEmits<{
  (e: 'toggleSort', key: SortKey): void
}>()

// 展开行状态
const expandedRowKeys = ref<(string | number)[]>([])

const columns = computed<DataTableColumns<HotAnalysisStockResult>>(() => [
  {
    type: 'expand',
    expandable: () => true,
    renderExpand: (row) => renderExpandedRow(row),
  },
  {
    title: () =>
      h(
        'span',
        {
          onClick: () => emit('toggleSort', 'code'),
          style: 'cursor:pointer',
        },
        `代码 ${props.sortKey === 'code' ? (props.sortOrder === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'code',
    width: 90,
    render: (r) => h('strong', { style: 'font-family: monospace' }, r.code),
  },
  {
    title: () =>
      h(
        'span',
        {
          onClick: () => emit('toggleSort', 'name'),
          style: 'cursor:pointer',
        },
        `名称 ${props.sortKey === 'name' ? (props.sortOrder === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'name',
    width: 110,
  },
  {
    title: () =>
      h(
        'span',
        {
          onClick: () => emit('toggleSort', 'avg_score'),
          style: 'cursor:pointer',
        },
        `平均分 ${props.sortKey === 'avg_score' ? (props.sortOrder === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'avg_score',
    width: 100,
    sorter: (a, b) => a.avg_score - b.avg_score,
    render: (r) =>
      h(
        'span',
        {
          style: `color:${scoreColor(r.avg_score)};font-weight:600;font-size:15px`,
        },
        String(r.avg_score),
      ),
  },
  {
    title: () =>
      h(
        'span',
        {
          onClick: () => emit('toggleSort', 'final_advice'),
          style: 'cursor:pointer',
        },
        `最终建议 ${props.sortKey === 'final_advice' ? (props.sortOrder === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'final_advice',
    width: 110,
    render: (r) =>
      h(
        NTag,
        { type: adviceType(r.final_advice), bordered: false, round: true },
        { default: () => r.final_advice || '-' },
      ),
  },
  {
    title: '命中榜',
    key: 'in_sources',
    width: 200,
    render: (r) =>
      h(NSpace, { size: 4 }, () =>
        r.in_sources.map((s) =>
          h(NTag, { size: 'small', type: 'info', bordered: false }, () => s),
        ),
      ),
  },
  {
    title: '通过原因',
    key: 'pass_reason',
    width: 110,
    render: (r) =>
      h(
        NTag,
        {
          type: passReasonType(r.pass_reason),
          size: 'small',
          bordered: false,
        },
        { default: () => passReasonText(r.pass_reason) },
      ),
  },
  {
    title: '轮详情',
    key: 'analyses',
    render: (r) =>
      h(NSpace, { size: 6 }, () =>
        r.analyses.map((a) =>
          h(
            NTooltip,
            { trigger: 'hover', placement: 'top' },
            {
              trigger: () =>
                h(
                  NTag,
                  {
                    size: 'small',
                    type: a.error ? 'error' : adviceType(a.operation_advice),
                    bordered: false,
                    round: true,
                  },
                  () =>
                    `R${a.round}: ${a.error ? 'ERR' : (a.sentiment_score ?? '-')}`,
                ),
              default: () => buildAnalysisTip(a),
            },
          ),
        ),
      ),
  },
])

/** 渲染展开行内容 */
function renderExpandedRow(row: HotAnalysisStockResult) {
  return h('div', { style: 'padding: 16px; background: var(--color-elevated); border-radius: 8px' }, [
    h('div', { style: 'font-weight: 600; margin-bottom: 12px; font-size: 15px' }, [
      `${row.code} ${row.name} — 各轮分析详情`,
    ]),
    h(NSpace, { vertical: true, size: 12 }, () =>
      row.analyses.map((a) => renderAnalysisCard(a)),
    ),
  ])
}

function renderAnalysisCard(a: AnalysisRunItem) {
  const isError = !!a.error
  const borderColor = isError ? '#fecaca' : '#d1fae5'
  const bgColor = isError ? '#fef2f2' : '#f0fdf4'

  return h(
    'div',
    {
      style: `padding: 12px 16px; border-radius: 8px; border: 1px solid ${borderColor}; background: ${bgColor}`,
    },
    [
      h('div', { style: 'display: flex; align-items: center; gap: 12px; flex-wrap: wrap' }, [
        h(NTag, { type: isError ? 'error' : 'success', size: 'small', bordered: false }, () =>
          isError ? `第 ${a.round} 轮 ❌` : `第 ${a.round} 轮 ✓`,
        ),
        !isError
          ? h(NSpace, { size: 16 }, () => [
              h('span', {}, `评分: ${a.sentiment_score ?? '-'}`),
              h('span', {}, `建议: ${a.operation_advice ?? '-'}`),
              h('span', {}, `趋势: ${a.trend_prediction ?? '-'}`),
              h('span', { style: 'color: #64748b; font-size: 12px' }, `模型: ${a.model_used ?? '-'}`),
              h('span', { style: 'color: #64748b; font-size: 12px' }, `时间: ${a.timestamp ?? '-'}`),
            ])
          : h('span', { style: 'color: #dc2626' }, `错误: ${a.error}`),
      ]),
    ],
  )
}
</script>

<template>
  <n-empty
    v-if="results.length === 0"
    description="暂未发现符合条件的股票"
    size="large"
    style="padding: 48px 0"
  >
    <template #extra>
      <n-text depth="3">
        建议调整：增大分析数量、关闭剔除科创板、提高轮间间隔
      </n-text>
    </template>
  </n-empty>

  <n-data-table
    v-else
    :columns="columns"
    :data="results"
    :row-key="(r: HotAnalysisStockResult) => r.code"
    :expanded-row-keys="expandedRowKeys"
    @update:expanded-row-keys="expandedRowKeys = $event"
    :pagination="results.length > 20 ? { pageSize: 20 } : false"
    size="medium"
    style="margin-top: 4px"
  />
</template>
