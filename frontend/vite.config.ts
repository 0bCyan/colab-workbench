import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Preserve the browser-facing Host so the backend can validate Origin on any dev port.
  server: { proxy: Object.fromEntries(['/api', '/llm', '/reference'].map(path =>
    [path, { target: 'http://127.0.0.1:8765', changeOrigin: false }])) },
})
