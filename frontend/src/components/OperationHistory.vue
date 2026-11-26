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
  <section v-if="events?.length" class="op-history" aria-label="操作记录">
    <el-divider content-position="left">操作记录</el-divider>
    <el-timeline>
      <el-timeline-item v-for="event in events" :key="event.id" :timestamp="formatter.format(new Date(event.at))" placement="top">
        <div class="op-row">
          <b>{{ labels[event.action] || event.action }}</b>
          <span class="muted" :title="event.actor">
            {{ event.actorName }}<template v-if="event.actor !== event.actorName && event.actor !== 'worker'">（{{ event.actor }}）</template>
          </span>
        </div>
      </el-timeline-item>
    </el-timeline>
  </section>
</template>
