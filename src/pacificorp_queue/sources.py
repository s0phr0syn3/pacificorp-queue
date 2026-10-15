"""The PacifiCorp OASIS pages this tool knows how to ingest."""

from collections.abc import Iterable
from dataclasses import dataclass

BASE_URL = "https://www.oasis.oati.com/woa/docs/PPW/PPWdocs/"


@dataclass(frozen=True, slots=True)
class Source:
    """One PacifiCorp queue page and how to treat it."""

    key: str
    page: str
    description: str
    # The legacy serial-queue archive page is published but has no rows. For every
    # other source an empty page means something broke, so it is refused.
    allow_empty: bool = False

    @property
    def url(self) -> str:
        return BASE_URL + self.page


def index_by_key(sources: Iterable[Source]) -> dict[str, Source]:
    """Key sources by `Source.key`, refusing duplicates instead of letting the
    later one silently replace the earlier."""
    index: dict[str, Source] = {}
    for source in sources:
        if source.key in index:
            raise ValueError(f"duplicate source key: {source.key!r}")
        index[source.key] = source
    return index


SOURCES: dict[str, Source] = index_by_key(
    (
        Source("tc1", "pacificorpcliaq.htm", "Transition Cluster"),
        Source("cs1", "pacificorpcliaq1.htm", "Cluster 1"),
        Source("cs2", "pacificorpcliaq2.htm", "Cluster 2"),
        Source("cs3", "pacificorpcliaq3.htm", "Cluster 3"),
        Source("cs4", "pacificorpcliaq4.htm", "Cluster 4"),
        Source("tc2", "pacificorpcliaqT2.htm", "Transition Cluster 2"),
        Source(
            "sq",
            "pacificorplgiaq.htm",
            "Legacy serial queue archive (currently empty)",
            allow_empty=True,
        ),
    )
)
