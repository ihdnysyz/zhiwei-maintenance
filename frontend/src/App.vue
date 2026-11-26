<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { Activity, ArrowUpRight, BookOpen, Check, ChevronDown, ChevronRight, CircleAlert, ClipboardList, Command, LayoutGrid, LogOut, MessageCircle, Settings, Sparkles, Users, X } from 'lucide-vue-next'
import { api, enter, errorMessage, loadDevices, notice, roleNames, store, type Profile, type Role } from './store'

const route = useRoute()
const router = useRouter()
const menu = ref(false)
const loginError = ref('')
const bootError = ref('')
const busy = ref(false)
const enterprise = ref(false)
const credentials = ref({ username: '', password: '', org: 'pilot', token: '' })
const links = computed(() => [
  { to: '/', title: '概览', short: '概览', icon: LayoutGrid },
  { to: '/ask', title: '智能问答', short: '问答', icon: Sparkles },
  { to: '/library', title: '知识库', short: '知识库', icon: BookOpen },
  { to: '/drafts', title: '工单草稿', short: '工单', icon: ClipboardList },
  { to: '/expert', title: '专家协作', short: '协作', icon: Users },
  ...(store.profile?.role === 'platform' ? [{ to: '/admin', title: '系统管理', short: '管理', icon: Settings }] : []),
])
const active = (path: string) => path === '/' ? route.path === '/' : route.path.startsWith(path)
const navStyle = computed(() => ({
  '--nav-index': links.value.findIndex(item => active(item.to)),
  '--nav-count': links.value.length,
}))
async function boot() {
  bootError.value = ''
  try {
    const state = await api<{ profile: Profile | null; demoAvailable: boolean; enterpriseIdentity: boolean; workorderEnabled: boolean }>('/auth/state')
    Object.assign(store, state)
    if (state.profile) await loadDevices()
    else if (state.demoAvailable && !sessionStorage.getItem('loggedOut')) await enter('tech')
  } catch (error) { bootError.value = errorMessage(error) }
  finally { store.ready = true }
}
onMounted(boot)
async function switchRole(role: Role) {
  menu.value = false
  try {
    await enter(role)
    await router.push('/')
    notice('已切换为' + roleNames[role])
  } catch (error) { notice(errorMessage(error), true) }
}
async function signIn() {
  loginError.value = ''
  busy.value = true
  try {
    const { org, username, password, token } = credentials.value
    store.profile = enterprise.value
      ? await api<Profile>('/auth/enterprise', 'POST', { token })
      : await api<Profile>('/auth/login', 'POST', { org, username, password })
    credentials.value.password = ''
    credentials.value.token = ''
    await loadDevices()
    await router.push('/')
  } catch (error) { loginError.value = errorMessage(error) }
  finally { busy.value = false }
}
async function logout() {
  try {
    await api('/auth/logout', 'POST')
    sessionStorage.setItem('loggedOut', '1')
    store.profile = null
    menu.value = false
  } catch (error) { notice(errorMessage(error), true) }
}
</script>

