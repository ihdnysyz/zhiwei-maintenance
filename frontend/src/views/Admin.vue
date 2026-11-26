<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Connection, Delete, Key, Plus, Refresh, User } from '@element-plus/icons-vue'
import OperationHistory from '../components/OperationHistory.vue'
import { api, can, date, errorMessage, loadDevices, notice, request, roleNames, statuses, statusTag, store, type Role, type Row } from '../store'

const tab = ref<'devices' | 'accounts' | 'ai' | 'operations'>('devices')
const accounts = ref<Row[]>([])
const data = ref<Row | null>(null)
const dialog = ref('')
const device = ref({ number: '', name: '', model: '', batch: '', location: '' })
const account = ref({ username: '', name: '', role: 'tech' as Role, password: '', models: '', active: true, primary: false })
const aiForm = ref({
  enabled: false,
  chatUrl: '',
  chatApiKey: '',
  hasChatApiKey: false,
  chatApiKeyMask: '',
  chatModel: '',
  embeddingUrl: '',
  embeddingApiKey: '',
  hasEmbeddingApiKey: false,
  embeddingApiKeyMask: '',
  embeddingModel: '',
  chunkSize: 600,
  chunkOverlap: 100,
  topK: 6,
  maxReactSteps: 5,
})
const batchLimit = 50
const bulkScope = ref({ role: 'tech' as Role, models: '', active: true })
const bulkRows = ref<{ id: number; username: string; name: string; password: string }[]>([])
let bulkRowId = 0
const editing = ref(false)
const error = ref('')
const working = ref(false)

const captions: Record<string, string> = { model: '对话大模型', embedding: '向量 Embedding', identity: '企业身份', registry: '设备台账', workorder: '正式工单回写' }
const jobLabels: Record<string, string> = { parse: 'RAG 切片与向量化', turn: '智能诊断问答', submit: '工单回写', check: '回写查询' }

const presets = [
  { label: 'DeepSeek', chatUrl: 'https://api.deepseek.com/v1', chatModel: 'deepseek-chat', embeddingUrl: '', embeddingModel: '' },
  { label: '硅基流动', chatUrl: 'https://api.siliconflow.cn/v1', chatModel: 'Qwen/Qwen2.5-72B-Instruct', embeddingUrl: 'https://api.siliconflow.cn/v1', embeddingModel: 'BAAI/bge-m3' },
  { label: '本地 Ollama', chatUrl: 'http://127.0.0.1:11434/v1', chatModel: 'qwen2.5:7b', embeddingUrl: 'http://127.0.0.1:11434/v1', embeddingModel: 'bge-m3' },
]

function applyPreset(preset: typeof presets[number]) {
  aiForm.value.enabled = true
  aiForm.value.chatUrl = preset.chatUrl
  aiForm.value.chatModel = preset.chatModel
  aiForm.value.embeddingUrl = preset.embeddingUrl
  aiForm.value.embeddingModel = preset.embeddingModel
}

onMounted(load)
async function load() {
  if (!can('system:manage')) return
  try {
    await loadDevices()
    const [accList, opsData, aiData] = await Promise.all([
      api<Row[]>('/accounts'),
      api<Row>('/operations'),
      api<Record<string, any>>('/settings/ai'),
    ])
    accounts.value = accList
    data.value = opsData
    aiForm.value = {
      ...aiForm.value,
      ...aiData,
      chatApiKey: '',
      embeddingApiKey: '',
    }
  } catch (e) { notice(errorMessage(e), true) }
}

function generatePassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%'
  const buf = new Uint32Array(14)
  crypto.getRandomValues(buf)
  return Array.from(buf, value => chars[value % chars.length]).join('')
}

function editDevice(row?: Row) {
  device.value = row ? { number: row.number, name: row.name, model: row.model, batch: row.batch, location: row.location }
    : { number: '', name: '', model: '', batch: '', location: '' }
  editing.value = !!row; dialog.value = 'device'; error.value = ''
}

function editAccount(row?: Row) {
  if (row?.primary && !store.profile?.primary) {
    notice('仅主管理员可修改主管理员账号', true)
    return
  }
  account.value = row
    ? { username: row.key, name: row.name, role: row.role, password: '', models: (row.models || []).join(', '), active: row.status === 'active', primary: !!row.primary }
    : { username: '', name: '', role: 'tech', password: '', models: '', active: true, primary: false }
  editing.value = !!row; dialog.value = 'account'; error.value = ''
}

function canDeleteAccount(row: Row) {
  if (row.primary || row.key === store.profile?.username) return false
  if (row.role === 'platform' && !store.profile?.primary) return false
  return true
}

