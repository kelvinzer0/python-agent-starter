// Get the global fs object initialized by virtualfs.js
const getFs = (): any => {
  return (window as any).fs;
};

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
 * Write a file to local virtual filesystem (@phcode/fs)
 */
export async function writeLocalFile(filename: string, content: string): Promise<void> {
  const fs = getFs();
  if (!fs) throw new Error('Virtual filesystem not initialized');
  return new Promise((resolve, reject) => {
    fs.writeFile('/' + filename, content, 'utf8', (err: any) => {
      if (err) reject(err);
      else resolve();
    });
  });
}

/**
 * Read a file from local virtual filesystem (@phcode/fs)
 */
export async function readLocalFile(filename: string): Promise<string> {
  const fs = getFs();
  if (!fs) return '';
  return new Promise((resolve) => {
    fs.readFile('/' + filename, 'utf8', (err: any, data: any) => {
      if (err) {
        // Fallback to empty string if file doesn't exist
        resolve('');
      } else {
        resolve(data);
      }
    });
  });
}

/**
 * List files in the root of virtual filesystem
 */
export async function listLocalFiles(): Promise<{ name: string; size: number }[]> {
  const fs = getFs();
  if (!fs) return [];
  return new Promise((resolve) => {
    fs.readdir('/', (err: any, files: string[]) => {
      if (err || !files || files.length === 0) {
        resolve([]);
        return;
      }

      const results: { name: string; size: number }[] = [];
      let pending = files.length;

      files.forEach(file => {
        fs.stat('/' + file, (statErr: any, stats: any) => {
          if (!statErr && stats && stats.isFile()) {
            results.push({ name: file, size: stats.size || 0 });
          }
          pending--;
          if (pending === 0) {
            // Sort alphabetically by filename
            results.sort((a, b) => a.name.localeCompare(b.name));
            resolve(results);
          }
        });
      });
    });
  });
}

/**
 * Delete a file from local virtual filesystem
 */
export async function deleteLocalFile(filename: string): Promise<void> {
  const fs = getFs();
  if (!fs) throw new Error('Virtual filesystem not initialized');
  return new Promise((resolve, reject) => {
    fs.unlink('/' + filename, (err: any) => {
      if (err) reject(err);
      else resolve();
    });
  });
}
