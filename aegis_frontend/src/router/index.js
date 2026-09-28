import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../store/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/login' },
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue'), meta: { public: true } },
    { path: '/auth/callback', name: 'auth-callback', component: () => import('../views/AuthCallbackView.vue'), meta: { public: true } },
    { path: '/tasks', name: 'tasks', component: () => import('../views/TaskConsoleView.vue') },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('../views/NotFoundView.vue'), meta: { public: true } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 登录页不需要 Keycloak 预检；这样首次访问或 OIDC 服务暂不可用时，
  // 用户仍能看到明确的登录入口，而不是被误导到回调错误页。
  if (to.name === 'login') return true

  try {
    const authenticated = await auth.initialize()
    if (to.name === 'auth-callback') return authenticated ? { name: 'tasks' } : { name: 'login' }
    if (!to.meta.public && !authenticated) return { name: 'login' }
  } catch {
    // 受保护页面与无效回调均安全降级到登录页。
    return { name: 'login' }
  }

  return true
})

export default router
