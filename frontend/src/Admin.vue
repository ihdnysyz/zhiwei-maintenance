<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Activity, Cpu, Plus, RefreshCw, Settings, ShieldCheck, Users, X } from 'lucide-vue-next'
import { api, date, errorMessage, loadDevices, notice, roleNames, statuses, store, type Role, type Row } from './store'

const tabs = { devices: '设备台账', accounts: '角色与范围', operations: '运行与审计' }
const tab = ref<keyof typeof tabs>('devices')
const accounts = ref<Row[]>([])
const data = ref<Row | null>(null)
const dialog = ref('')
const device = ref({ number: '', name: '', model: '', batch: '', location: '' })
const account = ref({ username: '', name: '', role: 'tech' as Role, password: '', models: '', active: true })
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
async function operation(work: () => Promise<void>) {
  if (working.value) return
  error.value = ''; working.value = true
  try { await work(); await load() }
  catch (e) { error.value = errorMessage(e); if (!dialog.value) notice(error.value, true) }
  finally { working.value = false }
}
function saveDevice() { return operation(async () => { await api('/devices', 'POST', device.value); dialog.value = ''; notice('设备台账已保存') }) }
function saveAccount() { return operation(async () => {
  await api('/accounts', 'POST', { ...account.value, models: account.value.models.split(/[,，]/).map(value => value.trim()).filter(Boolean) })
  dialog.value = ''; notice('账号权限已保存')
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
        <button v-if="tab === 'accounts'" class="btn primary" @click="editAccount()"><Plus :size="17"/> 新建账号</button>
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
        <div v-for="row in data?.submissions || []" :key="row.id" class="batch-row"><span>{{ row.requestId }}</span><span class="pill">{{ statuses[row.status] || row.status }}</span><button v-if="['manual_review','not_created'].includes(row.status)" class="btn secondary" @click="reconcile(row, 'check')">查原请求</button><button v-if="row.status === 'not_created' && row.sameKeyRetry" class="btn secondary" @click="reconcile(row, 'retry')">同键重试</button></div>
        <h3>后台任务</h3><p v-if="!data?.jobs?.length" class="muted">暂无后台任务。</p>
        <div v-for="row in data?.jobs || []" :key="row.id" class="batch-row"><span>{{ { parse: '资料解析', turn: '诊断问答', submit: '工单回写', check: '回写查询' }[row.kind as string] || row.kind }} · {{ row.id.slice(0, 8) }}</span><small v-if="row.error" class="error">{{ row.error }}</small><span class="pill">{{ statuses[row.state] || row.state }}</span></div>
        <h3>关键操作审计</h3><p v-if="!data?.audit?.length" class="muted">暂无操作记录。</p>
        <div v-for="row in data?.audit || []" :key="row.id" class="batch-row"><span>{{ row.action }} · {{ row.actor }}</span><small>{{ row.requestId?.slice(0, 12) }}</small><small>{{ date(row.created, true) }}</small></div>
      </div>
    </section>
    <div v-if="dialog" class="backdrop" @click="dialog = ''"></div>
    <dialog v-if="dialog" open class="modal" :aria-label="dialog === 'device' ? '维护设备' : '维护账号'">
      <div class="sheet-head"><h2>{{ dialog === 'device' ? (editing ? '编辑设备' : '添加设备') : (editing ? '编辑账号权限' : '创建账号') }}</h2><button class="icon-btn" aria-label="取消" @click="dialog = ''"><X :size="22"/></button></div>
      <form v-if="dialog === 'device'" class="sheet-body form-stack" @submit.prevent="saveDevice"><div class="form-grid">
        <label>设备编号<input v-model="device.number" :disabled="editing" required maxlength="80"/></label><label>设备名称<input v-model="device.name" required maxlength="120"/></label><label>型号<input v-model="device.model" required maxlength="80"/></label><label>批次<input v-model="device.batch" maxlength="80"/></label><label>位置<input v-model="device.location" maxlength="120"/></label></div><p v-if="error" class="error" role="alert">{{ error }}</p><div class="button-row"><button class="btn primary" :disabled="working">保存台账</button></div></form>
      <form v-else class="sheet-body form-stack" @submit.prevent="saveAccount"><div class="form-grid">
        <label>账号<input v-model="account.username" :disabled="editing" required maxlength="64"/></label><label>名称<input v-model="account.name" required maxlength="80"/></label><label>角色<select v-model="account.role"><option v-for="(label,key) in roleNames" :key="key" :value="key">{{ label }}</option></select></label><label>本地密码<input v-model="account.password" type="password" autocomplete="new-password" minlength="12" placeholder="至少 12 个字符"/></label></div>
        <label>允许访问的设备型号<input v-model="account.models" placeholder="多个型号用逗号隔开，留空允许本组织所有型号"/></label><label class="check"><input v-model="account.active" type="checkbox"/> 启用账号</label><p v-if="error" class="error" role="alert">{{ error }}</p><div class="button-row"><button class="btn primary" :disabled="working">保存权限</button></div></form>
    </dialog>
  </main>
</template>

