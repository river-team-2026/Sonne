# Room schedule publication checks

Sonne archives the published RVT and stores a snapshot of one approved room
schedule per version. It compares snapshots by Revit room UniqueId and checks
each brief requirement by room number. A publication with any measured
area outside tolerance, missing required room, unmeasurable area or unexpected
room name sends one English alert to Sonne's configured Discord channel.
The report also includes added, removed and changed rooms.

In Prompt Studio's shared Project brief, enter one line per required room,
for example `Room check | SNE-1 | 60 | Laboratory`. The last field is optional.
Choose **Review prompt → Apply to Sonne**. At capture time the worker uses
these rows in place of the configured room list and stores the exact brief
with the snapshot. The configured area tolerance still applies. With no rows
in the applied brief it uses the configured list. An invalid or duplicate
room check stops capture until the team corrects and applies the brief.

## Set up the check

In Revit 2027, use an itemized room schedule for up to 200 rooms. Show the
Number and Area columns and end the schedule name with `-Sonne`. Use room
numbers that are unique within the schedule. The schedule's filters define the
room list. Its rooms and their boundaries must belong to the host RVT. The
reader rejects hosts with room-bounding Revit links.

Get the schedule's UniqueId from the model once. On the approved workstation,
open the schedule and use the existing Revit MCP `send_code_to_revit` tool in
`none` transaction mode.

```csharp
return new { document.ActiveView.Name, document.ActiveView.UniqueId };
```

Install the [Revit integration](README.md) first. Run
`setup-revit-server.sh` for the designated client with `--enable-publications`
and transfer its renewed private enrollment settings. For an existing Windows
client, keep its local launch settings and add `"enablePublications": true`.
Stop the Windows client, run
`powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File scripts/install-revit-publications.ps1`, reload the Revit MCP
commands or restart Revit, then start the client again. Keep a document open
on the designated workstation. The reader opens each archived copy separately
and closes it without saving.

From the Linux Sonne runtime, create `.state/revit-publications/config.json`
with mode `600`. This example uses a tolerance of 0.5 m² and a two-room brief.
To change the source, model or schedule, stop the publication service and
move the history directory aside before creating a fresh one.

```json
{
  "source": "local",
  "model_key": "snowdon-test",
  "model_label": "Snowdon Towers test model",
  "schedule_id": "e12b7ec2-00e3-47be-9f28-c7014662ec50-0032c689",
  "worker": "windows-01",
  "brief": {
    "revision": "TEST-01",
    "tolerance_m2": 0.5,
    "rooms": [
      {"number": "SNE-1", "area_m2": 60},
      {"number": "SNE-2", "area_m2": 30}
    ]
  }
}
```

Start the optional service using the same Compose files as the Revit integration.

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.override.yaml -f compose.prompt-studio.yaml \
  -f compose.revit.yaml --profile revit-publications up -d --build revit-publications
```

The worker uses the configured Discord bot and channel. The worker and router
keep publication history and RVT archives in `.state/revit-publications`.
Keep that directory private. Each file can be up to 512 MiB.

## Publish saved RVT copies locally

Save separate versions, copy them to `.state/revit-publications/inbox/` and
submit the copies in version order from the Linux runtime with Node.js.

```bash
node integrations/revit/publish-room-version.mjs 1 Snowdon-Sonne-test-v1.rvt
node integrations/revit/publish-room-version.mjs 2 Snowdon-Sonne-test-v2.rvt
```

Ask Sonne for the latest room schedule comparison or a specific version.
The read-only `revit__revit_room_publications` tool includes the brief
revision, the measured value and the target in m², and Discord delivery status.
A repeated submission of the same version and RVT is idempotent.

## ACC integration

Follow the [ACC setup guide](ACC-SETUP.md) to create the Forma hub and test
project, connect the APS app, and verify the first Discord alert.
