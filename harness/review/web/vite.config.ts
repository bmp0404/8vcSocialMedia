import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../dist', emptyOutDir: true, assetsDir: 'assets' },
  css: {
    modules: {
      // react-tweet styles its parts with CSS modules. Stable names (rt-<file>-<class>) let styles.css hide
      // the parts this page leaves out, such as the Follow link, without depending on a build hash.
      generateScopedName: (name, filename) => `rt-${filename.split(/[\\/]/).pop()!.replace(/\.module\.css.*$/, '')}-${name}`,
    },
  },
  // `pnpm dev` serves the page on Vite's port and sends API calls to `harness review` on 8765.
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
  test: { environment: 'node', env: { TZ: 'UTC' }, include: ['src/**/*.test.ts'] },
})
