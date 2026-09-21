import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'

const exec = promisify(execFile)
const entry = fileURLToPath(new URL('../src/cli.js', import.meta.url))
let temporary
let binary

before(async () => {
  temporary = await mkdtemp(path.join(os.tmpdir(), 'gptgrep-wrapper-'))
  binary = path.join(temporary, 'native-fixture')
  await writeFile(binary, `#!/usr/bin/env node
const args = process.argv.slice(2);
if (args.includes('empty-result')) {
  process.stdout.write(JSON.stringify({hits: []})); process.exitCode = 1;
} else if (args.includes('native-error')) {
  process.stdout.write(JSON.stringify({error: 'failed', hits: []}));
  process.stderr.write('fixture diagnostic'); process.exitCode = 1;
} else process.stdout.write(JSON.stringify({args}));
`)
  await chmod(binary, 0o755)
})

after(async () => {
  await rm(temporary, { recursive: true, force: true })
})

function run(args, executable = binary) {
  return exec(process.execPath, [entry, ...args], {
    env: { ...process.env, GPTGREP_BIN: executable, NO_UPDATE_NOTIFIER: '1' },
  })
}

test('reserved flag-like query stays in native positional arguments', async () => {
  for (const query of ['--mcp', '--json', '--schema', 'search', '$(touch sentinel)']) {
    const { stdout } = await run(['search', '--request', JSON.stringify({ query, root: '/docs space', mode: 'lexical', limit: 7 }), '--json'])
    assert.deepEqual(JSON.parse(stdout).args,
      ['search', '--mode', 'lexical', '--limit', '7', '--context', '0', '--min-score', '0.5', '--json', '--', query, '/docs space'])
  }
})

test('all supported operations forward explicit native arguments', async () => {
  for (const [command, request, expected] of [
    ['index', { root: '/docs' }, ['index', '--json', '--', '/docs']],
    ['index', { root: '/docs', optimizeMerge: true }, ['index', '--optimize-merge', '--json', '--', '/docs']],
    ['status', { root: '/docs' }, ['status', '--json', '--', '/docs']],
    ['tree', { root: '/docs', file: 'a.pdf' }, ['tree', '--root=/docs', '--json', '--', 'a.pdf']],
    ['doctor', {}, ['doctor', '--json']],
  ]) {
    const { stdout } = await run([command, '--request', JSON.stringify(request), '--json'])
    assert.deepEqual(JSON.parse(stdout).args, expected)
  }
})

test('default search selects offline regex with native limits and rubric defaults', async () => {
  const result = JSON.parse((await run(['search', '--request', '{"query":"hello"}', '--json'])).stdout)
  assert.deepEqual(result.args, ['search', '--mode', 'regex', '--limit', '20', '--context', '0', '--min-score', '0.5', '--json', '--', 'hello', '.'])
  const semantic = JSON.parse((await run(['search', '--request', '{"query":"hello","mode":"semantic","minScore":0.75}', '--json'])).stdout)
  assert.equal(semantic.args[semantic.args.indexOf('--min-score') + 1], '0.75')
  for (const request of [{ query: 'hello', limit: 1001 }, { query: 'hello', minScore: 1.01 }]) {
    await assert.rejects(run(['search', '--request', JSON.stringify(request), '--json']),
      (error) => /INVALID_REQUEST/.test(error.stdout))
  }
})

test('schema discovery is local and exposes the JSON request contract', async () => {
  const { stdout } = await run(['request-schema', 'search', '--json'], '/missing/native')
  const schema = JSON.parse(stdout)
  assert.equal(schema.type, 'object')
  assert.equal(schema.properties.query.type, 'string')
  assert.deepEqual(schema.properties.mode.enum, ['regex', 'lexical', 'hybrid', 'semantic'])
  assert.equal(schema.properties.mode.default, 'regex')
  assert.equal(schema.properties.context.default, 0)
  const outer = JSON.parse((await run(['search', '--schema', '--json'], '/missing/native')).stdout)
  assert.equal(outer.options.properties.request.type, 'string')
  assert.equal(outer.env.properties.GPTGREP_BIN.type, 'string')
  const manifest = JSON.parse((await run(['--llms', '--format', 'json'], '/missing/native')).stdout)
  assert.ok(JSON.stringify(manifest).includes('search'))
})

test('explicit Codex host commands preserve model settings and flag-like data', async () => {
  const defaults = ['--codex-bin=codex', '--model=gpt-5.6-luna', '--reasoning-effort=max', '--timeout', '180', '--max-tool-calls', '12']
  const ask = JSON.parse((await run(['ask', '--request', '{"question":"--mcp","root":"/docs space"}', '--json'])).stdout)
  assert.deepEqual(ask.args, ['ask', ...defaults, '--json', '--', '--mcp', '/docs space'])
  const summary = JSON.parse((await run(['summarize', '--request', '{"nodeId":"document:node","root":"/docs","codexHome":"/account space"}', '--json'])).stdout)
  assert.deepEqual(summary.args, ['summarize', '--root=/docs', ...defaults, '--codex-home=/account space', '--json', '--', 'document:node'])
  const schema = JSON.parse((await run(['request-schema', 'ask', '--json'])).stdout)
  assert.equal(schema.properties.model.default, 'gpt-5.6-luna')
  assert.equal(schema.properties.reasoningEffort.default, 'max')
})

test('MCP and update routes are rejected before framework dispatch', async () => {
  for (const args of [['--mcp'], ['mcp', 'add'], ['--json', 'mcp', 'add'], ['skills', 'add'], ['--update']]) {
    await assert.rejects(run(args), (error) => {
      assert.equal(error.code, 2)
      assert.match(error.stderr, /MCP, skill sync and wrapper updates are disabled/)
      return true
    })
  }
  const help = (await run(['--help'])).stdout
  assert.doesNotMatch(help, /--mcp|--update|Register as MCP|Sync skill/)
})

test('invalid requests and missing native executable are actionable errors', async () => {
  await assert.rejects(run(['search', '--request', '{"query":"hi","limit":0}', '--json']),
    (error) => /INVALID_REQUEST/.test(error.stdout))
  await assert.rejects(run(['search', '--request', '{"query":"hi"}', '--json'], '/missing/native'),
    (error) => /NATIVE_ERROR/.test(error.stdout) && /not found/.test(error.stdout))
})

test('no-match exit is successful while native errors stay failures', async () => {
  const empty = await run(['search', '--request', '{"query":"empty-result"}', '--json'])
  assert.deepEqual(JSON.parse(empty.stdout), { hits: [] })
  await assert.rejects(run(['search', '--request', '{"query":"native-error"}', '--json']),
    (error) => /NATIVE_ERROR/.test(error.stdout) && /fixture diagnostic/.test(error.stderr))
})
