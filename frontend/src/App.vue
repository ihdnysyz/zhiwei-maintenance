<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import {
  ArrowDown, ChatDotRound, Check, Collection, Document, HomeFilled, Key, Lock,
  Menu as MenuIcon, OfficeBuilding, Setting, SwitchButton, User,
} from '@element-plus/icons-vue'
import { api, can, enter, errorMessage, loadDevices, notice, roleLabel, roleNames, store, type Profile, type Role } from './store'

const route = useRoute()
const router = useRouter()
const mobileNav = ref(false)
const loginError = ref('')
const bootError = ref('')
const busy = ref(false)
const enterprise = ref(false)
const showOrg = ref(false)
const credentials = ref({ username: '', password: '', org: 'pilot', token: '' })
const passwordDialog = ref(false)
const passwordForm = ref({ currentPassword: '', newPassword: '', confirmPassword: '' })
const passwordError = ref('')
const passwordBusy = ref(false)

const links = computed(() => [
  { to: '/', title: '概览', icon: HomeFilled },
  { to: '/ask', title: '智能问答', icon: ChatDotRound },
  { to: '/library', title: '知识库', icon: Collection },
  { to: '/drafts', title: '工单草稿', icon: Document },
  { to: '/expert', title: '专家协作', icon: User },
  ...(can('system:manage') ? [{ to: '/admin', title: '系统管理', icon: Setting }] : []),
])

const activeMenu = computed(() => {
  const match = links.value.find(item => item.to !== '/' && route.path.startsWith(item.to))
  return match ? match.to : '/'
})

async function boot() {
  bootError.value = ''
  try {
    const state = await api<{
      profile: Profile | null
      defaultOrg?: string
      demoAvailable: boolean
      enterpriseIdentity: boolean
      workorderEnabled: boolean
    }>('/auth/state')
    Object.assign(store, state)
    if (state.defaultOrg) credentials.value.org = state.defaultOrg
    if (state.profile) {
      await loadDevices()
      if (route.path === '/login') await router.replace('/')
      else if (route.meta.roles && !(route.meta.roles as Role[]).includes(state.profile.role)) await router.replace('/')
    } else if (route.path !== '/login') {
      await router.replace({ path: '/login', query: route.fullPath !== '/' ? { redirect: route.fullPath } : undefined })
    }
  } catch (error) { bootError.value = errorMessage(error) }
  finally { store.ready = true }
}
onMounted(boot)

async function switchRole(role: Role) {
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
      : await api<Profile>('/auth/login', 'POST', { org: org.trim() || store.defaultOrg, username: username.trim(), password })
    credentials.value.password = ''
    credentials.value.token = ''
    await loadDevices()
    const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') ? route.query.redirect : '/'
    await router.replace(target)
    notice(`欢迎回来，${store.profile.name}`)
  } catch (error) { loginError.value = errorMessage(error) }
  finally { busy.value = false }
}

async function logout() {
  try {
    await api('/auth/logout', 'POST')
    store.profile = null
    store.devices = []
    await router.replace('/login')
  } catch (error) { notice(errorMessage(error), true) }
}

async function submitPassword() {
  passwordError.value = ''
  if (passwordForm.value.newPassword.length < 6) {
    passwordError.value = '新密码至少 6 个字符'
    return
  }
  if (passwordForm.value.newPassword !== passwordForm.value.confirmPassword) {
    passwordError.value = '两次输入的新密码不一致'
    return
  }
  passwordBusy.value = true
  try {
    store.profile = await api<Profile>('/auth/password', 'POST', {
      currentPassword: passwordForm.value.currentPassword,
      newPassword: passwordForm.value.newPassword,
    })
    passwordDialog.value = false
    passwordForm.value = { currentPassword: '', newPassword: '', confirmPassword: '' }
    notice('密码已更新')
  } catch (error) { passwordError.value = errorMessage(error) }
  finally { passwordBusy.value = false }
}

function handleCommand(cmd: string) {
  if (cmd === 'logout') void logout()
  else if (cmd === 'password') {
    passwordForm.value = { currentPassword: '', newPassword: '', confirmPassword: '' }
    passwordError.value = ''
    passwordDialog.value = true
  } else void switchRole(cmd as Role)
}
</script>

