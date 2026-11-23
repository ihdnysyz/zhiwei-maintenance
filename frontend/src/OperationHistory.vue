<script setup lang="ts">
type Event = { id: string; action: string; actor: string; actorName: string; at: string }
defineProps<{ events?: Event[] }>()

const labels: Record<string, string> = {
  '资料·review': '校核后送审', '资料·approve': '审核通过', '资料·reject': '退回校核',
  '资料·publish': '发布资料', '资料·block': '下架资料',
  '资料·审核后自动发布': '审核后自动发布', '资料·授权回答模型': '授权回答模型',
  '专家待办·claim': '领取待办', '专家待办·assign': '分派待办', '专家待办·reply': '答复待办',
  '专家待办·close': '关闭待办', '专家待办·revise': '提交知识修订',
}
const formatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  hourCycle: 'h23',
})
</script>

<template>
  <section v-if="events?.length" class="operation-history" aria-label="操作记录">
    <h3>操作记录</h3>
    <ol>
      <li v-for="event in events" :key="event.id">
        <b>{{ labels[event.action] || event.action }}</b>
        <span :title="event.actor">{{ event.actorName }} <span v-if="event.actor !== event.actorName && event.actor !== 'worker'">（{{ event.actor }}）</span></span>
        <time :datetime="event.at">{{ formatter.format(new Date(event.at)) }}</time>
      </li>
    </ol>
  </section>
</template>
