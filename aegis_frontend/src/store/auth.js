import { defineStore } from 'pinia'
import { getProfile, initializeOidc, keycloak, login, logout } from '../utils/oidc'
import { clearSessionMemory } from '../api/sessionMemory'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    initialized: false,
    authenticated: false,
    profile: null,
  }),
  actions: {
    async initialize() {
      this.authenticated = await initializeOidc()
      this.initialized = true
      this.profile = this.authenticated ? getProfile() : null
      return this.authenticated
    },
    async signIn() {
      await login()
    },
    async signOut() {
      try {
        await clearSessionMemory()
      } catch {
        // Redis/API不可达时仍允许OIDC退出；缓存不具备授权能力并有TTL。
        console.warn('退出时工作缓存清理未完成，将按TTL过期。')
      }
      await logout()
      this.authenticated = false
      this.profile = null
    },
    isAuthenticated() {
      return this.authenticated && keycloak.authenticated === true
    },
  },
})
