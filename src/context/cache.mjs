/** Persistencia del manifiesto de hashes → permite enviar solo deltas entre sesiones. */
import fs from 'node:fs/promises';
import path from 'node:path';

export class ManifestCache {
  constructor(dir) {
    this.file = path.join(dir, 'manifest.json');
    this.dir = dir;
  }

  async load() {
    try {
      return JSON.parse(await fs.readFile(this.file, 'utf8'));
    } catch {
      return null;
    }
  }

  async save(manifest) {
    await fs.mkdir(this.dir, { recursive: true });
    await fs.writeFile(this.file, JSON.stringify(manifest, null, 2), 'utf8');
  }
}
