<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Delete, Download, Plus, Refresh, Search, Tickets, UploadFilled } from '@element-plus/icons-vue'
import OperationHistory from '../components/OperationHistory.vue'
import SourceDetail from '../components/SourceDetail.vue'
import { api, can, categories, date, errorMessage, notice, statuses, statusTag, store, type Row } from '../store'

interface UploadItem { file: File; title: string; number: string; version: string; model: string; batch: string; category: string; referenceUrl: string }

const route = useRoute()
const sources = ref<Row[]>([])
const batches = ref<Row[]>([])
const detail = ref<Row | null>(null)
const conflict = ref<Row | null>(null)
const conflictVersion = ref('')
const type = ref('all')
const term = ref('')
const filterStatus = ref('')
const deviceId = ref('')
const rawSearch = ref(false)
const searchResult = ref<Row[] | null>(null)
const ready = ref(false)
const openUpload = ref(false)
const showBatches = ref(false)
const upload = ref<UploadItem[]>([])
const working = ref(false)
const errorText = ref('')
const fileInput = ref<HTMLInputElement>()

const manager = computed(() => can('knowledge:manage'))
const expert = computed(() => can('knowledge:review'))
const models = computed(() => [...new Set(store.devices.map(row => row.model))])
const activeCategory = computed(() => type.value === 'all' ? '' : type.value)

const filtered = computed(() => sources.value.filter(source =>
  (!activeCategory.value || source.category === activeCategory.value)
  && (!filterStatus.value || source.status === filterStatus.value)
  && (searchResult.value !== null || !term.value || [source.title, source.number, source.model, source.batch].join(' ').toLowerCase().includes(term.value.toLowerCase())),
))

const filteredSearch = computed(() => {
  const matchingSources = filterStatus.value
    ? new Set(sources.value.filter(row => row.status === filterStatus.value).map(row => row.id))
    : null
  return (searchResult.value || []).filter(row => (!activeCategory.value || row.category === activeCategory.value)
    && (!matchingSources || matchingSources.has(row.sourceId)))
})

let timer: number | undefined
let searchGeneration = 0
let sourceGeneration = 0

async function fetchSources() {
  const generation = ++sourceGeneration
  try {
    const scope = deviceId.value ? '?' + new URLSearchParams({ deviceId: deviceId.value }) : ''
    const rows = await api<Row[]>('/sources' + scope)
    if (generation === sourceGeneration) sources.value = rows
  } catch (error) {
    if (generation === sourceGeneration) notice(errorMessage(error), true)
  } finally {
    if (generation === sourceGeneration) ready.value = true
  }
}

async function fetchData() {
  await fetchSources()
  if (!manager.value) return
  try { batches.value = await api<Row[]>('/imports') }
  catch (error) { notice(errorMessage(error), true) }
}

onMounted(() => {
  void fetchData().then(() => {
    const source = sources.value.find(row => row.id === route.query.source)
    if (source) detail.value = source
  })
  timer = window.setInterval(() => { if (showBatches.value) void fetchData() }, 1500)
})
onUnmounted(() => { clearInterval(timer); sourceGeneration++; clearSearch() })

function clearSearch() {
  searchGeneration++
  rawSearch.value = false
  searchResult.value = null
}

async function search() {
  if (!term.value.trim() || !deviceId.value) { clearSearch(); return }
  const generation = ++searchGeneration
  rawSearch.value = true
  searchResult.value = null
  try {
    const results = await api<Row[]>('/knowledge/search?' + new URLSearchParams({ deviceId: deviceId.value, q: term.value.trim() }))
    if (generation === searchGeneration) searchResult.value = results
  } catch (error) {
    if (generation === searchGeneration) { clearSearch(); notice(errorMessage(error), true) }
  }
}

