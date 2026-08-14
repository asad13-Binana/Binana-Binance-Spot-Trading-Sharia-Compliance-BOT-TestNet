import pytest
from binana2.config import Settings
from binana2.exchange.binance_rest import BinanceRestClient

def test_settings_reject_live(monkeypatch):
    monkeypatch.setenv("BOT_ENVIRONMENT","LIVE"); monkeypatch.setenv("BINANCE_TESTNET","false")
    with pytest.raises(ValueError,match="Testnet-only"): Settings.from_env()

def test_rest_client_rejects_production_endpoint():
    with pytest.raises(ValueError,match="Testnet-only"): BinanceRestClient("key","secret","https://api.binance.com")
