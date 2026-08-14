from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from .models import ShariaDecision, ShariaResult

VALID_V191_STATUSES=frozenset({"GREEN","GREEN_AVOID_OPTIONAL","NO_TRADE_INFO","NO_TRADE_YIELD","DOUBTFUL","HARAM","TECH_STOP"}); TRADEABLE_V191_STATUSES=frozenset({"GREEN","GREEN_AVOID_OPTIONAL"})

def _utc(value:str)->datetime:
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"));
    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

class V191Gate:
    def __init__(self,status_file:str|Path)->None: self.status_file=Path(status_file)
    def evaluate(self,symbol:str,*,now:datetime|None=None)->ShariaResult:
        now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc); base=self._base_asset(symbol)
        try:
            raw=json.loads(self.status_file.read_text(encoding="utf-8")); records=self._validate_dataset(raw,now); matches=[r for r in records if self._base_asset(str(r["symbol"]))==base]
            if len(matches)!=1: return self._unknown(symbol,"missing V19.1 record" if not matches else "conflicting V19.1 records")
            record=matches[0]; status=str(record["status"]).upper(); reviewed=_utc(str(record["reviewed_at"])); expires=_utc(str(record["expires_at"])); source=str(record["source"]).strip()
            if expires<=now: return ShariaResult(symbol,ShariaDecision.UNKNOWN,status,source,reviewed,expires,"V19.1 record expired")
            if status in TRADEABLE_V191_STATUSES: return ShariaResult(symbol,ShariaDecision.PASS,status,source,reviewed,expires,"current authoritative V19.1 approval")
            return ShariaResult(symbol,ShariaDecision.FAIL,status,source,reviewed,expires,"V19.1 status is non-tradeable")
        except Exception as exc: return self._unknown(symbol,f"V19.1 dataset invalid: {type(exc).__name__}: {exc}")
    def _validate_dataset(self,raw:Any,now:datetime)->list[dict[str,Any]]:
        if not isinstance(raw,dict) or raw.get("schema_version")!=2: raise ValueError("schema_version 2 required")
        records=raw.get("records");
        if not isinstance(records,list) or not records: raise ValueError("records must be a non-empty list")
        seen:set[str]=set(); valid=[]
        for record in records:
            if not isinstance(record,dict): raise ValueError("malformed record")
            base=self._base_asset(str(record.get("symbol","")));
            if not base or base in seen: raise ValueError("invalid or duplicate symbol")
            seen.add(base); status=str(record.get("status","")).upper()
            if status not in VALID_V191_STATUSES: raise ValueError(f"unknown V19.1 status {status}")
            if not str(record.get("source","")).strip(): raise ValueError("source missing")
            reviewed=_utc(str(record.get("reviewed_at",""))); expires=_utc(str(record.get("expires_at","")))
            if reviewed>now: raise ValueError("future reviewed_at")
            if expires<=reviewed: raise ValueError("expires_at must be after reviewed_at")
            valid.append(record)
        return valid
    @staticmethod
    def _base_asset(symbol:str)->str:
        normalized=symbol.upper().replace("/","").replace("-","");
        if normalized.endswith("USDT"): normalized=normalized[:-4]
        return normalized if normalized.isalnum() else ""
    @staticmethod
    def _unknown(symbol:str,reason:str)->ShariaResult: return ShariaResult(symbol,ShariaDecision.UNKNOWN,"UNKNOWN","",None,None,reason)
