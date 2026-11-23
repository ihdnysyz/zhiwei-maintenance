<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
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
    startAt.value = ''; endAt.value = ''; schedule.value = false; blockBatch.value = ''
    await load()
    emit('updated')
    notice(statuses[row.status] || '资料已更新')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}
</script>

<template>
  <dialog open class="sheet" aria-label="查看资料" @keydown.esc="emit('close')">
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
