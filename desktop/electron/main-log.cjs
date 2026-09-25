const fs = require('node:fs');
const path = require('node:path');
const {format} = require('node:util');

let logPath;

function write(level, ...values) {
  if (!logPath) return;
  try {
    fs.mkdirSync(path.dirname(logPath), {recursive: true});
    fs.appendFileSync(logPath, `[${new Date().toISOString()}] ${level}: ${format(...values)}\n`, 'utf8');
  } catch {
    // Logging must never turn a recoverable application error into a fatal one.
  }
}

function install(directory, {captureConsole = true} = {}) {
  logPath = path.join(directory, 'main.log');
  for (const stream of [process.stdout, process.stderr]) {
    stream.on('error', error => {
      if (error.code !== 'EPIPE') write('stdio', error);
    });
  }
  if (captureConsole) {
    console.error = (...values) => write('error', ...values);
    console.warn = (...values) => write('warn', ...values);
  }
  return logPath;
}

module.exports = {install, write};
