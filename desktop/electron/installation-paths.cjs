const fs = require('node:fs');
const path = require('node:path');

function assertLibraryOutsideInstallation(directory, installationDirectory) {
  // Resolve junctions as well as the user's spelling/casing of the paths.
  // Comparing the selected path text alone would allow an alias into app files.
  let selected, installed;
  try {
    selected = fs.realpathSync.native(directory);
    installed = fs.realpathSync.native(installationDirectory);
  } catch {
    throw new Error('无法确认文献库目录的实际位置，请选择可访问的目录');
  }
  const relative = path.relative(installed, selected);
  if (!relative || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep))) {
    throw new Error('请将文献库保存在程序安装目录之外，以免升级或卸载影响文献');
  }
}

module.exports = {assertLibraryOutsideInstallation};
