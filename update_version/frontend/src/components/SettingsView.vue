<script setup lang="ts">
/**
 * ⚙️ LLM 设置 — 自定义模型 + API key 管理
 *
 * 端点: GET/POST /api/v2/settings/llm
 * 安全设计：
 * - 敏感字段（API key）用 password 输入框（type=password），自带防窥
 * - 「显示明文」开关切换 type=text（仅本机操作时使用）
 * - 服务端永不回传明文 key，只回尾 4 位掩码（••••••••xxxx）
 * - 未修改的 key 提交哨兵值 __MASKED__，服务端保留原值
 * - 保存后立即写入 os.environ 生效，无需重启
 */
import { ref, onMounted, computed } from 'vue'
import {
  NCard, NButton, NAlert, NInput, NSwitch, NSpace, NTag,
  NForm, NFormItem, NText, NDivider, NSelect,
} from 'naive-ui'
import axios from 'axios'

const http = axios.create({ baseURL: '/api/v2', timeout: 30_000 })

const MASK_SENTINEL = '__MASKED__'

interface FieldStatus {
  key: string
  env: string
  sensitive: boolean
  is_set: boolean
  source: 'file' | 'env' | null
  masked?: string | null
  value?: string | null
  default?: string | null
}

const fields = ref<FieldStatus[]>([])
const draft = ref<Record<string, string>>({})   // 用户输入（敏感字段是明文，仅本地）
const reveal = ref<Record<string, boolean>>({}) // 敏感字段明文开关
const saving = ref(false)
const loading = ref(true)
const error = ref<string | null>(null)
const successMsg = ref<string | null>(null)

const GROUPS: { title: string; desc: string; keys: string[] }[] = [
  {
    title: 'OpenAI 兼容（自定义模型推荐入口）',
    desc: '任意 OpenAI 兼容服务：填 base_url + model + key 即可接入自定义/第三方模型（中转、vLLM、OneAPI 等）',
    keys: ['openai_api_key', 'openai_base_url', 'openai_model'],
  },
  {
    title: 'DeepSeek',
    desc: '官方 API（api.deepseek.com，模型 deepseek-chat）',
    keys: ['deepseek_api_key'],
  },
  {
    title: '通义千问 Qwen',
    desc: '阿里云 DashScope（模型 qwen-max）',
    keys: ['qwen_api_key'],
  },
  {
    title: 'Gemini',
    desc: 'Google AI（OpenAI 兼容端点）',
    keys: ['gemini_api_key', 'gemini_base_url', 'gemini_model'],
  },
]

const fieldMeta: Record<string, { label: string; placeholder?: string }> = {
  deepseek_api_key: { label: 'DEEPSEEK_API_KEY', placeholder: 'sk-...' },
  qwen_api_key: { label: 'QWEN_API_KEY（或 DASHSCOPE_API_KEY）', placeholder: 'sk-...' },
  openai_api_key: { label: 'OPENAI_API_KEY', placeholder: 'sk-...' },
  openai_base_url: { label: 'OPENAI_BASE_URL', placeholder: 'https://api.openai.com/v1' },
  openai_model: { label: 'OPENAI_MODEL（自定义模型名）', placeholder: 'gpt-4o-mini' },
  gemini_api_key: { label: 'GEMINI_API_KEY', placeholder: 'AIza...' },
  gemini_base_url: { label: 'GEMINI_BASE_URL', placeholder: 'https://generativelanguage.googleapis.com/v1beta/openai' },
  gemini_model: { label: 'GEMINI_MODEL', placeholder: 'gemini-2.0-flash' },
}

const fieldByKey = computed(() => Object.fromEntries(fields.value.map((f) => [f.key, f])))

