<script setup lang="ts">
/**
 * 🧠 深度研究 —— v3 融合 ai-berkshire 四大师价值投资框架
 *
 * - 输入股票代码 → 调后端 /deep-research（四大师综合：巴菲特/芒格/段永平/李录）
 * - 展示：信息丰富度评级 / 六维评分表 / 综合结论 / 操作建议 / 估值区间 / 最大风险
 * - LLM 不可用时后端返回 mock 占位（mocked=true 提示）
 */
import { ref, onMounted, h } from 'vue'
import {
  NCard, NInput, NButton, NSpace, NText, NTag, NSpin, NAlert,
  NDataTable, NEmpty, NGrid, NGi, NCollapse, NCollapseItem,
} from 'naive-ui'
import {
  runDeepResearch,
  getDeepResearchReport,
  getBerkshireToolsHealth,
  type DeepResearchResult,
} from '../api/hotAnalysis'
import { extractErrorMessage } from '../utils/formatters'

const code = ref('600519')
const loading = ref(false)
const report = ref<DeepResearchResult | null>(null)
const markdown = ref('')
const error = ref<string | null>(null)
const toolsHealth = ref<{ financial_rigor_importable: boolean } | null>(null)

async function run() {
  const c = code.value.trim()
  if (!c) return
  loading.value = true
  error.value = null
  markdown.value = ''
  try {
    report.value = await runDeepResearch(c)
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '深度研究失败')
    report.value = null
  } finally {
    loading.value = false
  }
}

async function exportReport() {
  if (!report.value) return
  try {
    const r = await getDeepResearchReport(report.value.code)
    markdown.value = r.markdown
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '生成报告失败')
  }
}

const scoreColumns = [
  { title: '维度', key: 'dimension', width: 170 },
  {
    title: '分数',
    key: 'score',
    width: 70,
    // ★ 探索期修复：NDataTable render 返回字符串不会渲染 HTML（显示为字面文本），
    //   必须返回 VNode（h 函数）才能生效
    render: (r: { score: number }) => {
      const s = Number(r.score)
      const color = s >= 70 ? '#16a34a' : s >= 50 ? '#d97706' : '#dc2626'
      return h('span', { style: `font-weight:700;color:${color}` }, Number.isFinite(s) ? s : '-')
    },
  },
  { title: '信心度', key: 'confidence', width: 90 },
  { title: '理由', key: 'reason', ellipsis: { tooltip: true } },
]

function scoreRowKey(r: { dimension: string }): string {
  return r.dimension
}

function conclusionType(c: string): 'success' | 'warning' | 'error' | 'default' {
  if (c.includes('买入')) return 'success'
  if (c.includes('回避')) return 'error'
  if (c.includes('观望')) return 'warning'
  return 'default'
}

onMounted(async () => {
  try {
    toolsHealth.value = await getBerkshireToolsHealth()
  } catch {
    toolsHealth.value = null
  }
})
</script>

