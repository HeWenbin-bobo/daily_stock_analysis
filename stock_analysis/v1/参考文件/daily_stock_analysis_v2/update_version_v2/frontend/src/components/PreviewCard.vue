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
      background: linear-gradient(135deg, #fef3c7 0%, #fde68a 50%, #fcd34d 100%);
      box-shadow: 0 4px 20px rgba(251,191,36,0.25), 0 0 0 1px rgba(251,191,36,0.1);
      overflow: hidden;
      position: relative;
    "
  >
    <!-- 装饰光晕 -->
    <div style="
      position: absolute; top: -30px; right: -30px;
      width: 120px; height: 120px;
      background: radial-gradient(circle, rgba(255,255,255,0.3), transparent);
      border-radius: 50%;
    "></div>

    <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 16px; position: relative; z-index: 1">
      <div style="
        font-size: 32px;
        width: 56px; height: 56px;
        display: flex; align-items: center; justify-content: center;
        background: rgba(255,255,255,0.5);
        border-radius: 14px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
      ">📋</div>
      <div style="flex: 1; min-width: 240px">
        <div style="font-size: 16px; font-weight: 700; color: #78350f; letter-spacing: 0.3px">
          阶段 1 完成：抓取 + 交集计算
        </div>
        <div
          style="
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            font-size: 14px;
            color: #422006;
          "
        >
          <div style="display: flex; align-items: center; gap: 6px; background: rgba(255,255,255,0.4); padding: 4px 12px; border-radius: 8px">
            <span>🎯</span>
            <strong style="color: #b45309; font-size: 16px">{{ preview.raw_intersection_count }}</strong>
            <span>只交集</span>
          </div>
          <div style="display: flex; align-items: center; gap: 6px; background: rgba(255,255,255,0.4); padding: 4px 12px; border-radius: 8px">
            <span>🔍</span>
            剔688后
            <strong style="color: #b45309; font-size: 16px">{{ preview.filtered_intersection_count }}</strong>
            只
          </div>
          <div style="display: flex; align-items: center; gap: 6px; background: rgba(185,28,28,0.08); padding: 4px 12px; border-radius: 8px">
            <span>🚀</span>
            实际分析
            <strong style="color: #b91c1c; font-size: 18px">{{ preview.target_count }}</strong>
            只
          </div>
        </div>
        <div style="margin-top: 10px; font-size: 13px; color: #92400e; display: flex; align-items: center; gap: 6px">
          <span style="font-size: 15px">⏱</span>
          <span>预估 <strong style="font-size: 15px">{{ preview.estimated_human }}</strong></span>
          <span style="opacity: 0.7">（~50s/调用 × {{ preview.target_count }}只 × {{ preview.rounds }}轮）</span>
        </div>
      </div>
      <n-space vertical align="center">
        <n-button
          size="large"
          class="btn-confirm btn-pulse"
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
