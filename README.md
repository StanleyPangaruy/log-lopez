# Log Lopez

Personal accomplishment-log webapp for the Municipality of Lopez, Quezon. Log tasks on weekdays; a PDF Accomplishment Report is generated automatically on the 1st and 16th of each month.

## What's here

```
log-lopez/
  app.py                       CDK app entry point
  cdk.json
  requirements.txt             CDK (Python) dependencies
  log_lopez/
    log_lopez_stack.py         The whole stack: DynamoDB, S3, CloudFront, Cognito, API Gateway, EventBridge Scheduler
  lambda/
    tasks_api/app.py           Create/list tasks, list reports (behind Cognito auth)
    report_generator/          Builds the Accomplishment Report PDF (needs reportlab, bundled via Docker at deploy time)
  frontend/
    index.html                 The webapp (login + daily log + reports)
    config.js                  Deploy-time config — fill in after first deploy
```

## Prerequisites

- Python 3.11+ and `pip`
- Node.js (for the AWS CDK CLI: `npm install -g aws-cdk`)
- **Docker Desktop running** — only needed at deploy time, to bundle the `reportlab` dependency for the report generator Lambda. Not needed to run the app afterward.
- An AWS CLI profile with the `log-lopez-admin` credentials from IAM, configured via `aws configure --profile log-lopez` (see the credentials setup we did earlier — never paste secret keys into a chat, only into your terminal).

## First-time deploy

```bash
# from the log-lopez/ directory
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export AWS_PROFILE=log-lopez        # or pass --profile log-lopez to every command below

cdk bootstrap                       # one-time per account/region
cdk deploy
```

`cdk deploy` will print a confirmation for the IAM changes it's about to make (Cognito, Lambda execution roles, etc.) — type `y` to proceed. It'll take a few minutes, mostly for the CloudFront distribution.

At the end, note the outputs — you'll need them next:

```
LogLopezStack.SiteUrl = https://dXXXXXXXXXXXXX.cloudfront.net
LogLopezStack.ApiUrl = https://XXXXXXXXXX.execute-api.ap-southeast-1.amazonaws.com
LogLopezStack.UserPoolId = ap-southeast-1_XXXXXXXXX
LogLopezStack.UserPoolClientId = XXXXXXXXXXXXXXXXXXXXXXXXXX
LogLopezStack.ReportsBucketName = log-lopez-XXXXXXXXXXXXXX
```

### Post-deploy configuration

1. Open `frontend/config.js` and replace the three placeholder values with `ApiUrl`, `UserPoolId`, and `UserPoolClientId` from above.
2. Redeploy just the frontend change:
   ```bash
   cdk deploy
   ```
   (CDK only re-uploads what changed — this will be quick.)

### Create your Cognito user

The user pool has self sign-up turned off on purpose — you're the only user, so create yourself directly:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username stanley \
  --user-attributes Name=email,Value=your@email.com \
  --temporary-password 'TempPass123!' \
  --message-action SUPPRESS
```

Sign in at the `SiteUrl` with username `stanley` and that temporary password — the app will prompt you to set a real password on first login (the "new password" field appears automatically).

## Everyday use

Just visit the `SiteUrl` — it's a bookmark-able page. Log a task, see it land in the ledger and the weekday strip. Reports appear under "Past reports" once generated.

## Testing the report generator without waiting for the schedule

```bash
aws lambda invoke --function-name <ReportGeneratorFunction name from `aws lambda list-functions`> \
  --payload '{"trigger":"day16"}' --cli-binary-format raw-in-base64-out out.json
cat out.json
```

Check the reports S3 bucket or the app's "Past reports" section afterward.

## Updating the app later

- Frontend or Lambda code changes: `cdk deploy` again from this directory.
- To tear everything down: `cdk destroy` (the DynamoDB table and reports bucket are set to survive this by design — delete them manually in the console if you really want to start over, so you never lose logged tasks or past reports by accident).

## Notes

- Region defaults to `ap-southeast-1` (Singapore) — closest AWS region to the Philippines. Change it in `app.py` if you'd rather use something else.
- The report generator's fixed fields (your name, position, and the mayor's name/title) live at the top of `lambda/report_generator/app.py` — update them there and redeploy if anything changes.
- No custom domain is set up — the app lives at the CloudFront-generated URL. Let me know if you want one added later.
