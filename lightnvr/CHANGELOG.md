# Changelog

## 0.3.0

- Bundle YTStream alongside LightNVR, go2rtc, and the internal detector.
- Keep YTStream's API and RTSP source on loopback, without publishing port 8081.
- Add the YTStream dashboard and Google OAuth client upload through Home Assistant Ingress.
- Persist YouTube accounts, tokens, retry state, settings, logs, and history under `/data/ytstream`.
- Automatically configure internal YouTube sync and remove `ytstream_server`.
- Stop YTStream workers and FFmpeg with the add-on; supervise the YTStream service.
- Standalone host deployments keep their separate containers.

## 0.2.0

- Bundle LightNVR, go2rtc, and light-object-detect in one add-on.
- Publish a prebuilt add-on image so Home Assistant does not build AI dependencies locally.
- Run object detection, face recognition, and custom classification internally on loopback port 8000.
- Include YOLO26 and InsightFace models in the image for local inference.
- Store face identities and classification models under persistent `/data/detector`.
- Automatically configure internal AI endpoints; remove the external `detection_server` option.
- Stop all services together and restart the add-on if the internal detector exits.

## 0.1.4

- Fix horizontal overflow in Live View mode tabs, filters, and the grid picker on mobile.
- Use the same responsive camera grid across WebRTC, HLS, MSE, and YouTube.
- Keep tablet navigation in the mobile menu and wrap the footer on narrow screens.
- Pin the enriched runtime image containing the mobile layout fixes.

## 0.1.3

- Add `auth_enabled` to optionally skip the LightNVR login. Default remains enabled.
- Preserve existing admin credentials when toggling authentication.

## 0.1.2

- Limit initial passwords to 31 characters, matching LightNVR's INI password buffer.
- Generate 30-character first-run passwords to avoid silent truncation.
- Log recovery requests when initializing a new database as well.

## 0.1.1

- Recover the existing admin password from Home Assistant options without Docker or SSH.
- Back up the database and revoke admin sessions before applying a password reset.

## 0.1.0

- Initial amd64 Home Assistant add-on for the enriched LightNVR image.
- Ingress proxy with WebSocket support and Supervisor-only access.
- Persistent configuration and database, plus recordings in Media.
- Retention, logging, external detection, ONVIF and WebRTC configuration.
- Graceful shutdown of LightNVR and the Ingress proxy.
