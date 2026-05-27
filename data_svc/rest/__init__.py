"""REST gateway package.

Thin adapter layer over the shared service modules
(`data_svc.db.cache.BarCache`, `data_svc.db.assets.AssetsRepo`,
`data_svc.services.quote.QuoteService`). Routers must not contain business
logic — they translate HTTP requests into service calls and back.

Entry point: `python -m data_svc.rest` (see `__main__.py`).
"""
