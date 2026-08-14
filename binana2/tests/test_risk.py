from datetime import datetime, timezone
from decimal import Decimal
from binana2.risk.engine import RiskContext, RiskEngine
from binana2.sharia.models import ShariaDecision, ShariaResult
from binana2.state.database import Database
from binana2.state.repositories import StateRepository

def test_risk_denies_unknown_sharia_and_durable_pause(tmp_path):
    db=Database(tmp_path/"risk.db"); state=StateRepository(db); risk=RiskEngine(state,max_positions=2,max_signal_age_seconds=180,max_candle_age_seconds=180); now=datetime.now(timezone.utc); ctx=RiskContext("BTCUSDT",now,now,Decimal("50"),Decimal("100"),0); unknown=ShariaResult("BTCUSDT",ShariaDecision.UNKNOWN,"UNKNOWN","",None,None,"missing"); assert not risk.approve_entry(ctx,unknown).approved
    passed=ShariaResult("BTCUSDT",ShariaDecision.PASS,"GREEN","local",now,now,"ok"); decision=risk.approve_entry(ctx,passed); assert not decision.approved; assert "global entry pause" in decision.reason; db.close()
