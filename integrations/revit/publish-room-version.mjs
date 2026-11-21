import {readConfig, api} from './common.mjs';

const [version, file] = process.argv.slice(2);
if (!/^[1-9]\d{0,14}$/.test(version ?? '') || !file || process.argv.length !== 4) {
  console.error('Usage: node integrations/revit/publish-room-version.mjs VERSION FILE.rvt');
  process.exit(2);
}

try {
  const {token} = await readConfig('.state/openclaw/integrations/revit/server.json');
  const publication = await api({url: 'http://127.0.0.1:9082/', token}, 'local/publish', {
    event: 'model.publish', version: Number(version), file
  });
  if (!Object.hasOwn(publication, 'queued')) throw new Error('Local publication is unavailable.');
  console.log(JSON.stringify(publication, null, 2));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