async function load() {
  loading.value = true
  error.value = null
  try {
    const r = await http.get('/settings/llm')
    fields.value = r.data.fields || []
    draft.value = {}
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function displayValue(f?: FieldStatus): string {
  if (!f) return ''   // ★ fields 未加载时 fieldByKey[key] 为 undefined，防渲染崩溃
  if (f.sensitive) {
    return draft.value[f.key] ?? ''   // 敏感字段输入框只显示用户新输入，已存值只显示掩码
  }
  return draft.value[f.key] ?? f.value ?? ''
}

function onInput(key: string, v: string) {
  draft.value[key] = v
}

function toggleReveal(key: string) {
  reveal.value[key] = !reveal.value[key]
}

function sourceTag(f: FieldStatus): { label: string; type: 'success' | 'info' | 'default' } | null {
  if (!f.is_set) return null
  if (f.source === 'file') return { label: '已保存', type: 'success' }
  if (f.source === 'env') return { label: '来自环境变量', type: 'info' }
  return null
}

async function onSave() {
  saving.value = true
  error.value = null
  successMsg.value = null
  try {
    // 组装提交：敏感字段空/未动 → 哨兵（保留原值）；非敏感空串 → 恢复默认（后端处理）
    const payload: Record<string, string> = {}
    for (const f of fields.value) {
      const d = draft.value[f.key]
      if (d === undefined) continue           // 用户完全没动过 → 不提交该字段
      if (f.sensitive && d.trim() === '') continue  // 敏感字段清空输入框 = 不修改
      payload[f.key] = d
    }
    const r = await http.post('/settings/llm', { fields: payload })
    const saved: string[] = r.data.saved || []
    successMsg.value = saved.length
      ? `已保存并即时生效: ${saved.join(', ')}（无需重启）`
      : '没有变更需要保存'
    draft.value = {}
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

// ============================================================================
// ★ 拉取上游模型列表 + 测试连接（OpenAI 兼容渠道；其它渠道走手动输入）
// ============================================================================
interface ModelOption { label: string; value: string }
const modelOptions = ref<Record<string, any[]>>({})   // per-group: openai / gemini
const fetchingModels = ref<Record<string, boolean>>({})
const testing = ref<Record<string, boolean>>({})
const testResult = ref<Record<string, { ok: boolean; text: string }>>({})

/** 组的连接参数：优先用草稿（表单当前值），未填的回退已保存/环境变量值 */
function groupConnParams(groupKey: 'openai' | 'gemini'): { base_url: string; api_key?: string; model?: string; api_key_field: string; base_field: string; model_field: string } | null {
  const keyField = groupKey === 'openai' ? 'openai_api_key' : 'gemini_api_key'
  const baseField = groupKey === 'openai' ? 'openai_base_url' : 'gemini_base_url'
  const modelField = groupKey === 'openai' ? 'openai_model' : 'gemini_model'
  const st = fieldByKey.value
  const base = draft.value[baseField] ?? st[baseField]?.value ?? ''
  const keyDraft = draft.value[keyField]
  const params: any = { base_url: base.trim(), api_key_field: keyField, base_field: baseField, model_field: modelField }
  // key：草稿里有新输入就传新值（服务端只在本请求内使用，不落盘）；否则传哨兵让服务端用已保存的
  if (keyDraft !== undefined && keyDraft.trim() !== '') params.api_key = keyDraft.trim()
  else params.api_key = MASK_SENTINEL
  const model = draft.value[modelField] ?? st[modelField]?.value ?? ''
  if (model.trim()) params.model = model.trim()
  return params.base_url ? params : null
}

async function onFetchModels(groupKey: 'openai' | 'gemini') {
  const params = groupConnParams(groupKey)
  if (!params) {
    error.value = '请先填写 base_url'
    return
  }
  fetchingModels.value[groupKey] = true
  error.value = null
  try {
    const r = await http.post('/settings/llm/models', params)
    if (r.data.ok) {
      modelOptions.value[groupKey] = (r.data.models || []).map((m: any) => ({ label: m.id, value: m.id })) as any
      successMsg.value = `已拉取 ${r.data.count ?? (r.data.models || []).length} 个模型`
      if (!(r.data.models || []).length) {
        successMsg.value = '上游返回空模型列表（渠道协议可能不完整，仍可手动输入模型名）'
      }
    } else {
      error.value = `拉取模型失败: ${r.data.error || '未知错误'}`
    }
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '拉取失败'
  } finally {
    fetchingModels.value[groupKey] = false
  }
}

async function onTestConnection(groupKey: 'openai' | 'gemini') {
  const params = groupConnParams(groupKey)
  if (!params) {
    error.value = '请先填写 base_url 和 model'
    return
  }
  if (!params.model) {
    error.value = '请先填写 model（或从拉取的列表中选择）'
    return
  }
  testing.value[groupKey] = true
  testResult.value[groupKey] = null as any
  try {
    const r = await http.post('/settings/llm/test', params, { timeout: 45_000 })
    if (r.data.ok) {
      const reply = (r.data.reply || '').slice(0, 80)
      testResult.value[groupKey] = { ok: true, text: `✅ 连接成功（${r.data.latency_ms}ms）上游回复: ${reply}` }
    } else {
      const upstream = r.data.upstream_body ? ` | 上游: ${r.data.upstream_body.slice(0, 120)}` : ''
      testResult.value[groupKey] = { ok: false, text: `❌ 连接失败（${r.data.latency_ms ?? '?'}ms）: ${r.data.error || ''}${upstream}` }
    }
  } catch (e: any) {
    testResult.value[groupKey] = { ok: false, text: `❌ 请求异常: ${e?.message || e}` }
  } finally {
    testing.value[groupKey] = false
  }
}

/** 渠道是否支持模型拉取/测试（OpenAI 兼容组 + Gemini 兼容组） */
const SUPPORTS_DISCOVERY: Record<string, 'openai' | 'gemini' | null> = {
  'OpenAI 兼容（自定义模型推荐入口）': 'openai',
  'Gemini': 'gemini',
}

onMounted(load)
</script>

<template>
  <div class="settings-view">
    <n-card title="⚙️ LLM 设置" size="small">
      <n-alert type="info" :show-icon="true" style="margin-bottom: 12px">
        在这里配置各 LLM 供应商的 API key 与自定义模型。<b>保存后立即生效，无需重启</b>。
        密钥以 password 形式显示（防窥），服务端只回传尾 4 位掩码，永不返回明文。
        配置存储在本机 <n-text code>backend/data/llm_settings.json</n-text>（权限 600）。
      </n-alert>
      <n-alert type="warning" :show-icon="true" :bordered="false">
        已通过环境变量设置的字段优先级低于此处保存的值；留空某字段表示不修改它。
      </n-alert>
    </n-card>

    <n-alert v-if="error" type="error" style="margin-top: 12px" closable @close="error = null">{{ error }}</n-alert>
    <n-alert v-if="successMsg" type="success" style="margin-top: 12px" closable @close="successMsg = null">{{ successMsg }}</n-alert>

    <n-card v-for="g in GROUPS" :key="g.title" :title="g.title" size="small" style="margin-top: 12px">
      <n-text depth="3" style="font-size: 12px">{{ g.desc }}</n-text>
      <n-form label-placement="left" label-width="240" style="margin-top: 10px">
        <n-form-item v-for="key in g.keys" :key="key" :label="fieldMeta[key]?.label || key">
          <n-space vertical style="width: 100%" :size="4">
            <!-- ★ model 字段：支持从上游拉取的列表中选，或手动输入 -->
            <template v-if="key.endsWith('_model') && SUPPORTS_DISCOVERY[g.title]">
              <n-select
                v-if="(modelOptions[SUPPORTS_DISCOVERY[g.title]!] || []).length"
                :value="draft[key] ?? fieldByKey[key]?.value ?? undefined"
                :options="modelOptions[SUPPORTS_DISCOVERY[g.title]!]"
                filterable
                tag
                :placeholder="fieldMeta[key]?.placeholder || '从列表选择或输入自定义模型名'"
                style="width: 100%"
                @update:value="(v: string) => onInput(key, v)"
              />
              <n-input
                v-else
                :value="displayValue(fieldByKey[key])"
                :placeholder="fieldMeta[key]?.placeholder || fieldByKey[key]?.default || ''"
                style="width: 100%"
                @update:value="(v: string) => onInput(key, v)"
              />
            </template>
            <!-- 其它字段 -->
            <template v-else>
              <n-input
                :value="displayValue(fieldByKey[key])"
                :type="fieldByKey[key]?.sensitive ? (reveal[key] ? 'text' : 'password') : 'text'"
                :placeholder="fieldMeta[key]?.placeholder || (fieldByKey[key]?.is_set ? `已设置 ${fieldByKey[key]?.masked || ''}（留空=不修改）` : '未设置')"
                :show-password-on="fieldByKey[key]?.sensitive ? 'click' : undefined"
                style="width: 100%"
                @update:value="(v: string) => onInput(key, v)"
              />
            </template>
            <n-space align="center" :size="6">
              <n-tag v-if="fieldByKey[key]?.is_set" :type="sourceTag(fieldByKey[key])?.type" size="small" :bordered="false">
                <template v-if="fieldByKey[key]?.sensitive">{{ fieldByKey[key]?.masked || '已设置' }} · </template>{{ sourceTag(fieldByKey[key])?.label }}
              </n-tag>
              <n-tag v-else type="default" size="small" :bordered="false">未设置</n-tag>
              <!-- 拉取模型列表（仅 model 字段显示） -->
              <n-button
                v-if="key.endsWith('_model') && SUPPORTS_DISCOVERY[g.title]"
                size="tiny" tertiary
                :loading="fetchingModels[SUPPORTS_DISCOVERY[g.title]!]"
                @click="onFetchModels(SUPPORTS_DISCOVERY[g.title]!)"
              >⬇ 拉取模型列表</n-button>
            </n-space>
          </n-space>
        </n-form-item>
        <!-- ★ 连接测试（仅 OpenAI 兼容 / Gemini 组显示） -->
        <n-form-item v-if="SUPPORTS_DISCOVERY[g.title]" label=" ">
          <n-space vertical :size="6" style="width: 100%">
            <n-button
              type="primary" ghost size="small"
              :loading="testing[SUPPORTS_DISCOVERY[g.title]!]"
              @click="onTestConnection(SUPPORTS_DISCOVERY[g.title]!)"
            >🔌 测试渠道连接</n-button>
            <n-alert
              v-if="testResult[SUPPORTS_DISCOVERY[g.title]!]"
              :type="testResult[SUPPORTS_DISCOVERY[g.title]!]!.ok ? 'success' : 'error'"
              :bordered="false" style="font-size: 12px; word-break: break-all"
            >{{ testResult[SUPPORTS_DISCOVERY[g.title]!]!.text }}</n-alert>
          </n-space>
        </n-form-item>
      </n-form>
    </n-card>

    <n-divider />
    <n-space justify="end" style="margin-bottom: 24px">
      <n-button quaternary @click="load" :loading="loading">重置</n-button>
      <n-button type="primary" :loading="saving" @click="onSave" :disabled="loading">
        💾 保存并生效
      </n-button>
    </n-space>
  </div>
</template>

<style scoped>
.settings-view { max-width: 860px; }
</style>
