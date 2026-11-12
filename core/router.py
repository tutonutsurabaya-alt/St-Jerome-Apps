"""Lightweight route collector preserving flat Flask endpoint names."""


class ModuleRouter:
    """Collects Flask routes in a domain module and registers them on ``app``."""

    def __init__(self, name):
        self.name = name
        self.rules = []

    def route(self, rule, **options):
        """Decorator matching ``@app.route`` while keeping function global at module top level."""
        def decorator(func):
            self.rules.append((rule, options, func))
            return func
        return decorator

    def register(self, app):
        """Bind all collected routes onto the Flask application instance."""
        for rule, options, func in self.rules:
            opts = dict(options)
            endpoint = opts.pop('endpoint', func.__name__)
            app.add_url_rule(rule, endpoint=endpoint, view_func=func, **opts)
