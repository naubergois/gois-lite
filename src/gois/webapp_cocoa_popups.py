"""Enable window.open() popups for the Gois macOS desktop app (pywebview / WKWebView)."""

from __future__ import annotations

import logging
import os

from .platform_paths import is_macos

logger = logging.getLogger("gois.webapp")

# Keep popup windows alive until the user closes them (WebKit only retains the webview).
_POPUP_SESSIONS: list[tuple[object, object, object]] = []
# Keep WKDownload objects alive for the duration of each download (WebKit does not retain them).
_ACTIVE_DOWNLOADS: list[object] = []


def enable_cocoa_popups() -> bool:
    """Patch pywebview's Cocoa UIDelegate so JS popups get a native window."""
    if not is_macos():
        return False

    try:
        import objc
        import AppKit
        import Foundation
        import WebKit
        from objc import nil
        from webview.platforms import cocoa
    except Exception as exc:
        logger.debug("cocoa popup patch unavailable: %s", exc)
        return False

    try:
        _wk_download_proto = objc.protocolNamed("WKDownloadDelegate")
    except Exception:
        _wk_download_proto = None

    browser_view = cocoa.BrowserView
    delegate_cls = browser_view.BrowserDelegate
    if getattr(delegate_cls, "_gois_popups_patched", False):
        return True

    original_create = (
        delegate_cls.webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_
    )
    link_activated = getattr(WebKit, "WKNavigationTypeLinkActivated", 0)
    allow_action = getattr(WebKit, "WKNavigationActionPolicyAllow", 1)
    allow_response = getattr(WebKit, "WKNavigationResponsePolicyAllow", 1)
    download_action = getattr(WebKit, "WKNavigationActionPolicyDownload", 2)
    download_response = getattr(WebKit, "WKNavigationResponsePolicyDownload", 2)
    modal_ok = getattr(AppKit, "NSModalResponseOK", 1)

    def _drop_download(download) -> None:
        global _ACTIVE_DOWNLOADS
        _ACTIVE_DOWNLOADS[:] = [d for d in _ACTIVE_DOWNLOADS if d is not download]

    def _present_save_panel(suggested_name: str):
        """Show an NSSavePanel and return the chosen NSURL (or nil)."""
        panel = AppKit.NSSavePanel.savePanel()
        if suggested_name:
            panel.setNameFieldStringValue_(str(suggested_name))
        downloads = os.path.expanduser("~/Downloads")
        if os.path.isdir(downloads):
            panel.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(downloads))
        AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
            AppKit.NSApplicationActivateIgnoringOtherApps
        )
        if panel.runModal() != modal_ok:
            return nil
        url = panel.URL()
        # WKDownload refuses to write if the destination already exists; the panel
        # already confirmed any overwrite, so clear the old file first.
        try:
            fm = Foundation.NSFileManager.defaultManager()
            if url is not None and fm.fileExistsAtPath_(url.path()):
                fm.removeItemAtURL_error_(url, None)
        except Exception:
            logger.exception("failed to clear existing download destination")
        return url

    class PopupWindowDelegate(AppKit.NSObject):
        def windowWillClose_(self, notification):
            window = notification.object()
            webview = window.contentView()
            if webview is not None:
                webview.setUIDelegate_(None)
                webview.setNavigationDelegate_(None)
            global _POPUP_SESSIONS
            _POPUP_SESSIONS[:] = [
                session for session in _POPUP_SESSIONS if session[0] is not window
            ]

    _popup_delegate_bases = (AppKit.NSObject,)
    _popup_delegate_kwargs = {"protocols": [_wk_download_proto]} if _wk_download_proto else {}

    class PopupBrowserDelegate(*_popup_delegate_bases, **_popup_delegate_kwargs):
        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, webview, config, action, features
        ):
            try:
                return _create_popup_window(webview, config, action, features)
            except Exception:
                logger.exception("failed to open nested cocoa popup")
                return nil

        def webView_decidePolicyForNavigationAction_decisionHandler_(
            self, webview, action, handler
        ):
            # An <a download> click (macOS 11.3+) flags the action; route it to a
            # native WKDownload instead of trying to navigate the popup to a file.
            try:
                if action.respondsToSelector_("shouldPerformDownload") and (
                    action.shouldPerformDownload()
                ):
                    handler(download_action)
                    return
            except Exception:
                logger.exception("decidePolicyForNavigationAction failed")
            handler(allow_action)

        def webView_decidePolicyForNavigationResponse_decisionHandler_(
            self, webview, navigationResponse, decisionHandler
        ):
            # MIME types WebKit cannot render (e.g. application/octet-stream from
            # the team-file download endpoint) become native downloads.
            try:
                if not navigationResponse.canShowMIMEType():
                    decisionHandler(download_response)
                    return
            except Exception:
                logger.exception("decidePolicyForNavigationResponse failed")
            decisionHandler(allow_response)

        def webView_navigationAction_didBecomeDownload_(self, webview, action, download):
            download.setDelegate_(self)
            _ACTIVE_DOWNLOADS.append(download)

        def webView_navigationResponse_didBecomeDownload_(self, webview, response, download):
            download.setDelegate_(self)
            _ACTIVE_DOWNLOADS.append(download)

        # --- WKDownloadDelegate ---
        def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(
            self, download, response, suggested, handler
        ):
            try:
                handler(_present_save_panel(str(suggested or "download")))
            except Exception:
                logger.exception("download destination handler failed")
                handler(nil)

        def downloadDidFinish_(self, download):
            _drop_download(download)

        def download_didFailWithError_resumeData_(self, download, error, resumeData):
            _drop_download(download)

        def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(
            self, webview, message, frame, handler
        ):
            AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
                AppKit.NSApplicationActivateIgnoringOtherApps
            )
            alert = AppKit.NSAlert.alloc().init()
            alert.setInformativeText_(str(message))
            alert.runModal()
            handler()

        def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
            self, webview, message, frame, handler
        ):
            result = browser_view.display_confirmation_dialog("OK", "Cancel", str(message))
            handler(result)

        def webViewDidClose_(self, webview):
            window = webview.window()
            if window is not None:
                window.close()

    def _feature_value(features, name: str, default: float) -> float:
        raw = getattr(features, name, None)
        if raw in (None, nil):
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _popup_title(action) -> str:
        try:
            url = action.request().URL().absoluteString()
            if url:
                return str(url)
        except Exception:
            pass
        return "Gois"

    def _create_popup_window(webview, config, action, features):
        width = max(320.0, min(_feature_value(features, "width", 900.0), 2400.0))
        height = max(240.0, min(_feature_value(features, "height", 700.0), 1800.0))
        rect = AppKit.NSMakeRect(0.0, 0.0, width, height)
        window_mask = (
            AppKit.NSTitledWindowMask
            | AppKit.NSClosableWindowMask
            | AppKit.NSMiniaturizableWindowMask
            | AppKit.NSResizableWindowMask
        )
        # Plain NSWindow — pywebview's WindowHost requires a .focus attribute and
        # is tied to BrowserView lifecycle; popups are independent native windows.
        popup_window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, window_mask, AppKit.NSBackingStoreBuffered, False
        )
        popup_window.setReleasedWhenClosed_(False)
        popup_window.setTitle_(_popup_title(action))

        x = getattr(features, "x", None)
        y = getattr(features, "y", None)
        if x not in (None, nil) and y not in (None, nil):
            try:
                popup_window.setFrameOrigin_(AppKit.NSPoint(float(x), float(y)))
            except (TypeError, ValueError):
                popup_window.center()
        else:
            parent_window = webview.window()
            if parent_window is not None:
                parent_frame = parent_window.frame()
                popup_window.setFrameOrigin_(
                    AppKit.NSPoint(parent_frame.origin.x + 24.0, parent_frame.origin.y - 24.0)
                )
            else:
                popup_window.center()

        popup_webview = browser_view.WebKitHost.alloc().initWithFrame_configuration_(rect, config)
        popup_delegate = PopupBrowserDelegate.alloc().init()
        window_delegate = PopupWindowDelegate.alloc().init()
        popup_webview.setUIDelegate_(popup_delegate)
        popup_webview.setNavigationDelegate_(popup_delegate)
        popup_window.setDelegate_(window_delegate)
        popup_window.setContentView_(popup_webview)
        _POPUP_SESSIONS.append((popup_window, popup_delegate, window_delegate))
        popup_window.makeKeyAndOrderFront_(None)
        AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
            AppKit.NSApplicationActivateIgnoringOtherApps
        )
        logger.info("opened cocoa popup %.0fx%.0f", width, height)
        return popup_webview

    def patched_create_webview(self, webview, config, action, features):
        if action.navigationType() == link_activated:
            return original_create(self, webview, config, action, features)
        try:
            return _create_popup_window(webview, config, action, features)
        except Exception:
            logger.exception("failed to open cocoa popup")
            return nil

    # Only patch createWebView on the main BrowserDelegate. Popup windows get their
    # own PopupBrowserDelegate (navigation + UI). Patching decidePolicy here with
    # plain Python functions breaks PyObjC 12 block signatures and crashes the app
    # on startup ("cannot call block without a signature").
    delegate_cls.webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_ = (
        patched_create_webview
    )
    delegate_cls._gois_popups_patched = True
    logger.info("enabled cocoa popup windows for pywebview")
    return True
