<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ArrowDownToLine, ArrowUpRight, X } from 'lucide-vue-next'
import OperationHistory from './OperationHistory.vue'
import { api, categories, date, errorMessage, notice, statuses, store, type Row } from './store'

const props = defineProps<{ sourceId: string; deviceId?: string }>()
const emit = defineEmits<{ close: []; updated: [] }>()
const detail = ref<Row | null>(null)
const correctionId = ref('')
const correctedText = ref('')
const verified = ref(false)
const allowExternalModel = ref(false)
const actionNote = ref('')
const startAt = ref('')
const endAt = ref('')
const schedule = ref(false)
const blockBatch = ref('')
const working = ref(false)
const errorText = ref('')
const manager = computed(() => store.profile?.role === 'library')
const expert = computed(() => store.profile?.role === 'expert')
const trialDevice = ref('')
const trialQuestion = ref('')
const trialResult = ref<Row | null>(null)
const trialError = ref('')
const trialBusy = ref(false)
const trialDevices = computed(() => detail.value ? store.devices.filter(device =>
  (detail.value?.model === '*' || detail.value?.model === device.model)
  && (!detail.value?.batch || detail.value.batch === device.batch)) : [])
const trialLabels: Record<string, string> = {
  answer: '已形成答复', evidence: '已找到原文', no_evidence: '缺少可用依据',
  clarify: '需补充信息', safety: '需专业处置', conflict: '资料存在冲突', out_of_scope: '设备范围不符',
}
watch(trialDevices, devices => {
  if (!devices.some(device => device.id === trialDevice.value)) trialDevice.value = ''
  if (!trialDevice.value && devices.length === 1) trialDevice.value = devices[0].id
})
watch([trialDevice, trialQuestion], () => { trialResult.value = null; trialError.value = '' })
let active = true
let timer: number | undefined

