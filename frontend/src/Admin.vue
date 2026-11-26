<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Activity, Cpu, Plus, RefreshCw, Settings, ShieldCheck, Users, X } from 'lucide-vue-next'
import OperationHistory from './OperationHistory.vue'
import { api, date, errorMessage, loadDevices, notice, roleNames, statuses, store, type Role, type Row } from './store'

const tabs = { devices: '设备台账', accounts: '角色与范围', operations: '运行与审计' }
const tab = ref<keyof typeof tabs>('devices')
const accounts = ref<Row[]>([])
const data = ref<Row | null>(null)
const dialog = ref('')
const device = ref({ number: '', name: '', model: '', batch: '', location: '' })
const account = ref({ username: '', name: '', role: 'tech' as Role, password: '', models: '', active: true })
const batchLimit = 50
const bulkScope = ref({ role: 'tech' as Role, models: '', active: true })
const bulkRows = ref<{ id: number; username: string; name: string; password: string }[]>([])
let bulkRowId = 0
const editing = ref(false)
const error = ref('')
const working = ref(false)
const captions: Record<string, string> = { model: '回答模型', embedding: '语义检索', identity: '企业身份', registry: '设备台账', workorder: '正式工单回写' }
onMounted(load)
async function load() {
  if (store.profile?.role !== 'platform') return
  try {
    await loadDevices()
    ;[accounts.value, data.value] = await Promise.all([api<Row[]>('/accounts'), api<Row>('/operations')])
  } catch (e) { notice(errorMessage(e), true) }
}
function editDevice(row?: Row) {
  device.value = row ? { number: row.number, name: row.name, model: row.model, batch: row.batch, location: row.location }
    : { number: '', name: '', model: '', batch: '', location: '' }
  editing.value = !!row; dialog.value = 'device'; error.value = ''
}
function editAccount(row?: Row) {
  account.value = row ? { username: row.key, name: row.name, role: row.role, password: '', models: (row.models || []).join(', '), active: row.status === 'active' }
    : { username: '', name: '', role: 'tech', password: '', models: '', active: true }
  editing.value = !!row; dialog.value = 'account'; error.value = ''
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
    error.value = '请粘贴账号和名称两列。本地密码可放在第三列。'
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
function saveAccount() { return operation(async () => {
  await api('/accounts', 'POST', { ...account.value, models: modelList(account.value.models) })
  dialog.value = ''; notice('账号权限已保存')
}) }
function saveBulkAccounts() { return operation(async () => {
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
}) }
function syncDevices() { return operation(async () => {
  const result = await api<{ count: number }>('/devices/sync', 'POST')
  notice('已同步 ' + result.count + ' 条设备记录')
}) }
function testModelConnection() { return operation(async () => {
  const result = await api<{ status: string; model: string }>('/integrations/model/test', 'POST')
  notice('模型连接成功 · ' + result.model)
}) }
function reconcile(row: Row, action: string) { return operation(async () => {
  await api('/submissions/' + row.id + '/actions', 'POST', { revision: row.revision, action })
  notice('已受理原请求核对')
}) }
</script>

<template>
  <main class="page">
    <div class="page-heading"><div><h1>系统管理</h1><p>维护组织设备、账号权限与运行记录。</p></div><button v-if="store.profile?.role === 'platform'" class="btn secondary" @click="load"><RefreshCw :size="17"/> 刷新</button></div>
    <div v-if="store.profile?.role !== 'platform'" class="card empty"><ShieldCheck :size="34"/><b>此页面需要平台管理员权限</b></div>
    <section v-else class="card list-card">
      <div class="filters"><div class="tabs"><button v-for="(label,key) in tabs" :key="key" :class="{ on: tab === key }" @click="tab = key">{{ label }}</button></div>
        <div v-if="tab === 'devices'" class="button-row"><button v-if="data?.integrations?.registry" class="btn secondary" :disabled="working" @click="syncDevices"><RefreshCw :size="16"/> 同步台账</button><button class="btn primary" @click="editDevice()"><Plus :size="17"/> 添加设备</button></div>
        <div v-if="tab === 'accounts'" class="button-row">
          <button class="btn secondary" @click="openBulkAccounts"><Users :size="17"/> 批量创建</button>
          <button class="btn primary" @click="editAccount()"><Plus :size="17"/> 新建账号</button>
        </div>
      </div>
      <template v-if="tab === 'devices'">
        <div v-if="!store.devices.length" class="empty"><Cpu :size="34"/><b>暂无设备</b><p>添加设备或接入企业台账后即可开展诊断。</p></div>
        <button v-for="row in store.devices" :key="row.id" class="source-row" @click="editDevice(row)"><span class="tile-icon mint"><Cpu :size="20"/></span><span class="source-main"><b>{{ row.name }}</b><small>{{ row.number }} · {{ row.location || '未登记位置' }}</small></span><span class="pill">{{ row.model }} {{ row.batch }}</span></button>
      </template>
      <template v-if="tab === 'accounts'">
        <button v-for="row in accounts" :key="row.id" class="source-row" @click="editAccount(row)"><span class="tile-icon blue"><Users :size="20"/></span><span class="source-main"><b>{{ row.name }}</b><small>{{ row.key }} · {{ row.models?.join('、') || '本组织所有型号' }}</small></span><span class="category-label">{{ roleNames[row.role as Role] }}</span><span class="pill">{{ statuses[row.status] }}</span></button>
      </template>
      <div v-if="tab === 'operations'" class="content-card form-stack">
        <div class="section-head"><div><h2>依赖与集成</h2></div><Activity :size="20" class="muted"/></div>
        <div class="tag-row"><span v-for="(enabled, key) in data?.integrations || {}" :key="key" :class="['pill', { green: enabled }]">{{ captions[String(key)] }} · {{ enabled ? '已配置' : '未启用' }}</span></div>
        <div class="tag-row"><button class="btn secondary" :disabled="working" @click="testModelConnection">测试模型连接</button><span class="muted">使用固定测试内容检查接口</span></div>
        <p v-for="alert in data?.alerts || []" :key="alert.kind" class="page-error" role="alert">{{ alert.message }} · {{ alert.kind }}</p>
        <h3>外部回写结果</h3><p v-if="!data?.submissions?.length" class="muted">暂无回写记录。</p>
        <article v-for="row in data?.submissions || []" :key="row.id" class="batch">
          <div class="batch-row"><span>{{ row.requestId }}</span><span class="pill">{{ statuses[row.status] || row.status }}</span><button v-if="['manual_review','not_created'].includes(row.status)" class="btn secondary" @click="reconcile(row, 'check')">查原请求</button><button v-if="row.status === 'not_created' && row.sameKeyRetry" class="btn secondary" @click="reconcile(row, 'retry')">同键重试</button></div>
          <OperationHistory :events="row.operationHistory"/>
        </article>
        <h3>后台任务</h3><p v-if="!data?.jobs?.length" class="muted">暂无后台任务。</p>
        <div v-for="row in data?.jobs || []" :key="row.id" class="batch-row"><span>{{ { parse: '资料解析', turn: '诊断问答', submit: '工单回写', check: '回写查询' }[row.kind as string] || row.kind }} · {{ row.id.slice(0, 8) }}</span><small v-if="row.error" class="error">{{ row.error }}</small><span class="pill">{{ statuses[row.state] || row.state }}</span></div>
        <h3>关键操作审计</h3><p v-if="!data?.audit?.length" class="muted">暂无操作记录。</p>
        <div v-for="row in data?.audit || []" :key="row.id" class="batch-row"><span>{{ row.action }} · {{ row.actorName || row.actor }}</span><small>{{ row.requestId?.slice(0, 12) }}</small><small>{{ date(row.created, true) }}</small></div>
      </div>
    </section>
    <Transition name="scrim">
      <div v-if="dialog" class="backdrop" @click="closeDialog"></div>
    </Transition>
    <Transition name="overlay">
      <dialog v-if="dialog" open class="modal" :class="{ wide: dialog === 'batch' }" aria-labelledby="admin-dialog-title" @keydown.esc="closeDialog">
        <div class="sheet-head">
          <h2 id="admin-dialog-title">{{ dialog === 'batch' ? '批量创建账号' : dialog === 'device' ? (editing ? '编辑设备' : '添加设备') : (editing ? '编辑账号权限' : '创建账号') }}</h2>
          <button class="icon-btn" aria-label="取消" :disabled="dialog === 'batch' && working" @click="closeDialog"><X :size="22"/></button>
        </div>
        <form v-if="dialog === 'device'" class="sheet-body form-stack" @submit.prevent="saveDevice"><div class="form-grid">
          <label>设备编号<input v-model="device.number" :disabled="editing" required maxlength="80"/></label><label>设备名称<input v-model="device.name" required maxlength="120"/></label><label>型号<input v-model="device.model" required maxlength="80"/></label><label>批次<input v-model="device.batch" maxlength="80"/></label><label>位置<input v-model="device.location" maxlength="120"/></label></div><p v-if="error" class="error" role="alert">{{ error }}</p><div class="button-row"><button class="btn primary" :disabled="working">保存台账</button></div></form>
        <form v-else-if="dialog === 'account'" class="sheet-body form-stack" @submit.prevent="saveAccount"><div class="form-grid">
          <label>账号<input v-model="account.username" :disabled="editing" required maxlength="64"/></label><label>名称<input v-model="account.name" required maxlength="80"/></label><label>角色<select v-model="account.role"><option v-for="(label,key) in roleNames" :key="key" :value="key">{{ label }}</option></select></label><label>本地密码<input v-model="account.password" type="password" autocomplete="new-password" minlength="12" placeholder="至少 12 个字符"/></label></div>
          <label>允许访问的设备型号<input v-model="account.models" placeholder="多个型号用逗号隔开，留空允许本组织所有型号"/></label><label class="check"><input v-model="account.active" type="checkbox"/> 启用账号</label><p v-if="error" class="error" role="alert">{{ error }}</p><div class="button-row"><button class="btn primary" :disabled="working">保存权限</button></div></form>
        <form v-else class="sheet-body form-stack" @submit.prevent="saveBulkAccounts">
          <div class="form-grid">
            <label>本批角色<select v-model="bulkScope.role" :disabled="working"><option v-for="(label,key) in roleNames" :key="key" :value="key">{{ label }}</option></select></label>
            <label>允许访问的设备型号<input v-model="bulkScope.models" :disabled="working" placeholder="逗号分隔，留空允许本组织所有型号"/></label>
          </div>
          <label class="check"><input v-model="bulkScope.active" :disabled="working" type="checkbox"/> 创建后启用</label>
          <p class="muted">可在账号栏粘贴 Excel 的账号、名称两列，或逐行添加。需要本地登录时，为每个账号设置至少 12 个字符的密码。</p>
          <div class="bulk-fields bulk-head" aria-hidden="true">
            <span>账号</span><span>名称</span><span>本地密码</span>
          </div>
          <div v-for="(row, index) in bulkRows" :key="row.id" class="bulk-fields">
            <input v-model="row.username" :aria-label="'第 ' + (index + 1) + ' 行账号'" placeholder="账号"
              :disabled="working" required minlength="2" maxlength="64" pattern="[A-Za-z0-9_\-]+" @paste="pasteAccounts($event, index)"/>
            <input v-model="row.name" :aria-label="'第 ' + (index + 1) + ' 行名称'" placeholder="名称" :disabled="working" required maxlength="80"/>
            <input v-model="row.password" :aria-label="'第 ' + (index + 1) + ' 行本地密码'" placeholder="本地密码（选填）"
              :disabled="working" type="password" autocomplete="new-password" minlength="12" maxlength="150"/>
            <button type="button" class="icon-btn" :aria-label="'移除第 ' + (index + 1) + ' 行'"
              :disabled="working || bulkRows.length === 1" @click="bulkRows.splice(index, 1)"><X :size="17"/></button>
          </div>
          <div class="button-row">
            <button type="button" class="btn secondary" :disabled="working || bulkRows.length >= batchLimit" @click="bulkRows.push(newBulkRow())"><Plus :size="17"/> 添加一行</button>
            <span class="muted">{{ bulkRows.length }} / {{ batchLimit }}</span>
          </div>
          <p v-if="error" class="error" role="alert">{{ error }}</p>
          <div class="button-row"><button class="btn primary" :disabled="working">{{ working ? '正在创建…' : '创建这批账号' }}</button></div>
        </form>
      </dialog>
    </Transition>
  </main>
</template>

<style scoped>
.bulk-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.15fr) auto; gap: 8px; align-items: center; }
.bulk-fields input { min-width: 0; }
.bulk-head { color: var(--muted); }
@media (max-width: 540px) {
  .bulk-head { display: none; }
  .bulk-fields { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; }
  .bulk-fields input[type="password"] { grid-column: 1 / 3; grid-row: 2; }
}
</style>

