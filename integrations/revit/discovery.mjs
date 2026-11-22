import {createDecipheriv, createHmac, timingSafeEqual} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';

const mac = (token, message) => createHmac('sha256', token).update(message).digest('hex');

export async function discoverOnline(token, fetcher = fetch) {
  const topic = 'sonne-' + mac(token, 'sonne-topic-v1');
  const response = await fetcher('https://ntfy.sh/' + topic + '/json?poll=1&since=latest', {
    redirect: 'error', signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error('Internet discovery unavailable (HTTP ' + response.status + ').');
  const line = (await response.text()).trim();
  if (!line || line.length > 8192) throw new Error('Waiting for a Sonne announcement.');
  const event = JSON.parse(line);
  if (event.event !== 'message' || event.topic !== topic || typeof event.message !== 'string')
    throw new Error('Invalid Sonne announcement.');
  const packet = Buffer.from(event.message, 'base64');
  if (packet.length < 29) throw new Error('Incomplete Sonne announcement.');
  const decoder = createDecipheriv('aes-256-gcm', Buffer.from(mac(token, 'sonne-discovery-key-v1'), 'hex'), packet.subarray(0, 12));
  decoder.setAAD(Buffer.from(topic));
  decoder.setAuthTag(packet.subarray(-16));
  const {url, expires} = JSON.parse(Buffer.concat([decoder.update(packet.subarray(12, -16)), decoder.final()]).toString());
  const now = Math.floor(Date.now() / 1000);
  if (typeof url !== 'string' || !/^https:\/\/[a-z0-9-]+\.trycloudflare\.com\/revit\/$/.test(url) ||
      !Number.isSafeInteger(expires) || expires <= now || expires > now + 6 * 3600 + 120)
    throw new Error('Expired or invalid Sonne tunnel address.');
  return {url, token: 'sr1-' + mac(token, 'sonne-access-v1\n' + url)};
}

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
