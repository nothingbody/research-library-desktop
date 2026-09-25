const {BrowserWindow, session} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
let loginWindow = null;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function request(root, action, payload) {
  const handoff = JSON.parse(fs.readFileSync(path.join(root, 'handoff.json'), 'utf8'));
  const value = handoff[action === 'session' ? 'url' : action === 'status' ? 'status_url' : 'control_url'];
  const url = new URL(value);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port) throw new Error('采集器控制地址不合法');
  const response = await fetch(url, {method: action === 'status' ? 'GET' : 'POST', redirect: 'error', signal: AbortSignal.timeout(10000),
    headers: {'Origin':'https://www.scholay.com','Content-Type':'application/json'}, body: action === 'status' ? undefined : JSON.stringify(payload)});
  if (!response.ok) throw new Error('采集器连接失败：HTTP ' + response.status);
  return response.json();
}

exports.connect = async ({app, parent, rpc, project}) => {
  if (loginWindow && !loginWindow.isDestroyed()) {loginWindow.focus(); return {opened:true};}
  const settings = await rpc('settings.get');
  if (settings.online === false) throw new Error('联网已关闭，请先在设置中开启');
  const root = settings.journalRoot;
  if (!root || !fs.existsSync(path.join(root,'journals.sqlite3'))) throw new Error('请先选择期刊数据目录');
  const partition = session.fromPartition('research-login-' + Date.now());
  let connecting = false, closed = false;
  const notify = data => {if(parent && !parent.isDestroyed()) parent.webContents.send('backend-event','connection.status',data);};
  loginWindow = new BrowserWindow({parent, width:1120, height:800, title:'连接 Scholay · 登录信息仅在本次会话内使用', autoHideMenuBar:true,
    webPreferences:{session:partition,nodeIntegration:false,contextIsolation:true,sandbox:true}});
  loginWindow.webContents.setWindowOpenHandler(({url})=>{if(new URL(url).protocol==='https:') loginWindow.loadURL(url);return {action:'deny'};});
  partition.setPermissionRequestHandler((_,__,cb)=>cb(false));
  partition.webRequest.onBeforeSendHeaders({urls:['https://www.scholay.com/api/v1/public/journals/*']},(details,callback)=>{
    const token = details.requestHeaders.Authorization || details.requestHeaders.authorization;
    callback({requestHeaders:details.requestHeaders});
    if(connecting || !token || typeof token!=='string' || !token.startsWith('Bearer ') || token.length<16) return;
    connecting=true;
    (async()=>{
      let status=null;
      try {status=await request(root,'status');} catch {}
      const rate=Math.min(10,Math.max(.5,Number(status?.rate?.maximum_rps)||4));
      const paused=!!status?.status?.startsWith('paused');
      if(status && status.version<7) {
        await request(root,'control',{action:'stop'});
        for(let n=0;n<300;n++) {try{await request(root,'status');await sleep(250);}catch{break;}}
        // The HTTP endpoint may close before the single writer releases its lock.
        await sleep(1500);
        status=null;
      }
      if(!status) {
        const executable=app.isPackaged?path.join(process.resourcesPath,'backend/research-backend.exe'):path.join(project,'.venv-client/Scripts/python.exe');
        const args=app.isPackaged?['--collector']:['-X','utf8',path.join(project,'backend_entry.py'),'--collector'];
        args.push('--output',root,'--network-mode','async','--max-rps',String(rate),'--wait-lock','90');
        const log=fs.openSync(path.join(root,'desktop-collector.log'),'a');
        const service=spawn(executable,args,{cwd:app.isPackaged?path.dirname(executable):project,windowsHide:true,detached:true,stdio:['ignore',log,log]});
        service.unref();fs.closeSync(log);
        service.on('error',()=>notify({connected:false,message:'采集器未能启动，请查看数据目录中的 desktop-collector.log'}));
        for(let n=0;n<400;n++) {await sleep(250);try{const s=await request(root,'status');if(s.version>=7){status=s;break;}}catch{}}
        if(!status) throw new Error('采集器启动超时；原数据和队列仍保留，请重试连接');
      }
      if(paused) await request(root,'control',{action:'pause'});
      await request(root,'session',{authorization:token});
      notify({connected:true,message:'Scholay 已连接，登录凭据仅保存在采集器内存中'});
      if(loginWindow && !loginWindow.isDestroyed()) loginWindow.close();
    })().catch(error=>{connecting=false;notify({connected:false,message:error.message});});
  });
  loginWindow.on('closed',()=>{closed=true;partition.webRequest.onBeforeSendHeaders(null);partition.clearStorageData().catch(()=>{});loginWindow=null;});
  await loginWindow.loadURL('https://www.scholay.com/journals/search');
  return {opened:true,message:'登录后在期刊页面执行一次搜索即可连接。关闭此窗口不影响当前采集器。'};
};
