const { app, BrowserWindow, Menu, dialog, session, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const net = require('node:net')
const http = require('node:http')
const { pathToFileURL } = require('node:url')

const PORT = 18767
const ORIGIN = `http://127.0.0.1:${PORT}`
const assets = app.isPackaged ? process.resourcesPath : path.join(__dirname, 'dist', 'resources')
const offline = pathToFileURL(path.join(__dirname, 'offline.html')).href
let window, backend, log, quitting = false, backendExited = false

function localURL(url) {
  try { return new URL(url).origin === ORIGIN } catch { return false }
}

function healthy() {
  return new Promise(resolve => {
    const request = http.get(`${ORIGIN}/api/auth/session`, { timeout: 1000 }, response => {
      response.resume(); resolve(response.statusCode === 200)
    })
    request.on('timeout', () => request.destroy())
    request.on('error', () => resolve(false))
  })
}

async function startBackend() {
  // Never attach a window to an unrelated process already using the desktop port.
  await new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.once('error', () => reject(new Error(`本地端口 ${PORT} 已被占用，请关闭占用它的程序后重试。`)))
    probe.listen(PORT, '127.0.0.1', () => probe.close(resolve))
  })
  const directory = path.join(app.getPath('userData'), 'data')
  const logs = app.getPath('logs')
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
  fs.mkdirSync(logs, { recursive: true, mode: 0o700 })
  const logPath = path.join(logs, 'backend.log')
  if (fs.existsSync(logPath) && fs.statSync(logPath).size > 5 * 1024 * 1024) {
    fs.renameSync(logPath, `${logPath}.previous`)
  }
  log = fs.openSync(logPath, 'a', 0o600)
  const environment = { ...process.env,
    PATH: `${path.join(app.getPath('home'), '.local', 'bin')}:${process.env.PATH || '/usr/bin:/bin'}`,
    PYTHONHOME: path.join(assets, 'python'), PYTHONPATH: path.join(assets, 'site'),
    PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1',
    WORKBENCH_DATA: directory, WORKBENCH_AUTH_REQUIRED: 'false', WORKBENCH_ALLOWED_HOSTS: '',
  }
  delete environment.WORKBENCH_SSH_IDENTITY
  backend = spawn(path.join(assets, 'python', 'bin', 'python3.12'), [
    '-m', 'uvicorn', 'backend.app:app', '--app-dir', path.join(assets, 'workbench'),
    '--host', '127.0.0.1', '--port', String(PORT), '--timeout-graceful-shutdown', '420', '--no-access-log',
  ], { env: environment, cwd: path.join(assets, 'workbench'), stdio: ['ignore', log, log] })
  let spawnError
  backend.once('error', error => { spawnError = error })
  backend.once('exit', () => {
    backendExited = true
    if (!quitting && window && !window.isDestroyed()) window.loadFile(path.join(__dirname, 'offline.html'))
  })
  const deadline = Date.now() + 30000
  while (Date.now() < deadline) {
    if (spawnError) throw spawnError
    if (backendExited) throw new Error('本地工作台启动失败，请查看“帮助 → 后端日志”。')
    if (await healthy()) return
    await new Promise(resolve => setTimeout(resolve, 150))
  }
  throw new Error('本地工作台启动超时，请查看“帮助 → 后端日志”。')
}

function createWindow() {
  const profile = session.fromPartition('persist:fieldwork-local')
  profile.setPermissionRequestHandler((contents, permission, callback) => {
    callback(localURL(contents.getURL()) && permission === 'clipboard-sanitized-write')
  })
  profile.setPermissionCheckHandler((contents, permission) => !!contents && localURL(contents.getURL()) && permission === 'clipboard-sanitized-write')
  profile.on('will-download', (_event, item) => {
    item.setSaveDialogOptions({ defaultPath: path.join(app.getPath('downloads'), path.basename(item.getFilename())) })
  })
  window = new BrowserWindow({ width: 1440, height: 900, minWidth: 760, minHeight: 580,
    title: 'Fieldwork', backgroundColor: '#f3f5ef', show: false,
    webPreferences: { session: profile, nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true },
  })
  window.once('ready-to-show', () => window.show())
  window.on('closed', () => { window = null })
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (localURL(url)) window.loadURL(url)
    else if (/^https?:\/\//i.test(url)) shell.openExternal(url).catch(() => {})
    return { action: 'deny' }
  })
  window.webContents.on('will-navigate', (event, url) => {
    if (localURL(url) || url === offline) return
    event.preventDefault()
    if (/^https?:\/\//i.test(url)) shell.openExternal(url).catch(() => {})
  })
  window.webContents.on('did-fail-load', (_event, code, _description, _url, mainFrame) => {
    if (mainFrame && code !== -3 && !quitting) window.loadFile(path.join(__dirname, 'offline.html'))
  })
  window.loadURL(ORIGIN)
}

function menu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'Fieldwork', submenu: [{ role: 'about' }, { type: 'separator' }, { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' }, { role: 'quit' }] },
    { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: '显示', submenu: [
      { label: '刷新工作台', accelerator: 'CmdOrCtrl+R', click: () => window?.loadURL(ORIGIN) },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'togglefullscreen' },
    ] },
    { role: 'windowMenu' },
    { label: '帮助', submenu: [
      { label: '在浏览器打开本地工作台', click: () => shell.openExternal(ORIGIN) },
      { label: '打开本地数据', click: () => shell.openPath(path.join(app.getPath('userData'), 'data')) },
      { label: '后端日志', click: () => shell.openPath(app.getPath('logs')) },
    ] },
  ]))
}

app.setName('Fieldwork')
if (!app.requestSingleInstanceLock()) app.quit()
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus() } })
  app.whenReady().then(async () => {
    menu()
    try { await startBackend(); createWindow() }
    catch (error) { dialog.showErrorBox('无法启动 Fieldwork', error.message); app.quit() }
  })
  app.on('activate', () => { if (!window && backend && !backendExited) createWindow() })
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
  app.on('before-quit', event => {
    if (quitting || !backend || backendExited) return
    event.preventDefault(); quitting = true
    window?.hide()
    // Let the backend honor its saved stop-on-exit policy and finish cloud allocation/release.
    backend.once('exit', () => app.quit())
    backend.kill('SIGTERM')
    setTimeout(() => { if (!backendExited) { backend.kill('SIGKILL'); app.quit() } }, 8 * 60 * 1000).unref()
  })
}