function newBulkRow(username = '', name = '', password = '') {
  return { id: ++bulkRowId, username, name, password }
}

function openBulkAccounts() {
  bulkScope.value = { role: 'tech', models: '', active: true }
  bulkRows.value = [newBulkRow()]
  dialog.value = 'batch'
  error.value = ''
}

function closeDialog() {
  if (dialog.value === 'batch') {
    if (working.value) return
    bulkRows.value = []
  }
  dialog.value = ''
}

function pasteAccounts(event: ClipboardEvent, index: number) {
  const value = event.clipboardData?.getData('text/plain')
  if (!value?.includes('\t')) return
  event.preventDefault()
  const rows = value.split(/\r\n|\n|\r/).filter(line => line.trim()).map(line => line.split('\t'))
  if (!rows.length || rows.some(row => row.length < 2 || row.length > 3)) {
    error.value = '请粘贴账号和名称两列，本地密码可放在第三列'
    return
  }
  if (bulkRows.value.length - 1 + rows.length > batchLimit) {
    error.value = '每批最多创建 ' + batchLimit + ' 个账号'
    return
  }
  bulkRows.value.splice(index, 1, ...rows.map(([username, name, password]) =>
    newBulkRow(username.trim(), name.trim(), password)))
  error.value = ''
}

function modelList(value: string) {
  return value.split(/[,，]/).map(model => model.trim()).filter(Boolean)
}

async function operation(work: () => Promise<void>) {
  if (working.value) return
  error.value = ''; working.value = true
  try { await work(); await load() }
  catch (e) { error.value = errorMessage(e); if (!dialog.value) notice(error.value, true) }
  finally { working.value = false }
}

