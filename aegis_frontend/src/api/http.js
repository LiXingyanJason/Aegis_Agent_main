import axios from 'axios'
import { getAccessToken } from '../utils/oidc'

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: 15_000,
  headers: { Accept: 'application/json' },
})

http.interceptors.request.use(async (config) => {
  const token = await getAccessToken()
  if (!token) {
    return Promise.reject(new Error('登录已失效，请重新登录。'))
  }
  config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      window.dispatchEvent(new Event('aegis:unauthorized'))
    }
    return Promise.reject(error)
  },
)

export default http
