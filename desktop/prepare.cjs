const fs = require('node:fs')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const root = path.resolve(__dirname, '..')
const resources = path.join(__dirname, 'dist', 'resources')
const run = (file, args) => execFileSync(file, args, { cwd: root, stdio: 'inherit' })

if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('This build targets the current Apple Silicon Mac.')
run('npm', ['--prefix', 'frontend', 'run', 'build'])
const python = execFileSync('uv', ['run', '--no-sync', 'python', '-c', 'import sys;print(sys.base_prefix)'], { cwd: root, encoding: 'utf8' }).trim()
fs.rmSync(resources, { recursive: true, force: true })
fs.mkdirSync(resources, { recursive: true })
fs.cpSync(python, path.join(resources, 'python'), { recursive: true, verbatimSymlinks: true,
  filter: source => !['__pycache__', 'test', 'tests', 'idlelib', 'tkinter'].includes(path.basename(source)) })
for (const directory of ['backend', 'templates']) {
  fs.cpSync(path.join(root, directory), path.join(resources, 'workbench', directory), { recursive: true,
    filter: source => !source.includes('__pycache__') && (fs.statSync(source).isDirectory()
      || (directory === 'backend' ? source.endsWith('.py') : source.endsWith('.json'))) })
}
fs.cpSync(path.join(root, 'frontend', 'dist'), path.join(resources, 'workbench', 'frontend', 'dist'), { recursive: true })
const requirements = path.join(__dirname, 'dist', 'requirements.txt')
run('uv', ['export', '--frozen', '--no-dev', '--no-emit-project', '--quiet', '--output-file', requirements])
run('uv', ['pip', 'install', '--python', path.join(resources, 'python', 'bin', 'python3.12'),
  '--target', path.join(resources, 'site'), '--require-hashes', '-r', requirements])
console.log('Bundled local backend, Python and runtime dependencies. No runtime data or account files included.')
