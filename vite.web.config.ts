import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { execFileSync } from 'node:child_process';

function buildCommit(): string {
  if (process.env.KNOWE_BUILD_COMMIT?.trim()) return process.env.KNOWE_BUILD_COMMIT.trim();
  try {
    const options = { cwd: __dirname, encoding: 'utf8' as const, stdio: 'pipe' as const };
    const revision = execFileSync('git', ['rev-parse', '--verify', 'HEAD'], options).trim();
    const changes = execFileSync('git', ['status', '--porcelain', '--untracked-files=no'], options).trim();
    return revision + (changes ? '-dirty' : '');
  } catch {
    return '';
  }
}

export default defineConfig({
  plugins: [react()],
  define: { __KNOWE_BUILD_COMMIT__: JSON.stringify(buildCommit()) },
  base: '/',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8090', ws: true },
      '/healthz': 'http://127.0.0.1:8090',
    },
  },
});
