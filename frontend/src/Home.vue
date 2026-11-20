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
  { title: '已发布资料', number: sources.value.filter(row => row.active).length, icon: BookOpen, theme: 'mint', to: '/library', note: '手册、工单与故障案例' },
  { title: '诊断会话', number: sessions.value.filter(row => row.sequence).length, icon: MessageSquare, theme: 'blue', to: '/ask', note: '可随时继续现场问答' },
  { title: '待确认工单', number: drafts.value.filter(row => row.status === 'draft').length, icon: ClipboardList, theme: 'amber', to: '/drafts', note: '核对现场处置后提交' },
])
const faultExample = computed(() => {
  if (store.profile?.demo) {
    const source = sources.value.find(row => row.active && row.number === 'PUB-SIEMENS-G120C-FAULTS' && row.referenceUrl)
    const device = source && store.devices.find(row => source.applicableDeviceId
      ? row.id === source.applicableDeviceId
      : row.model === source.model && row.batch === source.batch)
    if (device) return {
      title: 'F30005 是什么故障？', desc: '西门子 G120C · 核对厂家手册', icon: Search,
      to: { path: '/ask', query: { device: device.id, q: 'F30005 是什么故障？' } },
    }
    const sample = store.devices.find(row => row.demo)
    if (sample) return {
      title: '演示样例 · E07 温度告警', desc: 'IM-200 · 查看内置示例', icon: Search,
      to: { path: '/ask', query: { device: sample.id, q: 'E07 温度告警如何核对？' } },
    }
  }
  return {
    title: '查询设备故障码', desc: '查看适用手册和类似工单', icon: Search,
    to: { path: '/ask', query: { q: '面板显示的故障码是' } },
  }
})
const quick = computed(() => [
  faultExample.value,
  { title: '查找振动与异响案例', desc: '按现象定位历史处置经验', icon: Wrench, to: { path: '/ask', query: { q: '设备运行时有振动与异响，是否有类似历史案例？' } } },
  { title: '浏览维修资料', desc: '按手册、历史工单或案例筛选', icon: FileText, to: '/library' },
])
const today = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())
</script>

<template>
  <main class="page dashboard">
    <div class="page-heading overview-heading">
      <div><h1>设备维修工作台</h1><p>把维修经验沉淀为可查、可用的知识。</p></div>
      <div class="today"><Clock3 :size="15"/>{{ today }}</div>
    </div>
    <section class="hero">
      <div class="hero-content">
        <div class="hero-label"><Sparkles :size="15"/> 智能维修助手</div>
        <h2>让每一次维修，<br/>都有据可循。</h2>
        <p>查询故障、核对原文，一键整理维修工单。</p>
        <div class="hero-actions">
          <RouterLink to="/ask" class="btn hero-primary"><Sparkles :size="17"/> 开始问答 <ArrowRight :size="17"/></RouterLink>
          <RouterLink to="/library" class="btn hero-secondary">浏览知识库 <ArrowUpRight :size="16"/></RouterLink>
        </div>
      </div>
      <div class="hero-glow" aria-hidden="true"></div>
      <span class="hero-point point-one" aria-hidden="true"></span><span class="hero-point point-two" aria-hidden="true"></span>
    </section>
    <section class="metrics" aria-label="工作区数据">
      <RouterLink v-for="item in summary" :key="item.title" :to="item.to" class="metric card">
        <div class="metric-head"><div :class="['tile-icon', item.theme]"><component :is="item.icon" :size="20"/></div><ArrowUpRight class="metric-arrow" :size="16"/></div>
        <div class="metric-main"><strong>{{ ready ? item.number : '—' }}</strong><span>{{ item.title }}</span></div>
        <small>{{ item.note }}</small>
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

