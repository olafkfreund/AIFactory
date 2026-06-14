import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

// Tests must run against React's DEVELOPMENT build. When NODE_ENV is "production"
// (it is in CI and some shells), vite/@vitejs/plugin-react load React's
// production build, which strips `act` — so @testing-library/react's act shim
// fails with "React.act is not a function" and every render-based test errors.
// Force a non-production env for the test run so the dev build (with `act`) loads.
if (process.env.NODE_ENV === 'production') {
  process.env.NODE_ENV = 'test';
}

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