<template>
  <main v-if="!store.ready || bootError" class="center-screen">
    <el-empty :description="bootError || '正在连接工作台…'">
      <el-button v-if="bootError" type="primary" @click="boot">重新连接</el-button>
    </el-empty>
  </main>

  <main v-else-if="!store.profile" class="login-screen">
    <el-card class="login-card" shadow="always">
      <div class="brand login-brand">
        <span class="brand-mark">知</span>
        <strong>知维</strong>
      </div>
      <h1 class="login-title">账号登录</h1>
      <el-form label-position="top" @submit.prevent="signIn">
        <template v-if="enterprise">
          <el-form-item label="企业访问令牌" required>
            <el-input v-model="credentials.token" type="password" show-password :prefix-icon="Key" autocomplete="off" />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="账号" required>
            <el-input v-model="credentials.username" :prefix-icon="User" placeholder="admin" autocomplete="username" />
          </el-form-item>
          <el-form-item label="密码" required>
            <el-input v-model="credentials.password" type="password" show-password :prefix-icon="Lock" placeholder="输入密码" autocomplete="current-password" />
          </el-form-item>
          <el-form-item v-if="showOrg" label="组织">
            <el-input v-model="credentials.org" :prefix-icon="OfficeBuilding" />
          </el-form-item>
        </template>
        <el-alert v-if="loginError" :title="loginError" type="error" show-icon :closable="false" class="mb-16" />
        <el-button type="primary" native-type="submit" :loading="busy" size="large" class="w-full">登录</el-button>
        <div class="login-extra">
          <el-button v-if="!enterprise" link type="info" size="small" @click="showOrg = !showOrg">
            {{ showOrg ? '隐藏组织' : `组织：${credentials.org || store.defaultOrg}` }}
          </el-button>
          <el-button v-if="store.enterpriseIdentity" link type="primary" size="small" @click="enterprise = !enterprise">
            {{ enterprise ? '本地账号登录' : '企业身份登录' }}
          </el-button>
        </div>
      </el-form>
    </el-card>
  </main>

  <el-container v-else class="app-shell">
    <el-aside width="216px" class="app-aside">
      <RouterLink class="brand aside-brand" to="/">
        <span class="brand-mark">知</span>
        <strong>知维</strong>
      </RouterLink>
      <el-menu :default-active="activeMenu" router class="aside-menu">
        <el-menu-item v-for="item in links" :key="item.to" :index="item.to">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
      <div class="aside-foot">
        <el-icon><OfficeBuilding /></el-icon>
        <span>{{ store.profile.org }}</span>
      </div>
    </el-aside>

    <el-container direction="vertical" class="app-body">
      <el-header height="56px" class="app-header">
        <div class="header-left">
          <el-button class="mobile-menu-btn" text circle aria-label="打开导航" @click="mobileNav = true">
            <el-icon :size="18"><MenuIcon /></el-icon>
          </el-button>
          <span class="header-title">{{ route.meta.title }}</span>
        </div>
        <el-dropdown trigger="click" @command="handleCommand">
          <el-button round class="profile-trigger">
            <el-avatar :size="24" class="profile-avatar">{{ store.profile.name.slice(0, 1) }}</el-avatar>
            <span>{{ store.profile.name }}</span>
            <el-tag v-if="roleLabel(store.profile) !== store.profile.name" size="small" :type="store.profile.primary ? 'danger' : 'info'" effect="plain">{{ roleLabel(store.profile) }}</el-tag>
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <template v-if="store.profile.demo">
                <el-dropdown-item v-for="(label, key) in roleNames" :key="key" :command="key">
                  <span class="dropdown-role">
                    <span>{{ label }}</span>
                    <el-icon v-if="key === store.profile.role"><Check /></el-icon>
                  </span>
                </el-dropdown-item>
              </template>
              <el-dropdown-item v-if="!store.profile.demo" command="password">
                <el-icon><Key /></el-icon>
                <span>修改密码</span>
              </el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                <el-icon><SwitchButton /></el-icon>
                <span>退出登录</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main class="app-main">
        <RouterView v-slot="{ Component, route: viewRoute }">
          <Transition name="view-fade" mode="out-in">
            <component :is="Component" :key="store.profile.org + ':' + store.profile.username + ':' + viewRoute.matched[0]?.path" />
          </Transition>
        </RouterView>
      </el-main>
    </el-container>

    <el-drawer v-model="mobileNav" direction="ltr" size="240px" :with-header="false" body-class="drawer-nav-body">
      <div class="brand aside-brand">
        <span class="brand-mark">知</span>
        <strong>知维</strong>
      </div>
      <el-menu :default-active="activeMenu" router class="aside-menu" @select="mobileNav = false">
        <el-menu-item v-for="item in links" :key="item.to" :index="item.to">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-drawer>

    <el-dialog v-model="passwordDialog" title="修改密码" width="420px">
      <el-form label-position="top" @submit.prevent="submitPassword">
        <el-form-item label="当前密码" required>
          <el-input v-model="passwordForm.currentPassword" type="password" show-password autocomplete="current-password" />
        </el-form-item>
        <el-form-item label="新密码" required>
          <el-input v-model="passwordForm.newPassword" type="password" show-password autocomplete="new-password" placeholder="至少 6 个字符" />
        </el-form-item>
        <el-form-item label="确认新密码" required>
          <el-input v-model="passwordForm.confirmPassword" type="password" show-password autocomplete="new-password" />
        </el-form-item>
        <el-alert v-if="passwordError" :title="passwordError" type="error" show-icon :closable="false" />
      </el-form>
      <template #footer>
        <el-button @click="passwordDialog = false">取消</el-button>
        <el-button type="primary" :loading="passwordBusy" @click="submitPassword">保存密码</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>
