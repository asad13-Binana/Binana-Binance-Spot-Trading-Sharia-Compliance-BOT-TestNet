from decimal import Decimal
def bullish_flow_reason(s,p):
 if s.flow_status!="fresh":return "BULLISH_FLOW_STALE"
 try:
  ratio=Decimal(str(s.taker_buy_ratio_60s));cvd=Decimal(str(s.cvd_quote_60s));minimum=Decimal(str(p.get("normal_buy_ratio",.52)))
  if not all(x.is_finite() for x in (ratio,cvd,minimum)) or not 0<=ratio<=1:return "BULLISH_FLOW_UNKNOWN"
 except Exception:return "BULLISH_FLOW_UNKNOWN"
 return "" if ratio>=minimum and cvd>0 else "BULLISH_FLOW_NOT_CONFIRMED"
