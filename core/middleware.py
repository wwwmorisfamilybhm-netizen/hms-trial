import logging
from django.contrib.auth import logout

logger = logging.getLogger(__name__)

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

