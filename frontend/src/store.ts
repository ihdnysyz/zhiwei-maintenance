import { reactive } from 'vue'

export type Role = 'tech' | 'library' | 'expert' | 'platform'
export type Row = Record<string, any> & { id: string; revision: number; status: string }
export interface Profile { name: string; role: Role; org: string; username: string; demo: boolean }
export const roleNames: Record<Role, string> = {
  tech: '维修人员', library: '知识管理员', expert: '班组专家', platform: '平台管理员',
}
export const categories: Record<string, string> = { manual: '维修手册', case: '故障案例', workorder: '历史工单' }
export const statuses: Record<string, string> = {
  queued: '等待处理', calibration: '待校核', review: '待审核', approved: '审核通过',
  published: '已发布', withdrawn: '已下架', returned: '已退回', failed: '处理失败',
  duplicate: '重复复用', conflict: '版本冲突', draft: '待确认', confirmed: '已确认',
  exported: '已导出', linked: '已登记', submitting: '回写核对中', open: '待领取',
  assigned: '待答复', answered: '已答复', closed: '已关闭', active: '启用', disabled: '停用',
  unknown: '结果未知', created: '已创建', processing: '处理中', not_created: '终局未创建',
  manual_review: '待人工核对', done: '已完成', pending: '待执行', running: '执行中',
  resolved: '已登记新版本', discarded: '已保留原版本',
}
export const store = reactive({
  profile: null as Profile | null,
  demoAvailable: false,
  enterpriseIdentity: false,
  workorderEnabled: false,
  ready: false,
  devices: [] as Row[],
  toast: '',
  toastError: false,
})
let toastTimer: number | undefined
export function notice(message: string, error = false) {
  store.toast = message
  store.toastError = error
  clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { store.toast = '' }, 4500)
}
export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请重试'
}
export async function request(path: string, method = 'GET', payload?: object | FormData) {
  const headers = payload && !(payload instanceof FormData) ? { 'Content-Type': 'application/json' } : undefined
  let response: Response
  try {
    response = await fetch('/api/v1' + path, { method, headers,
      body: payload instanceof FormData ? payload : payload ? JSON.stringify(payload) : undefined })
  } catch {
    throw new Error('无法连接服务，请稍后重试')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.message || '操作失败，请稍后重试')
  }
  return response
}
export async function api<T = Row>(path: string, method = 'GET', payload?: object | FormData): Promise<T> {
  return (await request(path, method, payload)).json()
}
export async function loadDevices() {
  store.devices = await api<Row[]>('/devices')
}
export async function enter(role: Role) {
  store.profile = await api<Profile>('/auth/demo', 'POST', { role })
  sessionStorage.removeItem('loggedOut')
  await loadDevices()
}
export function date(value: string, time = false) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', time
    ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
    : { month: 'long', day: 'numeric' }).format(new Date(value))
}

