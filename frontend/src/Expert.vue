<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ArrowUpRight, BookOpen, ChevronRight, ClipboardCheck, MessageSquare, Users, X } from 'lucide-vue-next'
import OperationHistory from './OperationHistory.vue'
import SourceDetail from './SourceDetail.vue'
import { api, date, errorMessage, notice, statuses, store, type Row } from './store'

const tasks = ref<Row[]>([])
const completed = ref<Row[]>([])
const selected = ref<Row | null>(null)
const record = ref<Row | null>(null)
const caseSourceId = ref<string | null>(null)
const tab = ref('tasks')
const note = ref('')
const assignee = ref('')
const error = ref('')
const busy = ref(false)
const caseLoading = ref(false)
const caseError = ref('')
let caseTimer: number | undefined
let active = true
const expert = computed(() => store.profile?.role === 'expert')
const manage = computed(() => expert.value || store.profile?.role === 'library')
async function load() {
  try {
    tasks.value = await api<Row[]>('/tasks')
  } catch (e) { notice(errorMessage(e), true) }
}
onMounted(load)
onUnmounted(() => { active = false; clearTimeout(caseTimer) })
async function openCases() {
  tab.value = 'cases'
  await loadCases()
}
async function loadCases() {
  clearTimeout(caseTimer)
  if (caseLoading.value) return
  caseLoading.value = true
  caseError.value = ''
  try {
    completed.value = await api<Row[]>('/completed')
    if (active && tab.value === 'cases' && completed.value.some(row => row.caseSource?.status === 'queued')) {
      caseTimer = window.setTimeout(() => { if (tab.value === 'cases') void loadCases() }, 1200)
    }
  }
  catch (e) { caseError.value = errorMessage(e) }
  finally { caseLoading.value = false }
}
async function open(row: Row) {
  note.value = ''; assignee.value = ''; error.value = ''
  try { selected.value = await api<Row>('/tasks/' + row.id) }
  catch (e) { notice(errorMessage(e), true) }
}
function openCase(row: Row) {
  if (row.caseSource) {
    caseSourceId.value = row.caseSource.id
    return
  }
  record.value = row
  note.value = [
    '适用设备：' + row.device.model + (row.device.batch ? ' / ' + row.device.batch : ''),
    '现象：' + row.fields.symptom, '确认原因：' + row.fields.cause,
    '实际处置：' + row.fields.actions, '结果：' + row.fields.result,
  ].join('\n')
  error.value = ''
}
async function action(action: string) {
  if (!selected.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const updated = await api<Row>('/tasks/' + selected.value.id + '/actions', 'POST', {
      revision: selected.value.revision, action, note: note.value, assignee: assignee.value,
    })
    selected.value = { ...selected.value, ...updated }
    note.value = ''; assignee.value = ''
    await load()
    notice('专家待办已更新')
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
async function sendCase() {
  if (!record.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    await api('/drafts/' + record.value.id + '/case', 'POST', { text: note.value })
    record.value = null; note.value = ''
    notice('校验完成后将自动进入独立审核')
    await openCases()
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>
<template>
  <main class="page">
    <div class="page-heading"><div><h1>专家协作</h1><p>请班组专家核实疑难问题，并把已结单经验整理入库。</p></div><RouterLink class="btn secondary" to="/ask">返回诊断 <ArrowUpRight :size="17"/></RouterLink></div>
    <section class="card list-card">
      <div v-if="manage" class="filters"><div class="tabs"><button :class="{ on: tab === 'tasks' }" @click="tab = 'tasks'">专家待办</button><button :class="{ on: tab === 'cases' }" @click="openCases">案例回流</button></div></div>
      <template v-if="tab === 'tasks'">
        <div v-if="!tasks.length" class="empty"><Users :size="34"/><b>暂无专家待办</b><p>在问答中选择「转交专家」，就能在这里跟进。</p></div>
        <button v-for="row in tasks" :key="row.id" class="source-row" @click="open(row)"><span class="tile-icon mint"><MessageSquare :size="20"/></span><span class="source-main"><b>{{ row.title }}</b><small>{{ row.deviceName }} · {{ date(row.updated, true) }}</small></span><span v-if="row.overdue" class="pill">已逾期</span><span class="pill">{{ statuses[row.status] }}</span><ChevronRight :size="17"/></button>
      </template>
      <template v-else>
        <div v-if="caseLoading && !completed.length" class="empty" role="status">正在读取已结单记录…</div>
        <div v-else-if="caseError" class="empty"><p class="error" role="alert">{{ caseError }}</p><button class="btn secondary" @click="openCases">重新读取</button></div>
        <div v-else-if="!completed.length" class="empty"><ClipboardCheck :size="34"/><b>暂无可回流的已结单记录</b><p>请由维修人员在工单详情点击「确认外部工单已结单」。</p></div>
        <template v-else><button v-for="row in completed" :key="row.id" class="source-row" @click="openCase(row)"><span class="tile-icon amber"><ClipboardCheck :size="20"/></span><span class="source-main"><b>{{ row.fields.symptom }}</b><small>{{ row.externalNumber }} · {{ row.device.model }}</small></span><span class="pill">{{ row.caseSource ? (row.caseSource.active ? '案例已生效' : '案例 · ' + (row.caseSource.status === 'published' ? '未生效' : statuses[row.caseSource.status])) : '整理案例' }}</span><ChevronRight :size="17"/></button></template>
      </template>
    </section>
    <div v-if="selected || record || caseSourceId" class="backdrop" @click="selected = null; record = null; caseSourceId = null"></div>
    <SourceDetail v-if="caseSourceId" :key="caseSourceId" :source-id="caseSourceId" @close="caseSourceId = null" @updated="loadCases"/>
    <dialog v-if="selected" open class="sheet" aria-label="专家待办">
      <div class="sheet-head"><div><p class="eyebrow">{{ selected.deviceName }} · {{ statuses[selected.status] }}</p><h2>{{ selected.title }}</h2></div><button class="icon-btn" aria-label="关闭待办" @click="selected = null"><X :size="22"/></button></div>
      <div class="sheet-body form-stack">
        <p v-if="selected.assignee" class="muted">经办专家 · {{ selected.assignee }}</p>
        <div v-if="selected.note" class="document-text">{{ selected.note }}</div>
        <p v-if="selected.turn?.answer">{{ selected.turn.answer }}</p>
        <details v-for="item in selected.turn?.citations || []" :key="item.id" class="doc-section"><summary><BookOpen :size="15" class="inline-icon"/> {{ item.title }} · {{ item.position }}</summary><p class="document-text">{{ item.text }}</p></details>
        <div v-if="selected.reply" class="document-text"><b>专家答复</b><p>{{ selected.reply }}</p></div>
        <button v-if="expert && selected.status === 'open'" class="btn primary own-width" @click="action('claim')">领取待办</button>
        <form v-if="expert && ['open','assigned'].includes(selected.status) && (!selected.assignee || selected.assignee === store.profile?.username)" class="form-stack" @submit.prevent="action('assign')"><label>分派给其他专家<input v-model="assignee" placeholder="组织内专家账号" required/></label><button class="btn secondary own-width" :disabled="busy">确认分派</button></form>
        <template v-if="expert && selected.assignee === store.profile?.username && selected.status !== 'open'">
          <label>答复或知识修订内容<textarea v-model="note" rows="5" maxlength="4000"/></label>
          <div class="button-row"><button v-if="['assigned','answered'].includes(selected.status)" class="btn primary" :disabled="busy || !note.trim()" @click="action('reply')">保存答复</button><button v-if="selected.status === 'answered'" class="btn secondary" :disabled="busy" @click="action('close')">关闭待办</button><button v-if="['answered','closed'].includes(selected.status) && !selected.revisionCandidate" class="btn secondary" :disabled="busy || !note.trim()" @click="action('revise')">提交知识修订</button></div>
        </template>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <OperationHistory :events="selected.operationHistory"/>
      </div>
    </dialog>
    <dialog v-if="record" open class="modal" aria-label="整理回流案例">
      <div class="sheet-head"><div><p class="eyebrow">{{ record.externalNumber }}</p><h2>整理回流案例</h2></div><button class="icon-btn" aria-label="取消" @click="record = null"><X :size="22"/></button></div>
      <form class="sheet-body form-stack" @submit.prevent="sendCase"><p class="muted">核对适用条件、处置过程和结果，并移除人员姓名、联系方式等可识别信息。校验完成后将自动送审。</p><label>脱敏后的案例<textarea v-model="note" rows="9" required minlength="20" maxlength="16000"/></label><p v-if="error" class="error" role="alert">{{ error }}</p><div class="button-row"><button class="btn primary" :disabled="busy">送知识库校验</button></div><OperationHistory :events="record.operationHistory"/></form>
    </dialog>
  </main>
</template>

