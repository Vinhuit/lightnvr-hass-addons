# Installation

## Local installation before publishing

1. On a current Home Assistant OS/Supervisor installation, copy **this
   entire `lightnvr` directory from this add-on repository** to `/local_apps/lightnvr` through the
   SSH app, or the `local_apps` Samba share. The resulting file is
   `/local_apps/lightnvr/config.yaml`.
2. Open **Settings → Apps → App Store** (called **Add-ons** on older releases),
   then use **Check for updates** or reload the store. LightNVR should appear
   under **Local apps**.
3. Install LightNVR. Supervisor downloads the prebuilt image containing
   LightNVR, go2rtc, object detection, face recognition, classification, and YTStream.
4. Optionally set `initial_admin_password` before the first start; otherwise
   a random password is printed once in the add-on log.
5. Start the add-on, enable **Show in sidebar**, and open its web interface.
6. Log in as `admin`, then add cameras in **Streams**. By default a LightNVR login is
   required inside Ingress so the NVR retains its own user permissions.

## Repository installation

Add `https://github.com/Vinhuit/lightnvr-hass-addons` under the App Store's
**Repositories** menu. Then install **LightNVR** from that repository.

# Options

```yaml
retention_days: 30
log_level: info
initial_admin_password: ""
reset_admin_password: false
auth_enabled: true
# Optional:
# onvif_network: 192.168.1.0/24
# webrtc_external_ip: 192.168.1.10
```

| Option | Meaning |
| --- | --- |
| `retention_days` | Recording retention, 1–3650 days. Applied at every start. |
| `log_level` | `error`, `warning`, `info`, or `debug`. Applied at every start. |
| `initial_admin_password` | Used only when creating a new configuration. Empty generates a random password. A supplied password must contain at least 12 characters and at most 31 UTF-8 bytes with no whitespace, `;`, or `#`. Change existing users' passwords in LightNVR Users. |
| `auth_enabled` | Default `true`. Set `false` to skip LightNVR login on both Ingress and the direct web port. Home Assistant authentication still applies to Ingress. |
| `reset_admin_password` | Recovery only: reset existing admin to `initial_admin_password` at startup, backing up the database and revoking admin sessions. Disable after login. |
| `onvif_network` | Optional IPv4 camera network in CIDR notation. Enables discovery; adding cameras by RTSP URL works without it. Bridge networking may limit multicast discovery. |
| `webrtc_external_ip` | Optional reachable host IP advertised to WebRTC clients. Use your Home Assistant LAN IP if video cannot connect. |

Optional ONVIF and external-IP values update the corresponding
LightNVR settings when supplied. Omitting them preserves the existing NVR
configuration; clear them in LightNVR Settings when no longer needed.
Home Assistant provides the container timezone through `TZ`.

# Internal AI

This combined container is specific to the Home Assistant add-on. The
standalone Docker Compose deployment still uses separate services.

The add-on includes light-object-detect. LightNVR calls it on
`http://127.0.0.1:8000` for object detection, face recognition, and local
classification. Port 8000 is bound only to loopback and is not exposed to
Home Assistant's network or the host. No separate detector installation or
`detection_server` option is needed.

YOLO26 and InsightFace weights are included in the image. Face identities,
training samples, and classification artifacts are stored in `/data/detector`
and survive add-on updates. On upgrade, the global AI endpoints are set to
the internal service. Existing face-recognition and classification enable
settings are preserved; enable the features in LightNVR Settings as needed.

Previous face identities stored in a separate detector are not automatically
imported; register identities in the add-on's Faces page.

# Internal YouTube streaming

YTStream runs inside this add-on on `127.0.0.1:8081`. LightNVR automatically
syncs with `http://127.0.0.1:8081/api/sync`, and YTStream reads camera restreams
from `rtsp://127.0.0.1:8554`. Port 8081 is not exposed to the host.

Open **YTStream** from the LightNVR footer or YouTube Live View. Its dashboard
is served under the same Home Assistant Ingress session. To connect YouTube:

1. Open Home Assistant using your HTTPS address.
2. In Google Cloud, enable the YouTube Data API and create an OAuth client of
   type **Web application**. Configure the consent screen for your account.
3. Copy the **Authorized redirect URI** shown on the dashboard into that OAuth
   client's redirect URI list, then download its client JSON.
4. Choose **Upload Google OAuth client JSON**, then **Authorize** and approve
   Google access for the account you want to use.
5. Enable YouTube streaming for the cameras you want to publish in LightNVR.

Accounts, OAuth tokens, stream settings, retry state, logs, and broadcast
history persist under `/data/ytstream` and are included in the add-on backup.
They are not copied from a separate host's ytstream container. The app does
not automatically authorize Google or enable camera publishing on install.
YTStream's account selector supports multiple accounts.

# Network

Ingress uses an internal proxy on port 8099; it accepts connections only
from Supervisor (`172.30.32.2`). It is not published to the host. The default
installation exposes only WebRTC media on TCP/UDP 8555. If another service
already uses that port, resolve the conflict before starting.