function saveDevice() { return operation(async () => { await api('/devices', 'POST', device.value); dialog.value = ''; notice('设备台账已保存') }) }
function saveAccount() {
  return operation(async () => {
    const { primary: _primary, ...payload } = account.value
    await api('/accounts', 'POST', { ...payload, models: modelList(account.value.models) })
    dialog.value = ''; notice('账号权限已保存')
  })
}
function removeAccount(username: string) {
  return operation(async () => {
    await request('/accounts/' + encodeURIComponent(username), 'DELETE')
    notice('账号已删除')
  })
}
function saveBulkAccounts() {
  return operation(async () => {
    const { role, models, active } = bulkScope.value
    const scope = modelList(models)
    const result = await api<{ count: number }>('/accounts/batch', 'POST', {
      accounts: bulkRows.value.map(({ username, name, password }) => ({
        username, name, password, role, models: scope, active,
      })),
    })
    bulkRows.value = []
    dialog.value = ''
    notice('已创建 ' + result.count + ' 个账号')
  })
}
function saveAISettings() {
  return operation(async () => {
    await api('/settings/ai', 'PUT', {
      enabled: aiForm.value.enabled,
      chatUrl: aiForm.value.chatUrl,
      chatApiKey: aiForm.value.chatApiKey || undefined,
      chatModel: aiForm.value.chatModel,
      embeddingUrl: aiForm.value.embeddingUrl,
      embeddingApiKey: aiForm.value.embeddingApiKey || undefined,
      embeddingModel: aiForm.value.embeddingModel,
      chunkSize: aiForm.value.chunkSize,
      chunkOverlap: aiForm.value.chunkOverlap,
      topK: aiForm.value.topK,
      maxReactSteps: aiForm.value.maxReactSteps,
    })
    notice('模型与 RAG 配置已保存')
  })
}
function reindexAll() {
  return operation(async () => {
    const res = await api<{ queued: number }>('/sources/reindex-all', 'POST')
    notice(`已提交 ${res.queued} 份资料重新切片与向量化`)
  })
}
function syncDevices() {
  return operation(async () => {
    const result = await api<{ count: number }>('/devices/sync', 'POST')
    notice('已同步 ' + result.count + ' 条设备记录')
  })
}
function testModelConnection() {
  return operation(async () => {
    const result = await api<{ status: string; model: string; embeddingModel?: string; embeddingDim?: number }>('/integrations/model/test', 'POST')
    const emb = result.embeddingModel ? ` · Embedding ${result.embeddingModel} (${result.embeddingDim}维)` : ''
    notice(`连接成功 · ${result.model}${emb}`)
  })
}
function reconcile(row: Row, actionName: string) {
  return operation(async () => {
    await api('/submissions/' + row.id + '/actions', 'POST', { revision: row.revision, action: actionName })
    notice('已受理原请求核对')
  })
}
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <h1>系统管理</h1>
      <el-button v-if="can('system:manage')" :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <el-card v-if="!can('system:manage')" shadow="never">
      <el-empty description="此页面需要管理员权限" />
    </el-card>

    <el-card v-else shadow="never">
      <div class="space-between mb-16">
        <el-tabs v-model="tab" style="margin-bottom: -14px">
          <el-tab-pane label="设备台账" name="devices" />
          <el-tab-pane label="用户与权限" name="accounts" />
          <el-tab-pane label="模型与 RAG" name="ai" />
          <el-tab-pane label="运行与审计" name="operations" />
        </el-tabs>
        <div v-if="tab === 'devices'" class="row-gap">
          <el-button v-if="data?.integrations?.registry" :icon="Refresh" :loading="working" @click="syncDevices">同步台账</el-button>
          <el-button type="primary" :icon="Plus" @click="editDevice()">添加设备</el-button>
        </div>
        <div v-if="tab === 'accounts'" class="row-gap">
          <el-button :icon="User" @click="openBulkAccounts">批量创建</el-button>
          <el-button type="primary" :icon="Plus" @click="editAccount()">新建用户</el-button>
        </div>
        <div v-if="tab === 'ai'" class="row-gap">
          <el-button :icon="Refresh" :loading="working" @click="reindexAll">全量重建向量</el-button>
          <el-button :icon="Connection" :loading="working" @click="testModelConnection">测试连接</el-button>
          <el-button type="primary" :loading="working" @click="saveAISettings">保存配置</el-button>
        </div>
      </div>

      <Transition name="tab-slide" mode="out-in">
        <div v-if="tab === 'devices'" key="devices">
          <el-table v-if="store.devices.length" :data="store.devices" style="cursor: pointer" @row-click="editDevice">
            <el-table-column prop="number" label="设备编号" width="160" />
            <el-table-column prop="name" label="设备名称" min-width="180" />
            <el-table-column prop="model" label="型号" width="160" />
            <el-table-column prop="batch" label="批次" width="130">
              <template #default="{ row }">{{ row.batch || '—' }}</template>
            </el-table-column>
            <el-table-column prop="location" label="位置" min-width="160">
              <template #default="{ row }">{{ row.location || '—' }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无设备">
            <el-button type="primary" :icon="Plus" @click="editDevice()">添加设备</el-button>
          </el-empty>
        </div>

        <div v-else-if="tab === 'accounts'" key="accounts">
          <el-table :data="accounts" style="cursor: pointer" @row-click="editAccount">
            <el-table-column prop="key" label="账号" width="150" />
            <el-table-column prop="name" label="名称" width="160" />
            <el-table-column label="设备范围" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.models?.join('、') || '全部型号' }}</template>
            </el-table-column>
            <el-table-column label="角色" width="140">
              <template #default="{ row }">
                <el-tag size="small" :type="row.primary ? 'danger' : 'primary'">
                  {{ row.primary ? '主管理员' : roleNames[row.role as Role] }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.status !== 'active'" size="small" type="danger">{{ statuses[row.status] }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" align="right">
              <template #default="{ row }">
                <div @click.stop>
                  <el-popconfirm v-if="canDeleteAccount(row)" :title="`确定删除账号 ${row.key}？`" @confirm="removeAccount(row.key)">
                    <template #reference>
                      <el-button text type="danger" size="small" :icon="Delete" aria-label="删除账号" />
                    </template>
                  </el-popconfirm>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div v-else-if="tab === 'ai'" key="ai" class="stack">
          <div class="space-between">
            <el-switch v-model="aiForm.enabled" active-text="启用外部 OpenAI 兼容模型（对话 + 向量嵌入）" />
            <div class="row-gap">
              <el-button v-for="preset in presets" :key="preset.label" size="small" plain @click="applyPreset(preset)">{{ preset.label }}</el-button>
            </div>
          </div>

          <el-form label-position="top">
            <el-divider content-position="left">对话模型</el-divider>
            <div class="form-grid">
              <el-form-item label="接口地址 (Base URL)">
                <el-input v-model="aiForm.chatUrl" placeholder="https://api.deepseek.com/v1" />
              </el-form-item>
              <el-form-item label="对话模型名称">
                <el-input v-model="aiForm.chatModel" placeholder="deepseek-chat" />
              </el-form-item>
              <el-form-item label="API Key">
                <el-input v-model="aiForm.chatApiKey" type="password" show-password :placeholder="aiForm.hasChatApiKey ? `已配置（${aiForm.chatApiKeyMask}），留空保持不变` : 'sk-...'" autocomplete="off" />
              </el-form-item>
            </div>

            <el-divider content-position="left">向量模型（Embedding）</el-divider>
            <div class="form-grid">
              <el-form-item label="向量接口地址（留空复用对话地址）">
                <el-input v-model="aiForm.embeddingUrl" placeholder="留空复用对话 Base URL" />
              </el-form-item>
              <el-form-item label="Embedding 模型名称（留空使用本地 256 维向量）">
                <el-input v-model="aiForm.embeddingModel" placeholder="BAAI/bge-m3" />
              </el-form-item>
              <el-form-item label="向量 API Key（留空复用对话密钥）">
                <el-input v-model="aiForm.embeddingApiKey" type="password" show-password :placeholder="aiForm.hasEmbeddingApiKey ? `已配置（${aiForm.embeddingApiKeyMask}）` : '留空复用对话 API Key'" autocomplete="off" />
              </el-form-item>
            </div>

            <el-divider content-position="left">RAG 切片与思考参数</el-divider>
            <div class="form-grid">
              <el-form-item label="切片大小（字符数）">
                <el-input-number v-model="aiForm.chunkSize" :min="200" :max="2000" :step="50" />
              </el-form-item>
              <el-form-item label="切片重叠（字符数）">
                <el-input-number v-model="aiForm.chunkOverlap" :min="0" :max="400" :step="20" />
              </el-form-item>
              <el-form-item label="单轮检索召回 Top-K">
                <el-input-number v-model="aiForm.topK" :min="2" :max="15" />
              </el-form-item>
              <el-form-item label="最大思考轮次">
                <el-input-number v-model="aiForm.maxReactSteps" :min="1" :max="8" />
              </el-form-item>
            </div>
          </el-form>
        </div>

        <div v-else key="operations" class="stack">
          <div class="space-between">
            <div class="row-gap">
              <el-tag v-for="(enabled, key) in data?.integrations || {}" :key="key" :type="enabled ? 'success' : 'info'">
                {{ captions[String(key)] }} · {{ enabled ? '已启用' : '未启用' }}
              </el-tag>
            </div>
            <el-button :loading="working" @click="testModelConnection">测试模型连接</el-button>
          </div>

          <el-alert v-for="alert in data?.alerts || []" :key="alert.kind" :title="`${alert.message} · ${alert.kind}`" type="error" show-icon :closable="false" />

          <div class="stack">
            <h3>外部回写结果</h3>
            <el-card v-for="row in data?.submissions || []" :key="row.id" shadow="never">
              <div class="space-between">
                <div class="row-gap">
                  <span>{{ row.requestId }}</span>
                  <el-tag size="small" :type="statusTag(row.status)">{{ statuses[row.status] || row.status }}</el-tag>
                </div>
                <div class="row-gap">
                  <el-button v-if="['manual_review','not_created'].includes(row.status)" size="small" @click="reconcile(row, 'check')">查原请求</el-button>
                  <el-button v-if="row.status === 'not_created' && row.sameKeyRetry" size="small" type="primary" @click="reconcile(row, 'retry')">同键重试</el-button>
                </div>
              </div>
              <OperationHistory :events="row.operationHistory" />
            </el-card>
            <p v-if="!data?.submissions?.length" class="muted">暂无回写记录</p>
          </div>

          <div class="stack">
            <h3>后台任务</h3>
            <el-table v-if="data?.jobs?.length" :data="data.jobs" size="small">
              <el-table-column label="任务" width="200">
                <template #default="{ row }">{{ jobLabels[row.kind] || row.kind }} · {{ row.id.slice(0, 8) }}</template>
              </el-table-column>
              <el-table-column prop="error" label="异常" min-width="200" />
              <el-table-column label="状态" width="110" align="right">
                <template #default="{ row }"><el-tag size="small" :type="statusTag(row.state)">{{ statuses[row.state] || row.state }}</el-tag></template>
              </el-table-column>
            </el-table>
            <p v-else class="muted">暂无后台任务</p>
          </div>

          <div class="stack">
            <h3>关键操作审计</h3>
            <el-table v-if="data?.audit?.length" :data="data.audit" size="small">
              <el-table-column prop="action" label="操作" min-width="180" />
              <el-table-column label="执行人" width="150">
                <template #default="{ row }">{{ row.actorName || row.actor }}</template>
              </el-table-column>
              <el-table-column label="请求号" width="150">
                <template #default="{ row }">{{ row.requestId?.slice(0, 12) || '—' }}</template>
              </el-table-column>
              <el-table-column label="时间" width="150" align="right">
                <template #default="{ row }">{{ date(row.created, true) }}</template>
              </el-table-column>
            </el-table>
            <p v-else class="muted">暂无操作记录</p>
          </div>
        </div>
      </Transition>
    </el-card>

    <el-dialog :model-value="dialog === 'device'" :title="editing ? '编辑设备' : '添加设备'" width="540px" @close="closeDialog">
      <el-form label-position="top" @submit.prevent="saveDevice">
        <div class="form-grid">
          <el-form-item label="设备编号" required><el-input v-model="device.number" :disabled="editing" maxlength="80" /></el-form-item>
          <el-form-item label="设备名称" required><el-input v-model="device.name" maxlength="120" /></el-form-item>
          <el-form-item label="型号" required><el-input v-model="device.model" maxlength="80" /></el-form-item>
          <el-form-item label="批次"><el-input v-model="device.batch" maxlength="80" /></el-form-item>
          <el-form-item label="位置"><el-input v-model="device.location" maxlength="120" /></el-form-item>
        </div>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
      </el-form>
      <template #footer>
        <el-button @click="closeDialog">取消</el-button>
        <el-button type="primary" :loading="working" @click="saveDevice">保存台账</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="dialog === 'account'" :title="editing ? '编辑用户权限' : '新建用户'" width="560px" @close="closeDialog">
      <el-form label-position="top" @submit.prevent="saveAccount">
        <div class="form-grid">
          <el-form-item label="账号" required><el-input v-model="account.username" :disabled="editing" maxlength="64" /></el-form-item>
          <el-form-item label="名称" required><el-input v-model="account.name" maxlength="80" /></el-form-item>
          <el-form-item label="角色">
            <el-select v-model="account.role" :disabled="account.primary" class="w-full">
              <el-option v-for="(label, key) in roleNames" :key="key" :value="key" :label="label" />
            </el-select>
          </el-form-item>
          <el-form-item label="登录密码">
            <el-input v-model="account.password" type="password" show-password autocomplete="new-password" placeholder="至少 12 位">
              <template #append>
                <el-button :icon="Key" title="生成随机密码" @click="account.password = generatePassword()" />
              </template>
            </el-input>
          </el-form-item>
        </div>
        <el-form-item label="允许访问的设备型号">
          <el-input v-model="account.models" placeholder="逗号分隔，留空允许全部型号" />
        </el-form-item>
        <el-form-item v-if="!account.primary">
          <el-switch v-model="account.active" active-text="启用账号" />
        </el-form-item>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
      </el-form>
      <template #footer>
        <el-button @click="closeDialog">取消</el-button>
        <el-button type="primary" :loading="working" @click="saveAccount">保存权限</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="dialog === 'batch'" title="批量创建用户" width="740px" :close-on-click-modal="!working" @close="closeDialog">
      <el-form label-position="top" @submit.prevent="saveBulkAccounts">
        <div class="form-grid">
          <el-form-item label="本批角色">
            <el-select v-model="bulkScope.role" :disabled="working" class="w-full">
              <el-option v-for="(label, key) in roleNames" :key="key" :value="key" :label="label" />
            </el-select>
          </el-form-item>
          <el-form-item label="允许访问的设备型号">
            <el-input v-model="bulkScope.models" :disabled="working" placeholder="逗号分隔，留空允许全部型号" />
          </el-form-item>
        </div>
        <el-form-item>
          <el-switch v-model="bulkScope.active" :disabled="working" active-text="创建后启用" />
        </el-form-item>
        <div v-for="(row, index) in bulkRows" :key="row.id" class="bulk-row">
          <el-input v-model="row.username" :disabled="working" placeholder="账号（可粘贴 Excel）" maxlength="64" @paste="pasteAccounts($event, index)" />
          <el-input v-model="row.name" :disabled="working" placeholder="名称" maxlength="80" />
          <el-input v-model="row.password" :disabled="working" type="password" show-password autocomplete="new-password" placeholder="选填，≥12 位" maxlength="150" />
          <el-button text type="danger" :icon="Delete" :disabled="working || bulkRows.length === 1" :aria-label="'移除第 ' + (index + 1) + ' 行'" @click="bulkRows.splice(index, 1)" />
        </div>
        <div class="space-between mb-16">
          <el-button :icon="Plus" :disabled="working || bulkRows.length >= batchLimit" @click="bulkRows.push(newBulkRow())">添加一行</el-button>
          <span class="muted">{{ bulkRows.length }} / {{ batchLimit }}</span>
        </div>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
      </el-form>
      <template #footer>
        <el-button :disabled="working" @click="closeDialog">取消</el-button>
        <el-button type="primary" :loading="working" @click="saveBulkAccounts">创建这批用户</el-button>
      </template>
    </el-dialog>
  </section>
</template>
