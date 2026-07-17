"""Sentry error tracking with request context (Task 2).

Design constraints
------------------
**It must be impossible for this file to take the app down.** Error tracking is
support machinery; if it breaks, the API must still serve. So every entry point here
degrades to a no-op rather than raising:

* ``sentry_sdk`` not installed  -> no-op (the import is guarded)
* ``SENTRY_DSN`` unset          -> no-op (this is the normal local-dev state)
* Sentry itself throws          -> swallowed and logged

That means you can run, test and deploy this backend with no Sentry account at all,
and turn tracking on later purely by setting one environment variable.

What gets attached to an event
------------------------------
A bare stack trace tells you *what* broke but not *who* hit it or *what they asked
for*, which is the difference between a five-minute fix and an afternoon. Every event
carries:

* **user**     — ``user_id`` from the JWT (never the email/password)
* **endpoint** — the route *template* (``/product/{barcode}``, not ``/product/890...``)
                 so all failures of one route group into a single Sentry issue instead
                 of one issue per barcode
* **request**  — method, path, query string, and a request id echoed back in the
                 ``X-Request-ID`` response header, so a user's bug report ("I got a
                 500, here's the id") maps to exactly one Sentry event

PII
---
``send_default_pii`` is off, and ``before_send`` additionally strips the
``Authorization``/``Cookie`` headers and any ``password``/``token`` field before the
event leaves the process. We send the user's *id*, never their credentials.
"""

from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger("swapify.observability")

try:  # Sentry is optional — the app must run without it.
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
except ImportError:  # pragma: no cover
    sentry_sdk = None
    LoggingIntegration = None

# Header/body keys that must never reach Sentry.
_SCRUB_HEADERS = {"authorization", "cookie", "x-admin-token", "set-cookie"}
_SCRUB_FIELDS = {"password", "token", "access_token", "secret", "admin_token"}

_enabled = False


def _scrub(event, _hint):
    """Strip credentials from an event just before it is sent."""
    try:
        request = event.get("request") or {}

        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in _SCRUB_HEADERS:
                    headers[key] = "[redacted]"

        data = request.get("data")
        if isinstance(data, dict):
            for key in list(data):
                if key.lower() in _SCRUB_FIELDS:
                    data[key] = "[redacted]"
    except Exception as exc:  # never let scrubbing break delivery
        logger.warning("sentry before_send scrub failed: %s", exc)
    return event


def init_sentry() -> bool:
    """Initialise Sentry from the environment. Returns True when tracking is live.

    Env:
        SENTRY_DSN          the project DSN. **Unset disables Sentry entirely.**
        SENTRY_ENVIRONMENT  e.g. "production" / "development" (default "development")
        SENTRY_RELEASE      release identifier, e.g. a git sha (optional)
        SENTRY_TRACES_SAMPLE_RATE  performance-trace sampling, 0.0-1.0 (default 0.0)
    """
    global _enabled

    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.info("SENTRY_DSN unset - error tracking disabled (this is fine locally).")
        return False
    if sentry_sdk is None:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; "
                       "run: pip install 'sentry-sdk[fastapi]'")
        return False

    try:
        rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0") or 0)
    except ValueError:
        rate = 0.0

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "development"),
            release=os.environ.get("SENTRY_RELEASE") or None,
            traces_sample_rate=rate,
            # We attach the user id ourselves; never let the SDK harvest bodies,
            # cookies or IPs on its own.
            send_default_pii=False,
            # Do NOT ship local variables with stack traces.
            #
            # This is not paranoia — it was caught by an actual test. Sentry captures
            # every frame's locals by default, and the raw ASGI ``scope`` dict (which
            # embeds the complete header list, Authorization and X-Admin-Token included)
            # is a local in ~20 Starlette/FastAPI frames on any request that raises. The
            # credentials therefore reach Sentry *even though* ``before_send`` redacts
            # ``request.headers``, because they ride along a second time inside the
            # frame locals.
            #
            # Scrubbing those by name is unwinnable: they live in framework internals
            # (``scope``, ``conn``, ``solved_result``, ...) that we neither own nor
            # control across upgrades. Dropping locals entirely kills the whole class of
            # leak. We keep the full stack trace — file, function, line, source context —
            # plus the user/endpoint/request context attached below, which is what is
            # actually needed to chase a bug.
            include_local_variables=False,
            before_send=_scrub,
            # Breadcrumbs from WARNING+, and send ERROR+ logs as events, so a
            # logger.error() in a route shows up without an explicit capture call.
            integrations=[LoggingIntegration(level=logging.WARNING,
                                             event_level=logging.ERROR)]
            if LoggingIntegration else [],
        )
    except Exception as exc:  # pragma: no cover - never fatal
        logger.warning("Sentry init failed, continuing without it: %s", exc)
        return False

    _enabled = True
    logger.info("Sentry initialised (environment=%s).",
                os.environ.get("SENTRY_ENVIRONMENT", "development"))
    return True


def is_enabled() -> bool:
    return _enabled


def install_request_context(app, decode_user_id) -> None:
    """Attach user/endpoint/request context to every event, and tag responses.

    ``decode_user_id(auth_header)`` is injected rather than imported so this module
    stays independent of the app's auth internals (and unit-testable on its own).

    The middleware runs whether or not Sentry is live: it always assigns the request
    id and the ``X-Request-ID`` response header, which is useful for plain log
    correlation even with tracking switched off.
    """
    # Local imports: keep this module importable even without Starlette present.
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @app.middleware("http")
    async def _sentry_request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id

        if _enabled and sentry_sdk is not None:
            try:
                scope = sentry_sdk.get_current_scope()

                # The route *template*, so every failure of this route lands in one
                # Sentry issue. Falls back to the raw path before routing resolves.
                route = request.scope.get("route")
                endpoint = getattr(route, "path", None) or request.url.path

                scope.set_tag("endpoint", endpoint)
                scope.set_tag("method", request.method)
                scope.set_tag("request_id", request_id)
                scope.set_context("request_details", {
                    "method": request.method,
                    "path": request.url.path,
                    "query": str(request.url.query or ""),
                    "endpoint": endpoint,
                    "client": request.client.host if request.client else None,
                })

                user_id = decode_user_id(request.headers.get("Authorization"))
                # id only — never the email or anything else identifying.
                scope.set_user({"id": user_id} if user_id else None)
            except Exception as exc:  # pragma: no cover
                logger.warning("sentry context enrichment failed: %s", exc)

        try:
            response = await call_next(request)
        except Exception:
            # Capture explicitly so the event carries the scope built above rather
            # than a bare trace.
            if _enabled and sentry_sdk is not None:
                sentry_sdk.capture_exception()
            logger.exception("Unhandled error [request_id=%s] %s %s",
                             request_id, request.method, request.url.path)

            # Answer here instead of re-raising into Starlette's default 500. If we
            # re-raise, the response is generated above this middleware and never
            # gets the X-Request-ID header — losing the id on precisely the responses
            # where it matters. Returning it ourselves means a user can quote the id
            # from a failed request and we can pull up that exact Sentry event.
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )

        response.headers["X-Request-ID"] = request_id
        return response


def capture_message(message: str, level: str = "info", **tags) -> None:
    """Record a non-exception event (no-op when Sentry is off)."""
    if not (_enabled and sentry_sdk is not None):
        return
    try:
        scope = sentry_sdk.get_current_scope()
        for key, value in tags.items():
            scope.set_tag(key, value)
        sentry_sdk.capture_message(message, level=level)
    except Exception as exc:  # pragma: no cover
        logger.warning("sentry capture_message failed: %s", exc)
