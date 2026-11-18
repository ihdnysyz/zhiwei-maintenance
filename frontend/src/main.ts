import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import Home from './Home.vue'
import Ask from './Ask.vue'
import Library from './Library.vue'
import Drafts from './Drafts.vue'
import Expert from './Expert.vue'
import Admin from './Admin.vue'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Home, meta: { title: '概览' } },
    { path: '/ask/:id?', component: Ask, meta: { title: '智能问答' } },
    { path: '/library', component: Library, meta: { title: '知识库' } },
    { path: '/drafts/:id?', component: Drafts, meta: { title: '工单草稿' } },
    { path: '/expert', component: Expert, meta: { title: '专家协作' } },
    { path: '/admin', component: Admin, meta: { title: '系统管理' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
createApp(App).use(router).mount('#app')

