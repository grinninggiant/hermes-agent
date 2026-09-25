// Self-contained offline entrypoints; no Electron startup or node_modules at execution.
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
const root = new URL('../', import.meta.url)
for (const name of ['bundle-offline', 'runtime-offline']) {
  await build({
    entryPoints: [fileURLToPath(new URL(`scripts/${name}.ts`, root))],
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node20',
    outfile: fileURLToPath(new URL(`dist/${name}.mjs`, root)),
    logLevel: 'info'
  })
}
