<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ArrowDownToLine, ArrowUpRight, BookOpen, ClipboardList, CloudUpload, FileSearch, FileText, History, Plus, RefreshCw, Search, Wrench, X } from 'lucide-vue-next'
import { api, categories, date, errorMessage, notice, statuses, store, type Row } from './store'

interface UploadItem { file: File; title: string; number: string; version: string; model: string; batch: string; category: string; referenceUrl: string }
const sources = ref<Row[]>([])
const batches = ref<Row[]>([])
const detail = ref<Row | null>(null)
const conflict = ref<Row | null>(null)
const conflictVersion = ref('')
const correctionId = ref('')
const correctedText = ref('')
const type = ref('')
const term = ref('')
const filterStatus = ref('')
const deviceId = ref(store.devices[0]?.id || '')
const searchResult = ref<Row[] | null>(null)
const ready = ref(false)
const openUpload = ref(false)
const showBatches = ref(false)
const verified = ref(false)
const allowExternalModel = ref(false)
const actionNote = ref('')
const startAt = ref('')
const endAt = ref('')
const schedule = ref(false)
const blockBatch = ref('')
const upload = ref<UploadItem[]>([])
const working = ref(false)
const errorText = ref('')
const manager = computed(() => store.profile?.role === 'library')
const expert = computed(() => store.profile?.role === 'expert')
const models = computed(() => [...new Set(store.devices.map(row => row.model))])
const filtered = computed(() => sources.value.filter(source =>
  (!type.value || source.category === type.value) && (!filterStatus.value || source.status === filterStatus.value)
  && (searchResult.value !== null || !term.value || [source.title, source.number, source.model, source.batch].join(' ').toLowerCase().includes(term.value.toLowerCase()))))
