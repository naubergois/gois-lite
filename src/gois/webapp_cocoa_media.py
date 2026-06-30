"""Microphone capture for the Gois macOS desktop shell (pywebview / WKWebView)."""

from __future__ import annotations

import logging

from .platform_paths import is_macos

logger = logging.getLogger("gois.webapp")


def prime_macos_microphone_access() -> None:
    """Trigger the macOS TCC prompt before WKWebView asks for capture."""
    if not is_macos():
        return
    try:
        import AVFoundation
    except Exception as exc:
        logger.debug("AVFoundation unavailable: %s", exc)
        return

    try:
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio
        )
        not_determined = getattr(AVFoundation, "AVAuthorizationStatusNotDetermined", 0)
        if int(status) != int(not_determined):
            return

        def _done(granted: bool) -> None:
            logger.info("microphone permission granted=%s", bool(granted))

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio,
            _done,
        )
    except Exception:
        logger.exception("microphone permission request failed")


def enable_cocoa_media_capture() -> bool:
    """Let WKWebView use getUserMedia / MediaRecorder in the desktop app."""
    if not is_macos():
        return False

    try:
        import AVFoundation
        import WebKit
        from webview.platforms import cocoa
    except Exception as exc:
        logger.debug("cocoa media patch unavailable: %s", exc)
        return False

    delegate_cls = cocoa.BrowserView.BrowserDelegate
    if getattr(delegate_cls, "_gois_media_patched", False):
        return True

    grant = getattr(WebKit, "WKPermissionDecisionGrant", 1)
    deny = getattr(WebKit, "WKPermissionDecisionDeny", 2)
    authorized = getattr(AVFoundation, "AVAuthorizationStatusAuthorized", 3)
    not_determined = getattr(AVFoundation, "AVAuthorizationStatusNotDetermined", 0)

    def _decide(granted: bool, decision_handler) -> None:
        try:
            decision_handler(grant if granted else deny)
        except Exception:
            logger.exception("WKWebView media decision handler failed")

    def patched_request_media(self, webview, origin, frame, media_type, decision_handler):
        try:
            status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                AVFoundation.AVMediaTypeAudio
            )
            if int(status) == int(authorized):
                _decide(True, decision_handler)
                return
            if int(status) == int(not_determined):

                def _after(ok: bool) -> None:
                    _decide(bool(ok), decision_handler)

                AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                    AVFoundation.AVMediaTypeAudio,
                    _after,
                )
                return
            _decide(False, decision_handler)
        except Exception:
            logger.exception("WKWebView media capture permission failed")
            _decide(False, decision_handler)

    delegate_cls.webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_ = (
        patched_request_media
    )
    delegate_cls._gois_media_patched = True
    logger.info("enabled cocoa media capture for pywebview")
    prime_macos_microphone_access()
    return True
