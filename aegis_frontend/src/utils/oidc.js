import Keycloak from 'keycloak-js'

const authority = import.meta.env.VITE_OIDC_AUTHORITY
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID
const redirectUri = import.meta.env.VITE_OIDC_REDIRECT_URI || `${window.location.origin}/auth/callback`

function keycloakConfiguration() {
  if (!authority || !clientId) {
    throw new Error('缺少 VITE_OIDC_AUTHORITY 或 VITE_OIDC_CLIENT_ID 配置。')
  }

  const url = new URL(authority)
  const realmMatch = url.pathname.match(/^(.*)\/realms\/([^/]+)\/?$/)
  if (!realmMatch) {
    throw new Error('VITE_OIDC_AUTHORITY 必须是以 /realms/{realm} 结尾的 Keycloak issuer 地址。')
  }

  return {
    url: `${url.origin}${realmMatch[1]}`,
    realm: decodeURIComponent(realmMatch[2]),
    clientId,
  }
}

export const keycloak = new Keycloak(keycloakConfiguration())
let initialized = false
let initializationPromise = null

export async function initializeOidc({ checkSso = true } = {}) {
  if (initialized) return keycloak.authenticated === true
  if (!initializationPromise) {
    const options = {
      onLoad: 'check-sso',
      pkceMethod: 'S256',
      checkLoginIframe: false,
      redirectUri,
    }

    // 登录按钮本身会立即发起交互式登录。此处若先执行 check-sso，
    // 未登录用户可能经历一次额外的 OIDC 往返，使首次点击只完成预检。
    if (!checkSso) delete options.onLoad

    initializationPromise = keycloak.init(options).then((authenticated) => {
      initialized = true
      return authenticated
    }).catch((error) => {
      initializationPromise = null
      throw error
    })
  }
  return initializationPromise
}

export async function login() {
  await initializeOidc({ checkSso: false })
  return keycloak.login({ redirectUri, scope: 'openid profile email' })
}

export async function logout() {
  if (initialized && keycloak.authenticated) {
    await keycloak.logout({ redirectUri: window.location.origin })
  }
}

export async function getAccessToken() {
  await initializeOidc()
  if (!keycloak.authenticated) return null
  try {
    await keycloak.updateToken(30)
    return keycloak.token ?? null
  } catch {
    window.dispatchEvent(new Event('aegis:unauthorized'))
    return null
  }
}

export function getProfile() {
  const claims = keycloak.tokenParsed
  if (!claims) return null
  return {
    name: claims.name || claims.preferred_username || claims.email || '已登录用户',
    email: claims.email || '',
  }
}
