# LightNVR Home Assistant Add-ons

Run LightNVR in Home Assistant with Ingress, go2rtc live views, and persistent
camera recordings. This repository packages the enriched LightNVR image from
[Vinhuit/lightNVR](https://github.com/Vinhuit/lightNVR).

## Install

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FVinhuit%2Flightnvr-hass-addons)

Or open **Settings → Apps → App Store → Repositories** in Home Assistant
(called **Add-ons** on older releases), and add:

```text
https://github.com/Vinhuit/lightnvr-hass-addons
```

Install **LightNVR**, configure its options, start it, and enable **Show in
sidebar**. If you leave `initial_admin_password` empty, the first-run password
is printed once in the add-on log. Home Assistant OS or Supervisor is required.

## Included add-on

| Add-on | Architecture | Features |
| --- | --- | --- |
| [LightNVR](lightnvr) | amd64 / x86-64 | Ingress, WebRTC, MSE, HLS, RTSP restream, camera recording |

- [Installation and configuration](lightnvr/DOCS.md)
- [Changelog](lightnvr/CHANGELOG.md)

LightNVR, go2rtc, and light-object-detect run together in one add-on.
Object detection, face recognition, and local classification use an internal
loopback service; no separate AI container or port 8000 mapping is needed.
YOLO26 and InsightFace models are included in the image.

Recordings are stored in `/media/lightnvr/recordings`. The database,
configuration, face identities, and classifier models survive add-on
recreation. YouTube streaming uses the optional external ytstream service.

This packaging applies to the Home Assistant add-on. The standalone host
deployment continues to run LightNVR, light-object-detect, and ytstream as
separate Docker Compose services.

This is an experimental add-on. Docker tests cover login, Ingress,
WebSocket upgrades, shutdown and data persistence, and the browser has been
tested under a simulated Ingress path. Installation in a real Home Assistant
Supervisor instance has not yet been verified.

## Development

```sh
python -m pip install PyYAML==6.0.2
python -m unittest discover -s tests -p test_hass_addon.py
docker build -t lightnvr-hass-addon:0.2.0 lightnvr
python scripts/test_hass_addon.py
python scripts/package_hass_addon.py
```

The Dockerfile pins the public LightNVR and detector runtimes by digest.
Supervisor downloads the prebuilt combined image. CI validates configuration,
runs ShellCheck, builds the image, and tests Ingress, internal AI inference,
loopback isolation, and persistent data in disposable containers.

## Credits and license

Inspired by [Frigate hass-addons](https://github.com/blakeblackshear/frigate-hass-addons).
LightNVR source: [Vinhuit/lightNVR](https://github.com/Vinhuit/lightNVR).
Licensed under [GPL-3.0](LICENSE).
