from binana2.exchange.user_stream import parse_execution_report

def test_execution_report_from_ws_api_envelope():
    msg={"subscriptionId":0,"event":{"e":"executionReport","E":1,"s":"BTCUSDT","c":"b2-1","S":"BUY","o":"LIMIT","x":"TRADE","X":"PARTIALLY_FILLED","i":42,"l":"0.1","z":"0.1","L":"100","Z":"10","t":7}}; report=parse_execution_report(msg); assert report is not None; assert report.client_order_id=="b2-1"; assert report.order_status=="PARTIALLY_FILLED"; assert report.trade_id==7