function changeDevice() {
  detail.value = null
  errorText.value = ''
  ready.value = false
  sources.value = []
  void fetchSources()
  if (rawSearch.value && deviceId.value) void search()
  else clearSearch()
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
    notice('导入已登记，解析成功后自动生效')
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
async function reindexAll() {
  if (working.value) return
  working.value = true
  try {
    const res = await api<{ queued: number }>('/sources/reindex-all', 'POST')
    await fetchData()
    notice(`已提交 ${res.queued} 份资料重建切片与向量`)
  } catch (error) { notice(errorMessage(error), true) }
  finally { working.value = false }
}
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <h1>知识库</h1>
      <div v-if="manager" class="row-gap">
        <el-button :icon="Refresh" :loading="working" @click="reindexAll">重建向量</el-button>
        <el-button :icon="Tickets" @click="showBatches = !showBatches">{{ showBatches ? '返回资料' : '导入记录' }}</el-button>
        <el-button type="primary" :icon="Plus" @click="openUpload = true; errorText = ''">批量导入</el-button>
      </div>
    </div>

    <el-card v-if="showBatches" shadow="never">
      <template #header>
        <div class="space-between">
          <b>导入记录</b>
          <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
        </div>
      </template>
      <el-empty v-if="!batches.length" description="暂无导入批次" />
      <div v-else class="stack">
        <el-card v-for="batch in batches" :key="batch.id" shadow="never">
          <template #header>
            <div class="space-between">
              <div class="row-gap">
                <b>批次 {{ batch.id.slice(0, 8) }}</b>
                <span class="muted">{{ date(batch.created, true) }} · {{ batch.items.length }} 份</span>
              </div>
              <el-button v-if="batch.items.some((item: Row) => item.status === 'failed' && item.sourceId)" size="small" @click="retry(batch.id)">重试失败项</el-button>
            </div>
          </template>
          <el-table :data="batch.items" size="small">
            <el-table-column prop="filename" label="文件名" min-width="200" />
            <el-table-column prop="error" label="错误" min-width="180" />
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTag(row.status)">{{ statuses[row.status] || row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" align="right">
              <template #default="{ row }">
                <el-button v-if="row.status === 'conflict'" size="small" type="warning" plain @click="conflict = row; conflictVersion = ''; errorText = ''">处理冲突</el-button>
              </template>
            </el-table-column>
          </el-table>
          <OperationHistory :events="batch.operationHistory" />
        </el-card>
      </div>
    </el-card>

    <el-card v-else shadow="never">
      <div class="stack">
        <div class="row-gap">
          <el-input
            v-model="term"
            clearable
            :prefix-icon="Search"
            :placeholder="deviceId ? '搜索标题、编号、故障码或现象' : '搜索标题、编号或型号'"
            style="flex: 1; min-width: 240px"
            aria-label="搜索资料"
            @input="clearSearch"
            @keyup.enter="search"
          >
            <template #append>
              <el-button :disabled="!deviceId || !term.trim()" @click="search">搜索原文</el-button>
            </template>
          </el-input>
          <el-select v-model="deviceId" clearable filterable placeholder="全部设备" style="width: 240px" aria-label="适用设备" @change="changeDevice">
            <el-option v-for="row in store.devices" :key="row.id" :value="row.id" :label="`${row.number} · ${row.name}`" />
          </el-select>
          <el-select v-if="manager || expert" v-model="filterStatus" clearable placeholder="全部状态" style="width: 150px" aria-label="按状态筛选">
            <el-option v-for="key in ['queued','calibration','review','approved','published','withdrawn','returned','failed']" :key="key" :value="key" :label="statuses[key]" />
          </el-select>
        </div>

        <el-tabs v-model="type">
          <el-tab-pane label="全部" name="all" />
          <el-tab-pane v-for="(label, key) in categories" :key="key" :label="label" :name="key" />
        </el-tabs>

        <Transition name="tab-slide" mode="out-in">
          <div v-if="rawSearch" :key="'search-' + type">
            <div v-if="searchResult === null || !ready" class="muted" role="status">正在搜索原文…</div>
            <el-empty v-else-if="!filteredSearch.length" description="无匹配原文" />
            <div v-else>
              <div v-for="item in filteredSearch" :key="item.id" class="list-item" @click="detail = { ...item, id: item.sourceId }">
                <div class="list-main">
                  <b>{{ item.title }}</b>
                  <small>{{ categories[item.category] }} · {{ item.position }} · {{ item.text.slice(0, 116) }}</small>
                </div>
                <el-tag size="small" type="info">{{ categories[item.category] }}</el-tag>
              </div>
            </div>
          </div>

          <div v-else :key="'list-' + type">
            <el-table v-if="filtered.length" :data="filtered" @row-click="(row: Row) => detail = row" style="cursor: pointer">
              <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
              <el-table-column label="类别" width="100">
                <template #default="{ row }">{{ categories[row.category] }}</template>
              </el-table-column>
              <el-table-column label="来源 / 版本" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">{{ row.number }} · V{{ row.version }}</template>
              </el-table-column>
              <el-table-column label="适用型号" min-width="150" show-overflow-tooltip>
                <template #default="{ row }">{{ row.model }}{{ row.batch ? ' / ' + row.batch : '' }}</template>
              </el-table-column>
              <el-table-column v-if="manager || expert" label="RAG 索引" min-width="170" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="row.indexReady" class="muted">{{ row.chunkCount || 0 }} 段 · {{ row.embeddingModel || 'local' }}</span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>
              <el-table-column v-if="manager || expert" label="状态" width="110" align="right">
                <template #default="{ row }">
                  <el-tag size="small" :type="statusTag(row.status, row.active)">{{ row.active ? '已生效' : statuses[row.status] }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-else :description="ready ? '暂无资料' : '正在读取…'">
              <el-button v-if="ready && manager && !sources.length" type="primary" :icon="Plus" @click="openUpload = true; errorText = ''">批量导入</el-button>
            </el-empty>
          </div>
        </Transition>
      </div>
    </el-card>

    <el-dialog v-model="openUpload" title="批量导入资料" width="760px">
      <div class="stack">
        <div class="space-between">
          <input ref="fileInput" type="file" multiple accept=".pdf,.docx,.txt,.md,.csv,.tsv" style="display: none" @change="attach" />
          <el-button type="primary" plain :icon="UploadFilled" @click="fileInput?.click()">选择资料文件</el-button>
          <span class="muted">PDF / DOCX / TXT / MD / CSV / TSV · 最多 30 份</span>
        </div>
        <el-card v-for="(item, i) in upload" :key="i" shadow="never">
          <template #header>
            <div class="space-between">
              <b>{{ item.file.name }}</b>
              <el-button text type="danger" :icon="Delete" aria-label="移除文件" @click="upload.splice(i, 1)" />
            </div>
          </template>
          <el-form label-position="top" size="small">
            <div class="form-grid">
              <el-form-item label="标题" required><el-input v-model="item.title" maxlength="150" /></el-form-item>
              <el-form-item label="来源编号" required><el-input v-model="item.number" maxlength="90" /></el-form-item>
              <el-form-item label="版本" required><el-input v-model="item.version" maxlength="40" /></el-form-item>
              <el-form-item label="资料类别">
                <el-select v-model="item.category" class="w-full">
                  <el-option v-for="(label, key) in categories" :key="key" :value="key" :label="label" />
                </el-select>
              </el-form-item>
              <el-form-item label="设备型号" required>
                <el-select v-model="item.model" filterable allow-create default-first-option placeholder="通用资料填 *" class="w-full">
                  <el-option v-for="model in models" :key="model" :value="model" :label="model" />
                </el-select>
              </el-form-item>
              <el-form-item label="设备批次"><el-input v-model="item.batch" maxlength="80" placeholder="留空适用全部批次" /></el-form-item>
              <el-form-item label="来源网址"><el-input v-model="item.referenceUrl" placeholder="https://" /></el-form-item>
            </div>
          </el-form>
        </el-card>
        <el-alert v-if="errorText" :title="errorText" type="error" show-icon :closable="false" />
      </div>
      <template #footer>
        <el-button @click="openUpload = false">取消</el-button>
        <el-button type="primary" :loading="working" :disabled="!upload.length" @click="importFiles">开始导入</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="!!conflict" title="处理版本冲突" width="480px" @close="conflict = null">
      <div v-if="conflict" class="stack">
        <div class="space-between">
          <span>{{ conflict.filename }} · V{{ conflict.version }}</span>
          <el-button link type="primary" tag="a" :href="'/api/v1/conflicts/' + conflict.sourceId + '/file'" :icon="Download">下载冲突原件</el-button>
        </div>
        <el-form label-position="top" @submit.prevent="resolveConflict('new_version')">
          <el-form-item label="作为新版本重新导入" required>
            <el-input v-model="conflictVersion" placeholder="尚未使用的版本号" maxlength="40" />
          </el-form-item>
        </el-form>
        <el-alert v-if="errorText" :title="errorText" type="error" show-icon :closable="false" />
        <OperationHistory :events="conflict.operationHistory" />
      </div>
      <template #footer>
        <el-button :disabled="working" @click="resolveConflict('discard')">保留原版本</el-button>
        <el-button type="primary" :loading="working" :disabled="!conflictVersion.trim()" @click="resolveConflict('new_version')">登记新版本</el-button>
      </template>
    </el-dialog>

    <SourceDetail
      v-if="detail"
      :key="detail.id"
      :source-id="detail.id"
      :device-id="detail.applicableDeviceId || deviceId"
      @close="detail = null"
      @updated="fetchData"
    />
  </section>
</template>
