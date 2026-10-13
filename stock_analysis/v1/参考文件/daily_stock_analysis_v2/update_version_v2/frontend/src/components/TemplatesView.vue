<script setup lang="ts">
/**
 * 分析模板视图 —— 保存/加载过滤规则预设
 */
import { ref, onMounted } from 'vue'
import {
  NCard, NButton, NSpace, NText, NTag, NDataTable, NInput, NEmpty,
  NPopconfirm, NSpin, NModal, NInputNumber, NCheckboxGroup, NCheckbox,
  NSwitch, NAlert,
} from 'naive-ui'
import { getTemplates, saveTemplate, deleteTemplate, type AnalysisTemplate } from '../api/hotAnalysis'
import { extractErrorMessage } from '../utils/formatters'
import { h } from 'vue'

const emit = defineEmits<{
  (e: 'back'): void
  (e: 'apply', config: Record<string, unknown>): void
}>()

const loading = ref(false)
const items = ref<AnalysisTemplate[]>([])
const showModal = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)

// 新模板表单
const formName = ref('')
const formDesc = ref('')
const formMinScore = ref(60)
const formMaxIntersection = ref(10)
const formAllowedAdvice = ref(['买入', '加仓', '持有', 'BUY', 'HOLD', 'ADD'])
const formExcludeKeywords = ref(['卖出', '减仓', 'SELL', 'REDUCE'])
const formRequireAll = ref(true)
const formIsDefault = ref(false)

const ADVICE_OPTIONS = ['买入', '加仓', '持有', 'BUY', 'HOLD', 'ADD']
const KEYWORD_OPTIONS = ['卖出', '减仓', 'SELL', 'REDUCE']

async function load() {
  loading.value = true
  try {
    const data = await getTemplates()
    items.value = data.items
  } catch (e: unknown) {
    // ★ round 6 audit：用户可见而非 console
    error.value = extractErrorMessage(e, '加载模板失败')
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  if (!formName.value.trim()) return
  saving.value = true
  error.value = null
  try {
    await saveTemplate({
      name: formName.value.trim(),
      description: formDesc.value.trim(),
      is_default: formIsDefault.value,
      config: {
        min_score: formMinScore.value,
        max_intersection: formMaxIntersection.value,
        allowed_advice: formAllowedAdvice.value,
        exclude_keywords: formExcludeKeywords.value,
        require_all_rounds: formRequireAll.value,
      },
    })
    showModal.value = false
    formName.value = ''
    formDesc.value = ''
    formIsDefault.value = false
    await load()
  } catch (e: unknown) {
    // ★ round 6 audit：用户可见
    error.value = extractErrorMessage(e, '保存模板失败')
  } finally {
    saving.value = false
  }
}

async function handleDelete(id: number) {
  try {
    await deleteTemplate(id)
    await load()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '删除模板失败')
  }
}

function handleApply(t: AnalysisTemplate) {
  emit('apply', t.config)
}

onMounted(load)

const columns = [
  { title: '名称', key: 'name', width: 120, render: (r: AnalysisTemplate) => h('strong', {}, r.name) },
  { title: '说明', key: 'description', render: (r: AnalysisTemplate) => r.description || '-' },
  {
    title: '配置', key: 'config', width: 280,
    render: (r: AnalysisTemplate) => h(NSpace, { size: 4 }, () => [
      h(NTag, { size: 'small', bordered: false }, () => `评分≥${r.config.min_score ?? 60}`),
      h(NTag, { size: 'small', bordered: false, type: r.config.require_all_rounds ? 'info' : 'warning' }, () => r.config.require_all_rounds ? '全轮通过' : '多数轮通过'),
    ]),
  },
  {
    title: '操作', key: 'actions', width: 180,
    render: (r: AnalysisTemplate) => h(NSpace, { size: 6 }, () => [
      h(NButton, { size: 'tiny', class: 'btn-apply', 'aria-label': `应用模板 ${r.name}`, onClick: () => handleApply(r), style: 'border-radius: 6px' }, () => '⚡ 应用'),
      h(NPopconfirm, { onPositiveClick: () => handleDelete(r.id) },
        { trigger: () => h(NButton, { size: 'tiny', class: 'btn-danger', 'aria-label': `删除模板 ${r.name}`, style: 'border-radius: 6px' }, () => '🗑 删除'), default: () => '确定删除？' },
      ),
    ]),
  },
]
</script>

<template>
  <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px">
      <n-space align="center">
        <n-button class="btn-back" aria-label="返回分析主页" @click="$emit('back')" style="border-radius: 8px">← 返回</n-button>
        <n-text style="font-size: 16px; font-weight: 600">📋 分析模板</n-text>
        <n-text depth="3">保存/加载过滤规则预设</n-text>
      </n-space>
      <n-button class="btn-create" aria-label="新建分析模板" @click="showModal = true" style="border-radius: 8px">+ 新建模板</n-button>
    </div>

    <n-alert v-if="error" type="error" :show-icon="true" style="margin-bottom: 12px" closable @close="error = null" role="alert">{{ error }}</n-alert>
    <n-spin :show="loading" role="status" aria-live="polite">
      <n-empty v-if="!loading && items.length === 0" description="暂无模板，点击右上角新建" />
      <n-data-table v-else :columns="columns" :data="items" :row-key="(r: AnalysisTemplate) => r.id" :pagination="false" size="small" />
    </n-spin>

    <!-- 新建模板弹窗 -->
    <n-modal v-model:show="showModal" preset="card" title="新建分析模板" style="width: 500px">
      <n-space vertical style="gap: 14px">
        <n-input v-model:value="formName" placeholder="模板名称（如：激进型）" />
        <n-input v-model:value="formDesc" placeholder="说明（可选）" type="textarea" :rows="2" />
        <div>
          <n-text>最低评分：</n-text>
          <n-input-number v-model:value="formMinScore" :min="0" :max="100" :step="5" style="width: 120px" size="small" />
        </div>
        <div>
          <n-text>分析数量：</n-text>
          <n-input-number v-model:value="formMaxIntersection" :min="1" :max="500" :step="1" style="width: 120px" size="small" />
        </div>
        <div>
          <n-text>允许建议：</n-text>
          <n-checkbox-group v-model:value="formAllowedAdvice">
            <n-space><n-checkbox v-for="o in ADVICE_OPTIONS" :key="o" :value="o" :label="o" /></n-space>
          </n-checkbox-group>
        </div>
        <div>
          <n-text>排除关键词：</n-text>
          <n-checkbox-group v-model:value="formExcludeKeywords">
            <n-space><n-checkbox v-for="o in KEYWORD_OPTIONS" :key="o" :value="o" :label="o" /></n-space>
          </n-checkbox-group>
        </div>
        <div style="display: flex; align-items: center; gap: 10px">
          <n-text>要求所有轮通过：</n-text>
          <n-switch v-model:value="formRequireAll" />
        </div>
        <div style="display: flex; align-items: center; gap: 10px">
          <n-text>设为默认模板：</n-text>
          <n-switch v-model:value="formIsDefault" />
          <n-text depth="3" style="font-size: 12px">同一时间只能有一个默认模板</n-text>
        </div>
      </n-space>
      <template #action>
        <n-space justify="end">
          <n-button class="btn-cancel" @click="showModal = false" style="border-radius: 8px">取消</n-button>
          <n-button class="btn-confirm" :loading="saving" :disabled="!formName.trim()" @click="handleSave" style="border-radius: 8px">💾 保存模板</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-card>
</template>
