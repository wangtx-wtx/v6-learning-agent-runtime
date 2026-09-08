/**
 * vue-router（方案 13.1）：hash 历史（兼容既有 #/ 链接），视图全部动态导入分包。
 */
import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'dashboard', component: () => import('../components/DashboardPage.vue') },
  { path: '/courses', name: 'courses', component: () => import('../components/CoursesPage.vue') },
  { path: '/chapters', name: 'chapters', component: () => import('../components/ChaptersPage.vue') },
  { path: '/upload', name: 'upload', component: () => import('../components/UploadPage.vue') },
  { path: '/lesson-flow', name: 'lesson-flow', component: () => import('../components/LessonFlowPage.vue') },
  { path: '/homework-flow', name: 'homework-flow', component: () => import('../components/HomeworkFlowPage.vue') },
  { path: '/errors', name: 'errors', component: () => import('../components/ErrorsPage.vue') },
  { path: '/review', name: 'review', component: () => import('../components/ReviewPage.vue') },
  { path: '/graph', name: 'graph', component: () => import('../components/GraphPage.vue') },
  { path: '/models', name: 'models', component: () => import('../components/ModelsPage.vue') },
  { path: '/sync', name: 'sync', component: () => import('../components/SyncPage.vue') },
  { path: '/runs', name: 'runs', component: () => import('../components/RunsPage.vue') },
  { path: '/m-upload', name: 'm-upload', component: () => import('../components/MobileUploadPage.vue') },
  { path: '/m-token', name: 'm-token', component: () => import('../components/MobileTokenPage.vue') },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('../components/NotFoundPage.vue') },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})
