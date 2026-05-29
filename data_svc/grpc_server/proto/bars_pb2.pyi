from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Bar(_message.Message):
    __slots__ = ("ts", "open", "high", "low", "close", "volume")
    TS_FIELD_NUMBER: _ClassVar[int]
    OPEN_FIELD_NUMBER: _ClassVar[int]
    HIGH_FIELD_NUMBER: _ClassVar[int]
    LOW_FIELD_NUMBER: _ClassVar[int]
    CLOSE_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    def __init__(self, ts: _Optional[int] = ..., open: _Optional[float] = ..., high: _Optional[float] = ..., low: _Optional[float] = ..., close: _Optional[float] = ..., volume: _Optional[float] = ...) -> None: ...

class GetRecentBarsRequest(_message.Message):
    __slots__ = ("symbol", "timeframe", "count")
    SYMBOL_FIELD_NUMBER: _ClassVar[int]
    TIMEFRAME_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    symbol: str
    timeframe: str
    count: int
    def __init__(self, symbol: _Optional[str] = ..., timeframe: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class GetBarsInRangeRequest(_message.Message):
    __slots__ = ("symbol", "timeframe", "from_ts", "to_ts", "limit")
    SYMBOL_FIELD_NUMBER: _ClassVar[int]
    TIMEFRAME_FIELD_NUMBER: _ClassVar[int]
    FROM_TS_FIELD_NUMBER: _ClassVar[int]
    TO_TS_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    symbol: str
    timeframe: str
    from_ts: int
    to_ts: int
    limit: int
    def __init__(self, symbol: _Optional[str] = ..., timeframe: _Optional[str] = ..., from_ts: _Optional[int] = ..., to_ts: _Optional[int] = ..., limit: _Optional[int] = ...) -> None: ...

class BarsResponse(_message.Message):
    __slots__ = ("bars", "truncated")
    BARS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    bars: _containers.RepeatedCompositeFieldContainer[Bar]
    truncated: bool
    def __init__(self, bars: _Optional[_Iterable[_Union[Bar, _Mapping]]] = ..., truncated: bool = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ("symbol", "timeframe", "min_bars")
    SYMBOL_FIELD_NUMBER: _ClassVar[int]
    TIMEFRAME_FIELD_NUMBER: _ClassVar[int]
    MIN_BARS_FIELD_NUMBER: _ClassVar[int]
    symbol: str
    timeframe: str
    min_bars: int
    def __init__(self, symbol: _Optional[str] = ..., timeframe: _Optional[str] = ..., min_bars: _Optional[int] = ...) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("ready", "bars_available", "last_bar_ts")
    READY_FIELD_NUMBER: _ClassVar[int]
    BARS_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    LAST_BAR_TS_FIELD_NUMBER: _ClassVar[int]
    ready: bool
    bars_available: int
    last_bar_ts: int
    def __init__(self, ready: bool = ..., bars_available: _Optional[int] = ..., last_bar_ts: _Optional[int] = ...) -> None: ...

class PingRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PingResponse(_message.Message):
    __slots__ = ("db_reachable",)
    DB_REACHABLE_FIELD_NUMBER: _ClassVar[int]
    db_reachable: bool
    def __init__(self, db_reachable: bool = ...) -> None: ...

class Asset(_message.Message):
    __slots__ = ("symbol", "storage_symbol", "name", "exchange", "currency", "asset_class", "asset_subclass", "isin", "country")
    SYMBOL_FIELD_NUMBER: _ClassVar[int]
    STORAGE_SYMBOL_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    EXCHANGE_FIELD_NUMBER: _ClassVar[int]
    CURRENCY_FIELD_NUMBER: _ClassVar[int]
    ASSET_CLASS_FIELD_NUMBER: _ClassVar[int]
    ASSET_SUBCLASS_FIELD_NUMBER: _ClassVar[int]
    ISIN_FIELD_NUMBER: _ClassVar[int]
    COUNTRY_FIELD_NUMBER: _ClassVar[int]
    symbol: str
    storage_symbol: str
    name: str
    exchange: str
    currency: str
    asset_class: str
    asset_subclass: str
    isin: str
    country: str
    def __init__(self, symbol: _Optional[str] = ..., storage_symbol: _Optional[str] = ..., name: _Optional[str] = ..., exchange: _Optional[str] = ..., currency: _Optional[str] = ..., asset_class: _Optional[str] = ..., asset_subclass: _Optional[str] = ..., isin: _Optional[str] = ..., country: _Optional[str] = ...) -> None: ...

class GetAssetProfileRequest(_message.Message):
    __slots__ = ("symbol",)
    SYMBOL_FIELD_NUMBER: _ClassVar[int]
    symbol: str
    def __init__(self, symbol: _Optional[str] = ...) -> None: ...

class SearchAssetsRequest(_message.Message):
    __slots__ = ("query", "limit")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    query: str
    limit: int
    def __init__(self, query: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class SearchAssetsResponse(_message.Message):
    __slots__ = ("results",)
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[Asset]
    def __init__(self, results: _Optional[_Iterable[_Union[Asset, _Mapping]]] = ...) -> None: ...

class AssetWithStatus(_message.Message):
    __slots__ = ("asset", "status", "added_at", "last_bar_ts")
    ASSET_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ADDED_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_BAR_TS_FIELD_NUMBER: _ClassVar[int]
    asset: Asset
    status: str
    added_at: int
    last_bar_ts: int
    def __init__(self, asset: _Optional[_Union[Asset, _Mapping]] = ..., status: _Optional[str] = ..., added_at: _Optional[int] = ..., last_bar_ts: _Optional[int] = ...) -> None: ...

class ListAssetsRequest(_message.Message):
    __slots__ = ("exchange", "asset_class", "query", "cursor", "limit")
    EXCHANGE_FIELD_NUMBER: _ClassVar[int]
    ASSET_CLASS_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    exchange: str
    asset_class: str
    query: str
    cursor: str
    limit: int
    def __init__(self, exchange: _Optional[str] = ..., asset_class: _Optional[str] = ..., query: _Optional[str] = ..., cursor: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class ListAssetsResponse(_message.Message):
    __slots__ = ("assets", "next_cursor")
    ASSETS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    assets: _containers.RepeatedCompositeFieldContainer[AssetWithStatus]
    next_cursor: str
    def __init__(self, assets: _Optional[_Iterable[_Union[AssetWithStatus, _Mapping]]] = ..., next_cursor: _Optional[str] = ...) -> None: ...

class CreateAssetRequest(_message.Message):
    __slots__ = ("asset", "timeframes", "tv_symbol")
    ASSET_FIELD_NUMBER: _ClassVar[int]
    TIMEFRAMES_FIELD_NUMBER: _ClassVar[int]
    TV_SYMBOL_FIELD_NUMBER: _ClassVar[int]
    asset: Asset
    timeframes: _containers.RepeatedScalarFieldContainer[str]
    tv_symbol: str
    def __init__(self, asset: _Optional[_Union[Asset, _Mapping]] = ..., timeframes: _Optional[_Iterable[str]] = ..., tv_symbol: _Optional[str] = ...) -> None: ...

class CreateAssetResponse(_message.Message):
    __slots__ = ("asset_with_status", "created", "poll_eta_seconds")
    ASSET_WITH_STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    POLL_ETA_SECONDS_FIELD_NUMBER: _ClassVar[int]
    asset_with_status: AssetWithStatus
    created: bool
    poll_eta_seconds: int
    def __init__(self, asset_with_status: _Optional[_Union[AssetWithStatus, _Mapping]] = ..., created: bool = ..., poll_eta_seconds: _Optional[int] = ...) -> None: ...
