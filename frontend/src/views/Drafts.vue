<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, ArrowRight, Check, Delete, Download, Paperclip, Select } from '@element-plus/icons-vue'
import OperationHistory from '../components/OperationHistory.vue'
import { api, can, date, errorMessage, notice, request, statuses, statusTag, store, type Row } from '../store'

const route = useRoute()
const router = useRouter()
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
const fileInput = ref<HTMLInputElement>()

const fieldNames = { symptom: '故障现象', faultCode: '故障码', cause: '已确认原因', actions: '实际处置', result: '现场结果' }
const locked = computed(() => ['linked', 'submitting'].includes(draft.value?.status || ''))
const edited = computed(() => JSON.stringify(fields.value) !== JSON.stringify(draft.value?.fields || {})
  || contractNumber.value !== (draft.value?.pendingExternalNumber || ''))
const canExport = computed(() => ['confirmed', 'exported', 'linked'].includes(draft.value?.status || '') && !edited.value)
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

function approve() {
  return modify(async () => {
    await persist()
    const current = draft.value!
    update(await api<Row>('/drafts/' + current.id + '/confirm', 'POST', {
      revision: current.revision, number: contractNumber.value,
    }))
    confirmOpen.value = false
    notice('工单已确认')
  })
}

function download() {
  return modify(async () => {
    const current = draft.value!
    const result = await request('/drafts/' + current.id + '/export', 'POST', { revision: current.revision })
    const url = URL.createObjectURL(await result.blob())
    const anchor = Object.assign(document.createElement('a'), { href: url, download: '维修工单-' + current.id.slice(0, 8) + '.json' })
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1500)
    update(await api<Row>('/drafts/' + current.id))
    notice('已导出确认稿')
  })
}

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

function removeFile(id: string) {
  return modify(async () => {
    const current = draft.value!
    update(await api<Row>('/drafts/' + current.id + '/attachments/' + id + '?revisionId=' + current.revision, 'DELETE'), edited.value)
  })
}

function submit() {
  return modify(async () => {
    const current = draft.value!
    submission.value = await api<Row>('/drafts/' + current.id + '/submit', 'POST', { revision: current.revision })
    update(await api<Row>('/drafts/' + current.id))
    notice('已受理回写')
  })
}

function refresh() {
  return modify(async () => {
    const current = draft.value!
    await loadSubmission()
    update(await api<Row>('/drafts/' + current.id))
  })
}

