import {createHmac, timingSafeEqual} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {Readable} from 'node:stream';
import {archive, atomicJson, maxArchiveBytes, publicationError} from './publications.mjs';

const origin = 'https://developer.api.autodesk.com';
function navigationKey(url) {
  // APS pagination can encode the colons in the item URN differently.
  return decodeURIComponent(url.pathname);
}

export function validateAps(config) {
  if (!config || !/^b\.[a-f0-9-]{36}$/i.test(config.project_id) ||
      !/^urn:adsk\.wipprod:dm\.lineage:[\w-]{1,200}$/.test(config.lineage_urn) ||
      !/^[\w-]{1,100}$/.test(config.hook_id) || typeof config.webhook_secret !== 'string' ||
      config.webhook_secret.length < 32 || !Number.isSafeInteger(config.first_version) || config.first_version < 1)
    throw publicationError('Configure the APS project, model lineage, publish hook, signature key and first version.');
}

export function acceptPublishHook(raw, signature, config) {
  const expected = Buffer.from('sha1hash=' + createHmac('sha1', config.webhook_secret).update(raw).digest('hex'));
  const given = Buffer.from(typeof signature === 'string' ? signature : '');
  if (expected.length !== given.length || !timingSafeEqual(expected, given)) throw publicationError('Invalid APS signature.', 401);
  let body;
  try { body = JSON.parse(raw); } catch { throw publicationError('Invalid APS payload.'); }
  const hook = body?.hook;
  if (hook?.hookId !== config.hook_id || hook.system !== 'adsk.c4r' || hook.event !== 'model.publish') return false;
  // Some C4R callbacks carry the model lineage, others only the folder hook.
  // With no lineage, check only the server-configured model's published versions.
  const candidates = [body.resourceUrn];
  for (const [key, value] of Object.entries(body.payload ?? {})) {
    if (/^(lineageUrn|itemId|itemUrn|resourceUrn|urn)$/i.test(key)) candidates.push(value);
  }
  const lineage = candidates.find(urn => typeof urn === 'string' && /^urn:adsk\.wipprod:dm\.lineage:/i.test(urn));
  return !lineage || lineage.split('?')[0].toLowerCase() === config.lineage_urn.toLowerCase();
}

export function createApsSource(config, directory, fetcher = fetch) {
  let access, expires = 0;
  async function token() {
    if (access && expires > Date.now()) return access;
    const path = join(directory, 'aps-auth.json');
    const auth = JSON.parse(await readFile(path, 'utf8').catch(() => {throw publicationError('APS credentials are not configured.', 503);}));
    if (![auth.client_id, auth.client_secret, auth.refresh_token].every(value => typeof value === 'string' && value))
      throw publicationError('APS credentials are incomplete.', 503);
    const response = await fetcher(origin + '/authentication/v2/token', {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(20000),
      headers: {'Content-Type': 'application/x-www-form-urlencoded', Authorization: 'Basic ' + Buffer.from(auth.client_id + ':' + auth.client_secret).toString('base64')},
      body: new URLSearchParams({grant_type: 'refresh_token', refresh_token: auth.refresh_token, scope: 'data:read'})
    });
    if (!response.ok) throw publicationError('APS authentication failed (HTTP ' + response.status + ').', 503);
    const result = await response.json();
    if (!result.access_token || !result.expires_in) throw publicationError('APS did not return an access token.', 503);
    if (result.refresh_token && result.refresh_token !== auth.refresh_token)
      await atomicJson(path, {...auth, refresh_token: result.refresh_token});
    expires = Date.now() + Math.max(0, (result.expires_in - 60) * 1000);
    return access = result.access_token;
  }
  async function request(url, description) {
    const response = await fetcher(url, {headers: {Authorization: 'Bearer ' + await token()}, redirect: 'error', signal: AbortSignal.timeout(25000)});
    if (response.status === 401) expires = 0;
    if (!response.ok) throw publicationError(description + ' (HTTP ' + response.status + ').', 502);
    return response.json();
  }
  return {
    async versions() {
      const path = '/data/v1/projects/' + encodeURIComponent(config.project_id) + '/items/' + encodeURIComponent(config.lineage_urn) + '/versions';
      let url = new URL(path + '?page%5Blimit%5D=100', origin);
      const versions = new Map();
      for (let page = 0; url && page < 50; page++) {
        if (url.origin !== origin || navigationKey(url) !== decodeURIComponent(path) || url.username || url.password)
          throw publicationError('Unexpected APS pagination URL.', 502);
        const body = await request(url, 'Could not list APS publications');
        if (!Array.isArray(body.data)) throw publicationError('Malformed APS version list.', 502);
        for (const item of body.data) {
          if (item.attributes?.extension?.type !== 'versions:autodesk.bim360:C4RModel') continue;
          const n = item.attributes.versionNumber;
          if (!Number.isSafeInteger(n) || n < config.first_version) continue;
          if (typeof item.id !== 'string' || !/^urn:adsk\.wipprod:fs\.file:vf\.[\w-]+\?version=\d+$/.test(item.id) ||
              Number(new URLSearchParams(item.id.split('?')[1]).get('version')) !== n ||
              (item.relationships?.item?.data?.id && item.relationships.item.data.id !== config.lineage_urn))
            throw publicationError('APS returned a version outside the configured model.', 502);
          if (versions.has(n) && versions.get(n) !== item.id) throw publicationError('Conflicting APS version references.', 502);
          versions.set(n, item.id);
        }
        url = body.links?.next?.href ? new URL(body.links.next.href, origin) : null;
      }
      if (url) throw publicationError('APS version listing exceeded the pilot page limit.', 502);
      return [...versions].sort((a,b) => a[0] - b[0]);
    },
    async download(row, archives) {
      const url = origin + '/construction/rcm/v1/projects/' + encodeURIComponent(config.project_id) +
        '/published-versions/' + encodeURIComponent(row.reference) + '/linked-files?limit=1';
      const {hostFile: host} = await request(url, 'Could not resolve the published RVT');
      if (host?.itemId !== config.lineage_urn || host.publishStatus !== 'Published' ||
          ![row.reference, config.lineage_urn + '?version=' + row.version].includes(host.versionId) ||
          !Number.isSafeInteger(host.size) || host.size < 8 || host.size > maxArchiveBytes || !/\.rvt$/i.test(host.modelName ?? ''))
        throw publicationError('APS has not supplied the exact approved published RVT.', 502);
      const signed = new URL(host.signedUrl);
      if (signed.protocol !== 'https:' || signed.port || signed.username || signed.password ||
          !/(?:^|\.)s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com$/i.test(signed.hostname))
        throw publicationError('Unexpected APS archive download host.', 502);
      const response = await fetcher(signed, {redirect: 'error', signal: AbortSignal.timeout(180000)});
      if (!response.ok || !response.body) throw publicationError('Published RVT download failed (HTTP ' + response.status + ').', 502);
      return archive(Readable.fromWeb(response.body), archives, host.size);
    }
  };
}
