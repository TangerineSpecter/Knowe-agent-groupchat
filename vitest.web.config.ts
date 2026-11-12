import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  server: { hmr: false },
  test: {
    environment: 'jsdom', setupFiles: ['./src/test-setup.ts'],
    include: ['src/web/**/*.test.tsx', 'src/components/FirstRunModelGate.test.tsx',
      'src/store/completionProjection.test.ts', 'src/transport/socket.wars.test.ts'],
  },
});
