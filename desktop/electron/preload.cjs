const {contextBridge, ipcRenderer, webUtils} = require('electron');
contextBridge.exposeInMainWorld('research', {
  call: (method, params = {}) => ipcRenderer.invoke('rpc', method, params),
  files: (kind, params = {}) => ipcRenderer.invoke('files', kind, params),
  dropped: files => ipcRenderer.invoke('drop', Array.from(files).map(f => webUtils.getPathForFile(f))),
  external: url => ipcRenderer.invoke('external', url),
  clipboard: text => ipcRenderer.invoke('clipboard', text),
  assistant: {
    status: () => ipcRenderer.invoke('assistant-secret', 'status'),
    saveKey: value => ipcRenderer.invoke('assistant-secret', 'save', value),
    deleteKey: () => ipcRenderer.invoke('assistant-secret', 'delete')
  },
  window: action => ipcRenderer.send('window', action),
  on: callback => {
    const listener = (_, event, data) => callback(event, data);
    ipcRenderer.on('backend-event', listener);
    return () => ipcRenderer.removeListener('backend-event', listener);
  }
});
