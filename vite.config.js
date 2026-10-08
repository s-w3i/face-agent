import { defineConfig } from 'vite';

const isolation = {
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Embedder-Policy': 'require-corp',
};
export default defineConfig({
  server: { headers: isolation, proxy: { '/api': { target: 'http://127.0.0.1:5174' }, '/prerendered': { target: 'http://127.0.0.1:5174' } } },
  preview: { headers: isolation },
  build: { rollupOptions: { input: { studio: 'index.html', original: 'original-dots.html', robot: 'robot.html' } } },
});
