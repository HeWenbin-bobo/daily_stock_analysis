<script setup lang="ts">
/**
 * 阶段 1 预览结果卡片
 *
 * Phase 1: 新增交集股票列表展示（可折叠）
 */
import type { HotAnalysisPreviewResponse, IntersectionStock } from '../api/hotAnalysis'

defineProps<{
  preview: HotAnalysisPreviewResponse
}>()

defineEmits<{
  (e: 'confirm'): void
  (e: 'cancel'): void
}>()

const stockColumns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name', width: 110 },
  {
    title: '命中榜',
    key: 'in_sources',
    render: (r: IntersectionStock) => r.in_sources.join(' + '),
  },
]
</script>

<template>
  <n-card
    style="
      margin-top: 18px;
      border-radius: 16px;
      background: var(--accent-soft);
      border: 1px solid var(--accent-border);
      box-shadow: 0 4px 18px rgba(37,99,235,0.10);
      overflow: hidden;
      position: relative;
    "
  >
    <!-- 装饰光晕 -->
    <div style="
      position: absolute; top: -30px; right: -30px;
      width: 120px; height: 120px;
      background: radial-gradient(circle, rgba(255,255,255,0.4), transparent);
      border-radius: 50%;
    "></div>

    <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 16px; position: relative; z-index: 1">
      <div style="
        font-size: 32px;
        width: 56px; height: 56px;
        display: flex; align-items: center; justify-content: center;
        background: var(--color-card);
        border: 1px solid var(--accent-border);
        border-radius: 14px;
        box-shadow: 0 2px 8px rgba(37,99,235,0.08);
      ">📋</div>
      <div style="flex: 1; min-width: 240px">
        <div style="font-size: 16px; font-weight: 700; color: var(--text-primary); letter-spacing: 0.3px">
          阶段 1 完成：抓取 + 交集计算
        </div>
        <div
          style="
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            font-size: 14px;
            color: var(--text-secondary);
          "
        >
          <div style="display: flex; align-items: center; gap: 6px; background: var(--color-card); padding: 4px 12px; border-radius: 8px; border: 1px solid var(--color-border)">
            <span>🎯</span>
            <strong style="color: var(--accent); font-size: 16px">{{ preview.raw_intersection_count }}</strong>
            <span>只交集</span>
          </div>
          <div style="display: flex; align-items: center; gap: 6px; background: var(--color-card); padding: 4px 12px; border-radius: 8px; border: 1px solid var(--color-border)">
            <span>🔍</span>
            剔688后
            <strong style="color: var(--accent); font-size: 16px">{{ preview.filtered_intersection_count }}</strong>
            只
          </div>
          <div style="display: flex; align-items: center; gap: 6px; background: rgba(220,38,38,0.08); padding: 4px 12px; border-radius: 8px">
            <span>🚀</span>
            实际分析
            <strong style="color: var(--danger); font-size: 18px">{{ preview.target_count }}</strong>
            只
          </div>
        </div>
        <div style="margin-top: 10px; font-size: 13px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px">
          <span style="font-size: 15px">⏱</span>
          <span>预估 <strong style="font-size: 15px; color: var(--text-primary)">{{ preview.estimated_human }}</strong></span>
          <span style="opacity: 0.7">（~140s/轮 × {{ preview.target_count }}只 × {{ preview.rounds }}轮，Agent 模式实测）</span>
        </div>
      </div>
      <n-space vertical align="center">
        <n-button
          size="large"
          class="btn-confirm"
          aria-label="确认开始分析"
          @click="$emit('confirm')"
          style="min-width: 160px; height: 48px; font-size: 17px; border-radius: 12px"
        >
          ⚡ 确认开始分析
        </n-button>
        <n-button size="small" class="btn-cancel" aria-label="取消预览" @click="$emit('cancel')" style="border-radius: 8px">
          取消
        </n-button>
      </n-space>
    </div>

    <!-- 交集股票列表（可折叠） -->
    <n-collapse v-if="preview.intersection_stocks?.length" style="margin-top: 16px">
      <n-collapse-item
        :title="`📋 交集股票列表（${preview.intersection_stocks.length} 只）`"
        aria-label="查看交集股票列表"
        name="stocks"
      >
        <n-data-table
          :columns="stockColumns"
          :data="preview.intersection_stocks"
          :row-key="(r: IntersectionStock) => r.code"
          :pagination="{ pageSize: 20 }"
          size="small"
          :max-height="300"
        />
      </n-collapse-item>
    </n-collapse>
  </n-card>
</template>
