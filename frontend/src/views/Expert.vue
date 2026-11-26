<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight } from '@element-plus/icons-vue'
import MarkdownView from '../components/MarkdownView.vue'
import OperationHistory from '../components/OperationHistory.vue'
import SourceDetail from '../components/SourceDetail.vue'
import { api, can, date, errorMessage, notice, statuses, statusTag, store, type Row } from '../store'

const router = useRouter()
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

const expert = computed(() => can('expert:task'))
const manage = computed(() => can('case:feed'))

async function load() {
  try { tasks.value = await api<Row[]>('/tasks') }
  catch (e) { notice(errorMessage(e), true) }
}
onMounted(load)
onUnmounted(() => { active = false; clearTimeout(caseTimer) })

function onTabChange(name: string | number) {
  if (name === 'cases') void loadCases()
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
  } catch (e) { caseError.value = errorMessage(e) }
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
    ...(row.fields.faultCode ? ['故障码：' + row.fields.faultCode] : []),
    '现象：' + row.fields.symptom, '确认原因：' + row.fields.cause,
    '实际处置：' + row.fields.actions, '结果：' + row.fields.result,
  ].join('\n')
  error.value = ''
}

async function action(actionName: string) {
  if (!selected.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const updated = await api<Row>('/tasks/' + selected.value.id + '/actions', 'POST', {
      revision: selected.value.revision, action: actionName, note: note.value, assignee: assignee.value,
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
    tab.value = 'cases'
    await loadCases()
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <h1>专家协作</h1>
      <el-button @click="router.push('/ask')">返回诊断 <el-icon class="el-icon--right"><ArrowRight /></el-icon></el-button>
    </div>

    <el-card shadow="never">
      <el-tabs v-if="manage" v-model="tab" @tab-change="onTabChange">
        <el-tab-pane label="专家待办" name="tasks" />
        <el-tab-pane label="案例回流" name="cases" />
      </el-tabs>

      <Transition name="tab-slide" mode="out-in">
        <div v-if="tab === 'tasks'" key="tasks">
          <el-table v-if="tasks.length" :data="tasks" style="cursor: pointer" @row-click="open">
            <el-table-column prop="title" label="问题" min-width="260" show-overflow-tooltip />
            <el-table-column prop="deviceName" label="设备" min-width="160" show-overflow-tooltip />
            <el-table-column label="更新时间" width="140">
              <template #default="{ row }">{{ date(row.updated, true) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="160" align="right">
              <template #default="{ row }">
                <div class="row-gap" style="justify-content: flex-end">
                  <el-tag v-if="row.overdue" size="small" type="danger">已逾期</el-tag>
                  <el-tag size="small" :type="statusTag(row.status)">{{ statuses[row.status] }}</el-tag>
                </div>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无专家待办">
            <el-button type="primary" plain @click="router.push('/ask')">前往问答</el-button>
          </el-empty>
        </div>

        <div v-else key="cases">
          <div v-if="caseLoading && !completed.length" class="muted" role="status">正在读取已结单记录…</div>
          <el-alert v-else-if="caseError" :title="caseError" type="error" show-icon :closable="false">
            <el-button size="small" @click="loadCases">重新读取</el-button>
          </el-alert>
          <el-table v-else-if="completed.length" :data="completed" style="cursor: pointer" @row-click="openCase">
            <el-table-column label="故障现象" min-width="260" show-overflow-tooltip>
              <template #default="{ row }">{{ row.fields.symptom }}</template>
            </el-table-column>
            <el-table-column prop="externalNumber" label="外部单号" width="160" />
            <el-table-column label="设备型号" width="160">
              <template #default="{ row }">{{ row.device.model }}</template>
            </el-table-column>
            <el-table-column label="案例状态" width="150" align="right">
              <template #default="{ row }">
                <el-tag size="small" :type="row.caseSource ? statusTag(row.caseSource.status, row.caseSource.active) : 'info'">
                  {{ row.caseSource ? (row.caseSource.active ? '案例已生效' : (row.caseSource.status === 'published' ? '未生效' : statuses[row.caseSource.status])) : '整理案例' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无已结单记录" />
        </div>
      </Transition>
    </el-card>

    <SourceDetail v-if="caseSourceId" :key="caseSourceId" :source-id="caseSourceId" @close="caseSourceId = null" @updated="loadCases" />

    <el-drawer :model-value="!!selected" size="560px" :title="selected?.title || '专家待办'" @close="selected = null">
      <div v-if="selected" class="stack">
        <div class="row-gap">
          <el-tag size="small" :type="statusTag(selected.status)">{{ statuses[selected.status] }}</el-tag>
          <span class="muted">{{ selected.deviceName }}</span>
          <span v-if="selected.assignee" class="muted">· 经办专家：{{ selected.assignee }}</span>
        </div>
        <div v-if="selected.note" class="doc-text">{{ selected.note }}</div>
        <MarkdownView v-if="selected.turn?.answer" :content="selected.turn.answer" />
        <el-collapse v-if="selected.turn?.citations?.length">
          <el-collapse-item v-for="item in selected.turn.citations" :key="item.id" :title="`${item.title} · ${item.position}`" :name="item.id">
            <div class="doc-text">{{ item.text }}</div>
          </el-collapse-item>
        </el-collapse>
        <el-card v-if="selected.reply" shadow="never">
          <template #header><b>专家答复</b></template>
          <MarkdownView :content="selected.reply" />
        </el-card>

        <el-button v-if="expert && selected.status === 'open'" type="primary" :loading="busy" @click="action('claim')">领取待办</el-button>

        <el-form v-if="expert && ['open','assigned'].includes(selected.status) && (!selected.assignee || selected.assignee === store.profile?.username)" label-position="top" @submit.prevent="action('assign')">
          <el-form-item label="分派给其他专家" required>
            <div class="row-gap w-full">
              <el-input v-model="assignee" placeholder="组织内专家账号" style="flex: 1" />
              <el-button native-type="submit" :loading="busy" :disabled="!assignee.trim()">确认分派</el-button>
            </div>
          </el-form-item>
        </el-form>

        <div v-if="expert && selected.assignee === store.profile?.username && selected.status !== 'open'" class="stack">
          <el-input v-model="note" type="textarea" :rows="5" maxlength="4000" placeholder="答复或知识修订内容" />
          <div class="row-gap">
            <el-button v-if="['assigned','answered'].includes(selected.status)" type="primary" :loading="busy" :disabled="!note.trim()" @click="action('reply')">保存答复</el-button>
            <el-button v-if="selected.status === 'answered'" :loading="busy" @click="action('close')">关闭待办</el-button>
            <el-button v-if="['answered','closed'].includes(selected.status) && !selected.revisionCandidate" :loading="busy" :disabled="!note.trim()" @click="action('revise')">提交知识修订</el-button>
          </div>
        </div>

        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
        <OperationHistory :events="selected.operationHistory" />
      </div>
    </el-drawer>

    <el-dialog :model-value="!!record" :title="record ? `整理回流案例 · ${record.externalNumber}` : '整理回流案例'" width="600px" @close="record = null">
      <el-form label-position="top" @submit.prevent="sendCase">
        <el-form-item label="脱敏案例（不含姓名与联系方式）" required>
          <el-input v-model="note" type="textarea" :rows="10" maxlength="16000" />
        </el-form-item>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
        <OperationHistory v-if="record" :events="record.operationHistory" />
      </el-form>
      <template #footer>
        <el-button @click="record = null">取消</el-button>
        <el-button type="primary" :loading="busy" :disabled="note.trim().length < 20" @click="sendCase">送知识库校验</el-button>
      </template>
    </el-dialog>
  </section>
</template>
