from datetime import datetime, timedelta, timezone


def registry_document(symbols):
    today=datetime.now(timezone.utc).date()
    return {'schema_version':1,'version':'test-v1','symbols':sorted(symbols),
            'last_reviewed':today.isoformat(),'next_review':(today+timedelta(days=30)).isoformat()}