<template>
  <div style="display: flex; flex-direction: column; gap: 16px">
    <!-- 输入区 -->
    <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06)">
      <div style="margin-bottom: 12px">
        <n-text style="font-size: 16px; font-weight: 600">🧠 深度研究（四大师价值投资框架）</n-text>
        <n-text depth="3" style="margin-left: 12px; font-size: 13px">
          融合 ai-berkshire：巴菲特 · 芒格 · 段永平 · 李录 综合分析，强制结论 + 价格区间
        </n-text>
      </div>
      <n-space align="center" :wrap="true">
        <n-input
          v-model:value="code"
          placeholder="输入股票代码，例如：600519"
          style="max-width: 280px"
          @keydown.enter="run"
          aria-label="深度研究股票代码"
        />
        <n-button type="primary" :loading="loading" @click="run" aria-label="开始四大师深度研究">
          🔬 开始研究
        </n-button>
        <n-button v-if="report" quaternary @click="exportReport" aria-label="导出 Markdown 报告">
          📄 导出报告
        </n-button>
        <n-tag v-if="toolsHealth" :type="toolsHealth.financial_rigor_importable ? 'success' : 'warning'" size="small" :bordered="false">
          {{ toolsHealth.financial_rigor_importable ? '✓ financial_rigor 已集成' : '⚠ financial_rigor 不可用' }}
        </n-tag>
      </n-space>
    </n-card>

    <n-alert v-if="error" type="error" :show-icon="true" role="alert" aria-live="assertive">
      {{ error }}
    </n-alert>

    <n-spin :show="loading">
      <n-empty v-if="!loading && !report" description="输入股票代码，点击「开始研究」进行四大师深度分析" style="margin: 32px 0" />

      <template v-if="report">
        <n-alert
          v-if="report.mocked"
          type="warning"
          :show-icon="true"
          title="LLM 未配置真实 API Key，当前为占位分析"
          style="margin-bottom: 12px"
        >
          后端未检测到可用的 LLM 配置，返回的是占位响应。配置 DEEPSEEK_API_KEY / GEMINI_API_KEY 等后即可获得真实四大师分析。
        </n-alert>

        <!-- 头部摘要 -->
        <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
          <n-space justify="space-between" align="center" wrap>
            <n-space align="center">
              <n-text style="font-size: 20px; font-weight: 700">{{ report.name }}({{ report.code }})</n-text>
              <n-tag :type="conclusionType(report.conclusion)" size="medium" round :bordered="false">
                {{ report.conclusion }}
              </n-tag>
              <n-tag size="small" :bordered="false">置信度：{{ report.confidence }}</n-tag>
              <n-tag v-if="report.provider" size="small" :bordered="false">模型：{{ report.provider }}</n-tag>
            </n-space>
            <n-space v-if="report.quote.price != null">
              <n-text style="font-size: 18px; font-weight: 700">
                ¥{{ report.quote.price }}
              </n-text>
              <n-text depth="3" style="font-size: 12px">
                {{ report.quote.change_pct }}% · 来源 {{ report.quote.source }}
              </n-text>
            </n-space>
          </n-space>
          <div style="margin-top: 8px; font-size: 13px; color: var(--text-secondary)">
            信息丰富度评级：<n-tag size="small" :type="report.info_richness.grade === 'A' ? 'success' : report.info_richness.grade === 'B' ? 'warning' : 'error'" :bordered="false">
              {{ report.info_richness.grade }} 级
            </n-tag>
            {{ report.info_richness.note }}
          </div>
        </n-card>

        <!-- 六维评分 -->
        <n-card title="📊 四大师六维评分" :bordered="false" style="border-radius: 14px">
          <n-data-table
            :columns="scoreColumns"
            :data="report.scores"
            :bordered="false"
            size="small"
            :row-key="scoreRowKey"
          />
        </n-card>

        <!-- 结论 + 建议 + 估值 -->
        <n-grid :cols="2" :x-gap="12" :y-gap="12" responsive="screen">
          <n-gi>
            <n-card title="💡 操作建议" :bordered="false" size="small" style="border-radius: 12px">
              <div v-for="(v, k) in report.advice" :key="k" style="margin-bottom: 8px; font-size: 13px">
                <n-text strong style="font-size: 13px">{{ { empty_position: '空仓者', holder: '持仓者', add_signal: '加仓信号', sell_signal: '卖出信号' }[k] || k }}：</n-text>
                <n-text depth="2">{{ v }}</n-text>
              </div>
              <n-empty v-if="!Object.keys(report.advice || {}).length" description="暂无操作建议" size="small" />
            </n-card>
          </n-gi>
          <n-gi>
            <n-card title="💰 估值区间" :bordered="false" size="small" style="border-radius: 12px">
              <div style="margin-bottom: 8px; font-size: 13px">
                <n-text strong>合理买入区间：</n-text>
                <n-text depth="2">{{ report.price_range.buy_zone || '-' }}</n-text>
              </div>
              <div style="font-size: 13px">
                <n-text strong>高估区间：</n-text>
                <n-text depth="2">{{ report.price_range.overvalued_zone || '-' }}</n-text>
              </div>
              <n-alert v-if="report.cap_verify?.ok" type="info" :show-icon="true" style="margin-top: 10px" title="市值验算（financial_rigor）">
                <!-- ★ skipped：没有报告市值可比对时不显示"偏差 %"，否则会渲染成"偏差 0%"误导用户 -->
                <template v-if="report.cap_verify.skipped">
                  计算市值 {{ report.cap_verify.computed_cap }}（{{ report.cap_verify.note || '无报告市值可比对' }}）
                </template>
                <template v-else>
                  计算市值 {{ report.cap_verify.computed_cap }} vs 报告市值 {{ report.cap_verify.reported_cap }}，偏差 {{ report.cap_verify.diff_pct }}%
                </template>
              </n-alert>
            </n-card>
          </n-gi>
        </n-grid>

        <!-- 最大风险 -->
        <n-card title="⚠️ 最大风险（芒格视角）" :bordered="false" style="border-radius: 14px">
          <n-space vertical :size="8">
            <n-alert v-for="(r, i) in report.risks" :key="i" type="warning" :show-icon="false" size="small">
              {{ r }}
            </n-alert>
            <n-empty v-if="!report.risks.length" description="暂无风险条目" size="small" />
          </n-space>
        </n-card>

        <!-- 详细 -->
        <n-collapse>
          <n-collapse-item v-if="report.analysis_confidence_note" title="📋 AI 分析置信度说明" name="conf">
            <n-text depth="2">{{ report.analysis_confidence_note }}</n-text>
          </n-collapse-item>
          <n-collapse-item v-if="report.verify_list?.length" title="🔍 需人工核验清单" name="verify">
            <ul style="margin: 0; padding-left: 20px">
              <li v-for="(v, i) in report.verify_list" :key="i">{{ v }}</li>
            </ul>
          </n-collapse-item>
          <n-collapse-item v-if="markdown" title="📄 Markdown 报告" name="md">
            <pre style="white-space: pre-wrap; word-break: break-word; background: #f8fafc; padding: 12px; border-radius: 8px; font-size: 12px; line-height: 1.6; max-height: 400px; overflow: auto">{{ markdown }}</pre>
          </n-collapse-item>
        </n-collapse>

        <n-text depth="3" style="font-size: 11px; display: block; margin-top: 8px">
          {{ report.disclaimer }}
        </n-text>
      </template>
    </n-spin>
  </div>
</template>
