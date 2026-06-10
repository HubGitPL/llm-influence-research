from datetime import datetime, timedelta
from typing import Iterator, Tuple


def datetime_range(
    start: datetime,
    end: datetime,
    step: timedelta
) -> Iterator[Tuple[datetime, datetime]]:
    current_start = start

    while current_start < end:
        current_end = min(current_start + step, end)
        yield current_start, current_end
        current_start = current_end