import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base: './' makes built asset paths relative so they load from file:// inside
// the Capacitor Android WebView. server.host exposes the dev server on the LAN
// so you can also test from a phone browser before the APK exists.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: { host: true, port: 5173 },
})