Under the add-on's **Network** settings, you can optionally enable:

- `8080/tcp`: direct LightNVR web interface. LightNVR authentication follows `auth_enabled`.
- `8554/tcp`: RTSP restream, for example `rtsp://HA_IP:8554/camera-name`.

Keep the WebRTC host mapping at 8555, matching the configured ICE port.
Ingress transports the signaling and WebSocket requests; the camera media
still needs a reachable TCP/UDP media port. HLS is available when WebRTC is
unsuitable. The go2rtc API is intentionally kept inside the container.

# Persistence and backup

| Data | Container path |
| --- | --- |
| Editable INI configuration and go2rtc overrides | `/etc/lightnvr` (Supervisor add-on configuration volume) |
| Database and migrations state | `/data/database` |
| Models | `/data/models` |
| Face identities and classification training | `/data/detector` |
| YouTube accounts, credentials, settings, logs and history | `/data/ytstream` |
| HLS cache | `/data/hls` |
| Application logs | `/data/log` |
| Recordings | `/media/lightnvr/recordings` |

Do not mount over `/var/lib/lightnvr/www`; it contains the built frontend.
Startup preserves custom stream definitions and other user settings, while
enforcing the storage paths and ports that the add-on needs.

The add-on uses a **cold backup** so Supervisor stops LightNVR before
backing up its database. Recordings are in Home Assistant's shared Media
directory; include **Media** in a full Home Assistant backup, or back up
recordings separately. An add-on-only backup does not cover shared Media.

# Development

The Dockerfile pins the public enriched image by digest for reproducible
runtime selection. Its frontend already supports Home Assistant's Ingress
base path. This image is amd64 only; do not add ARM to `arch` before an ARM
image with the same functionality is published and tested.

```sh
python -m unittest discover -s tests -p test_hass_addon.py
docker build -t lightnvr-hass-addon:0.3.0 lightnvr
python scripts/test_hass_addon.py
python scripts/package_hass_addon.py
```

The root `repository.yaml` makes this source repository discoverable by
Supervisor. No `image` field is set in `config.yaml`, so Supervisor builds
the wrapper from this directory without requiring an add-on image release.

This wrapper uses the current `app_config` volume type and `io.hass.type=app`
label. Update Supervisor before installing on an older Home Assistant release.

The Docker smoke test uses disposable containers and a mock Supervisor.
If its default subnet overlaps an existing network, supply
`--test-subnet 172.29.32.0/29` (or another unused `/29`). This changes only
the trusted address in the test fixture; the installed add-on always allows
the real Supervisor address `172.30.32.2`. It verifies source filtering,
login, session cookies, WebSocket upgrades, graceful shutdown and database
and media persistence after container recreation.

The Docker build argument `LIGHTNVR_IMAGE` may be overridden for development;
the replacement must provide `/bin/lightnvr`, `/bin/go2rtc`, the
`--generate-go2rtc-config` command, and frontend assets with Ingress support.

# References and license

- [Frigate Home Assistant add-ons](https://github.com/blakeblackshear/frigate-hass-addons)
- [Home Assistant add-on configuration](https://developers.home-assistant.io/docs/add-ons/configuration/)
- [Home Assistant Ingress requirements](https://developers.home-assistant.io/docs/add-ons/presentation/#ingress)

This wrapper follows the repository's GPL-3.0 license. It does not install
ytstream or an external AI detection server; those are separate services.

# Forgotten admin password on HAOS

Editing `[web] password` in `lightnvr.ini` does not change an existing database
user. Authentication follows the add-on `auth_enabled` option at startup, so editing
only `auth_enabled` in the INI is overridden. Use the recovery option instead:

1. Update the add-on to 0.1.2 or later.
2. In its Configuration tab, set `initial_admin_password` to your new password
   (at least 12 characters, at most 31 UTF-8 bytes, without whitespace, `;`, or `#`).
3. Enable `reset_admin_password`, save, and restart the add-on.
4. Sign in as `admin` using that password.
5. Disable `reset_admin_password`, save, then restart again. Leaving it enabled
   resets the password and revokes admin sessions at every start.

The reset preserves cameras, recordings and other users. It does not disable MFA
or change roles or account restrictions. A database backup is saved under
`/data/database/lightnvr.before-password-reset-<timestamp>.db`. The new password
is never printed in logs. If the database has no `admin` user, recovery stops
with an error instead of modifying another account.

The bundled LightNVR binary prints a legacy `Password: admin` banner even when
it creates the admin with the configured password. Use the configured password
or the add-on first-run password, rather than that static banner.

# Skip LightNVR login

In the add-on Configuration tab, set `auth_enabled: false`, save and restart.
Ingress still requires Home Assistant login. This also disables LightNVR login
on port 8080 if that direct port is exposed. Existing users and passwords are
retained. Set `auth_enabled: true` and restart to restore LightNVR login.
Editing only the INI is overwritten by the add-on option on the next start.
