"""Low-overhead response timing for browser performance diagnostics."""

from time import perf_counter


class ServerTimingMiddleware:
    """Expose Django response time in the standard Server-Timing header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = perf_counter()
        response = self.get_response(request)
        if not request.path.startswith("/static/"):
            elapsed_ms = (perf_counter() - started) * 1000
            timing = f"django;dur={elapsed_ms:.1f}"
            existing = response.headers.get("Server-Timing")
            response.headers["Server-Timing"] = f"{existing}, {timing}" if existing else timing
        return response
