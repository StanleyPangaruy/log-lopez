import calendar
import json
import os
import uuid
from datetime import date, datetime

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ["TABLE_NAME"]
REPORTS_BUCKET = os.environ["REPORTS_BUCKET"]
table = dynamodb.Table(TABLE_NAME)


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _current_period():
    """Mirrors the report generator's period logic: 1-15, or 16-end of month."""
    today = date.today()
    if today.day <= 15:
        start = today.replace(day=1)
        end = today.replace(day=15)
    else:
        last_day = calendar.monthrange(today.year, today.month)[1]
        start = today.replace(day=16)
        end = today.replace(day=last_day)
    return start.isoformat(), end.isoformat()


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")

    try:
        if method == "POST" and path == "/tasks":
            return create_task(event)
        if method == "GET" and path == "/tasks":
            return list_tasks(event)
        if method == "GET" and path == "/reports":
            return list_reports()
        return _response(404, {"message": "Not found"})
    except ValueError as e:
        return _response(400, {"message": str(e)})
    except Exception as e:  # noqa: BLE001
        print("Unhandled error:", repr(e))
        return _response(500, {"message": "Internal error"})


def create_task(event):
    body = json.loads(event.get("body") or "{}")
    task_date = body.get("date")
    description = (body.get("description") or "").strip()

    if not task_date:
        raise ValueError("date is required (YYYY-MM-DD)")
    if not description:
        raise ValueError("description is required")
    try:
        datetime.strptime(task_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be in YYYY-MM-DD format") from exc

    task_id = str(uuid.uuid4())
    item = {
        "pk": "TASK",
        "sk": f"{task_date}#{task_id}",
        "taskId": task_id,
        "date": task_date,
        "description": description,
        "createdAt": datetime.utcnow().isoformat() + "Z",
    }
    table.put_item(Item=item)
    return _response(201, item)


def list_tasks(event):
    params = event.get("queryStringParameters") or {}
    start = params.get("start")
    end = params.get("end")
    if not start or not end:
        start, end = _current_period()

    resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq("TASK") & Key("sk").between(f"{start}#", f"{end}#￿")
        )
    )
    items = sorted(resp.get("Items", []), key=lambda i: i["sk"])
    return _response(200, {"start": start, "end": end, "tasks": items})


def list_reports():
    resp = s3.list_objects_v2(Bucket=REPORTS_BUCKET)
    reports = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if not key.endswith(".pdf"):
            continue
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": REPORTS_BUCKET, "Key": key},
            ExpiresIn=300,
        )
        reports.append(
            {
                "key": key,
                "lastModified": obj["LastModified"].isoformat(),
                "downloadUrl": url,
            }
        )
    reports.sort(key=lambda r: r["key"], reverse=True)
    return _response(200, {"reports": reports})
