<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Download, Link as LinkIcon, Refresh } from '@element-plus/icons-vue'
import MarkdownView from './MarkdownView.vue'
import OperationHistory from './OperationHistory.vue'
import { api, can, categories, date, errorMessage, notice, statuses, statusTag, store, type Row } from '../store'

const props = defineProps<{ sourceId: string; deviceId?: string }>()
const emit = defineEmits<{ close: []; updated: [] }>()
const router = useRouter()
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
const trialDevice = ref('')
const trialQuestion = ref('')
const trialResult = ref<Row | null>(null)
const trialError = ref('')
const trialBusy = ref(false)

const manager = computed(() => can('knowledge:manage'))
const expert = computed(() => can('knowledge:review'))
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

async function reindex() {
  if (!detail.value || working.value) return
  working.value = true
  errorText.value = ''
  try {
    await api('/sources/' + props.sourceId + '/reindex', 'POST')
    await load()
    emit('updated')
    notice('已加入重新切片与向量化队列')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}

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
    notice('校核片段已保存并更新向量')
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
  <el-drawer :model-value="true" size="620px" :title="detail?.title || '资料详情'" @close="emit('close')">
    <div v-if="!detail" class="muted" role="status">{{ errorText || '正在读取资料…' }}</div>
    <div v-else class="stack">
      <div class="row-gap">
        <el-tag size="small">{{ categories[detail.category] }}</el-tag>
        <el-tag size="small" type="info">V{{ detail.version }}</el-tag>
        <el-tag size="small" type="info">{{ detail.model }}{{ detail.batch ? ' / ' + detail.batch : '' }}</el-tag>
        <el-tag v-if="manager || expert" size="small" :type="statusTag(detail.status, detail.active)">{{ detail.active ? '已生效' : statuses[detail.status] }}</el-tag>
        <el-tag v-if="detail.ocr" size="small" type="warning">OCR</el-tag>
      </div>

      <el-descriptions v-if="manager || expert" :column="2" border size="small" title="RAG 切片与向量索引">
        <el-descriptions-item label="切片数量">{{ detail.chunkCount ?? detail.chunks?.length ?? 0 }} 段</el-descriptions-item>
        <el-descriptions-item label="已向量化">{{ detail.embeddedCount ?? 0 }} 段{{ detail.embeddingDim ? ` (${detail.embeddingDim} 维)` : '' }}</el-descriptions-item>
        <el-descriptions-item label="向量模型">{{ detail.embeddingModel || '未嵌入' }}</el-descriptions-item>
        <el-descriptions-item label="切片策略">{{ detail.chunkSize ? `${detail.chunkSize} 字 / 重叠 ${detail.chunkOverlap ?? 0}` : '默认' }}</el-descriptions-item>
      </el-descriptions>

      <el-alert v-if="detail.error" :title="detail.error" type="error" show-icon :closable="false" />
      <p v-if="detail.note" class="muted">审核意见：{{ detail.note }}</p>
      <p v-if="detail.externalModelApproved" class="muted">已授权回答模型：{{ detail.modelApprovalHost }}</p>

      <div class="row-gap">
        <el-button v-if="manager" :icon="Refresh" :loading="working" :disabled="detail.status === 'queued'" @click="reindex">重新切片与向量化</el-button>
        <el-button v-if="detail.filename && (manager || expert || detail.chunks?.length)" tag="a" :href="'/api/v1/sources/' + detail.id + '/file' + (manager || expert ? '' : '?deviceId=' + detail.viewDeviceId + '&chunkId=' + detail.chunks[0].id)" target="_blank" rel="noopener" :icon="Download">{{ detail.filename }}</el-button>
        <el-button v-if="detail.referenceUrl" tag="a" :href="detail.referenceUrl" target="_blank" rel="noopener noreferrer" :icon="LinkIcon">查看发布来源</el-button>
        <el-button v-if="!manager && !expert && detail.active && detail.viewDeviceId" type="primary" plain @click="router.push({ path: '/ask', query: { device: detail.viewDeviceId } })">按此设备提问</el-button>
      </div>

      <el-collapse v-if="(manager || expert) && (detail.indexReady || detail.scopes?.length)">
        <el-collapse-item v-if="detail.indexReady" title="试问这份资料" name="trial">
          <div class="stack">
            <p v-if="detail.indexedCodeExamples?.length" class="muted">故障码：{{ detail.indexedCodeExamples.join('、') }}</p>
            <p v-if="!trialDevices.length" class="muted">暂无匹配设备</p>
            <el-form v-else label-position="top" @submit.prevent="trySource">
              <el-form-item label="测试设备" required>
                <el-select v-model="trialDevice" :disabled="trialBusy" placeholder="选择适用设备" class="w-full">
                  <el-option v-for="device in trialDevices" :key="device.id" :value="device.id" :label="`${device.number} · ${device.name}${device.batch ? ' · ' + device.batch : ''}`" />
                </el-select>
              </el-form-item>
              <el-form-item label="现场问题" required>
                <el-input v-model="trialQuestion" type="textarea" :rows="2" maxlength="3000" :disabled="trialBusy" />
              </el-form-item>
              <el-button type="primary" plain native-type="submit" :loading="trialBusy" :disabled="!trialDevice || !trialQuestion.trim()">试问</el-button>
            </el-form>
            <el-alert v-if="trialError" :title="trialError" type="error" show-icon :closable="false" />
            <div v-if="trialResult" class="stack" aria-live="polite">
              <div class="row-gap">
                <el-tag size="small">{{ trialLabels[trialResult.decision] || trialResult.decision }}</el-tag>
                <span class="muted">命中 {{ trialResult.hits?.length || 0 }} 段 · {{ trialResult.availableInAsk ? '已生效' : '未生效' }}</span>
              </div>
              <div class="md-panel"><MarkdownView :content="trialResult.answer || ''" /></div>
            </div>
          </div>
        </el-collapse-item>

        <el-collapse-item v-if="detail.scopes?.length" title="版本生效记录" name="scopes">
          <div v-for="scope in detail.scopes" :key="scope.id" class="muted" style="margin-bottom: 6px">
            {{ scope.model }} {{ scope.batch }} · {{ scope.status === 'bind' ? '绑定' : '阻断' }} · {{ date(scope.start, true) }} 至 {{ scope.end ? date(scope.end, true) : '持续有效' }}
          </div>
        </el-collapse-item>
      </el-collapse>

      <div class="stack">
        <el-card v-for="(chunk, idx) in detail.chunks" :key="chunk.id" shadow="never">
          <template #header>
            <div class="space-between">
              <b>#{{ idx + 1 }} · {{ chunk.position }}{{ chunk.ocr ? ' · OCR' : '' }}</b>
              <el-button v-if="manager && ['calibration','returned'].includes(detail.status) && correctionId !== chunk.id" link type="primary" @click="correctionId = chunk.id; correctedText = chunk.text">校正片段</el-button>
            </div>
          </template>
          <div v-if="correctionId === chunk.id" class="stack">
            <el-input v-model="correctedText" type="textarea" :rows="6" maxlength="80000" />
            <div class="row-gap">
              <el-button @click="correctionId = ''">取消</el-button>
              <el-button type="primary" :loading="working" @click="saveCorrection(chunk.id)">保存修正</el-button>
            </div>
          </div>
          <div v-else class="doc-text">{{ chunk.text }}</div>
        </el-card>
      </div>

      <el-card v-if="manager && ['calibration','returned'].includes(detail.status)" shadow="never">
        <div class="stack">
          <el-checkbox v-model="verified">已核对原件、故障码、关键数值、步骤及图表</el-checkbox>
          <el-input v-model="actionNote" type="textarea" :rows="3" maxlength="1500" placeholder="校核说明" />
          <el-button type="primary" :disabled="!verified" :loading="working" @click="action('review')">送独立审核</el-button>
        </div>
      </el-card>

      <el-card v-if="expert && detail.status === 'review'" shadow="never">
        <div class="stack">
          <el-input v-model="actionNote" type="textarea" :rows="3" maxlength="1500" placeholder="审核意见" />
          <el-checkbox v-if="detail.modelApprovalHost" v-model="allowExternalModel">允许发送给回答模型（{{ detail.modelApprovalHost }}）</el-checkbox>
          <div class="row-gap">
            <el-button :disabled="!actionNote.trim()" :loading="working" @click="action('reject')">退回校核</el-button>
            <el-button type="primary" :loading="working" @click="action('approve')">{{ detail.category === 'case' && detail.workorderLocator ? '审核通过并发布' : '审核通过' }}</el-button>
          </div>
        </div>
      </el-card>

      <el-button v-if="expert && detail.publishOnParse && detail.status === 'published' && detail.modelApprovalHost && !detail.externalModelApproved" :loading="working" @click="action('authorize_model')">
        授权回答模型（{{ detail.modelApprovalHost }}）
      </el-button>

      <el-card v-if="manager && ['approved','published','withdrawn'].includes(detail.status)" shadow="never">
        <div class="stack">
          <el-checkbox v-model="schedule">指定生效时间</el-checkbox>
          <div v-if="schedule" class="form-grid">
            <el-form-item label="开始时间"><el-input v-model="startAt" type="datetime-local" /></el-form-item>
            <el-form-item label="结束时间"><el-input v-model="endAt" type="datetime-local" /></el-form-item>
          </div>
          <el-input v-if="detail.active && !detail.batch" v-model="blockBatch" placeholder="下架批次（留空下架整个型号范围）" maxlength="80" />
          <div class="row-gap">
            <el-button v-if="detail.active" :loading="working" @click="action('block')">下架适用范围</el-button>
            <el-button v-if="!detail.active || schedule" type="primary" :loading="working" @click="action('publish')">发布该版本</el-button>
          </div>
        </div>
      </el-card>

      <el-alert v-if="errorText" :title="errorText" type="error" show-icon :closable="false" />
      <OperationHistory :events="detail.operationHistory" />
    </div>
  </el-drawer>
</template>
