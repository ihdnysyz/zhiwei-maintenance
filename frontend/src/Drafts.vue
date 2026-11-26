<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDownToLine, ArrowLeft, BookOpen, Check, ChevronRight, ClipboardList, File, Link2, Paperclip, Save, ShieldCheck, X } from 'lucide-vue-next'
import { useRoute } from 'vue-router'
import OperationHistory from './OperationHistory.vue'
import { api, date, errorMessage, notice, request, statuses, store, type Row } from './store'

const route = useRoute()
const rows = ref<Row[]>([])
const draft = ref<Row | null>(null)
const submission = ref<Row | null>(null)
const fields = ref<Record<string, string>>({})
const busy = ref(false)
const confirmOpen = ref(false)
const selectedRef = ref<Row | null>(null)
const contractNumber = ref('')
const err = ref('')
const ready = ref(false)
const fieldNames = { symptom: '故障现象', faultCode: '故障码', cause: '已确认原因', actions: '实际处置', result: '现场结果' }
const locked = computed(() => ['linked', 'submitting'].includes(draft.value?.status || ''))
const edited = computed(() => JSON.stringify(fields.value) !== JSON.stringify(draft.value?.fields || {})
  || contractNumber.value !== (draft.value?.pendingExternalNumber || ''))
const canExport = computed(() => ['confirmed','exported','linked'].includes(draft.value?.status || '') && !edited.value)
const hasOperation = computed(() => store.workorderEnabled && !store.profile?.demo)
const caseLabel = computed(() => {
  const progress = draft.value?.caseProgress
  if (!progress) return ''
  if (progress.sourceId) return '已生效'
  if (progress.status === 'not_submitted') return '待整理'
  if (progress.status === 'published') return '当前设备未生效'
  return statuses[progress.status]
})
function draftStatus(row: Row) {
  if (row.completedAt) return '已结单'
  return row.status === 'linked' ? '待确认结单' : statuses[row.status]
}
function update(row: Row, preserve = false) {
  draft.value = row
  if (!preserve) {
    fields.value = { ...row.fields }
    contractNumber.value = row.pendingExternalNumber || ''
  }
}
watch(() => route.params.id, async id => {
  err.value = ''; ready.value = false; draft.value = null; submission.value = null; contractNumber.value = ''
  try {
    if (id) {
      const row = await api<Row>('/drafts/' + id)
      update(row)
      if (row.status === 'submitting') await loadSubmission()
    } else rows.value = await api<Row[]>('/drafts')
  } catch (error) { err.value = errorMessage(error) }
  finally { ready.value = true }
}, { immediate: true })
async function loadSubmission() {
  if (draft.value) submission.value = await api<Row>('/drafts/' + draft.value.id + '/submission')
}
async function modify(work: () => Promise<void>) {
  if (busy.value || !draft.value) return
  err.value = ''; busy.value = true
  try { await work() }
  catch (error) { err.value = errorMessage(error) }
  finally { busy.value = false }
}
async function persist() {
  if (draft.value && edited.value) update(await api<Row>('/drafts/' + draft.value.id, 'PATCH', {
    revision: draft.value.revision, fields: fields.value, number: contractNumber.value,
  }))
}
function save() { return modify(async () => { await persist(); notice('已暂存工单草稿') }) }
function approve() { return modify(async () => {
  await persist()
  const current = draft.value!
  update(await api<Row>('/drafts/' + current.id + '/confirm', 'POST', {
    revision: current.revision, number: contractNumber.value,
  }))
  confirmOpen.value = false
  notice('工单已确认并登记单号，实际结单后可直接确认结单')
}) }
function download() { return modify(async () => {
  const current = draft.value!
  const result = await request('/drafts/' + current.id + '/export', 'POST', { revision: current.revision })
  const url = URL.createObjectURL(await result.blob())
  const anchor = Object.assign(document.createElement('a'), { href: url, download: '维修工单-' + current.id.slice(0, 8) + '.json' })
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1500)
  update(await api<Row>('/drafts/' + current.id))
  notice('已导出确认稿')
}) }
function addFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  return modify(async () => {
    const current = draft.value!
    const body = new FormData(); body.append('file', file)
    update(await api<Row>('/drafts/' + current.id + '/attachments?revisionId=' + current.revision, 'POST', body), edited.value)
    notice('附件已添加')
  })
}
function removeFile(id: string) { return modify(async () => {
  const current = draft.value!
  update(await api<Row>('/drafts/' + current.id + '/attachments/' + id + '?revisionId=' + current.revision, 'DELETE'), edited.value)
}) }
function submit() { return modify(async () => {
  const current = draft.value!
  submission.value = await api<Row>('/drafts/' + current.id + '/submit', 'POST', { revision: current.revision })
  update(await api<Row>('/drafts/' + current.id))
  notice('已受理回写，可查询原请求结果')
}) }
function refresh() { return modify(async () => {
  const current = draft.value!
  await loadSubmission()
  update(await api<Row>('/drafts/' + current.id))
}) }
function markDone() { return modify(async () => {
  const current = draft.value!
  update(await api<Row>('/drafts/' + current.id + '/complete', 'POST', { revision: current.revision }))
  notice('已结单，可在工单详情查看案例回流状态')
}) }
</script>

