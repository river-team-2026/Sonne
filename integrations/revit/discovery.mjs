import {createHmac, timingSafeEqual} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';

const mac = (token, message) => createHmac('sha256', token).update(message).digest('hex');

export async function discoverRouter(directory, token) {
  const filename = mac(token, 'sonne-discovery-v1') + '.json';
  const {url, expires, signature} = JSON.parse(await readFile(join(directory, filename), 'utf8'));
  const now = Math.floor(Date.now() / 1000);
  if (typeof url !== 'string' || !/^https:\/\/[a-z0-9-]+\.trycloudflare\.com\/revit\/$/.test(url) ||
      !Number.isSafeInteger(expires) || expires <= now || expires > now + 300 ||
      typeof signature !== 'string' || !/^[a-f0-9]{64}$/.test(signature) ||
      !timingSafeEqual(Buffer.from(signature), Buffer.from(mac(token, `sonne-route-v1\n${url}\n${expires}`)))) {
    throw new Error('Expired or invalid Revit discovery record. Waiting for the server.');
  }
  return {url, token: 'sr1-' + mac(token, 'sonne-access-v1\n' + url)};
}
