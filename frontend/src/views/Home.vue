<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, ChatDotRound, Collection, Document, Plus, Setting } from '@element-plus/icons-vue'
import { api, can, date, errorMessage, notice, statuses, statusTag, store, type Row } from '../store'

const router = useRouter()
const sessions = ref<Row[]>([])
const sources = ref<Row[]>([])
const drafts = ref<Row[]>([])
const ready = ref(false)

onMounted(async () => {
  try {
    ;[sessions.value, sources.value, drafts.value] = await Promise.all([
      api<Row[]>('/sessions'), api<Row[]>('/sources'), api<Row[]>('/drafts'),
    ])
  } catch (error) { notice(errorMessage(error), true) }
  finally { ready.value = true }
})

const recentSessions = computed(() => sessions.value.filter(row => row.sequence).slice(0, 5))
const recentDrafts = computed(() => drafts.value.slice(0, 5))
const needsDeviceSetup = computed(() => can('device:manage') && !store.devices.length)
const summary = computed(() => [
  { title: '已发布资料', number: sources.value.filter(row => row.active).length, icon: Collection, theme: 'success', to: '/library' },
  { title: '诊断会话', number: sessions.value.filter(row => row.sequence).length, icon: ChatDotRound, theme: 'primary', to: '/ask' },
  { title: '待确认工单', number: drafts.value.filter(row => row.status === 'draft').length, icon: Document, theme: 'warning', to: '/drafts' },
])

function draftLabel(row: Row) {
  if (row.completedAt) return '已结单'
  return row.status === 'linked' ? '待确认结单' : statuses[row.status]
}
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <h1>概览</h1>
      <div class="row-gap">
        <el-button v-if="needsDeviceSetup" :icon="Setting" @click="router.push('/admin')">添加设备</el-button>
        <el-button v-else :icon="Collection" @click="router.push('/library')">知识库</el-button>
        <el-button type="primary" :icon="ChatDotRound" @click="router.push('/ask')">开始问答</el-button>
      </div>
    </div>

    <div class="kpi-grid" aria-label="工作区数据">
      <el-card v-for="item in summary" :key="item.title" shadow="hover" class="kpi-card" @click="router.push(item.to)">
        <el-statistic :title="item.title" :value="ready ? item.number : 0" />
        <div :class="['kpi-icon', item.theme]">
          <el-icon><component :is="item.icon" /></el-icon>
        </div>
      </el-card>
    </div>

    <div class="two-col">
      <el-card shadow="never">
        <template #header>
          <div class="space-between">
            <b>最近诊断</b>
            <el-button link type="primary" @click="router.push('/ask')">全部 <el-icon><ArrowRight /></el-icon></el-button>
          </div>
        </template>
        <template v-if="recentSessions.length">
          <div v-for="row in recentSessions" :key="row.id" class="list-item" @click="router.push('/ask/' + row.id)">
            <div class="list-main">
              <b>{{ row.title }}</b>
              <small>{{ row.deviceName }} · {{ date(row.updated, true) }}</small>
            </div>
            <el-icon><ArrowRight /></el-icon>
          </div>
        </template>
        <el-empty v-else :image-size="64" :description="ready ? '暂无诊断记录' : '正在读取…'">
          <el-button type="primary" plain :icon="Plus" @click="router.push('/ask')">新建诊断</el-button>
        </el-empty>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="space-between">
            <b>工单草稿</b>
            <el-button link type="primary" @click="router.push('/drafts')">全部 <el-icon><ArrowRight /></el-icon></el-button>
          </div>
        </template>
        <template v-if="recentDrafts.length">
          <div v-for="row in recentDrafts" :key="row.id" class="list-item" @click="router.push('/drafts/' + row.id)">
            <div class="list-main">
              <b>{{ row.fields.symptom || row.device.name }}</b>
              <small>{{ row.device.number }} · {{ date(row.updated, true) }}</small>
            </div>
            <el-tag size="small" :type="statusTag(row.status, Boolean(row.completedAt))">{{ draftLabel(row) }}</el-tag>
          </div>
        </template>
        <el-empty v-else :image-size="64" :description="ready ? '暂无工单草稿' : '正在读取…'">
          <el-button type="primary" plain :icon="needsDeviceSetup ? Setting : ChatDotRound" @click="router.push(needsDeviceSetup ? '/admin' : '/ask')">
            {{ needsDeviceSetup ? '添加设备' : '开始问答' }}
          </el-button>
        </el-empty>
      </el-card>
    </div>
  </section>
</template>
