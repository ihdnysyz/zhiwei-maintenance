import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import App from './App.vue'
import Home from './views/Home.vue'
import Ask from './views/Ask.vue'
import Library from './views/Library.vue'
import Drafts from './views/Drafts.vue'
import Expert from './views/Expert.vue'
import Admin from './views/Admin.vue'
import { notice, store, type Role } from './store'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: Home, meta: { title: '登录', public: true } },
    { path: '/', component: Home, meta: { title: '概览' } },
    { path: '/ask/:id?', component: Ask, meta: { title: '智能问答' } },
    { path: '/library', component: Library, meta: { title: '知识库' } },
    { path: '/drafts/:id?', component: Drafts, meta: { title: '工单草稿' } },
    { path: '/expert', component: Expert, meta: { title: '专家协作' } },
    { path: '/admin', component: Admin, meta: { title: '系统管理', roles: ['platform'] as Role[] } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(to => {
  if (!store.ready) return true
  if (!store.profile) {
    if (to.path === '/login') return true
    return { path: '/login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : undefined }
  }
  if (to.path === '/login') return '/'
  const allowed = to.meta.roles as Role[] | undefined
  if (allowed && !allowed.includes(store.profile.role)) {
    notice('当前角色没有此页面访问权限', true)
    return '/'
  }
  return true
})

createApp(App).use(ElementPlus, { locale: zhCn }).use(router).mount('#app')
