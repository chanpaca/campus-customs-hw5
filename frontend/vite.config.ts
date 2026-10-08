import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

// Resolve env files next to this config, not from the launcher's cwd.
const HERE = dirname(fileURLToPath(import.meta.url))

// The Campus Customs agent desk talks to the FastAPI backend in ../backend.
// Calls go to same-origin /api/* and are proxied, so the browser never has to
// care which port the API is on and CORS stays a backend concern rather than a
// frontend workaround.
//
// VITE_API_TARGET overrides the backend address (handy when 8000 is occupied).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, HERE, '')
  const apiTarget = env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target: apiTarget, changeOrigin: true },
      },
    },
  }
})
