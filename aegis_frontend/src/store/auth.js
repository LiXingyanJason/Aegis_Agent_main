import { defineStore } from 'pinia'
import { getProfile, initializeOidc, keycloak, login, logout } from '../utils/oidc'

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
      await logout()
      this.authenticated = false
      this.profile = null
    },
    isAuthenticated() {
      return this.authenticated && keycloak.authenticated === true
    },
  },
})
