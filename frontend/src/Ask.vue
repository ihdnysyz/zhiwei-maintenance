<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ArrowDownToLine, ArrowLeft, ArrowRight, ArrowUp, ArrowUpRight, BookOpen, ChevronDown, ChevronRight, CircleAlert, ClipboardPlus, Cpu, History, LoaderCircle, Plus, Search, Sparkles, Trash2, Users, X } from 'lucide-vue-next'
import { useRoute, useRouter } from 'vue-router'
import { api, categories, errorMessage, notice, request, store, type Row } from './store'

const router = useRouter()
const route = useRoute()
const sessions = ref<Row[]>([])
const session = ref<Row | null>(null)
const deviceId = ref((store.devices.find(row => row.demo) || store.devices[0])?.id || '')
const question = ref(String(route.query.q || ''))
const evidence = ref<Row | null>(null)
const note = ref('')
const escalateTurn = ref<Row | null>(null)
const feedback = ref(false)
const deviceSearch = ref('')
const pickDevice = ref(false)
const showHistory = ref(false)
const loading = ref(false)
const sending = ref(false)
const deleteTarget = ref<Row | null>(null)
const deleting = ref(false)
const removedIds = new Set<string>()
const composer = ref<HTMLTextAreaElement>()
const bottom = ref<HTMLElement>()
let timer: number | undefined
let active = true
let change = 0
let retryKey: { sessionId: string; text: string; id: string } | undefined
const pending = computed(() => session.value?.turns?.some((row: Row) => row.status === 'queued'))
const currentDevice = computed(() => store.devices.find(row => row.id === deviceId.value))
const defaultPrompts = computed(() => [
  currentDevice.value?.demo ? 'E07 温度告警如何核对？' : '如何核对设备的故障码？',
  '设备出现振动与异响，有类似案例吗？',
  '有哪些适用于这台设备的维修手册？',
])
const matchedDevices = computed(() => {
  const words = deviceSearch.value.normalize('NFKC').toLowerCase().trim().split(/\s+/)
  return store.devices.filter(row => {
    const description = [row.name, row.model, row.number, row.batch, row.location].join(' ').normalize('NFKC').toLowerCase()
    return words.every(word => description.includes(word))
  })
})
const safeScroll = () => nextTick(() => bottom.value?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }))
async function history() { sessions.value = (await api<Row[]>('/sessions')).filter(row => !removedIds.has(row.id)) }
onMounted(() => history().catch(error => notice(errorMessage(error), true)))
onUnmounted(() => { active = false; clearTimeout(timer) })
watch(() => [route.params.id, route.query.q, route.query.device], async ([id, query, requestedDevice]) => {
  const generation = ++change
  clearTimeout(timer)
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
  session.value = null
  loading.value = true
  try {
    const row = await api<Row>('/sessions/' + id)
    if (!active || generation !== change) return
    session.value = row
    deviceId.value = row.deviceId
    poll()
    safeScroll()
  } catch (error) {
    if (generation === change) notice(errorMessage(error), true)
  } finally { if (generation === change) loading.value = false }
}, { immediate: true })
function poll() {
  clearTimeout(timer)
  if (!active || !session.value || !pending.value) return
  const id = session.value.id
  const turn = session.value.turns.find((item: Row) => item.status === 'queued')
  timer = window.setTimeout(async () => {
    try {
      const result = await api<Row>('/sessions/' + id + '/turns/' + turn.id)
      if (!active || session.value?.id !== id) return
      session.value.turns = session.value.turns.map((row: Row) => row.id === result.id ? result : row)
      if (result.status !== 'queued') safeScroll()
      else poll()
    } catch (error) {
      if (!active || session.value?.id !== id) return
      if (errorMessage(error) === '此对话已删除') {
        removedIds.add(id)
        sessions.value = sessions.value.filter(row => row.id !== id)
        void newSession()
      } else poll()
    }
  }, 850)
}
async function selectDevice(row: Row) {
  pickDevice.value = false
  if (deviceId.value === row.id) return
  deviceId.value = row.id
  evidence.value = null
  if (session.value) {
    await router.push('/ask')
    notice('已切换设备，新的提问将创建诊断会话')
  } else if (route.query.device) {
    if (question.value === route.query.q) question.value = ''
    await router.replace('/ask')
  }
}
function useSuggestedDevice(item: Row, turn: Row) {
  const text = item.category === 'case' ? `请查找案例《${item.title}》`
    : item.category === 'workorder' ? `请查找历史工单《${item.title}》` : turn.text
  void router.push({ path: '/ask', query: { device: item.id, q: text } }).then(() => composer.value?.focus())
}
async function send() {
  const text = question.value.trim()
  if (!text || sending.value || loading.value || pending.value) return
  if (!deviceId.value) { pickDevice.value = true; return }
  sending.value = true
  try {
    let row = session.value
    if (!row) {
      row = await api<Row>('/sessions', 'POST', { deviceId: deviceId.value })
      session.value = { ...row, turns: [] }
    }
    if (!retryKey || retryKey.sessionId !== row.id || retryKey.text !== text) {
      retryKey = { sessionId: row.id, text, id: crypto.randomUUID() }
    }
    const response = await api<Row>('/sessions/' + row.id + '/turns', 'POST', { text, clientMessageId: retryKey.id })
    retryKey = undefined
    question.value = ''
    if (route.params.id !== row.id) await router.replace('/ask/' + row.id)
    else if (session.value?.id === row.id) {
      session.value.turns = [...session.value.turns, response]
      poll()
    }
    void history().catch(() => undefined)
    safeScroll()
  } catch (error) { notice(errorMessage(error), true) }
  finally { sending.value = false }
}
function fill(value: string) { question.value = value; composer.value?.focus() }
function originalLink(item: Row) {
  const page = /第 (\d+) 页/.exec(item.position)?.[1]
  return '/api/v1/sources/' + item.sourceId + '/file?deviceId=' + deviceId.value + '&chunkId=' + item.id
    + (page ? '#page=' + page : '')
}
async function newSession() {
  question.value = ''
  showHistory.value = false
  if (route.path !== '/ask') await router.push('/ask')
  else if (route.query.q || route.query.device) await router.replace('/ask')
  composer.value?.focus()
}
function closeDelete() {
  if (!deleting.value) deleteTarget.value = null
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
      clearTimeout(timer)
      session.value = null
      retryKey = undefined
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
    notice('已提交专家待办，可在专家协作中查看答复')
  } catch (error) { notice(errorMessage(error), true) }
}
</script>

