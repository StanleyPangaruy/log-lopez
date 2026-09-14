import calendar
import json
import os
import uuid
from datetime import date, datetime

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")

TABLE_NAME = os.environ["TABLE_NAME"]
REPORTS_BUCKET = os.environ["REPORTS_BUCKET"]
REPORT_FUNCTION_NAME = os.environ["REPORT_FUNCTION_NAME"]
table = dynamodb.Table(TABLE_NAME)


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _user_id(event):
    """The verified Cognito sub for the caller, from the JWT authorizer's claims."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    sub = claims.get("sub")
    if not sub:
        raise ValueError("Missing authenticated user")
    return sub


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
        if method == "PUT" and path.startswith("/tasks/"):
            return update_task(event)
        if method == "GET" and path == "/reports":
            return list_reports(event)
        if method == "POST" and path == "/reports/generate":
            return generate_report(event)
        if method == "DELETE" and path.startswith("/reports/"):
            return delete_report(event)
        if method == "GET" and path == "/profile":
            return get_profile(event)
        if method == "POST" and path == "/profile":
            return create_profile(event)
        return _response(404, {"message": "Not found"})
    except ValueError as e:
        return _response(400, {"message": str(e)})
    except Exception as e:  # noqa: BLE001
        print("Unhandled error:", repr(e))
        return _response(500, {"message": "Internal error"})


def create_task(event):
    user_id = _user_id(event)
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
        "pk": f"TASK#{user_id}",
        "sk": f"{task_date}#{task_id}",
        "taskId": task_id,
        "date": task_date,
        "description": description,
        "createdAt": datetime.utcnow().isoformat() + "Z",
    }
    table.put_item(Item=item)
    return _response(201, item)


def update_task(event):
    user_id = _user_id(event)
    path_params = event.get("pathParameters") or {}
    old_date = path_params.get("date")
    task_id = path_params.get("taskId")
    if not old_date or not task_id:
        raise ValueError("date and taskId are required in the path")

    body = json.loads(event.get("body") or "{}")
    new_date = body.get("date")
    description = (body.get("description") or "").strip()

    if not new_date:
        raise ValueError("date is required (YYYY-MM-DD)")
    if not description:
        raise ValueError("description is required")
    try:
        datetime.strptime(new_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be in YYYY-MM-DD format") from exc

    pk = f"TASK#{user_id}"
    existing = table.get_item(Key={"pk": pk, "sk": f"{old_date}#{task_id}"}).get("Item")
    if not existing:
        return _response(404, {"message": "Task not found"})

    updated_at = datetime.utcnow().isoformat() + "Z"

    if new_date == old_date:
        table.update_item(
            Key={"pk": pk, "sk": f"{old_date}#{task_id}"},
            UpdateExpression="SET description = :d, updatedAt = :u",
            ExpressionAttributeValues={":d": description, ":u": updated_at},
        )
        existing["description"] = description
        existing["updatedAt"] = updated_at
        return _response(200, existing)

    item = dict(existing)
    item["sk"] = f"{new_date}#{task_id}"
    item["date"] = new_date
    item["description"] = description
    item["updatedAt"] = updated_at
    table.put_item(Item=item)
    table.delete_item(Key={"pk": pk, "sk": f"{old_date}#{task_id}"})
    return _response(200, item)


def list_tasks(event):
    user_id = _user_id(event)
    params = event.get("queryStringParameters") or {}
    start = params.get("start")
    end = params.get("end")
    if not start or not end:
        start, end = _current_period()

    resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"TASK#{user_id}") & Key("sk").between(f"{start}#", f"{end}#￿")
        )
    )
    items = sorted(resp.get("Items", []), key=lambda i: i["sk"])
    return _response(200, {"start": start, "end": end, "tasks": items})


def generate_report(event):
    user_id = _user_id(event)
    resp = lambda_client.invoke(
        FunctionName=REPORT_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps({"trigger": "now", "userId": user_id}).encode("utf-8"),
    )
    if resp.get("FunctionError"):
        print("report generator error:", resp["Payload"].read())
        return _response(502, {"message": "Report generation failed"})
    payload = json.loads(resp["Payload"].read() or "{}")
    return _response(200, {"message": payload.get("body", "Report generated")})


def list_reports(event):
    user_id = _user_id(event)
    prefix = f"{user_id}/"
    resp = s3.list_objects_v2(Bucket=REPORTS_BUCKET, Prefix=prefix)
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
                "key": key[len(prefix):],
                "lastModified": obj["LastModified"].isoformat(),
                "downloadUrl": url,
            }
        )
    reports.sort(key=lambda r: r["key"], reverse=True)
    return _response(200, {"reports": reports})


def delete_report(event):
    user_id = _user_id(event)
    path_params = event.get("pathParameters") or {}
    report_key = path_params.get("reportKey")
    if not report_key:
        raise ValueError("reportKey is required in the path")

    key = f"{user_id}/{report_key}"
    s3.delete_object(Bucket=REPORTS_BUCKET, Key=key)
    return _response(200, {"message": "Report deleted"})


def get_profile(event):
    user_id = _user_id(event)
    item = table.get_item(Key={"pk": f"PROFILE#{user_id}", "sk": "PROFILE"}).get("Item")
    return _response(200, {"profile": item})


def create_profile(event):
    user_id = _user_id(event)
    body = json.loads(event.get("body") or "{}")
    name = (body.get("name") or "").strip()
    designation = (body.get("designation") or "").strip()

    if not name:
        raise ValueError("name is required")
    if not designation:
        raise ValueError("designation is required")

    item = {
        "pk": f"PROFILE#{user_id}",
        "sk": "PROFILE",
        "userId": user_id,
        "name": name,
        "designation": designation,
        "updatedAt": datetime.utcnow().isoformat() + "Z",
    }
    table.put_item(Item=item)
    return _response(200, item)
