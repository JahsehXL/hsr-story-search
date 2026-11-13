import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist', emptyOutDir: true,
    rollupOptions: { output: { chunkFileNames: chunk => chunk.name === 'Admin' ? 'assets/admin-[hash].js' : 'assets/[name]-[hash].js' } },
  },
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
});
