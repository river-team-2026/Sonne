import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';

const run = promisify(execFile);
const helper = fileURLToPath(new URL('../../../scripts/enable-prompt-studio.sh', import.meta.url));
test('automatic setup creates private profiles, preserves OAuth configuration on reruns and retains removed users data', {skip: process.getuid?.() === 0}, async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'studio-setup-'));
  t.after(() => rm(root, {recursive: true}));
  await mkdir(path.join(root, '.state/openclaw'), {recursive: true});
  await mkdir(path.join(root, 'templates/workspace'), {recursive: true});
  await writeFile(path.join(root, 'compose.yaml'), 'services: {}\n');
  const loginEnv = 'DISCORD_APPLICATION_ID=999999999999999999\nDISCORD_OAUTH_CLIENT_SECRET=synthetic-secret\nPROMPT_STUDIO_URL=http://localhost:9080\n';
  await writeFile(path.join(root, '.env'), loginEnv + 'DISCORD_USER_IDS=111111111111111111,222222222222222222\n');
  await writeFile(path.join(root, 'templates/workspace/SOUL.md'), 'Keep the shared persona.');
  await writeFile(path.join(root, '.state/openclaw/openclaw.json'), JSON.stringify({agents: {defaults: {workspace: '/workspace'}}, plugins: {allow: ['discord', 'ollama']}}));
  const command = [helper, '--dir', root, '--prepare-only'];
  await run('bash', command);
  const studio = path.join(root, '.state/prompt-studio');
  const first = await readFile(path.join(studio, 'accounts.json'), 'utf8'), oauth = await readFile(path.join(studio, 'oauth.json'), 'utf8');
  assert.equal(JSON.parse(first).users.length, 2);
  assert.equal((await stat(path.join(studio, 'oauth.json'))).mode & 0o777, 0o600);
  assert.equal((await stat(path.join(studio, 'active/shared'))).mode & 0o777, 0o700);
  assert.equal((await stat(path.join(root, 'workspace/files/prompt-studio/shared/.sonne-memory'))).uid, process.getuid());
  assert.ok((await readFile(path.join(root, 'compose.prompt-studio.yaml'),'utf8')).includes('./.state/prompt-studio/active/shared:/workspace/files/prompt-studio/shared/.sonne-memory:ro'));
  assert.deepEqual(JSON.parse(oauth), {clientId: '999999999999999999', clientSecret: 'synthetic-secret', redirectUri: 'http://localhost:9080/auth/discord/callback'});
  assert.ok(!first.includes('keyHash'));
  await assert.rejects(stat(path.join(studio, 'access-codes.txt')), {code: 'ENOENT'});
  assert.ok((await readFile(path.join(root, 'prototypes/prompt-studio/discord-auth.mjs'), 'utf8')).includes('createDiscordAuth'));
  const config = JSON.parse(await readFile(path.join(root, '.state/openclaw/openclaw.json'), 'utf8'));
  assert.ok(config.plugins.allow.includes('discord'));
  assert.equal(config.plugins.entries['sonne-personal-prompts'].hooks.allowConversationAccess, true);
  assert.ok(config.agents.defaults.bootstrapMaxChars >= 40000);
  await run('bash', command);
  assert.equal(await readFile(path.join(studio, 'accounts.json'), 'utf8'), first);
  assert.equal(await readFile(path.join(studio, 'oauth.json'), 'utf8'), oauth);
  assert.equal(await readFile(path.join(root, 'templates/workspace/SOUL.md'), 'utf8'), 'Keep the shared persona.');
  await mkdir(path.join(studio, 'profiles/222222222222222222'), {recursive: true});
  await writeFile(path.join(studio, 'profiles/222222222222222222/current.json'), 'Retain removed data.');
  await writeFile(path.join(root, '.env'), loginEnv + 'DISCORD_USER_IDS=111111111111111111\n');
  const result = await run('bash', command);
  assert.equal(JSON.parse(await readFile(path.join(studio, 'accounts.json'), 'utf8')).users.length, 1);
  assert.deepEqual(JSON.parse(await readFile(path.join(studio, 'active/users.json'), 'utf8')), ['111111111111111111']);
  assert.equal(await readFile(path.join(studio, 'profiles/222222222222222222/current.json'), 'utf8'), 'Retain removed data.');
  assert.ok(!result.stdout.includes('synthetic-secret'));
  // The bot can run while web login awaits the administrator's OAuth secret.
  await writeFile(path.join(root, '.env'), loginEnv.replace('DISCORD_OAUTH_CLIENT_SECRET=synthetic-secret', 'DISCORD_OAUTH_CLIENT_SECRET=') + 'DISCORD_USER_IDS=111111111111111111\n');
  const incomplete = await run('bash', command);
  assert.match(incomplete.stdout, /sign-in is disabled/);
  assert.equal(JSON.parse(await readFile(path.join(studio, 'oauth.json'), 'utf8')).clientSecret, '');
  await writeFile(path.join(root, '.env'), loginEnv.replace('http://localhost:9080', 'http://public.example') + 'DISCORD_USER_IDS=111111111111111111\n');
  await assert.rejects(run('bash', command), error => error.stderr.includes('HTTPS origin'));
});