async function load() {
  clearTimeout(timer)
  errorText.value = ''
  try {
    const row = await api<Row>('/sources/' + props.sourceId + '?' + new URLSearchParams({ deviceId: props.deviceId || '' }))
    if (!active) return
    const finishedParsing = detail.value?.status === 'queued' && row.status !== 'queued'
    detail.value = { ...row, viewDeviceId: props.deviceId || '' }
    if (finishedParsing) emit('updated')
    if (row.status === 'queued') timer = window.setTimeout(load, 1500)
  } catch (error) { if (active) errorText.value = errorMessage(error) }
}
onMounted(load)
onUnmounted(() => { active = false; clearTimeout(timer) })
async function trySource() {
  if (!trialDevice.value || !trialQuestion.value.trim() || trialBusy.value) return
  trialBusy.value = true
  trialResult.value = null
  trialError.value = ''
  try {
    const result = await api<Row>('/sources/' + props.sourceId + '/trial', 'POST', {
      deviceId: trialDevice.value, question: trialQuestion.value.trim(),
    })
    if (active) trialResult.value = result
  } catch (error) { if (active) trialError.value = errorMessage(error) }
  finally { trialBusy.value = false }
}
async function saveCorrection(id: string) {
  if (!detail.value || working.value) return
  working.value = true
  errorText.value = ''
  try {
    await api('/sources/' + props.sourceId + '/chunks/' + id, 'PATCH', {
      revision: detail.value.revision, text: correctedText.value,
    })
    if (!active) return
    correctionId.value = ''
    verified.value = false
    trialResult.value = null
    await load()
    notice('校核片段已保存')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}
async function action(name: string) {
  if (!detail.value || working.value) return
  working.value = true
  errorText.value = ''
  try {
    const convert = (value: string) => schedule.value && value ? new Date(value).toISOString() : ''
    const row = await api<Row>('/sources/' + props.sourceId + '/actions', 'POST', {
      revision: detail.value.revision, action: name, verified: verified.value, note: actionNote.value,
      start: convert(startAt.value), end: convert(endAt.value),
      ...(name === 'block' && blockBatch.value.trim() ? { batch: blockBatch.value.trim() } : {}),
      ...(name === 'approve' && allowExternalModel.value ? { allowExternalModel: true } : {}),
    })
    if (!active) return
    verified.value = false
    allowExternalModel.value = false
    correctionId.value = ''
    actionNote.value = ''
    trialResult.value = null
    startAt.value = ''; endAt.value = ''; schedule.value = false; blockBatch.value = ''
    await load()
    emit('updated')
    notice(name === 'authorize_model' ? '已授权回答模型' : statuses[row.status] || '资料已更新')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}
</script>

<template>
  <dialog v-sheet-drag="() => emit('close')" open class="sheet" aria-label="查看资料" @keydown.esc="emit('close')">
    <div class="sheet-head">
      <div><p v-if="detail" class="eyebrow">{{ categories[detail.category] }} · V{{ detail.version }}</p><h2>{{ detail?.title || '资料详情' }}</h2></div>
      <button class="icon-btn" aria-label="关闭资料" @click="emit('close')"><X :size="23"/></button>
    </div>
    <div v-if="!detail" class="sheet-body" role="status">{{ errorText || '正在读取资料…' }}</div>
      <div v-else class="sheet-body form-stack">
        <div class="tag-row"><span class="pill">{{ detail.model }} {{ detail.batch }}</span><span class="pill">{{ detail.demo ? '演示资料' : statuses[detail.status] }}</span><span v-if="detail.ocr" class="pill">包含 OCR</span></div>
        <p v-if="detail.error" class="error">{{ detail.error }}</p>
        <p v-if="detail.note" class="muted">最近审核意见 · {{ detail.note }}</p>
        <p v-if="detail.externalModelApproved" class="muted">已授权回答模型 · {{ detail.modelApprovalHost }}</p>
        <div v-if="detail.referenceUrl || (!manager && !expert && detail.active && detail.viewDeviceId)" class="tag-row">
          <a v-if="detail.referenceUrl" class="text-link" :href="detail.referenceUrl" target="_blank" rel="noopener noreferrer"><ArrowUpRight :size="17"/> 查看发布来源</a>
          <RouterLink v-if="!manager && !expert && detail.active && detail.viewDeviceId" class="text-link" :to="{ path: '/ask', query: { device: detail.viewDeviceId } }">按此设备提问</RouterLink>
        </div>
        <a v-if="detail.filename && (manager || expert || detail.chunks?.length)" class="btn secondary own-width" :href="'/api/v1/sources/' + detail.id + '/file' + (manager || expert ? '' : '?deviceId=' + detail.viewDeviceId + '&chunkId=' + detail.chunks[0].id)" target="_blank" rel="noopener"><ArrowDownToLine :size="17"/> {{ detail.filename }}</a>
        <details v-if="(manager || expert) && detail.indexReady" class="doc-section">
          <summary>试问这份资料</summary>
          <form class="form-stack" @submit.prevent="trySource">
            <p class="muted">本地预演只读取本版本的片段。{{ detail.declaredCodeCount ? '已识别 ' + detail.declaredCodeCount + ' 个明确的故障码。' : detail.indexedCodeCount ? '已识别 ' + detail.indexedCodeCount + ' 个待核对的候选报码。' : '本版本未识别到故障码，可按现象试问。' }}</p>
            <p v-if="detail.indexedCodeExamples?.length" class="muted">{{ detail.indexedCodeExamples.join('、') }}</p>
            <p v-if="!trialDevices.length" class="muted">设备台账中还没有匹配的型号和批次。</p>
            <template v-else>
              <label>测试设备<select v-model="trialDevice" required :disabled="trialBusy">
                <option value="" disabled>选择适用设备</option>
                <option v-for="device in trialDevices" :key="device.id" :value="device.id">{{ device.number }} · {{ device.name }}{{ device.batch ? ' · ' + device.batch : '' }}</option>
              </select></label>
              <label>现场问题<textarea v-model="trialQuestion" required maxlength="3000" rows="2" :disabled="trialBusy" :placeholder="detail.category === 'manual' ? '例如：4110 怎么处理？' : '例如：这个故障在历史案例里是怎么处理的？'"></textarea></label>
              <button class="btn secondary own-width" :disabled="trialBusy || !trialDevice || !trialQuestion.trim()">{{ trialBusy ? '正在试问…' : '试问' }}</button>
            </template>
            <p v-if="trialError" class="error" role="alert">{{ trialError }}</p>
          </form>
          <div v-if="trialResult" class="form-stack" aria-live="polite">
            <p class="muted">{{ trialLabels[trialResult.decision] || trialResult.decision }} · 命中 {{ trialResult.hits.length }} 个片段{{ trialResult.availableInAsk ? ' · 对该设备已生效' : ' · 对该设备尚未生效' }}</p>
            <p v-if="trialResult.requestedCodes.length" class="muted">查询故障码 {{ trialResult.requestedCodes.join('、') }}</p>
            <div class="document-text">{{ trialResult.answer }}</div>
            <details v-for="(hit, index) in trialResult.hits" :key="hit.id" class="doc-section">
              <summary>{{ index + 1 }}. {{ hit.position }}{{ trialResult.referenceIds.includes(hit.id) ? ' · 答复引用 [' + (trialResult.referenceIds.indexOf(hit.id) + 1) + ']' : '' }}</summary>
              <div class="document-text">{{ hit.text }}</div>
            </details>
          </div>
        </details>
        <details v-if="detail.scopes?.length && (manager || expert)" class="doc-section"><summary>版本生效记录</summary><p v-for="scope in detail.scopes" :key="scope.id">{{ scope.model }} {{ scope.batch }} · {{ scope.status === 'bind' ? '绑定' : '阻断' }} · {{ date(scope.start, true) }} 至 {{ scope.end ? date(scope.end, true) : '持续有效' }}</p></details>
        <article v-for="chunk in detail.chunks" :key="chunk.id" class="doc-section">
          <div class="section-head"><h3>{{ chunk.position }} {{ chunk.ocr ? '· OCR' : '' }}</h3><button v-if="manager && ['calibration','returned'].includes(detail.status) && correctionId !== chunk.id" class="text-link" @click="correctionId = chunk.id; correctedText = chunk.text">校正片段</button></div>
          <form v-if="correctionId === chunk.id" class="form-stack" @submit.prevent="saveCorrection(chunk.id)">
            <label>核对原件后的文字<textarea v-model="correctedText" rows="6" required maxlength="80000"></textarea></label>
            <div class="button-row"><button type="button" class="btn secondary" @click="correctionId = ''">取消</button><button class="btn primary" :disabled="working">保存修正</button></div>
          </form>
          <div v-else class="document-text">{{ chunk.text }}</div>
        </article>
        <template v-if="manager && ['calibration','returned'].includes(detail.status)">
          <label class="check"><input v-model="verified" type="checkbox"/> 已核对原件、故障码、关键数值、步骤及图表</label>
          <label>校核说明<textarea v-model="actionNote" rows="3" maxlength="1500"></textarea></label>
          <button class="btn primary own-width" :disabled="!verified || working" @click="action('review')">送独立审核</button>
        </template>
        <template v-if="expert && detail.status === 'review'">
          <label>审核意见<textarea v-model="actionNote" rows="3" maxlength="1500"></textarea></label>
          <label v-if="detail.modelApprovalHost" class="check">
            <input v-model="allowExternalModel" type="checkbox"/>
            允许将本版本的相关摘录和提问发送给回答模型（{{ detail.modelApprovalHost }}）
          </label>
          <div class="button-row"><button class="btn secondary" :disabled="!actionNote.trim() || working" @click="action('reject')">退回校核</button><button class="btn primary" :disabled="working" @click="action('approve')">{{ detail.category === 'case' && detail.workorderLocator ? '审核通过并发布' : '审核通过' }}</button></div>
        </template>
        <details v-if="expert && detail.publishOnParse && detail.status === 'published' && detail.modelApprovalHost && !detail.externalModelApproved" class="doc-section">
          <summary>授权回答模型（可选）</summary>
          <p class="muted">授权后，相关资料摘录和提问可以发送给 {{ detail.modelApprovalHost }}。</p>
          <button class="btn secondary" :disabled="working" @click="action('authorize_model')">允许发送给回答模型</button>
        </details>
        <template v-if="manager && ['approved','published','withdrawn'].includes(detail.status)">
          <label class="check"><input v-model="schedule" type="checkbox"/> 指定生效时间</label>
          <div v-if="schedule" class="form-grid"><label>开始时间<input v-model="startAt" type="datetime-local"/></label><label>结束时间<input v-model="endAt" type="datetime-local"/></label></div>
          <label v-if="detail.active && !detail.batch">下架批次<input v-model="blockBatch" placeholder="留空下架整个型号范围" maxlength="80"/></label>
          <div class="button-row"><button v-if="detail.active" class="btn secondary" :disabled="working" @click="action('block')">下架适用范围</button><button v-if="!detail.active || schedule" class="btn primary" :disabled="working" @click="action('publish')">发布该版本</button></div>
        </template>
        <p v-if="errorText" class="error" role="alert">{{ errorText }}</p>
        <OperationHistory :events="detail.operationHistory"/>
      </div>
  </dialog>
</template>