<template>
  <main class="page">
    <div class="page-heading"><div><h1>智能问答</h1><p>描述现场现象，核对适用的手册和历史案例。</p></div><button v-if="session?.turns?.length" class="btn secondary" @click="createDraft"><ClipboardPlus :size="18"/> 生成工单草稿</button></div>
    <div class="ask-layout">
      <aside :class="['card', 'history-panel', { 'history-visible': showHistory }]">
        <button class="btn secondary new-chat" @click="newSession"><Plus :size="18"/> 新建诊断</button>
        <h2><History :size="15"/> 诊断记录</h2>
        <p v-if="!sessions.some(row => row.sequence)" class="muted history-empty">暂无诊断记录</p>
        <div v-for="row in sessions.filter(item => item.sequence)" :key="row.id" class="history-entry">
          <RouterLink :to="'/ask/' + row.id" :class="{ current: session?.id === row.id }" @click="showHistory = false"><b>{{ row.title }}</b><small>{{ row.deviceNumber }}</small></RouterLink>
          <button class="icon-btn delete-conversation" :aria-label="'删除对话 ' + row.title" title="删除对话" :disabled="sending || deleting" @click="deleteTarget = row"><Trash2 :size="16"/></button>
        </div>
      </aside>
      <section class="card chat-card">
        <div class="chat-head">
          <button class="icon-btn mobile-history" aria-label="诊断记录" @click="showHistory = !showHistory"><History :size="20"/></button>
          <div class="device-select">
            <button class="btn device-btn" :aria-expanded="pickDevice" @click="pickDevice = !pickDevice">
              <Cpu :size="18"/>
              <span v-if="currentDevice" class="device-label">
                <b>{{ currentDevice.name }}</b>
                <small class="chat-model">{{ currentDevice.model }}{{ currentDevice.batch ? ' · 批次 ' + currentDevice.batch : '' }}{{ currentDevice.demo ? ' · 演示设备' : '' }}</small>
              </span>
              <span v-else>选择设备</span><ChevronDown :size="15"/>
            </button>
            <div v-if="pickDevice" class="device-options">
              <label class="search-field"><Search :size="16"/><input v-model="deviceSearch" placeholder="搜索名称、型号、批次或位置" aria-label="搜索设备"/></label>
              <button v-for="item in matchedDevices" :key="item.id" :class="{ selected: item.id === deviceId }" @click="selectDevice(item)">
                <b>{{ item.name }}{{ item.id === deviceId ? ' · 当前' : '' }}</b>
                <small>{{ item.number }} · {{ item.model }}{{ item.batch ? ' · 批次 ' + item.batch : '' }}</small>
              </button>
              <p v-if="!matchedDevices.length" class="muted">没有匹配的设备。请联系管理员维护台账。</p>
            </div>
          </div>
          <button class="icon-btn mobile-history" aria-label="新建诊断" @click="newSession"><Plus :size="20"/></button>
        </div>
        <div class="chat-messages">
          <div v-if="loading" class="loading"><LoaderCircle :size="20" /> 正在恢复会话…</div>
          <div v-else-if="!session?.turns?.length" class="chat-welcome">
            <div class="welcome-mark"><Sparkles :size="29"/></div><h2>今天要排查什么问题？</h2>
            <p>输入故障码或描述现场现象，问答会附上可核查的依据。</p>
            <div class="suggestions"><button v-for="(prompt, index) in defaultPrompts" :key="prompt" @click="fill(prompt)">{{ currentDevice?.demo && index === 0 ? '演示样例 · ' : '' }}{{ prompt }}<ArrowRight :size="16"/></button></div>
          </div>
          <div v-else v-for="turn in session.turns" :key="turn.id" class="turn">
            <div class="user-message">{{ turn.text }}</div>
            <div class="assistant-message">
              <div class="answer-title"><span class="tile-icon mint compact"><Sparkles :size="17"/></span><strong>知维助手</strong></div>
              <p v-if="turn.status === 'queued'" class="loading"><LoaderCircle :size="17"/> 正在查找适用资料…</p>
              <template v-else>
                <p class="answer-body">{{ turn.answer }}</p>
                <div v-if="turn.citations?.length" class="citations">
                  <h3><BookOpen :size="16"/> {{ turn.decision === 'evidence' ? '相关资料' : '资料出处' }}</h3>
                  <button v-for="(item, index) in turn.citations" :key="item.id" @click="evidence = item"><span class="ref-index">{{ index + 1 }}</span><span><b>{{ item.title }}</b><small>{{ item.demo ? '演示样例 · ' : '' }}{{ categories[item.category] }} · {{ item.position }} · V{{ item.version }}</small></span><ChevronRight :size="17"/></button>
                </div>
                <div v-if="turn.deviceSuggestions?.length" class="citations">
                  <h3><Cpu :size="16"/> 其他设备有匹配资料</h3>
                  <button v-for="item in turn.deviceSuggestions" :key="item.id" @click="useSuggestedDevice(item, turn)">
                    <Cpu :size="17"/><span><b>{{ item.name }}</b>
                      <small>{{ item.model }}{{ item.batch ? ' · 批次 ' + item.batch : '' }}</small>
                      <small>{{ categories[item.category] }} · {{ item.title }}</small></span><ArrowRight :size="17"/>
                  </button>
                </div>
                <div class="tag-row"><button class="text-link escalate" @click="feedback = false; escalateTurn = turn"><Users :size="16"/> 转交专家</button><button class="text-link escalate" @click="feedback = true; escalateTurn = turn">反馈答复</button></div>
              </template>
            </div>
          </div>
          <div ref="bottom"></div>
        </div>
        <form class="composer" @submit.prevent="send">
          <textarea ref="composer" v-model="question" placeholder="描述现场情况，或输入故障码…" aria-label="维修问题" maxlength="3000" rows="2" @keydown.enter.exact.prevent="send"></textarea>
          <button class="send-btn" type="submit" aria-label="发送问题" :disabled="sending || loading || pending || !question.trim()"><LoaderCircle v-if="sending" :size="19" class="spin"/><ArrowUp v-else :size="20"/></button>
        </form>
        <p class="composer-note"><CircleAlert :size="13"/> 处置前请核对手册版本和设备适用范围。</p>
      </section>
    </div>
    <dialog v-if="evidence" open class="sheet" aria-label="资料出处" @keydown.esc="evidence = null">
      <div class="sheet-head"><div><p class="eyebrow">{{ evidence.demo ? '演示样例 · ' : '' }}{{ categories[evidence.category] }} · V{{ evidence.version }}</p><h2>{{ evidence.title }}</h2></div><button class="icon-btn" aria-label="关闭出处" @click="evidence = null"><X :size="22"/></button></div>
      <div class="sheet-body"><p class="muted">{{ evidence.number }} · {{ evidence.position }}</p><div class="document-text">{{ evidence.text }}</div>
        <div class="tag-row">
          <a v-if="evidence.referenceUrl" class="btn secondary" :href="evidence.referenceUrl" target="_blank" rel="noopener noreferrer"><ArrowUpRight :size="17"/> 查看发布来源</a>
          <a v-if="!evidence.demo" class="btn secondary" :href="originalLink(evidence)" target="_blank" rel="noopener"><ArrowDownToLine :size="17"/> 查看入库文件</a>
        </div>
      </div>
    </dialog>
    <div v-if="evidence || escalateTurn || deleteTarget" class="backdrop" @click="evidence = null; escalateTurn = null; closeDelete()"></div>
    <dialog v-if="deleteTarget" open class="modal" aria-label="删除对话" @keydown.esc.prevent="closeDelete">
      <div class="sheet-head"><h2>删除这条对话？</h2><button class="icon-btn" aria-label="关闭" :disabled="deleting" @click="closeDelete"><X :size="22"/></button></div>
      <div class="sheet-body form-stack">
        <p>「{{ deleteTarget.title }}」将从诊断记录中删除。已提交到工单和专家待办的内容仍可在原业务记录中查看。</p>
        <div class="button-row"><button class="btn secondary" :disabled="deleting" @click="closeDelete">取消</button><button class="btn destructive" :disabled="deleting" @click="deleteSession">{{ deleting ? '正在删除…' : '删除对话' }}</button></div>
      </div>
    </dialog>
    <dialog v-if="escalateTurn" open class="modal" aria-label="转交班组专家">
      <div class="sheet-head"><h2>{{ feedback ? '反馈答复或引用' : '转交班组专家' }}</h2><button class="icon-btn" aria-label="取消" @click="escalateTurn = null"><X :size="22"/></button></div>
      <form class="sheet-body form-stack" @submit.prevent="escalate"><p>{{ escalateTurn.text }}</p><label>补充说明<textarea v-model="note" rows="4" maxlength="1500" :required="feedback" placeholder="补充现场条件或需要核实的内容"></textarea></label><div class="button-row"><button class="btn primary">提交专家待办</button></div></form>
    </dialog>
  </main>
</template>

