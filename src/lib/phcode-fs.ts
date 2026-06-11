// Get the global fs object initialized by virtualfs.js
const getFs = (): any => {
  return (window as any).fs;
};

let workspaceRoot = '/';

export function setWorkspaceRoot(path: string) {
  workspaceRoot = path.endsWith('/') ? path : path + '/';
}

export function getWorkspaceRoot(): string {
  return workspaceRoot;
}

/**
 * Initialize the filesystem by ensuring the global object is loaded
 */
export function initLocalFs(): Promise<void> {
  return new Promise((resolve) => {
    // Wait for window.fs to be defined if it's not yet
    const checkInterval = setInterval(() => {
      if ((window as any).fs) {
        clearInterval(checkInterval);
        resolve();
      }
    }, 50);
    // Timeout fallback after 3 seconds
    setTimeout(() => {
      clearInterval(checkInterval);
      resolve();
    }, 3000);
  });
}

/**
 * Helper to ensure parent directories exist before writing
 */
async function ensureDir(path: string): Promise<void> {
  const fs = getFs();
  if (!fs) return;
  
  const segments = path.split('/').filter(Boolean);
  let currentPath = path.startsWith('/') ? '/' : '';
  
  for (const segment of segments) {
    currentPath = currentPath === '/' ? '/' + segment : currentPath + '/' + segment;
    await new Promise<void>((resolve) => {
      fs.mkdir(currentPath, () => {
        // Ignore folder creation error if it already exists
        resolve();
      });
    });
  }
}

/**
 * Helper to recursively read all file paths in a directory
 */
async function readDirRecursive(path: string): Promise<string[]> {
  const fs = getFs();
  if (!fs) return [];
  return new Promise((resolve) => {
    fs.readdir(path, async (err: any, entries: string[]) => {
      if (err || !entries) {
        resolve([]);
        return;
      }
      
      const filePaths: string[] = [];
      const tasks = entries.map(entry => {
        const fullPath = path.endsWith('/') ? path + entry : path + '/' + entry;
        
        // Ignore common metadata/dependency folders to keep sync clean
        if (
          entry.startsWith('.') || 
          entry === 'node_modules' || 
          entry === '__pycache__' || 
          entry === 'venv' || 
          entry === '.git'
        ) {
          return Promise.resolve();
        }
        
        return new Promise<void>((resolveEntry) => {
          fs.stat(fullPath, async (statErr: any, stats: any) => {
            if (statErr || !stats) {
              resolveEntry();
              return;
            }
            if (stats.isFile()) {
              filePaths.push(fullPath);
              resolveEntry();
            } else if (stats.isDirectory()) {
              const subFiles = await readDirRecursive(fullPath);
              filePaths.push(...subFiles);
              resolveEntry();
            } else {
              resolveEntry();
            }
          });
        });
      });
      
      await Promise.all(tasks);
      resolve(filePaths);
    });
  });
}

/**
 * Write a file to local virtual filesystem (@phcode/fs) or mounted folder
 */
export async function writeLocalFile(filename: string, content: string): Promise<void> {
  const fs = getFs();
  if (!fs) throw new Error('Virtual filesystem not initialized');
  
  const fullPath = workspaceRoot + filename;
  const parentDir = fullPath.split('/').slice(0, -1).join('/');
  await ensureDir(parentDir);

  return new Promise((resolve, reject) => {
    fs.writeFile(fullPath, content, 'utf8', (err: any) => {
      if (err) reject(err);
      else resolve();
    });
  });
}

/**
 * Read a file from local virtual filesystem (@phcode/fs) or mounted folder
 */
export async function readLocalFile(filename: string): Promise<string> {
  const fs = getFs();
  if (!fs) return '';
  
  const fullPath = workspaceRoot + filename;
  return new Promise((resolve) => {
    fs.readFile(fullPath, 'utf8', (err: any, data: any) => {
      if (err) {
        resolve('');
      } else {
        resolve(data);
      }
    });
  });
}

/**
 * List files recursively in the workspace root or mounted folder
 */
export async function listLocalFiles(): Promise<{ name: string; size: number; mtime: number }[]> {
  const fs = getFs();
  if (!fs) return [];
  
  const root = getWorkspaceRoot();
  const searchRoot = root === '/' ? '/' : root.replace(/\/$/, '');
  
  const allFiles = await readDirRecursive(searchRoot);
  const results: { name: string; size: number; mtime: number }[] = [];
  
  const tasks = allFiles.map(filePath => {
    return new Promise<void>((resolveStat) => {
      fs.stat(filePath, (err: any, stats: any) => {
        if (!err && stats && stats.isFile()) {
          let relPath = filePath;
          if (filePath.startsWith(root)) {
            relPath = filePath.slice(root.length);
          } else if (filePath.startsWith(searchRoot + '/')) {
            relPath = filePath.slice((searchRoot + '/').length);
          }
          
          results.push({ 
            name: relPath, 
            size: stats.size || 0,
            mtime: stats.mtimeMs || (stats.mtime ? new Date(stats.mtime).getTime() : 0)
          });
        }
        resolveStat();
      });
    });
  });
  
  await Promise.all(tasks);
  results.sort((a, b) => a.name.localeCompare(b.name));
  return results;
}

/**
 * Delete a file from local virtual filesystem or mounted folder
 */
export async function deleteLocalFile(filename: string): Promise<void> {
  const fs = getFs();
  if (!fs) throw new Error('Virtual filesystem not initialized');
  
  const fullPath = workspaceRoot + filename;
  return new Promise((resolve, reject) => {
    fs.unlink(fullPath, (err: any) => {
      if (err) reject(err);
      else resolve();
    });
  });
}

/**
 * Mount a local native folder using File System Access API
 */
export function mountLocalFolder(): Promise<string> {
  const fs = getFs();
  if (!fs || !fs.mountNativeFolder) {
    return Promise.reject(new Error('Browser FileSystem mounting API not available'));
  }
  
  return new Promise((resolve, reject) => {
    fs.mountNativeFolder((err: any, mountPaths: string[]) => {
      if (err) {
        reject(err);
      } else if (mountPaths && mountPaths[0]) {
        resolve(mountPaths[0]);
      } else {
        reject(new Error('No mount path returned by directory picker'));
      }
    });
  });
}

/**
 * Unmount the local folder and switch back to browser internal virtual fs
 */
export function unmountLocalFolder() {
  setWorkspaceRoot('/');
}