<template>
  <main class="page">
    <div class="page-heading"><div><h1>{{ draft ? (locked ? '工单详情' : '编辑工单草稿') : '工单草稿' }}</h1><p>{{ draft ? (locked ? '查看现场记录、结单和案例回流状态。' : '填写外部工单号，核对现场记录后确认。') : '从诊断会话生成草稿，保留处置过程与现场结果。' }}</p></div><RouterLink v-if="draft" class="btn secondary" to="/drafts"><ArrowLeft :size="17"/> 返回草稿</RouterLink><RouterLink v-else class="btn primary" to="/ask">开始诊断 <ChevronRight :size="17"/></RouterLink></div>
    <p v-if="!draft && err" class="page-error" role="alert">{{ err }}</p>
    <section v-if="!route.params.id" class="card list-card">
      <div v-if="!rows.length" class="empty"><ClipboardList :size="34"/><b>{{ ready ? '还没有工单草稿' : '正在读取草稿…' }}</b><p v-if="ready">在问答中选择「生成工单草稿」即可开始填写。</p></div>
      <RouterLink v-for="row in rows" :key="row.id" class="source-row" :to="'/drafts/' + row.id"><span class="tile-icon mint"><ClipboardList :size="21"/></span><span class="source-main"><b>{{ row.fields.symptom || row.device.name }}</b><small>{{ row.device.number }} · {{ date(row.updated, true) }}</small></span><span class="pill">{{ draftStatus(row) }}</span><ChevronRight :size="18"/></RouterLink>
    </section>
    <div v-if="draft" class="edit-layout">
      <section class="card content-card form-stack">
        <div class="section-head"><div><h2>现场记录</h2><p>{{ draft.device.name }} · {{ draft.device.number }}</p></div><span :class="['pill', { green: draft.completedAt || ['confirmed', 'exported'].includes(draft.status) }]">{{ draftStatus(draft) }}</span></div>
        <div class="device-facts"><div><small>设备型号 / 批次</small><b>{{ draft.device.model }} / {{ draft.device.batch || '未知' }}</b></div><div><small>设备位置</small><b>{{ draft.device.location || '未登记' }}</b></div></div>
        <label v-if="!locked"><span class="field-label">外部工单号 <em>*</em></span><input v-model.trim="contractNumber" required maxlength="64" placeholder="填写正式系统的工单号"/></label>
        <label v-for="(label, key) in fieldNames" :key="key"><span class="field-label">{{ label }} <em v-if="key !== 'faultCode'">*</em><small v-if="draft.origins[key]">{{ draft.origins[key] }}</small></span><input v-if="key === 'faultCode'" v-model="fields[key]" maxlength="120" :disabled="locked" placeholder="如有，请核对后填写"/><textarea v-else v-model="fields[key]" :disabled="locked" rows="3" maxlength="8000" :placeholder="key === 'symptom' ? '描述现场故障' : '填写经现场确认的' + label"></textarea></label>
        <div class="attachments">
          <div class="section-head"><h3><Paperclip :size="16"/> 现场附件</h3><label v-if="!locked" class="btn secondary upload-button"><Paperclip :size="16"/> 添加附件<input type="file" accept="application/pdf,image/png,image/jpeg" :disabled="busy" @change="addFile"/></label></div>
          <div v-for="file in draft.attachments" :key="file.id" class="attachment-row"><File :size="17"/><a :href="'/api/v1/drafts/' + draft.id + '/attachments/' + file.id">{{ file.filename }}</a><button v-if="!locked" class="icon-btn" aria-label="移除附件" :disabled="busy" @click="removeFile(file.id)"><X :size="17"/></button></div>
          <p v-if="!draft.attachments.length" class="muted">支持 PDF、PNG、JPEG，单件最多 20 MiB。</p>
        </div>
        <p v-if="err && !confirmOpen" class="error" role="alert">{{ err }}</p>
        <div v-if="!locked" class="button-row"><button class="btn secondary" :disabled="busy" @click="save"><Save :size="16"/> 暂存</button><button class="btn primary" :disabled="busy || contractNumber.length < 3" @click="confirmOpen = true; err = ''"><ShieldCheck :size="17"/> 预览并确认</button></div>
      </section>
      <aside class="form-stack">
        <section class="card content-card"><div class="section-head"><div><h2>参考依据</h2><p>来自诊断会话中的适用资料</p></div><BookOpen :size="20" class="muted"/></div>
          <button v-for="item in draft.references" :key="item.id" class="source-row short" @click="selectedRef = item"><span class="source-main"><b>{{ item.title }}</b><small>{{ item.position }} · V{{ item.version }}</small></span><ChevronRight :size="16"/></button>
          <p v-if="!draft.references?.length" class="muted">当前没有可用的引用。</p><RouterLink v-if="draft.sessionAvailable" class="text-link" :to="'/ask/' + draft.sessionId">回到诊断会话 <ChevronRight :size="15"/></RouterLink><p v-else class="muted">来源对话已删除。</p>
        </section>
        <section class="card content-card form-stack">
          <div class="section-head"><div><h2>结单与导出</h2><p>确认后可随时导出工单快照。</p></div><Link2 :size="20" class="muted"/></div>
          <button v-if="canExport" class="btn secondary own-width" :disabled="busy" @click="download"><ArrowDownToLine :size="17"/> 导出确认稿</button>
          <button v-if="hasOperation && canExport && !locked" class="btn primary own-width" :disabled="busy" @click="submit">正式回写</button>
          <p v-if="draft.externalNumber"><Check :size="17" class="inline-icon"/> {{ draft.externalNumber }}</p>
          <template v-if="draft.status === 'linked' && !draft.completedAt">
            <p class="muted">外部工单实际结单后，请在这里确认。确认后，知识管理员可在案例回流中查看。</p>
            <button class="btn primary own-width" :disabled="busy" @click="markDone">确认外部工单已结单</button>
          </template>
          <div v-if="draft.caseProgress" class="form-stack">
            <div class="tag-row"><b>案例回流</b><span :class="['pill', { green: draft.caseProgress.sourceId }]">{{ caseLabel }}</span></div>
            <RouterLink v-if="draft.caseProgress.sourceId" class="text-link" :to="{ path: '/library', query: { source: draft.caseProgress.sourceId } }">查看已发布案例 <ChevronRight :size="15"/></RouterLink>
            <p v-else-if="draft.caseProgress.status === 'not_submitted'" class="muted">已进入「专家协作 → 案例回流」。知识管理员或班组专家可以整理并送审。</p>
            <p v-if="!draft.caseProgress.sourceId" class="muted">审核通过并生效后，可在「知识库 → 故障案例」查看。</p>
          </div>
          <div v-if="submission"><p class="muted">回写请求 {{ submission.requestId }} · {{ statuses[submission.status] || submission.status }}</p><button class="btn secondary own-width" :disabled="busy" @click="refresh">查询原请求</button></div>
          <OperationHistory :events="draft.operationHistory"/>
        </section>
      </aside>
    </div>
    <Transition name="scrim">
      <div v-if="confirmOpen || selectedRef" class="backdrop" @click="confirmOpen = false; selectedRef = null"></div>
    </Transition>
    <Transition name="overlay">
      <dialog v-if="confirmOpen && draft" open class="modal" aria-label="现场工单确认" @keydown.esc="confirmOpen = false">
        <div class="sheet-head"><h2>确认现场处置</h2><button class="icon-btn" aria-label="取消" @click="confirmOpen = false"><X :size="22"/></button></div>
        <div class="sheet-body form-stack"><p class="muted">请核对设备、实测原因、实际处置和结果。确认后保存当前快照。</p>
          <dl class="preview-fields"><dt>外部工单号</dt><dd>{{ contractNumber }}</dd><dt>设备</dt><dd>{{ draft.device.number }} · {{ draft.device.name }}</dd><template v-for="(label,key) in fieldNames" :key="key"><dt>{{ label }}</dt><dd>{{ fields[key] || '未填写' }}</dd></template></dl>
          <p v-if="err" class="error" role="alert">{{ err }}</p><div class="button-row"><button class="btn secondary" @click="confirmOpen = false">返回编辑</button><button class="btn primary" :disabled="busy" @click="approve"><Check :size="17"/> 确认现场记录</button></div>
        </div>
      </dialog>
    </Transition>
    <Transition name="overlay">
      <dialog v-if="selectedRef" v-sheet-drag="() => { selectedRef = null }" open class="sheet" aria-label="工单引用" @keydown.esc="selectedRef = null">
        <div class="sheet-head"><div><p class="eyebrow">{{ selectedRef.position }} · V{{ selectedRef.version }}</p><h2>{{ selectedRef.title }}</h2></div><button class="icon-btn" aria-label="关闭出处" @click="selectedRef = null"><X :size="22"/></button></div>
        <div class="sheet-body"><div class="document-text">{{ selectedRef.text }}</div></div>
      </dialog>
    </Transition>
  </main>
</template>

