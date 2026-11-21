import logging
from django.contrib.auth import logout
from django.shortcuts import redirect

logger = logging.getLogger(__name__)


class DirectNavigationLogoutMiddleware:
    """End existing sessions on a new address-bar navigation."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and request.method == "GET"
            and request.headers.get("Sec-Fetch-Mode") == "navigate"
            and request.headers.get("Sec-Fetch-Dest") == "document"
            and request.headers.get("Sec-Fetch-Site") == "none"
        ):
            logout(request)
            return redirect("login")
        return self.get_response(request)

class AutoLogoutMiddleware:
    """Logout the user after each response.

    This ensures that any page refresh (or new request) will see the user as logged out,
    forcing the login page to appear again. The current view is rendered using the
    existing authenticated session, but the session is cleared immediately after
    the response is generated.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        logger.debug("AutoLogoutMiddleware initialized")

    def __call__(self, request):
        response = self.get_response(request)
        # If the user is authenticated, log them out before the next request.
        if hasattr(request, "user") and request.user.is_authenticated:
            logout(request)
            logger.debug("User %s logged out by AutoLogoutMiddleware", request.user.username)
        return response

