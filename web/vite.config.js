import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `npm run dev` proxies to the agent running locally on 8765.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
      '/upload': 'http://127.0.0.1:8765',
      '/voice': 'http://127.0.0.1:8765',
      '/files': 'http://127.0.0.1:8765',
    },
  },
});
