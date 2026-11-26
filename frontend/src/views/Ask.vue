<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowRight, ChatLineSquare, Cpu, Delete, DocumentAdd, Download,
  Link as LinkIcon, Loading, Operation, Plus, Position, Reading, User,
} from '@element-plus/icons-vue'
import MarkdownView from '../components/MarkdownView.vue'
import { api, can, categories, errorMessage, notice, request, store, type Row } from '../store'

const router = useRouter()
const route = useRoute()
const sessions = ref<Row[]>([])
const session = ref<Row | null>(null)
const deviceId = ref(store.devices[0]?.id || '')
const question = ref(String(route.query.q || ''))
const evidence = ref<Row | null>(null)
const note = ref('')
const escalateTurn = ref<Row | null>(null)
const feedback = ref(false)
const loading = ref(false)
const sending = ref(false)
const deleteTarget = ref<Row | null>(null)
const deleting = ref(false)
const openTraces = ref<Record<string, string[]>>({})
const removedIds = new Set<string>()
const bottom = ref<HTMLElement>()
let active = true
let change = 0
let streamAbort: AbortController | null = null

const pending = computed(() => session.value?.turns?.some((row: Row) => row.status === 'queued' || row.status === 'streaming'))
const historyRows = computed(() => sessions.value.filter(item => item.sequence))

function generateId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}

const safeScroll = () => nextTick(() => {
  const messages = bottom.value?.parentElement
  if (messages) messages.scrollTo({ top: messages.scrollHeight, behavior: 'smooth' })
})

async function history() {
  sessions.value = (await api<Row[]>('/sessions')).filter(row => !removedIds.has(row.id))
}

function resetSession() {
  ++change
  streamAbort?.abort()
  streamAbort = null
  session.value = null
  evidence.value = null
  escalateTurn.value = null
  loading.value = false
}

onMounted(() => history().catch(error => notice(errorMessage(error), true)))
onUnmounted(() => { active = false; streamAbort?.abort() })

watch(() => [route.params.id, route.query.q, route.query.device], async ([id, query, requestedDevice]) => {
  const generation = ++change
  streamAbort?.abort()
  evidence.value = null
  escalateTurn.value = null
  if (query) question.value = String(query)
  if (!id) {
    session.value = null
    loading.value = false
    const selected = store.devices.find(row => row.id === requestedDevice)
    if (selected) deviceId.value = selected.id
    return
  }
  if (session.value?.id === id && sending.value) return
  session.value = null
  loading.value = true
  try {
    const row = await api<Row>('/sessions/' + id)
    if (!active || generation !== change) return
    session.value = row
    deviceId.value = row.deviceId
    safeScroll()
  } catch (error) {
    if (generation === change) notice(errorMessage(error), true)
  } finally { if (generation === change) loading.value = false }
}, { immediate: true })

async function onDeviceChange(id: string) {
  if (sending.value) return
  const hadSession = !!session.value || !!route.params.id
  resetSession()
  deviceId.value = id
  if (hadSession) {
    if (route.fullPath !== '/ask') await router.push('/ask')
    notice('已切换设备，新的提问将创建诊断会话')
  } else if (route.query.device) {
    if (question.value === route.query.q) question.value = ''
    await router.replace('/ask')
  }
}

function useSuggestedDevice(item: Row, turn: Row) {
  const text = item.category === 'case' ? `请查找案例《${item.title}》`
    : item.category === 'workorder' ? `请查找历史工单《${item.title}》` : turn.text
  void router.push({ path: '/ask', query: { device: item.id, q: text } })
}

function formatArgs(args: Record<string, any> | undefined) {
  if (!args) return ''
  return Object.entries(args).filter(([, v]) => v !== '' && v !== undefined && v !== null)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' · ')
}