test('adding Discord users updates Studio and the configured bot allowlists idempotently', {skip: process.getuid?.() === 0}, async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'studio-users-'));
  t.after(() => rm(root, {recursive: true}));
  const state = path.join(root, '.state/openclaw');
  const studio = path.join(root, '.state/prompt-studio');
  const configPath = path.join(state, 'openclaw.json');
  const env = path.join(root, '.env');
  const original = '111111111111111111', added = '222222222222222222', extra = '333333333333333333';
  const guildId = '444444444444444444', channelId = '555555555555555555', otherChannelId = '666666666666666666';
  const channel = {enabled: true, requireMention: true, users: [original]};
  const otherChannel = {enabled: true, users: [original]};
  await mkdir(state, {recursive: true});
  await mkdir(path.join(root, 'templates/workspace'), {recursive: true});
  await writeFile(path.join(root, 'compose.yaml'), 'services: {}\n');
  await writeFile(path.join(root, 'templates/workspace/SOUL.md'), 'Shared instructions');
  await writeFile(env, `DISCORD_APPLICATION_ID=999999999999999999\nDISCORD_GUILD_ID=${guildId}\nDISCORD_CHANNEL_ID=${channelId}\nDISCORD_USER_IDS=${original}\nDISCORD_OAUTH_CLIENT_SECRET=fixture-secret\n`);
  await writeFile(configPath, JSON.stringify({
    agents: {defaults: {workspace: '/workspace', thinkingDefault: 'off'}},
    channels: {discord: {groupPolicy: 'allowlist', dmPolicy: 'disabled', guilds: {
      [guildId]: {requireMention: true, users: [original], channels: {[channelId]: channel, [otherChannelId]: otherChannel}},
    }}},
    mcp: {servers: {revit: {command: 'node'}}},
  }));
  const args = [helper, '--dir', root, '--prepare-only'];
  const add = [...args, '--add-discord-user', added, '--add-discord-user', extra];
  await assert.rejects(run('bash', [...args, '--add-discord-user', '@user']), /numeric Discord User ID/);
  for (let i = 0; i < 2; i++) await run('bash', add);
  const users = [original, added, extra];
  const contents = await readFile(env, 'utf8');
  assert.deepEqual(contents.split('\n').filter(line => line.startsWith('DISCORD_USER_IDS=')), ['DISCORD_USER_IDS=' + users.join(',')]);
  assert.match(contents, /DISCORD_OAUTH_CLIENT_SECRET=fixture-secret/);
  assert.equal((await stat(env)).mode & 0o777, 0o600);
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  const guild = config.channels.discord.guilds[guildId];
  assert.equal(config.channels.discord.dmPolicy, 'disabled');
  assert.equal(guild.requireMention, true);
  assert.deepEqual(guild.users, users);
  assert.deepEqual(guild.channels[channelId], {...channel, users});
  assert.deepEqual(guild.channels[otherChannelId], otherChannel);
  assert.equal(config.mcp.servers.revit.command, 'node');
  const batch = JSON.parse(await readFile(path.join(state, 'studio-discord-users.json'), 'utf8'));
  assert.deepEqual(batch, [{path: 'channels.discord', value: config.channels.discord}]);
  const accounts = JSON.parse(await readFile(path.join(studio, 'accounts.json'), 'utf8'));
  assert.deepEqual(accounts.users.map(user => user.id), users);
  assert.deepEqual(JSON.parse(await readFile(path.join(studio, 'active/users.json'), 'utf8')), users);
});
