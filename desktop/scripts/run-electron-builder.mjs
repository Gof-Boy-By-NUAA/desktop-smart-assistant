#!/usr/bin/env node
// Canonical local electron-builder invocation for `npm run dist`.
//
// A bare `electron-builder` in npm scripts is wrong on Windows twice over:
//   1. PATHEXT resolution: desktop/electron-builder.js shadows the npm shim
//      and the wrong program runs (fixed separately in every dist script).
//   2. Effective config: the implicit package.json "build" field carries
//      NEITHER the Windows signing hook NOR the rg.exe resource — only
//      electron-builder.win.js wires both. Without it, the README-recommended
//      `npm run dist` silently produces artifacts that differ from dist:win.
//
// This helper mirrors CI release.yml: on Windows it appends
// `--config electron-builder.win.js` (absolute path, so cwd does not matter);
// every other platform keeps the implicit config unchanged (the mac config
// stays a dist:mac/CI concern).
//
// Passthrough rules follow the ACTUAL parser (yargs), not literal spellings.
// Policy on Windows: EVERY explicit entrance to the config option is rejected
// loudly — electron-builder honors only the last --config, so appending ours
// after the user's would silently drop theirs.
//   - Long form and its alias 'c' (builder.js): --config VALUE, --config=V,
//     --c VALUE, --c=V; negation --no-config / --no-c (config=false, would be
//     overridden by our append); dot-notation --config.k=v / --c.k=v (would
//     merge into a config object). Other long options starting with 'c'
//     (e.g. --cscLink) are NOT config and must not be flagged.
//   - A single-dash token is a SHORT FLAG GROUP that yargs splits per
//     character — 'c' is the config option wherever it appears (-c, -c=…,
//     -wc@…, -mwc=…, -wlc/…; separators like =, @, / additionally glue the
//     remainder as the value, other trailing letters become further flags —
//     the config flag is set either way). So every single-dash token
//     containing 'c' is treated as an explicit config.
//   - Everything after a bare `--` is positional to yargs: config flags there
//     are not config (never rejected), and our own --config is inserted
//     BEFORE the separator — appended after `--` the parser would silently
//     drop it too, stripping the signing hook and rg.exe from the artifact.

import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

// Matches --config / --c in value, negated and dot-notation forms; does NOT
// match longer c-options like --cscLink ("c" must end or be followed by
// '.', '=' or the full "onfig").
const LONG_CONFIG_RE = /^--(no-)?c(onfig)?([.=]|$)/

function findExplicitConfigFlag(argv) {
  for (const arg of argv) {
    if (arg === '--') return null // positional-only from here on
    if (LONG_CONFIG_RE.test(arg)) return arg
    // Short flag group: per-char flags, 'c' = config wherever it appears.
    if (arg.startsWith('-') && !arg.startsWith('--') && arg.length > 1 && arg.includes('c')) {
      return arg
    }
  }
  return null
}

export function buildElectronBuilderArgs(platform, passthrough) {
  // Split at the first bare `--`: options before it, positionals after it.
  const sepIndex = passthrough.indexOf('--')
  const options = sepIndex === -1 ? passthrough : passthrough.slice(0, sepIndex)
  const tail = sepIndex === -1 ? [] : passthrough.slice(sepIndex)

  const args = [...options]
  if (platform === 'win32') {
    const explicit = findExplicitConfigFlag(passthrough)
    if (explicit) {
      throw new Error(
        `${explicit} cannot be passed through: this entry appends its own ` +
        `--config electron-builder.win.js on Windows, and electron-builder ` +
        `honors only the last --config, so yours would be silently ignored. ` +
        `Merge your settings into electron-builder.win.js, or invoke ` +
        `node_modules/electron-builder/cli.js directly.`
      )
    }
    const winConfig = fileURLToPath(new URL('../electron-builder.win.js', import.meta.url))
    args.push('--config', winConfig)
  }
  return [...args, ...tail]
}

function main() {
  const cli = fileURLToPath(new URL('../node_modules/electron-builder/cli.js', import.meta.url))
  let args
  try {
    args = buildElectronBuilderArgs(process.platform, process.argv.slice(2))
  } catch (e) {
    console.error(String(e.message))
    process.exit(1)
  }
  const result = spawnSync(process.execPath, [cli, ...args], { stdio: 'inherit' })
  process.exit(result.status ?? 1)
}

// Import guard: tests import buildElectronBuilderArgs directly without
// triggering a build.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
}