async function send() {
  const text = question.value.trim()
  if (!text || sending.value || loading.value || pending.value || !deviceId.value) return
  if (session.value && session.value.deviceId !== deviceId.value) resetSession()
  const generation = change
  const selectedDevice = deviceId.value
  sending.value = true
  try {
    let row = session.value
    if (!row) {
      row = await api<Row>('/sessions', 'POST', { deviceId: selectedDevice })
      if (!active || generation !== change) return
      session.value = { ...row, turns: [] }
    }
    const clientMessageId = generateId()
    const tempId = 'stream-' + clientMessageId
    const liveTurn: Row = {
      id: tempId,
      revision: 1,
      status: 'streaming',
      text,
      answer: '',
      reactSteps: [],
      engine: '',
      citations: [],
    }
    session.value!.turns = [...(session.value!.turns || []), liveTurn]
    openTraces.value[tempId] = ['trace']
    question.value = ''
    if (route.params.id !== row.id) await router.replace('/ask/' + row.id)
    safeScroll()

    streamAbort = new AbortController()
    const response = await fetch(`/api/v1/sessions/${row.id}/turns/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, clientMessageId }),
      signal: streamAbort.signal,
    })
    if (!response.ok || !response.body) {
      const body = await response.json().catch(() => null)
      throw new Error(body?.message || '流式问答启动失败')
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let currentEvent = ''

    const handleBlock = (block: string) => {
      let evName = ''
      const dataLines: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) evName = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) return
      currentEvent = evName || currentEvent
      const payload = JSON.parse(dataLines.join('\n'))
      const target = session.value?.turns?.find((t: Row) => t.id === liveTurn.id)
      if (!target) return

      if (currentEvent === 'start') {
        const oldId = target.id
        target.id = payload.turnId
        liveTurn.id = payload.turnId
        openTraces.value[payload.turnId] = openTraces.value[oldId] || ['trace']
      } else if (currentEvent === 'meta') {
        target.engine = payload.engine
      } else if (currentEvent === 'thought_delta') {
        const delta = payload.delta || ''
        if (delta) {
          const steps = target.reactSteps || []
          const thoughtId = payload.thoughtId
          const last = steps[steps.length - 1]
          if (last && last.phase === 'thought' && thoughtId && last.thoughtId === thoughtId) {
            last.content = (last.content || '') + delta
            last.streaming = true
          } else {
            for (const s of steps) s.streaming = false
            target.reactSteps = [
              ...steps,
              {
                step: payload.step || steps.length + 1,
                phase: 'thought',
                thoughtId,
                content: delta,
                streaming: true,
              },
            ]
          }
          safeScroll()
        }
      } else if (currentEvent === 'step') {
        for (const s of target.reactSteps || []) s.streaming = false
        target.reactSteps = [...(target.reactSteps || []), payload]
        safeScroll()
      } else if (currentEvent === 'token') {
        const delta = payload.delta || ''
        if (delta) {
          for (const s of target.reactSteps || []) s.streaming = false
          if (!target._collapsedOnAnswer) {
            target._collapsedOnAnswer = true
            openTraces.value[target.id] = []
          }
          target.answer = (target.answer || '') + delta
          safeScroll()
        }
      } else if (currentEvent === 'done' && payload.turn) {
        const idx = session.value!.turns.findIndex((t: Row) => t.id === liveTurn.id || t.id === payload.turn.id)
        const wasCollapsed = Boolean(target._collapsedOnAnswer)
        if (idx >= 0) session.value!.turns[idx] = payload.turn
        if (!wasCollapsed) openTraces.value[payload.turn.id] = []
        safeScroll()
      } else if (currentEvent === 'error') {
        notice(payload.message || '问答处理失败', true)
      }
    }

    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split('\n\n')
      buffer = parts.pop() || ''
      for (const part of parts) {
        if (part.trim()) handleBlock(part)
      }
    }
    if (buffer.trim()) handleBlock(buffer)
    void history().catch(() => undefined)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return
    if (active && generation === change) {
      notice(errorMessage(error), true)
      if (session.value?.turns) {
        session.value.turns = session.value.turns.filter((t: Row) => t.status !== 'streaming')
      }
    }
  } finally {
    streamAbort = null
    sending.value = false
  }
}

function originalLink(item: Row) {
  const page = /第 (\d+) 页/.exec(item.position)?.[1]
  return '/api/v1/sources/' + item.sourceId + '/file?deviceId=' + deviceId.value + '&chunkId=' + item.id
    + (page ? '#page=' + page : '')
}

async function newSession() {
  if (sending.value) return
  resetSession()
  question.value = ''
  if (route.path !== '/ask') await router.push('/ask')
  else if (route.query.q || route.query.device) await router.replace('/ask')
}

async function deleteSession() {
  const id = deleteTarget.value?.id
  if (!id || deleting.value) return
  deleting.value = true
  try {
    await request('/sessions/' + id, 'DELETE')
    removedIds.add(id)
    sessions.value = sessions.value.filter(row => row.id !== id)
    if (route.params.id === id || session.value?.id === id) {
      resetSession()
      await newSession()
    }
    deleteTarget.value = null
    notice('对话已删除')
  } catch (error) { notice(errorMessage(error), true) }
  finally { deleting.value = false }
}

async function createDraft() {
  if (!session.value) return
  try {
    const row = await api<Row>('/drafts', 'POST', { sessionId: session.value.id })
    await router.push('/drafts/' + row.id)
    notice('工单草稿已保存')
  } catch (error) { notice(errorMessage(error), true) }
}

async function escalate() {
  if (!escalateTurn.value || !session.value) return
  try {
    await api('/sessions/' + session.value.id + '/turns/' + escalateTurn.value.id + (feedback.value ? '/feedback' : '/escalate'), 'POST', { note: note.value })
    escalateTurn.value = null
    note.value = ''
    notice('已提交专家待办')
  } catch (error) { notice(errorMessage(error), true) }
}
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <h1>智能问答</h1>
      <el-button v-if="session?.turns?.length" :icon="DocumentAdd" @click="createDraft">生成工单草稿</el-button>
    </div>

    <div class="ask-layout">
      <el-card shadow="never" class="history-card">
        <el-button type="primary" plain :icon="Plus" :disabled="sending" class="w-full" @click="newSession">新建诊断</el-button>
        <div class="history-list" aria-label="诊断记录">
          <el-empty v-if="!historyRows.length" :image-size="48" description="暂无诊断记录" />
          <div v-for="row in historyRows" :key="row.id" :class="['history-entry', { current: session?.id === row.id }]">
            <RouterLink :to="'/ask/' + row.id" class="history-link">
              <b>{{ row.title }}</b>
              <small>{{ row.deviceNumber }}</small>
            </RouterLink>
            <el-button text type="danger" circle size="small" :aria-label="'删除对话 ' + row.title" :disabled="sending || deleting" @click="deleteTarget = row">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
      </el-card>

      <el-card shadow="never" class="chat-card">
        <template #header>
          <div class="space-between">
            <el-select
              :model-value="deviceId"
              filterable
              placeholder="选择设备"
              :disabled="sending"
              style="width: min(380px, 100%)"
              aria-label="选择设备"
              @update:model-value="onDeviceChange"
            >
              <el-option
                v-for="item in store.devices"
                :key="item.id"
                :value="item.id"
                :label="`${item.name}（${item.model}${item.batch ? ' / ' + item.batch : ''}）`"
              >
                <div class="space-between">
                  <span>{{ item.name }}</span>
                  <span class="muted">{{ item.number }} · {{ item.model }}{{ item.batch ? ' / ' + item.batch : '' }}</span>
                </div>
              </el-option>
            </el-select>
          </div>
        </template>

        <div class="chat-messages">
          <div v-if="loading" class="row-gap muted"><el-icon class="is-loading"><Loading /></el-icon> 正在恢复会话…</div>
          <el-empty v-else-if="!session?.turns?.length" class="chat-welcome" :image-size="72" :description="store.devices.length ? '输入故障码或现场现象' : '暂无设备'">
            <el-button v-if="!store.devices.length && can('device:manage')" type="primary" @click="router.push('/admin')">添加设备</el-button>
          </el-empty>

          <div v-else v-for="turn in session.turns" :key="turn.id" class="turn">
            <div class="user-bubble">{{ turn.text }}</div>
            <div class="assistant-block" aria-label="回答">
              <el-collapse
                v-if="turn.reactSteps?.length || (turn.status === 'streaming' && !turn.answer)"
                v-model="openTraces[turn.id]"
                class="react-collapse"
              >
                <el-collapse-item name="trace">
                  <template #title>
                    <div class="row-gap">
                      <el-icon :class="{ 'is-loading': turn.status === 'streaming' && !turn.answer }">
                        <Loading v-if="turn.status === 'streaming' && !turn.answer" />
                        <Operation v-else />
                      </el-icon>
                      <span>{{ turn.status === 'streaming' && !turn.answer ? '正在思考…' : '思考过程' }}{{ turn.reactSteps?.length ? `（${turn.reactSteps.length} 步）` : '' }}</span>
                      <el-tag v-if="turn.engine" size="small" :type="turn.engine === 'llm_react' ? 'success' : 'info'" effect="plain">
                        {{ turn.engine === 'llm_react' ? '大模型' : '本地检索' }}
                      </el-tag>
                    </div>
                  </template>
                  <el-timeline v-if="turn.reactSteps?.length" class="react-timeline">
                    <el-timeline-item
                      v-for="(step, idx) in turn.reactSteps || []"
                      :key="idx"
                      :type="step.phase === 'action' ? 'warning' : step.phase === 'observation' ? 'success' : 'primary'"
                    >
                      <div class="react-step">
                        <el-tag size="small" :type="step.phase === 'action' ? 'warning' : step.phase === 'observation' ? 'success' : 'primary'">
                          {{ step.phase === 'thought' ? '思考' : step.phase === 'action' ? `工具 · ${step.label || step.tool}` : '观察' }}
                        </el-tag>
                        <MarkdownView
                          v-if="step.phase === 'thought'"
                          :content="step.content || ''"
                          :streaming="Boolean(step.streaming)"
                          class="react-text"
                        />
                        <span v-else-if="step.phase === 'action'" class="react-text muted">{{ formatArgs(step.args) }}</span>
                        <span v-else class="react-text">{{ step.summary }}</span>
                      </div>
                    </el-timeline-item>
                  </el-timeline>
                </el-collapse-item>
              </el-collapse>

              <div v-if="turn.answer || turn.status === 'streaming'" aria-live="polite">
                <MarkdownView
                  :content="turn.answer || ''"
                  :streaming="turn.status === 'streaming'"
                  :cite-count="turn.citations?.length || 0"
                  @cite="idx => { const item = turn.citations?.[idx - 1]; if (item) evidence = item }"
                />
              </div>

              <template v-if="turn.status === 'done'">
                <div v-if="turn.citations?.length" class="citation-box">
                  <div class="citation-title"><el-icon><Reading /></el-icon>{{ turn.decision === 'evidence' ? '相关资料' : '资料出处' }}</div>
                  <button v-for="(item, index) in turn.citations" :key="item.id" class="citation-item" @click="evidence = item">
                    <el-tag size="small" effect="plain">{{ index + 1 }}</el-tag>
                    <span><b>{{ item.title }}</b><small>{{ categories[item.category] }} · {{ item.position }} · V{{ item.version }}</small></span>
                    <el-icon><ArrowRight /></el-icon>
                  </button>
                </div>
                <div v-if="turn.deviceSuggestions?.length" class="citation-box">
                  <div class="citation-title"><el-icon><Cpu /></el-icon>其他设备有匹配资料</div>
                  <button v-for="item in turn.deviceSuggestions" :key="item.id" class="citation-item" @click="useSuggestedDevice(item, turn)">
                    <el-icon><Cpu /></el-icon>
                    <span><b>{{ item.name }}</b><small>{{ item.model }}{{ item.batch ? ' · 批次 ' + item.batch : '' }} · {{ categories[item.category] }} · {{ item.title }}</small></span>
                    <el-icon><ArrowRight /></el-icon>
                  </button>
                </div>
                <div class="row-gap">
                  <el-button link type="primary" :icon="User" @click="feedback = false; escalateTurn = turn">转交专家</el-button>
                  <el-button link type="primary" :icon="ChatLineSquare" @click="feedback = true; escalateTurn = turn">反馈答复</el-button>
                </div>
              </template>
            </div>
          </div>
          <div ref="bottom"></div>
        </div>

        <form class="chat-composer" @submit.prevent="send">
          <el-input
            v-model="question"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 5 }"
            maxlength="3000"
            placeholder="输入故障码或现象…"
            aria-label="维修问题"
            @keydown.enter.exact.prevent="send"
          />
          <el-button
            type="primary"
            native-type="submit"
            :icon="Position"
            :loading="sending"
            :disabled="!store.devices.length || sending || loading || pending || !question.trim()"
            aria-label="发送问题"
          >发送</el-button>
        </form>
      </el-card>
    </div>

    <el-drawer :model-value="!!evidence" size="520px" :title="evidence?.title || '资料出处'" @close="evidence = null">
      <div v-if="evidence" class="stack">
        <div class="row-gap">
          <el-tag size="small">{{ categories[evidence.category] }}</el-tag>
          <el-tag size="small" type="info">V{{ evidence.version }}</el-tag>
          <span class="muted">{{ evidence.number }} · {{ evidence.position }}</span>
        </div>
        <div class="doc-text">{{ evidence.text }}</div>
        <div class="row-gap">
          <el-button v-if="evidence.referenceUrl" tag="a" :href="evidence.referenceUrl" target="_blank" rel="noopener noreferrer" :icon="LinkIcon">查看发布来源</el-button>
          <el-button v-if="!evidence.demo" tag="a" :href="originalLink(evidence)" target="_blank" rel="noopener" :icon="Download">查看入库文件</el-button>
        </div>
      </div>
    </el-drawer>

    <el-dialog :model-value="!!deleteTarget" title="删除对话" width="420px" @close="!deleting && (deleteTarget = null)">
      <p>删除「{{ deleteTarget?.title }}」？关联工单和专家待办不受影响。</p>
      <template #footer>
        <el-button :disabled="deleting" @click="deleteTarget = null">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="deleteSession">删除对话</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="!!escalateTurn" :title="feedback ? '反馈答复或引用' : '转交班组专家'" width="480px" @close="escalateTurn = null">
      <el-form label-position="top" @submit.prevent="escalate">
        <p class="mb-16">{{ escalateTurn?.text }}</p>
        <el-form-item label="补充说明" :required="feedback">
          <el-input v-model="note" type="textarea" :rows="4" maxlength="1500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="escalateTurn = null">取消</el-button>
        <el-button type="primary" :disabled="feedback && !note.trim()" @click="escalate">提交专家待办</el-button>
      </template>
    </el-dialog>
  </section>
</template>
