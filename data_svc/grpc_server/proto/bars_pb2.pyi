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

class BarsResponse(_message.Message):
    __slots__ = ("bars",)
    BARS_FIELD_NUMBER: _ClassVar[int]
    bars: _containers.RepeatedCompositeFieldContainer[Bar]
    def __init__(self, bars: _Optional[_Iterable[_Union[Bar, _Mapping]]] = ...) -> None: ...

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
