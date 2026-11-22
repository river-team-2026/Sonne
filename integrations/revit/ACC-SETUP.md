# Set up Sonne for ACC room schedule checks

Use a dedicated Forma test hub and one Revit 2027 cloud workshared model.
Sonne reads the published RVT, checks the approved room schedule against the
configured brief, and posts a Discord alert if an area exceeds the tolerance.

## 1. Check the prerequisites

- Complete the [Discord application setup](../../README.md#automated-installation).
  The installer uses the same Discord bot and channel for publication alerts.
- Allow approximately **12 GiB** of host RAM for Sonne with Revit and Qwen, or
  **16 GiB** if you choose Gemma.
- Prepare the Windows workstation with Revit 2027. Use an HTTPS endpoint for its
  outbound Revit client connection. The Sonne installer prompts for that URL
  and creates the private enrollment file. [Router and Windows setup](README.md#linux-server)
- Use an Autodesk account with Revit and **Forma Design Collaboration**
  (formerly BIM Collaborate Pro). Check that **Revit Cloud Worksharing** is
  assigned to the account signed into Revit. An active Forma Design
  Collaboration trial includes this service. [Revit 2027 access requirements](https://help.autodesk.com/cloudhelp/2027/ENU/Revit-Cloud/files/GUID-F7295877-241B-4A97-B71C-C714D2CEC7C9.htm) · [Autodesk trial setup](https://help.autodesk.com/cloudhelp/ENU/Collab-GS/files/getting-started-with-bim-collaborate-pro/account-setup/Design_Collab_About_BIM_Collaborate_trial.html)

## 2. Create the Forma hub, ACC project, and test folder

1. In **Autodesk Account → Products and services → Hubs**, select the team
   that has the Forma entitlement. Click **Create hub → Forma** and enter
   `River Team – Sonne Test`. Choose a data region and save it for step 5.
   For the European Union region, enter `EMEA` later. Select **Go to ACC / Forma**
   when the hub is ready. [Create the hub](https://help.autodesk.com/cloudhelp/ENU/Docs-Trials-Subscriptions/files/Purchase_And_Activate_Account.html) · [APS region codes](https://aps.autodesk.com/en/docs/data/v2/reference/http/hubs-hub_id-GET)
2. In **Hub Admin → Projects → Create project**, select the new hub and name
   the project `Sonne Revit Publication Test`. Choose the project type, fill in
   the required fields, and create the project. [Create the project](https://help.autodesk.com/cloudhelp/ENG/Docs-Admin/files/hub-administration/Create_Manage_Projects.html)
3. In the project, open **Data Management → Files**. In the folder tree, open
   the **…** menu for **Project Files**, select **Add subfolder**, and name it
   `Sonne Pilot`. In **Project Admin → Members**, confirm that the account used
   in Revit belongs to the project. It needs **Edit** permission on `Sonne Pilot`
   to create and work on the cloud model. [Folder actions](https://help.autodesk.com/cloudhelp/ENU/Docs-Files/files/File_Folder_Actions_Docs.html) · [Cloud model permissions](https://help.autodesk.com/cloudhelp/2027/ENU/Revit-Cloud/files/GUID-F7295877-241B-4A97-B71C-C714D2CEC7C9.htm)

## 3. Register the APS app and add it to the Forma hub

1. In the existing **Sonne APS Developer Hub**, go to **Applications → Create
   application**. Create `Sonne ACC Test` as a **Traditional Web App**, or open
   that app if it already exists. Enable **Data Management**, **Forma APIs**,
   and **Webhooks** under **API Access**. Save the OAuth callback URL
   `http://localhost:8765/aps/oauth/callback` exactly as shown.
   [APS app setup](https://get-started.aps.autodesk.com/#create-app-credentials) · [Published RVT API requirements](https://aps.autodesk.com/en/docs/acc/v1/tutorials/files/rcm-linked-files)
2. In the **Forma** hub, go to **Hub Admin → Custom Integrations → Add custom
   integration**. Paste the APS **Client ID**, enter a name and description,
   then click **Next**. If Autodesk finds no Secure Service Account,
   **Proceed anyway** grants the app access across the hub. Use that option in
   the dedicated test hub and confirm the integration shows **Active**. Keep
   the **Client Secret** for the terminal prompt. [Forma Custom Integrations](https://help.autodesk.com/cloudhelp/ENU/Docs-Admin/files/hub-administration/Custom_Integrations.html)

## 4. Publish the baseline in Revit

1. Open `Snowdon-Sonne-test-v1.rvt` in Revit 2027. On the River Team workstation
   the file is in `.state/revit-publication-test` under the Sonne checkout.
   Find the existing `-Sonne` schedule containing `SNE-1` and `SNE-2` and
   rename that same view to `Test Rooms-Sonne`. For the example, name the rooms
   Laboratory and Storage and confirm their areas are **60 m²** and **30 m²**.
   The default publication check uses this schedule’s
   existing `UniqueId`. Keep this schedule in the RVT you publish.
2. Choose **Collaborate → Collaborate → In the cloud** and save
   the model in **Sonne Revit Publication Test → Project Files → Sonne Pilot**.
   This step creates the Revit Cloud Worksharing model required by
   `model.publish`. [Revit Cloud Worksharing](https://help.autodesk.com/cloudhelp/2027/ENU/Revit-Cloud/files/GUID-ABFFFEE6-2C6D-4779-B508-A1C4BC0C4885.htm) · [APS publish events](https://aps.autodesk.com/blog/build-your-own-webhooks-revit-cloud-model-changes)
3. In **Revit Home → Autodesk Projects**, find the cloud model and choose
   **Publish Latest** if a publication is pending. Wait until the baseline
   appears in ACC **Data Management → Files**. Open the model there and copy
   its browser URL, or use the URL of the folder containing it. [Publish cloud models](https://help.autodesk.com/cloudhelp/2027/ENU/Revit-Cloud/files/GUID-A2444193-FCE8-4219-A590-62CA2B8B5C5D.htm)

**If In the cloud is greyed out**, select **Cancel**. In Autodesk Account,
open **User Management → By Product → Revit Cloud Worksharing → Assign Users**
and check the assignment. Check project membership and folder permissions
from step 2, then sign out and back in to Revit. Check for the test project in
**Revit Home → Autodesk Projects** before retrying. [Autodesk troubleshooting](https://help.autodesk.com/view/DOCS/ENU/?caas=caas%2Fsfdcarticles%2Fsfdcarticles%2FRevit-Collaborate-using-A360-greyed-out.html)

## 5. Install Sonne and connect ACC in one run

Use one WSL terminal on the machine running Sonne. On the River Team workstation,
run this from PowerShell.

```powershell
wsl -u river --cd /mnt/c/Users/sperm/Documents/ChatGPT/riverteam/Sonne bash scripts/install-sonne.sh --with-acc
```

If you are already in WSL, run this from the Sonne source checkout.

```bash
bash scripts/install-sonne.sh --with-acc
```

For a remote Linux server, connect with
`ssh -L 8765:127.0.0.1:8765 user@sonne-server` and run the same Bash command
in the server's source checkout. Keep that SSH session open while signing in
through the browser on your workstation so the APS OAuth callback reaches
the installer.

The installer sets up the local model, Discord, Prompt Studio, and the Revit
router. It keeps the configured publication worker, schedule and brief if
they already exist. A fresh install uses the Snowdon test schedule in step 4.
The installer starts a Cloudflare Quick Tunnel and puts its HTTPS `/revit/` URL
in the Windows enrollment. After interactive setup the sync and tunnel detach
into a tmux session. Folder mode also registers automatic recovery. On native
Linux the user systemd service starts at boot. On Windows a per-user scheduled
task starts WSL at sign-in. Both renew the APS access token, create a fresh
Revit tunnel and write its URL into managed enrollments. The current Windows
client rereads its local enrollment when reconnecting. For a workstation on
another computer, deliver the new URL separately or use a permanent router.
Revoked or expired Autodesk authorization requires a new interactive sign-in.

On WSL the installer copies the enrollment into the Windows checkout at
`.state/revit-client/config.json` and preserves an existing client configuration.
If the checkout is inside Linux, enter the Windows checkout path when prompted.
For a separate workstation, transfer the printed enrollment file privately to
that same relative path on Windows. For a new workstation, run the
[Revit client installer](README.md#windows-workstation), which also installs
the publication reader. For an enrolled workstation, retain its launch
settings and use the existing enrollment if it already has
`"enablePublications": true`. Keep the client running during the test.
See [upgrading the publication reader](PUBLICATIONS.md#set-up-the-check)
for an older client.

The script uses `cloudflared` from WSL if it is installed. Otherwise it
downloads and verifies Cloudflare’s release for x86-64 or ARM64 and caches it
in the runtime’s `.state/revit-publications-acc/bin` directory. It starts the
relay and temporary tunnel, then checks that the public URL reaches the relay.
[Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) · [Official release checksums](https://github.com/cloudflare/cloudflared/releases/tag/2026.11.1)

Paste the ACC model or folder URL, enter the hub’s region, then enter the
Client ID and Client Secret at the prompts. Open the Autodesk sign-in link
printed by the script in your Windows browser and sign in with the account
that has access to the selected folder. Folder mode refreshes the inventory every
minute and writes readable extracts to the workspace. It supports text-based
PDFs, UTF text, Markdown, CSV, TSV, JSON and XML up to 32 MiB per source file.
For translated RVT, DWG and IFC files it reads one model view's object hierarchy.
The inventory records unavailable formats or downloads. Use a direct model URL
for the schedule alert workflow. To inspect the background session use the
attach command printed at startup. Press Ctrl+B followed by D to detach again.

Wait for `ACC mode is running`. The script prints the model and baseline
version, registers the folder-scoped `adsk.c4r` → `model.publish` webhook,
and stores ACC history and private credentials under the Sonne runtime’s
`.state/revit-publications-acc`. Monitoring continues in the background.
It displays publication and Discord status as they change. Press **Ctrl+C**
to close the relay and tunnel. Sonne and Prompt Studio continue running.

To reconnect the ACC webhook after closing the terminal, run this from the
source checkout in WSL. It starts a new tunnel and updates the webhook for the
same ACC model.

```bash
python3 scripts/setup-revit-acc.py --runtime "$HOME/agents/sonne" --auto
```

The same command works for a runtime with the local publication check already
configured. If Sonne installed successfully but the Revit setup was interrupted
before the room check was configured, use `--install` here in place of `--auto`.

For an existing public HTTPS endpoint, use `--callback URL` in place of
`--auto`. The endpoint must route signed `POST /aps/publish` requests to the
Sonne relay. You can run the relay separately with
`python3 scripts/forward-revit-aps.py` on its default port, 9083.

## 6. Publish the change and check Discord

1. In Prompt Studio's shared Project brief, enter `Room check | SNE-1 | 60 | Laboratory`
   and `Room check | SNE-2 | 30 | Storage` on separate lines, then choose
   **Review prompt → Apply to Sonne**. In the same cloud model, open **Sonne publication test plan**. Move the
   right boundary of the wider test room **1 m inward**. Confirm in the
   schedule that `SNE-1` reads **54 m²**. Rename `SNE-2` to Office and keep its area at **30 m²**.
2. Select **Synchronize with Central**. Then, in **Revit Home → Autodesk
   Projects**, choose **Publish Latest** for the same model. Keep Revit, the
   Sonne Windows client, and the setup terminal running. Sonne processes
   the baseline and the new version after it receives the publish event.
   [APS publish event behavior](https://aps.autodesk.com/blog/build-your-own-webhooks-revit-cloud-model-changes)
3. Watch the publication status in the setup terminal. For a one-time check
   from another WSL terminal in the source checkout, run this command.

   ```bash
   python3 scripts/setup-revit-acc.py --runtime "$HOME/agents/sonne" --status
   ```

   The test passes when the baseline shows `captured` and `Discord: not_needed`,
   and the changed version shows `captured` and `Discord: sent`. The report
   should show `SNE-1` at **54 m²** against **60 m²**, a **−6 m²** difference,
   and flag Office instead of Storage. Both findings share one Discord message. You can also ask Sonne to read
   the result with `revit__revit_room_publications`.

## Fix setup and delivery errors

| Symptom | What to check |
| --- | --- |
| Autodesk reports a redirect URI mismatch | In the APS app settings, use `http://localhost:8765/aps/oauth/callback` exactly, including the port and path. |
| The script cannot download or cache cloudflared | Check WSL’s access to GitHub Releases and confirm you can write to the Sonne runtime directory. |
| Cloudflare Quick Tunnel exits or the installer reports an unreachable URL | Check that `cloudflared` can connect from WSL and rerun `--auto`. If you use `--callback`, verify the tunnel is running and that the URL ends in `/aps/publish`. |
| APS returns `403` during model lookup or RVT access | Check the app’s API access, the active Custom Integration, and whether the Autodesk account used for sign-in can download the test RVT in ACC Files. |
| Status says `Waiting for a matching publish event` | Confirm **Publish Latest** completed on the enrolled cloud model after the installer reported that ACC mode was running. Check that the tunnel stayed up. |
| A publication stays queued | Read the error in `--status`. Check that the configured Windows client is connected, Revit has a model open, and the published archive contains the approved schedule. |
| Discord delivery stays unconfirmed | Check the reported error and the configured bot’s permission to post in the destination channel. Check the channel for the alert before resending it manually. |

To return Sonne to local RVT history after the test, stop the automatic setup
with **Ctrl+C** and run the installer with `--local` in place of `--auto`.
The APS webhook can be removed separately in APS.
