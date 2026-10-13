"""DynamoDB/S3 access for Boardagents. boto3 is imported lazily so unit tests run offline."""
import os
import time
import uuid
from decimal import Decimal

from core.config import cfg

_TABLES = {}
_S3 = None


def _ddb():
    import boto3
    if not _TABLES:
        c = cfg()
        _TABLES.update({
            'findings': boto3.resource('dynamodb').Table(c['findings_table']),
            'audit': boto3.resource('dynamodb').Table(c['audit_table']),
            'feed': boto3.resource('dynamodb').Table(c['feed_table']),
            'meetings': boto3.resource('dynamodb').Table(c['meetings_table']),
        })
    return _TABLES


def _bucket():
    global _S3
    import boto3
    if _S3 is None:
        _S3 = boto3.resource('s3').Bucket(cfg()['bucket'])
    return _S3


def now_ms():
    return int(time.time() * 1000)


# ---------- DynamoDB number codec ----------
# DynamoDB rejects Python floats. Convert on the way in, and back on the way out
# so API responses stay JSON-serialisable.

def _to_ddb(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_ddb(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_ddb(v) for v in obj]
    return obj


def _from_ddb(obj):
    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    if isinstance(obj, dict):
        return {k: _from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_ddb(v) for v in obj]
    return obj


# ---------- audit (append-only) ----------

def audit(finding_id, kind, payload, **extra):
    ts = now_ms()
    seq = uuid.uuid4().hex[:8]
    item = {
        'finding_id': finding_id or '__system__',
        'ts_seq': f'{ts}#{seq}',
        'ts': ts,
        'kind': kind,
        'payload': payload,
    }
    item.update(extra)
    _ddb()['audit'].put_item(Item=_to_ddb(item))
    return item


def audit_for(finding_id):
    from boto3.dynamodb.conditions import Key
    resp = _ddb()['audit'].query(
        KeyConditionExpression=Key('finding_id').eq(finding_id),
        ScanIndexForward=True,
    )
    return _from_ddb(resp.get('Items', []))


# ---------- findings ----------

def create_finding(finding):
    _ddb()['findings'].put_item(Item=_to_ddb(finding))
    return finding


def get_finding(finding_id):
    resp = _ddb()['findings'].get_item(Key={'finding_id': finding_id})
    return _from_ddb(resp['Item']) if resp.get('Item') else None


def put_finding(finding, expect_version=None):
    """Optimistic-concurrency write. Raises StorageConflict on version mismatch."""
    class StorageConflict(Exception):
        pass
    f = _to_ddb(dict(finding))
    f['updated_at'] = now_ms()
    table = _ddb()['findings']
    if expect_version is None:
        table.put_item(Item=f)
    else:
        from boto3.dynamodb.conditions import Attr
        cond = Attr('version').eq(expect_version)
        try:
            table.put_item(Item=f, ConditionExpression=cond)
        except _ddb_client_error():
            raise StorageConflict(f"version conflict on {f.get('finding_id')} (expected {expect_version})")
    return f


def _ddb_client_error():
    from botocore.exceptions import ClientError
    return ClientError


def list_findings(limit=50):
    resp = _ddb()['findings'].scan(Limit=limit)
    items = _from_ddb(resp.get('Items', []))
    return sorted(items, key=lambda f: f.get('created_at', 0), reverse=True)


# ---------- feed ----------

def put_feed_event(event):
    _ddb()['feed'].put_item(Item=_to_ddb({**event, 'processed': False}))


def unprocessed_events():
    return [i for i in _from_ddb(_ddb()['feed'].scan().get('Items', [])) if not i.get('processed')]


def mark_processed(event_id):
    # 'processed' is a DynamoDB reserved keyword, so it must be aliased.
    _ddb()['feed'].update_item(
        Key={'event_id': event_id},
        UpdateExpression='SET #p = :p, #pa = :t',
        ExpressionAttributeNames={'#p': 'processed', '#pa': 'processed_at'},
        ExpressionAttributeValues={':p': True, ':t': now_ms()},
    )


def clear_tables():
    """Demo reset: empties findings/audit/feed. Audit is append-only in normal operation."""
    ddb = _ddb()
    for key in ('findings', 'audit', 'feed', 'meetings'):
        table = ddb[key]
        items = _scan_all(table)
        if key == 'audit':
            # composite key: both attributes are needed for a delete
            keys = [{'finding_id': i['finding_id'], 'ts_seq': i['ts_seq']} for i in items]
        else:
            pk = _pk(key)
            keys = [{pk: i[pk]} for i in items]
        with table.batch_writer() as b:
            for k in keys:
                b.delete_item(Key=k)
    return True


def _scan_all(table):
    """Fully paginated scan (a single scan pages at 1 MB)."""
    items, start = [], None
    while True:
        kw = {'ExclusiveStartKey': start} if start else {}
        resp = table.scan(**kw)
        items.extend(resp.get('Items', []))
        start = resp.get('LastEvaluatedKey')
        if not start:
            return items


def _pk(key):
    return {'findings': 'finding_id', 'feed': 'event_id', 'audit': 'finding_id',
            'meetings': 'meeting_id'}[key]


# ---------- config / kill switch ----------

def get_config_item():
    resp = _ddb()['findings'].get_item(Key={'finding_id': '__config__'})
    return _from_ddb(resp['Item']) if resp.get('Item') else None


def is_paused():
    item = get_config_item()
    return bool(item and item.get('paused'))


def set_paused(paused):
    _ddb()['findings'].put_item(Item=_to_ddb({'finding_id': '__config__', 'paused': paused, 'updated_at': now_ms()}))


def incr_daily_counter(name, limit):
    """Increments a daily counter keyed by UTC date; returns the new count."""
    day = time.strftime('%Y-%m-%d', time.gmtime())
    _ddb()['findings'].update_item(
        Key={'finding_id': '__config__'},
        UpdateExpression='ADD #c :one',
        ExpressionAttributeNames={'#c': f'count_{name}_{day}'},
        ExpressionAttributeValues={':one': 1},
    )
    item = get_config_item() or {}
    return int(item.get(f'count_{name}_{day}', 0)), day


# ---------- S3 ----------

def put_artifact(key, content):
    _bucket().put_object(Key=key, Body=content.encode() if isinstance(content, str) else content)
    return key
