const fs = require('node:fs');
const path = require('node:path');

function nsis(value) {
  if (/[\r\n\0]/.test(value)) throw new Error('Unsupported installer file name');
  return value.replace(/\$/g, () => '$$').replace(/"/g, '$\\"');
}

function generate(appOutDir, destination) {
  const root = fs.realpathSync(appOutDir);
  const files = new Set(), directories = new Set();
  function walk(directory, relative = '') {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      if (entry.isSymbolicLink()) throw new Error('Installer program files cannot contain symbolic links');
      const value = relative ? relative + '\\' + entry.name : entry.name;
      if (entry.isDirectory()) {directories.add(value); walk(path.join(directory, entry.name), value);}
      else if (entry.isFile()) files.add(value);
      else throw new Error('Unsupported installer program file');
    }
  }
  walk(root);
  // electron-builder adds its elevation helper after afterPack. The other
  // two files are created by the installer rather than the app packager.
  files.add('resources\\elevate.exe');
  files.add('resources\\install-owner.ini');
  directories.add('resources');
  const owned = [...new Set([...files, ...directories])].sort();
  const output = ['; Generated from the packaged program; never edit by hand.', '!macro RL_IsOwnedRelativePath VALUE OUT', '  StrCpy ${OUT} "0"'];
  for (const value of owned) output.push(`  \${If} \${VALUE} == "${nsis(value)}"`, '    StrCpy ${OUT} "1"', '  ${EndIf}');
  output.push('  ${If} ${VALUE} == "${UNINSTALL_FILENAME}"', '    StrCpy ${OUT} "1"', '  ${EndIf}', '!macroend', '', '!macro RL_RemoveOwnedProgramFiles');
  for (const value of [...files].filter(value => value !== 'resources\\install-owner.ini').sort()) output.push(`  !insertmacro RL_DeleteProgramFile "${nsis(value)}"`);
  output.push('  !insertmacro RL_DeleteProgramFile "${UNINSTALL_FILENAME}"');
  output.push('  !insertmacro RL_DeleteProgramFile "resources\\install-owner.ini"');
  for (const directory of [...directories].sort((a, b) => b.split('\\').length - a.split('\\').length || b.length - a.length)) {
    output.push(`  RMDir "$INSTDIR\\${nsis(directory)}"`);
  }
  output.push('  RMDir "$INSTDIR"', '!macroend', '');
  fs.writeFileSync(destination, output.join('\n'), 'utf8');
  return {files: files.size + 1, directories: directories.size};
}

module.exports = async context => {
  if (context.electronPlatformName !== 'win32') return;
  generate(context.appOutDir, path.join(__dirname, 'install-files.nsh'));
};
module.exports.generate = generate;
