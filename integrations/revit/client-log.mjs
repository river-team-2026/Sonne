import {appendFileSync, mkdirSync, renameSync, statSync} from 'node:fs';
import {join} from 'node:path';

export function createClientLogger({localAppData = process.env.LOCALAPPDATA, maxBytes = 1024 * 1024} = {}) {
  const directory = localAppData && join(localAppData, 'Sonne', 'RevitClient');
  let file = directory ? join(directory, 'client.log') : '';
  function write(level, event, message, fields = {}) {
    const timestamp = new Date().toISOString();
    const terminal = level === 'INFO' ? console.log : console.error;
    terminal(`[${timestamp.slice(11, 19)}Z] ${level} ${message}`);
    if (!file) return;
    const line = JSON.stringify({timestamp, level, event, message, ...fields}) + '\n';
    try {
      mkdirSync(directory, {recursive: true});
      const size = statSync(file, {throwIfNoEntry: false})?.size || 0;
      if (size && size + Buffer.byteLength(line) > maxBytes) {
        const previous = file + '.1';
        if (statSync(previous, {throwIfNoEntry: false})) renameSync(previous, file + '.2');
        renameSync(file, previous);
      }
      appendFileSync(file, line, 'utf8');
    } catch {
      file = '';
      console.error('WARN Could not write the Revit client log file. Terminal logging continues.');
    }
  }
  return {file, write};
}
