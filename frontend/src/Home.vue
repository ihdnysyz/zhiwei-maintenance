<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowRight, ArrowUpRight, BookOpen, ChevronRight, ClipboardList, Clock3, FileText, History, MessageSquare, Plus, Search, Sparkles, Wrench } from 'lucide-vue-next'
import { api, date, errorMessage, notice, store, type Row } from './store'

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
const summary = computed(() => [
  { title: '已发布资料', number: sources.value.filter(row => row.active).length, icon: BookOpen, theme: 'mint', to: '/library' },
  { title: '诊断会话', number: sessions.value.filter(row => row.sequence).length, icon: MessageSquare, theme: 'blue', to: '/ask' },
  { title: '待确认工单', number: drafts.value.filter(row => row.status === 'draft').length, icon: ClipboardList, theme: 'amber', to: '/drafts' },
])
const quick = computed(() => [
  ...(store.profile?.role === 'platform' && !store.devices.length
    ? [{ title: '添加设备', desc: '先录入设备型号和批次，再导入适用资料', icon: Wrench, to: '/admin' }]
    : [{ title: '查询设备故障码', desc: '选择设备后查找适用手册和类似工单', icon: Search, to: '/ask' }]),
  { title: '浏览维修资料', desc: '按手册、历史工单或案例筛选', icon: FileText, to: '/library' },
])
const today = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())
</script>

<template>
  <main class="page dashboard">
    <div class="page-heading overview-heading">
      <h1>设备维修工作台</h1>
      <div class="today"><Clock3 :size="15"/>{{ today }}</div>
    </div>
    <section class="hero">
      <div class="hero-content">
        <h2>现场故障，<br/>快速定位。</h2>
        <p>按设备查故障码、排查步骤和原始出处。</p>
        <div class="hero-actions">
          <RouterLink to="/ask" class="btn hero-primary"><Sparkles :size="17"/> 开始问答 <ArrowRight :size="17"/></RouterLink>
          <RouterLink to="/library" class="btn hero-secondary">浏览知识库 <ArrowUpRight :size="16"/></RouterLink>
        </div>
      </div>
      <div class="hero-glow" aria-hidden="true"></div>
    </section>
    <section class="metrics" aria-label="工作区数据">
      <RouterLink v-for="item in summary" :key="item.title" :to="item.to" class="metric card">
        <div class="metric-head"><div :class="['tile-icon', item.theme]"><component :is="item.icon" :size="20"/></div><ArrowUpRight class="metric-arrow" :size="16"/></div>
        <div class="metric-main"><strong>{{ ready ? item.number : '—' }}</strong><span>{{ item.title }}</span></div>
      </RouterLink>
    </section>
    <div class="overview-grid">
      <section class="card overview-card">
        <div class="section-head"><div><h2>最近诊断</h2><p>继续查阅你的现场问答</p></div><RouterLink class="text-link" to="/ask">全部会话 <ChevronRight :size="16"/></RouterLink></div>
        <div v-if="sessions.filter(row => row.sequence).length" class="overview-rows">
          <RouterLink v-for="row in sessions.filter(item => item.sequence).slice(0,4)" :key="row.id" :to="'/ask/' + row.id" class="row-link">
            <span class="tile-icon mint compact"><History :size="18"/></span><div><b>{{ row.title }}</b><small>{{ row.deviceName }} · {{ date(row.updated, true) }}</small></div><ChevronRight :size="17"/>
          </RouterLink>
        </div>
        <div v-else class="empty-overview"><div class="empty-icon"><History :size="25"/></div><b>{{ ready ? '还没有诊断记录' : '正在读取会话…' }}</b><p>发起一次问答，随时接续未完成的诊断。</p><RouterLink to="/ask" class="btn subtle"><Plus :size="16"/> 新建诊断</RouterLink></div>
      </section>
      <section class="card overview-card">
        <div class="section-head"><div><h2>快速开始</h2><p>从常用的维修场景出发</p></div></div>
        <div class="overview-rows">
          <RouterLink v-for="item in quick" :key="item.title" :to="item.to" class="row-link">
            <span class="tile-icon mint compact"><component :is="item.icon" :size="18"/></span><div><b>{{ item.title }}</b><small>{{ item.desc }}</small></div><ChevronRight :size="17"/>
          </RouterLink>
        </div>
      </section>
    </div>
  </main>
</template>

