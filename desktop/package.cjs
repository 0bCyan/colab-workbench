const { packager } = require('@electron/packager')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')

packager({
  dir: __dirname, name: 'Fieldwork', platform: 'darwin', arch: 'arm64',
  appBundleId: 'cn.moebit.fieldwork.local', appVersion: require('./package.json').version,
  out: path.join(__dirname, 'dist', 'app'), overwrite: true, asar: true,
  extraResource: ['python', 'site', 'workbench'].map(name => path.join(__dirname, 'dist', 'resources', name)),
  ignore: (file) => !!file && !/^\/(main\.cjs|offline\.html|package\.json)$/.test(file),
}).then(paths => {
  for (const output of paths) {
    const app = path.join(output, 'Fieldwork.app')
    const source = path.join(__dirname, 'dist', 'resources')
    const target = path.join(app, 'Contents', 'Resources')
    function relink(directory) {
      for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
        const file = path.join(directory, item.name)
        if (item.isSymbolicLink()) {
          const link = fs.readlinkSync(file)
          if (path.isAbsolute(link) && link.startsWith(source + path.sep)) {
            const destination = path.join(target, path.relative(source, link))
            fs.unlinkSync(file)
            fs.symlinkSync(path.relative(path.dirname(file), destination), file)
          }
        } else if (item.isDirectory()) relink(file)
      }
    }
    relink(target)
    execFileSync('codesign', ['--force', '--deep', '--sign', '-', app], { stdio: 'inherit' })
    execFileSync('codesign', ['--verify', '--deep', '--strict', app], { stdio: 'inherit' })
  }
  console.log(paths.join('\n'))
}).catch(error => { console.error(error); process.exit(1) })