<template>
  <main v-if="!store.ready || bootError" class="center-screen">
    <div class="brand-mark"><Command :size="22" /></div>
    <p>{{ bootError || '正在连接工作台…' }}</p>
    <button v-if="bootError" class="btn primary" @click="boot">重新连接</button>
  </main>
  <main v-else-if="!store.profile" class="login-screen">
    <section class="login-art"><div class="brand"><div class="brand-mark"><Command :size="23"/></div><strong>知维</strong></div><h1>让每一次维修<br/>都有据可循。</h1></section>
    <section class="login-body">
      <form class="login-form" @submit.prevent="signIn">
        <h2>登录维修工作台</h2>
        <p class="muted">访问设备知识库、诊断会话与工单。</p>
        <template v-if="enterprise">
          <label>企业访问令牌<input v-model="credentials.token" type="password" required autocomplete="off" /></label>
        </template>
        <template v-else>
          <label>组织<input v-model="credentials.org" required /></label>
          <label>账号<input v-model="credentials.username" required autocomplete="username" /></label>
          <label>密码<input v-model="credentials.password" required type="password" autocomplete="current-password" /></label>
        </template>
        <p v-if="loginError" class="error" role="alert">{{ loginError }}</p>
        <button class="btn primary" :disabled="busy">登录 <ArrowUpRight :size="16" /></button>
        <button v-if="store.enterpriseIdentity" type="button" class="btn ghost" @click="enterprise = !enterprise">{{ enterprise ? '本地账号登录' : '企业身份登录' }}</button>
        <button v-if="store.demoAvailable" type="button" class="btn ghost" @click="switchRole('tech')">进入演示工作区</button>
      </form>
    </section>
  </main>
  <div v-else class="app-shell" @keydown.esc="menu = false">
    <button v-if="menu" class="dismiss" tabindex="-1" aria-label="关闭菜单" @click="menu = false"></button>
    <aside class="sidebar glass">
      <RouterLink class="brand" to="/">
        <span class="brand-mark"><Command :size="22" :stroke-width="2.4" /></span>
        <span><strong>知维</strong><small>设备维修知识库</small></span>
      </RouterLink>
      <div class="nav-section">工作空间</div>
      <nav class="nav-track" aria-label="主导航" :style="navStyle" :data-inactive="navStyle['--nav-index'] < 0 || undefined">
        <RouterLink v-for="item in links" :key="item.to" :to="item.to" :class="{ selected: active(item.to) }" :aria-current="active(item.to) ? 'page' : undefined">
          <component :is="item.icon" :size="19" :stroke-width="1.85" /><span>{{ item.title }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar-foot">
        <RouterLink to="/expert" class="side-help"><MessageCircle :size="18"/><span>联系班组专家</span><ArrowUpRight :size="15"/></RouterLink>
        <div class="side-workspace"><div class="work-avatar"><Activity :size="17"/></div><div><b>{{ store.profile.demo ? '维修试点工作区' : store.profile.org }}</b><small>{{ store.profile.demo ? '演示环境' : '当前组织' }}</small></div></div>
      </div>
    </aside>
    <div class="app-body">
      <header class="topbar glass">
        <div class="crumb"><RouterLink to="/" class="compact-brand" aria-label="知维概览"><span class="brand-mark"><Command :size="18"/></span></RouterLink><span>工作空间</span><ChevronRight :size="14" /><b>{{ route.meta.title }}</b></div>
        <div class="top-actions">
          <span class="live"><i aria-hidden="true"></i>{{ store.profile.demo ? '演示模式' : '工作区在线' }}</span>
          <div class="profile-wrap">
            <button class="profile-btn" :aria-expanded="menu" @click.stop="menu = !menu"><span class="profile-avatar">{{ store.profile.name.slice(0, 1) }}</span><span class="profile-name">{{ store.profile.name }}</span><ChevronDown :size="14"/></button>
            <Transition name="popover">
            <div v-if="menu" class="profile-menu glass">
              <template v-if="store.profile.demo">
                <div class="menu-caption">切换演示角色</div>
                <button v-for="(label, key) in roleNames" :key="key" @click="switchRole(key)">{{ label }} <Check v-if="key === store.profile.role" :size="15"/></button>
              </template>
              <button @click="logout"><LogOut :size="16"/> 退出登录</button>
            </div>
            </Transition>
          </div>
        </div>
      </header>
      <RouterView v-slot="{ Component, route: viewRoute }">
        <Transition name="page-enter">
          <component :is="Component" :key="store.profile.org + ':' + store.profile.username + ':' + viewRoute.matched[0]?.path" />
        </Transition>
      </RouterView>
    </div>
    <nav class="tabbar nav-track glass" aria-label="底部导航" :style="navStyle" :data-inactive="navStyle['--nav-index'] < 0 || undefined">
      <RouterLink v-for="item in links" :key="item.to" :to="item.to" :class="{ selected: active(item.to) }" :aria-label="item.title" :aria-current="active(item.to) ? 'page' : undefined" @click="menu = false">
        <component :is="item.icon" :size="21" :stroke-width="1.85"/><span>{{ item.short }}</span>
      </RouterLink>
    </nav>
  </div>
  <Transition>
    <div v-if="store.toast" :class="['toast', { bad: store.toastError }]" role="status">
      <CircleAlert v-if="store.toastError" :size="19"/><Check v-else :size="19"/>
      {{ store.toast }} <button class="icon-btn" aria-label="关闭提示" @click="store.toast = ''"><X :size="16"/></button>
    </div>
  </Transition>
</template>

