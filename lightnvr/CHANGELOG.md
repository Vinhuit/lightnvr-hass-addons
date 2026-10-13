# Changelog

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
