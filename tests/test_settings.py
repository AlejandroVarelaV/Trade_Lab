"""The paper-only guard must reject anything but the Alpaca paper endpoint."""
import pytest

from tradelab import settings
from tradelab.settings import PAPER_BASE_URL, UnsafeConfigError, check_paper_url


@pytest.mark.parametrize("url", [
    "https://paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets/",
    "https://paper-api.alpaca.markets/v2",
    "  https://paper-api.alpaca.markets  ",
])
def test_paper_url_accepted(url):
    assert check_paper_url(url) == PAPER_BASE_URL


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets",                      # live trading
    "http://paper-api.alpaca.markets",                 # not https
    "https://paper-api.alpaca.markets.evil.com",       # look-alike host
    "https://evil.com/paper-api.alpaca.markets",
    "https://user@paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets:8443",
    "https://paper-api.alpaca.markets/v2/orders",
    "https://paper-api.alpaca.markets?x=1",
    "",
])
def test_non_paper_url_rejected(url):
    with pytest.raises(UnsafeConfigError):
        check_paper_url(url)


def test_load_hard_fails_on_live_url(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(SystemExit) as exc:
        settings.load()
    assert "paper" in str(exc.value)


def test_load_defaults_to_paper(monkeypatch):
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    assert settings.load().alpaca_base_url == PAPER_BASE_URL
