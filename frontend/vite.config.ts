import { defineConfig } from 'vite'
import path from 'path'
import { fileURLToPath } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import hostingConfig from './.openai/hosting.json'
import { sites } from './build/sites-vite-plugin'

const SITE_CREATOR_PLACEHOLDER_DATABASE_ID = '00000000-0000-4000-8000-000000000000'
const { d1, r2 } = hostingConfig

const localBindingConfig = {
  main: './worker/index.ts',
  compatibility_flags: ['nodejs_compat'],
  assets: {
    binding: 'ASSETS',
    not_found_handling: 'single-page-application' as const,
    run_worker_first: true,
  },
  d1_databases: d1
    ? [
        {
          binding: d1,
          database_name: 'graphrag-studio-d1',
          database_id: SITE_CREATOR_PLACEHOLDER_DATABASE_ID,
        },
      ]
    : [],
  r2_buckets: r2
    ? [
        {
          binding: r2,
          bucket_name: 'graphrag-studio-r2',
        },
      ]
    : [],
}

export default defineConfig(async () => {
  // Vercel's Clerk integration exposes the framework-neutral public key under
  // NEXT_PUBLIC_*. Mirror it into Vite's public build namespace. Sites may
  // still provide VITE_CLERK_PUBLISHABLE_KEY directly for its static shell.
  process.env.VITE_CLERK_PUBLISHABLE_KEY ??= process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
  process.env.WRANGLER_WRITE_LOGS ??= 'false'
  process.env.WRANGLER_LOG_PATH ??= '.wrangler/logs'
  process.env.MINIFLARE_REGISTRY_PATH ??= '.wrangler/registry'

  const { cloudflare } = await import('@cloudflare/vite-plugin')

  return {
    plugins: [
      // The React and Tailwind plugins are both required for Make, even if
      // Tailwind is not being actively used – do not remove them
      react(),
      tailwindcss(),
      sites(),
      cloudflare({
        viteEnvironment: { name: 'server' },
        config: localBindingConfig,
      }),
    ],
    resolve: {
      alias: {
        // Alias @ to the src directory
        '@': path.resolve(fileURLToPath(new URL('.', import.meta.url)), './src'),
      },
    },

    // File types to support raw imports. Never add .css, .tsx, or .ts files to this.
    assetsInclude: ['**/*.svg', '**/*.csv'],
  }
})
