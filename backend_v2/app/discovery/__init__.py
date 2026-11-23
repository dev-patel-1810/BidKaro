"""The scraping module.

The base has no browser dependency: this package imports Playwright lazily in
``browser`` and registers its routes and job handler only through
``app.discovery.register``.
"""
