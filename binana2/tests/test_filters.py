from decimal import Decimal
import pytest
from binana2.exchange.order_filters import SymbolFilters

def filters():
    return SymbolFilters.from_exchange_info({"symbol":"BTCUSDT","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.10","minPrice":"0.10","maxPrice":"1000000"},{"filterType":"LOT_SIZE","stepSize":"0.00001000","minQty":"0.00001000","maxQty":"100"},{"filterType":"NOTIONAL","minNotional":"5"}]})

def test_filters_round_down_without_exceeding_intent():
    f=filters(); assert f.normalize_quantity(Decimal("0.001019"))==Decimal("0.00101000"); assert f.normalize_price(Decimal("50000.19"))==Decimal("50000.10")

def test_min_notional_is_enforced():
    with pytest.raises(ValueError): filters().validate_notional(Decimal("0.00001"),Decimal("100"))