const categoryIcons = { manual: BookOpen, workorder: ClipboardList, case: Wrench }
let timer: number | undefined
async function fetchData() {
  try {
    sources.value = await api<Row[]>('/sources')
    if (manager.value) batches.value = await api<Row[]>('/imports')
  } catch (error) { notice(errorMessage(error), true) }
  finally { ready.value = true }
}
onMounted(() => { void fetchData(); timer = window.setInterval(() => { if (showBatches.value) void fetchData() }, 2500) })
onUnmounted(() => clearInterval(timer))
async function search() {
  if (!term.value.trim()) { searchResult.value = null; return }
  if (!deviceId.value) { searchResult.value = null; return }
  try { searchResult.value = await api<Row[]>('/knowledge/search?' + new URLSearchParams({ deviceId: deviceId.value, q: term.value.trim() })) }
  catch (error) { notice(errorMessage(error), true) }
}
async function view(row: Row) {
  verified.value = false
  allowExternalModel.value = false
  correctionId.value = ''
  actionNote.value = ''
  startAt.value = ''; endAt.value = ''; schedule.value = false; blockBatch.value = ''; errorText.value = ''
  const viewDeviceId = row.applicableDeviceId || deviceId.value
  try {
    const source = await api<Row>('/sources/' + row.id + '?' + new URLSearchParams({ deviceId: viewDeviceId }))
    detail.value = { ...source, viewDeviceId }
  }
  catch (error) { notice(errorMessage(error), true) }
}
async function saveCorrection(id: string) {
  if (!detail.value || working.value) return
  working.value = true
  errorText.value = ''
  try {
    const row = await api<Row>('/sources/' + detail.value.id + '/chunks/' + id, 'PATCH', {
      revision: detail.value.revision, text: correctedText.value,
    })
    await view(row)
    notice('校核片段已保存')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}
function attach(ev: Event) {
  const input = ev.target as HTMLInputElement
  for (const file of Array.from(input.files || []).slice(0, 30 - upload.value.length)) {
    const title = file.name.replace(/\.[^.]+$/, '')
    upload.value.push({ file, title, number: title.replace(/[:\s]/g, '-').slice(0, 90), version: '1.0', model: models.value[0] || '', batch: '', category: 'manual', referenceUrl: '' })
  }
  input.value = ''
}
async function importFiles() {
  if (!upload.value.length || working.value) return
  working.value = true
  errorText.value = ''
  try {
    const form = new FormData()
    for (const entry of upload.value) form.append('files', entry.file)
    form.append('metadata', JSON.stringify(upload.value.map(({ file, referenceUrl, ...info }) =>
      ({ ...info, referenceUrl: referenceUrl.trim() || undefined }))))
    await api('/sources/import', 'POST', form)
    upload.value = []
    openUpload.value = false
    showBatches.value = true
    await fetchData()
    notice('导入批次已登记，文件将在后台解析')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}
async function retry(id: string) {
  try { await api('/imports/' + id + '/retry', 'POST'); await fetchData(); notice('已安排重试失败项') }
  catch (error) { notice(errorMessage(error), true) }
}
async function resolveConflict(action: string) {
  if (!conflict.value || working.value) return
  working.value = true
  try {
    await api('/conflicts/' + conflict.value.sourceId + '/resolve', 'POST', {
      action, revision: conflict.value.revision, version: action === 'new_version' ? conflictVersion.value : '',
    })
    conflict.value = null
    await fetchData()
    notice('版本冲突已处理')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}
async function action(name: string) {
  if (!detail.value || working.value) return
  working.value = true
  errorText.value = ''
  try {
    const convert = (value: string) => schedule.value && value ? new Date(value).toISOString() : ''
    const row = await api<Row>('/sources/' + detail.value.id + '/actions', 'POST', {
      revision: detail.value.revision, action: name, verified: verified.value, note: actionNote.value,
      start: convert(startAt.value), end: convert(endAt.value),
      ...(name === 'block' && blockBatch.value.trim() ? { batch: blockBatch.value.trim() } : {}),
      ...(name === 'approve' && allowExternalModel.value ? { allowExternalModel: true } : {}),
    })
    await fetchData()
    await view(row)
    notice(statuses[row.status] || '资料已更新')
  } catch (error) { errorText.value = errorMessage(error) }
  finally { working.value = false }
}
const filename = (row: Row) => row.filename || row.number
function closeDetail() { detail.value = null; errorText.value = '' }
</script>

<template>
  <main class="page">
    <div class="page-heading"><div><h1>知识库</h1><p>查阅维修手册、历史工单和故障案例。</p></div><div v-if="manager" class="button-row"><button class="btn secondary" @click="showBatches = !showBatches"><History :size="17"/> {{ showBatches ? '返回资料' : '导入记录' }}</button><button class="btn primary" @click="openUpload = true; errorText = ''"><Plus :size="18"/> 批量导入</button></div></div>
    <section v-if="showBatches" class="card content-card">
      <div class="section-head"><div><h2>导入记录</h2><p>查看解析结果，或按批次重试失败文件。</p></div><button class="btn secondary" @click="fetchData"><RefreshCw :size="16"/> 刷新</button></div>
      <div v-if="!batches.length" class="empty"><CloudUpload :size="32"/><b>还没有导入批次</b></div>
      <article v-for="batch in batches" :key="batch.id" class="batch">
        <div class="section-head"><div><b>批次 {{ batch.id.slice(0, 8) }}</b><p>{{ date(batch.created, true) }} · {{ batch.items.length }} 份资料</p></div><button v-if="batch.items.some((item: Row) => item.status === 'failed' && item.sourceId)" class="btn secondary" @click="retry(batch.id)">重试失败项</button></div>
        <div v-for="(item, index) in batch.items" :key="index" class="batch-row"><FileText :size="17"/><span>{{ item.filename }}</span><small v-if="item.error" class="error">{{ item.error }}</small><span class="pill">{{ statuses[item.status] || item.status }}</span><button v-if="item.status === 'conflict'" class="btn secondary" @click="conflict = item; conflictVersion = ''; errorText = ''">处理冲突</button></div>
      </article>
    </section>
    <section v-else class="card list-card">
      <div class="list-tools">
        <form class="search-field" @submit.prevent="search"><Search :size="18"/><input v-model="term" placeholder="搜索标题、编号、故障码或现象" aria-label="搜索资料" @input="searchResult = null"/><button v-if="term" type="button" class="icon-btn" aria-label="清空搜索" @click="term = ''; searchResult = null"><X :size="17"/></button><button class="btn subtle">搜索原文</button></form>
        <select v-model="deviceId" aria-label="适用设备" @change="searchResult = null"><option value="">选择适用设备</option><option v-for="row in store.devices" :key="row.id" :value="row.id">{{ row.number }} · {{ row.name }}</option></select>
      </div>
      <div class="filters">
        <div class="tabs"><button v-for="(label, key) in { '': '全部资料', ...categories }" :key="key" :class="{ on: type === key }" @click="type = key">{{ label }}</button></div>
        <select v-if="manager || expert" v-model="filterStatus" aria-label="按状态筛选"><option value="">全部状态</option><option v-for="key in ['queued','calibration','review','approved','published','withdrawn','returned','failed']" :key="key" :value="key">{{ statuses[key] }}</option></select>
      </div>
      <div v-if="searchResult !== null" class="source-list">
        <div v-if="!searchResult.length" class="empty"><FileSearch :size="34"/><b>没有匹配的原文</b><p>试试缩短关键词，或选择正确的设备。</p></div>
        <button v-for="item in searchResult.filter(row => !type || row.category === type)" :key="item.id" class="source-row" @click="view({ ...item, id: item.sourceId })">
          <span class="tile-icon mint"><FileText :size="21"/></span><span class="source-main"><b>{{ item.title }}</b><small>{{ item.position }} · {{ item.text.slice(0, 116) }}</small></span><span class="pill">{{ categories[item.category] }}</span>
        </button>
      </div>
      <div v-else class="source-list">
        <div v-if="!filtered.length" class="empty"><FileSearch :size="34"/><b>{{ ready ? '暂无符合条件的资料' : '正在读取知识库…' }}</b><p v-if="ready">资料发布后将出现在这里。</p></div>
        <button v-for="row in filtered" :key="row.id" class="source-row" @click="view(row)">
          <span :class="['tile-icon', row.category === 'manual' ? 'mint' : row.category === 'case' ? 'blue' : 'amber']"><component :is="categoryIcons[row.category as keyof typeof categoryIcons]" :size="21"/></span>
          <span class="source-main"><b>{{ row.title }}</b><small>{{ row.number }} · V{{ row.version }} · {{ row.model }}{{ row.batch ? ' / ' + row.batch : '' }}</small></span>
          <span class="category-label">{{ categories[row.category] }}</span><span :class="['pill', { green: row.active }]">{{ row.active ? '已生效' : statuses[row.status] }}</span>
        </button>
      </div>
    </section>
    <div v-if="openUpload || detail || conflict" class="backdrop" @click="openUpload = false; conflict = null; closeDetail()"></div>
    <dialog v-if="conflict" open class="modal" aria-label="处理版本冲突">
      <div class="sheet-head"><div><h2>处理版本冲突</h2><p class="muted">{{ conflict.filename }} · V{{ conflict.version }}</p></div><button class="icon-btn" aria-label="取消" @click="conflict = null"><X :size="22"/></button></div>
      <form class="sheet-body form-stack" @submit.prevent="resolveConflict('new_version')">
        <a class="text-link" :href="'/api/v1/conflicts/' + conflict.sourceId + '/file'"><ArrowDownToLine :size="17"/> 下载冲突原件</a>
        <label>作为新版本重新导入<input v-model="conflictVersion" placeholder="输入尚未使用的版本号" maxlength="40" required/></label>
        <p v-if="errorText" class="error" role="alert">{{ errorText }}</p>
        <div class="button-row"><button type="button" class="btn secondary" :disabled="working" @click="resolveConflict('discard')">保留原版本</button><button class="btn primary" :disabled="working">登记新版本</button></div>
      </form>
    </dialog>
    <dialog v-if="openUpload" open class="modal wide" aria-label="批量导入资料">
      <div class="sheet-head"><div><h2>批量导入资料</h2><p class="muted">支持 PDF、DOCX、TXT、MD、CSV。</p></div><button class="icon-btn" aria-label="关闭导入" @click="openUpload = false"><X :size="23"/></button></div>
      <form class="sheet-body form-stack" @submit.prevent="importFiles">
        <label class="upload-zone"><CloudUpload :size="26"/><b>选择资料文件</b><small>每批最多 30 个文件</small><input type="file" multiple accept=".pdf,.docx,.txt,.md,.csv" @change="attach"/></label>
        <div v-for="(item, i) in upload" :key="i" class="upload-item">
          <div class="section-head"><b>{{ item.file.name }}</b><button type="button" class="icon-btn" aria-label="移除文件" @click="upload.splice(i, 1)"><X :size="17"/></button></div>
          <div class="form-grid">
            <label>标题<input v-model="item.title" required maxlength="150"/></label>
            <label>来源编号<input v-model="item.number" required maxlength="90"/></label>
            <label>版本<input v-model="item.version" required maxlength="40"/></label>
            <label>资料类别<select v-model="item.category"><option v-for="(label,key) in categories" :key="key" :value="key">{{ label }}</option></select></label>
            <label>设备型号<input v-model="item.model" list="library-models" required maxlength="80" placeholder="通用资料填 *"/></label>
            <label>设备批次<input v-model="item.batch" maxlength="80" placeholder="留空适用该型号全部批次"/></label>
            <label>来源网址<input v-model="item.referenceUrl" type="url" placeholder="https://"/></label>
          </div>
        </div>
        <datalist id="library-models"><option v-for="model in models" :key="model" :value="model"/></datalist>
        <p v-if="errorText" class="error" role="alert">{{ errorText }}</p>
        <div class="button-row"><button class="btn primary" :disabled="!upload.length || working"><CloudUpload :size="17"/> {{ working ? '正在上传…' : '开始导入' }}</button></div>
      </form>
    </dialog>
    <dialog v-if="detail" open class="sheet" aria-label="查看资料">
      <div class="sheet-head"><div><p class="eyebrow">{{ categories[detail.category] }} · V{{ detail.version }}</p><h2>{{ detail.title }}</h2></div><button class="icon-btn" aria-label="关闭资料" @click="closeDetail"><X :size="23"/></button></div>
      <div class="sheet-body form-stack">
        <div class="tag-row"><span class="pill">{{ detail.model }} {{ detail.batch }}</span><span class="pill">{{ detail.demo ? '演示资料' : statuses[detail.status] }}</span><span v-if="detail.ocr" class="pill">包含 OCR</span></div>
        <p v-if="detail.error" class="error">{{ detail.error }}</p>
        <p v-if="detail.note" class="muted">最近审核意见 · {{ detail.note }}</p>
        <p v-if="detail.externalModelApproved" class="muted">已授权回答模型 · {{ detail.modelApprovalHost }}</p>
        <div v-if="detail.referenceUrl || (!manager && !expert && detail.active && detail.viewDeviceId)" class="tag-row">
          <a v-if="detail.referenceUrl" class="text-link" :href="detail.referenceUrl" target="_blank" rel="noopener noreferrer"><ArrowUpRight :size="17"/> 查看发布来源</a>
          <RouterLink v-if="!manager && !expert && detail.active && detail.viewDeviceId" class="text-link" :to="{ path: '/ask', query: { device: detail.viewDeviceId } }">按此设备提问</RouterLink>
        </div>
        <a v-if="detail.filename && (manager || expert || detail.chunks?.length)" class="btn secondary own-width" :href="'/api/v1/sources/' + detail.id + '/file' + (manager || expert ? '' : '?deviceId=' + detail.viewDeviceId + '&chunkId=' + detail.chunks[0].id)" target="_blank" rel="noopener"><ArrowDownToLine :size="17"/> {{ filename(detail) }}</a>
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
          <div class="button-row"><button class="btn secondary" :disabled="!actionNote.trim() || working" @click="action('reject')">退回校核</button><button class="btn primary" :disabled="working" @click="action('approve')">审核通过</button></div>
        </template>
        <template v-if="manager && ['approved','published','withdrawn'].includes(detail.status)">
          <label class="check"><input v-model="schedule" type="checkbox"/> 指定生效时间</label>
          <div v-if="schedule" class="form-grid"><label>开始时间<input v-model="startAt" type="datetime-local"/></label><label>结束时间<input v-model="endAt" type="datetime-local"/></label></div>
          <label v-if="detail.active && !detail.batch">下架批次<input v-model="blockBatch" placeholder="留空下架整个型号范围" maxlength="80"/></label>
          <div class="button-row"><button v-if="detail.active" class="btn secondary" :disabled="working" @click="action('block')">下架适用范围</button><button class="btn primary" :disabled="working" @click="action('publish')">发布该版本</button></div>
        </template>
        <p v-if="errorText" class="error" role="alert">{{ errorText }}</p>
      </div>
    </dialog>
  </main>
</template>

