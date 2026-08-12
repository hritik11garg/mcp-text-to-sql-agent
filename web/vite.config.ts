/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * The dev server proxies `/v1` and `/health` to the API instead of the browser
 * calling `http://127.0.0.1:8000` directly.
 *
 * That is a security decision, not a convenience. A direct call is
 * cross-origin, so it would only work if `API_CORS_ORIGINS` listed the dev
 * server -- and `APISettings` keeps that empty on purpose, because the API has
 * no authentication and every entry in that list is an origin allowed to drive
 * it from someone's browser. Proxying means the page only ever talks to its own
 * origin, so the closed default stays closed and development needs no
 * exception to it. Production has the same property for a different reason:
 * FastAPI serves the built files itself, so there is one origin there too.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
        // Compression would buffer the event stream, which is the one thing
        // this UI exists to show arriving in pieces.
        //
        // Declared rather than hooked. This was a `configure` callback reaching
        // for `proxy.on('proxyReq', ...)` until Vite 7 swapped its proxy
        // implementation and the handle stopped being an EventEmitter of that
        // shape. `headers` is the option the proxy has always documented for
        // exactly this, it is typed, and it does not reach past the interface
        // into the library behind it -- which is why the swap broke the old
        // form and cannot break this one.
        headers: { 'accept-encoding': 'identity' },
      },
      '/health': { target: 'http://127.0.0.1:8000' },
      '/ready': { target: 'http://127.0.0.1:8000' },
    },
  },
  build: {
    outDir: 'dist',
    // Every script and style is an external file with its own URL, so the
    // Content-Security-Policy the API serves can be `script-src 'self'` with no
    // `unsafe-inline`. Vite inlines small assets by default; 0 turns that off.
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test-setup.ts'],
  },
});
