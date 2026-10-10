# External runtime modules

The full Pod wrapper contains only a generic module loader. Applications provide
POD_MODULE_BUNDLE_URL (HTTPS), POD_MODULE_BUNDLE_SHA256 and optionally
POD_MODULE_BUNDLE_TOKEN. The loader verifies a bounded manifest-first archive and
all file hashes before executing its entrypoint. Redirects and links are rejected.
No application gateway, billing, task scheduling or model-cache code is embedded.
Without configured modules the wrapper fails closed; use the base image for
standalone ComfyUI. Never embed object-store credentials in image builds.