function markDone() {
  return modify(async () => {
    const current = draft.value!
    update(await api<Row>('/drafts/' + current.id + '/complete', 'POST', { revision: current.revision }))
    notice('已确认结单')
  })
}
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <h1>{{ draft ? (locked ? '工单详情' : '编辑工单草稿') : '工单草稿' }}</h1>
      <el-button v-if="draft" :icon="ArrowLeft" @click="router.push('/drafts')">返回草稿</el-button>
      <el-button v-else type="primary" @click="router.push('/ask')">开始诊断 <el-icon class="el-icon--right"><ArrowRight /></el-icon></el-button>
    </div>

    <el-alert v-if="!draft && err" :title="err" type="error" show-icon :closable="false" />

    <el-card v-if="!route.params.id" shadow="never">
      <el-table v-if="rows.length" :data="rows" style="cursor: pointer" @row-click="(row: Row) => router.push('/drafts/' + row.id)">
        <el-table-column label="故障现象" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ row.fields.symptom || row.device.name }}</template>
        </el-table-column>
        <el-table-column label="设备" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.device.number }} · {{ row.device.name }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="140">
          <template #default="{ row }">{{ date(row.updated, true) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="120" align="right">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTag(row.status, Boolean(row.completedAt))">{{ draftStatus(row) }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :description="ready ? '暂无工单草稿' : '正在读取…'">
        <el-button v-if="ready" type="primary" plain @click="router.push('/ask')">前往问答</el-button>
      </el-empty>
    </el-card>

    <div v-if="draft" class="edit-layout">
      <el-card shadow="never">
        <template #header>
          <div class="space-between">
            <div>
              <b>现场记录</b>
              <span class="muted" style="margin-left: 8px">{{ draft.device.name }} · {{ draft.device.number }}</span>
            </div>
            <el-tag size="small" :type="statusTag(draft.status, Boolean(draft.completedAt))">{{ draftStatus(draft) }}</el-tag>
          </div>
        </template>

        <div class="stack">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="设备型号 / 批次">{{ draft.device.model }} / {{ draft.device.batch || '未知' }}</el-descriptions-item>
            <el-descriptions-item label="设备位置">{{ draft.device.location || '未登记' }}</el-descriptions-item>
          </el-descriptions>

          <el-form label-position="top">
            <el-form-item v-if="!locked" label="外部工单号" :required="!hasOperation">
              <el-input v-model.trim="contractNumber" maxlength="64" placeholder="正式系统工单号" />
            </el-form-item>
            <el-form-item v-for="(label, key) in fieldNames" :key="key" :required="key !== 'faultCode'">
              <template #label>
                <div class="space-between w-full">
                  <span>{{ label }}</span>
                  <el-tag v-if="draft.origins[key] && draft.origins[key] !== '经办人填写'" size="small" type="info">{{ draft.origins[key] }}</el-tag>
                </div>
              </template>
              <el-input v-if="key === 'faultCode'" v-model="fields[key]" maxlength="120" :disabled="locked" />
              <el-input v-else v-model="fields[key]" type="textarea" :rows="3" maxlength="8000" :disabled="locked" />
            </el-form-item>
          </el-form>

          <div class="stack">
            <div class="space-between">
              <b>现场附件</b>
              <template v-if="!locked">
                <input ref="fileInput" type="file" accept="application/pdf,image/png,image/jpeg" style="display: none" @change="addFile" />
                <el-button size="small" :icon="Paperclip" :disabled="busy" @click="fileInput?.click()">添加附件</el-button>
              </template>
            </div>
            <div v-if="draft.attachments.length">
              <div v-for="file in draft.attachments" :key="file.id" class="list-item">
                <el-link type="primary" :href="'/api/v1/drafts/' + draft.id + '/attachments/' + file.id">{{ file.filename }}</el-link>
                <el-button v-if="!locked" text type="danger" :icon="Delete" aria-label="移除附件" :disabled="busy" @click="removeFile(file.id)" />
              </div>
            </div>
            <p v-else class="muted">PDF / PNG / JPEG ≤ 20 MiB</p>
          </div>

          <el-alert v-if="err && !confirmOpen" :title="err" type="error" show-icon :closable="false" />

          <div v-if="!locked" class="row-gap" style="justify-content: flex-end">
            <el-button :loading="busy" @click="save">暂存</el-button>
            <el-button type="primary" :icon="Select" :disabled="busy || (!hasOperation && contractNumber.length < 3)" @click="confirmOpen = true; err = ''">预览并确认</el-button>
          </div>
        </div>
      </el-card>

      <aside class="stack">
        <el-card shadow="never">
          <template #header><b>参考依据</b></template>
          <div class="stack">
            <template v-if="draft.references?.length">
              <div v-for="item in draft.references" :key="item.id" class="list-item" @click="selectedRef = item">
                <div class="list-main">
                  <b>{{ item.title }}</b>
                  <small>{{ item.position }} · V{{ item.version }}</small>
                </div>
                <el-icon><ArrowRight /></el-icon>
              </div>
            </template>
            <p v-else class="muted">暂无引用</p>
            <el-button v-if="draft.sessionAvailable" link type="primary" @click="router.push('/ask/' + draft.sessionId)">回到诊断会话 <el-icon><ArrowRight /></el-icon></el-button>
            <p v-else class="muted">来源对话已删除</p>
          </div>
        </el-card>

        <el-card shadow="never">
          <template #header><b>结单与导出</b></template>
          <div class="stack">
            <div class="row-gap">
              <el-button v-if="canExport" :icon="Download" :loading="busy" @click="download">导出确认稿</el-button>
              <el-button v-if="hasOperation && canExport && !locked" type="primary" :loading="busy" @click="submit">正式回写</el-button>
            </div>
            <div v-if="draft.externalNumber" class="row-gap">
              <el-icon color="var(--el-color-success)"><Check /></el-icon>
              <b>{{ draft.externalNumber }}</b>
            </div>
            <el-button v-if="draft.status === 'linked' && !draft.completedAt" type="primary" :loading="busy" @click="markDone">确认外部工单已结单</el-button>

            <div v-if="draft.caseProgress" class="stack">
              <div class="row-gap">
                <b>案例回流</b>
                <el-tag size="small" :type="draft.caseProgress.sourceId ? 'success' : 'info'">{{ caseLabel }}</el-tag>
              </div>
              <el-button v-if="draft.caseProgress.sourceId" link type="primary" @click="router.push({ path: '/library', query: { source: draft.caseProgress.sourceId } })">查看已发布案例 <el-icon><ArrowRight /></el-icon></el-button>
              <el-button v-else-if="draft.caseProgress.status === 'not_submitted' && can('case:feed')" link type="primary" @click="router.push('/expert')">前往整理案例 <el-icon><ArrowRight /></el-icon></el-button>
            </div>

            <div v-if="submission" class="stack">
              <p class="muted">回写请求 {{ submission.requestId }} · {{ statuses[submission.status] || submission.status }}</p>
              <el-button size="small" :loading="busy" @click="refresh">查询原请求</el-button>
            </div>

            <OperationHistory :events="draft.operationHistory" />
          </div>
        </el-card>
      </aside>
    </div>

    <el-dialog v-model="confirmOpen" title="确认现场处置" width="540px">
      <div v-if="draft" class="stack">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item v-if="contractNumber" label="外部工单号">{{ contractNumber }}</el-descriptions-item>
          <el-descriptions-item label="设备">{{ draft.device.number }} · {{ draft.device.name }}</el-descriptions-item>
          <el-descriptions-item v-for="(label, key) in fieldNames" :key="key" :label="label">{{ fields[key] || '未填写' }}</el-descriptions-item>
        </el-descriptions>
        <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" />
      </div>
      <template #footer>
        <el-button @click="confirmOpen = false">返回编辑</el-button>
        <el-button type="primary" :loading="busy" :icon="Check" @click="approve">确认现场记录</el-button>
      </template>
    </el-dialog>

    <el-drawer :model-value="!!selectedRef" size="520px" :title="selectedRef?.title || '工单引用'" @close="selectedRef = null">
      <div v-if="selectedRef" class="stack">
        <span class="muted">{{ selectedRef.position }} · V{{ selectedRef.version }}</span>
        <div class="doc-text">{{ selectedRef.text }}</div>
      </div>
    </el-drawer>
  </section>
</template>
