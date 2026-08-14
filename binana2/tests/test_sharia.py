import json
from datetime import datetime, timedelta, timezone
from binana2.sharia.engine import V191Gate
from binana2.sharia.models import ShariaDecision

def record(status="GREEN",*,days=1):
    now=datetime.now(timezone.utc); return {"symbol":"BTCUSDT","status":status,"source":"owner-approved-local-source","reviewed_at":(now-timedelta(days=1)).isoformat(),"expires_at":(now+timedelta(days=days)).isoformat()}

def test_green_is_pass(tmp_path):
    path=tmp_path/"sharia.json"; path.write_text(json.dumps({"schema_version":2,"records":[record()]})); assert V191Gate(path).evaluate("BTCUSDT").decision is ShariaDecision.PASS

def test_non_green_is_fail(tmp_path):
    path=tmp_path/"sharia.json"; path.write_text(json.dumps({"schema_version":2,"records":[record("DOUBTFUL")]})); assert V191Gate(path).evaluate("BTCUSDT").decision is ShariaDecision.FAIL

def test_missing_corrupt_and_expired_fail_closed(tmp_path):
    assert V191Gate(tmp_path/"missing.json").evaluate("BTCUSDT").decision is ShariaDecision.UNKNOWN; bad=tmp_path/"bad.json"; bad.write_text("not json"); assert V191Gate(bad).evaluate("BTCUSDT").decision is ShariaDecision.UNKNOWN; expired=tmp_path/"expired.json"; expired.write_text(json.dumps({"schema_version":2,"records":[record(days=-1)]})); assert V191Gate(expired).evaluate("BTCUSDT").decision is ShariaDecision.UNKNOWN

def test_duplicate_symbol_is_unknown(tmp_path):
    path=tmp_path/"sharia.json"; path.write_text(json.dumps({"schema_version":2,"records":[record(),record()]})); assert V191Gate(path).evaluate("BTCUSDT").decision is ShariaDecision.UNKNOWN
