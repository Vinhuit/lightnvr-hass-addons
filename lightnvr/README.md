# LightNVR for Home Assistant

LightNVR records IP cameras, provides live views through go2rtc, and manages
recordings from the Home Assistant sidebar. This add-on packages the enriched
LightNVR image used by this repository. It supports **amd64/x86-64**.

- Home Assistant Ingress, including WebSocket and streaming requests.
- Persistent configuration, database, models, and recording history.
- Recordings under `/media/lightnvr/recordings`.
- Optional RTSP restream and direct web access.
- External detection service configuration for AI processing.

See [DOCS.md](DOCS.md) for installation and configuration. This is an
experimental first release; Home Assistant OS or a Supervisor installation
is required. Home Assistant Container by itself cannot install add-ons.

Inspired by the layout and persistent-volume configuration of
[frigate-hass-addons](https://github.com/blakeblackshear/frigate-hass-addons).
The startup scripts and configuration in this directory are specific to LightNVR.
