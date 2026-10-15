import pytest

from pacificorp_queue.cli import main
from pacificorp_queue.sources import SOURCES, Source, index_by_key


def test_duplicate_source_keys_are_refused() -> None:
    first = Source("cs1", "a.htm", "first")
    second = Source("cs1", "b.htm", "second")
    with pytest.raises(ValueError, match="duplicate source key: 'cs1'"):
        index_by_key([first, second])


def test_every_source_is_indexed_under_its_own_key() -> None:
    assert all(key == source.key for key, source in SOURCES.items())


def test_load_help_describes_every_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["load", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for source in SOURCES.values():
        assert source.key in out
        assert source.description in out
