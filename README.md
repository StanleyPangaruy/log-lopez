# Log Lopez

Accomplishment-log webapp for the Municipality of Lopez, Quezon. Each employee registers their own account, logs tasks on weekdays, and gets a PDF Accomplishment Report generated automatically on the 1st and 16th of each month (or on demand).

## What's here

```
log-lopez/
  app.py                       CDK app entry point
  cdk.json
  requirements.txt             CDK (Python) dependencies
  log_lopez/
    log_lopez_stack.py         The whole stack: DynamoDB, S3, CloudFront, Cognito, API Gateway, EventBridge Scheduler
  lambda/
    tasks_api/app.py           Create/edit/list tasks, list/generate reports, get/set profile (behind Cognito auth)
    report_generator/          Builds each user's Accomplishment Report PDF (needs reportlab, bundled via Docker at deploy time)
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

### Creating accounts

Self sign-up is enabled — anyone visiting `SiteUrl` can click "Create an account" and register with their name, designation, username, email, and password. Cognito emails a confirmation code (from its default sender, capped at 50 emails/day — fine for a small office) to verify the address before first sign-in. Name and designation aren't stored as Cognito attributes; they're saved to DynamoDB as that user's profile and used as the "Prepared by" name/position on their reports.

If you'd rather provision someone directly instead of having them self-register:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username theirname \
  --user-attributes Name=email,Value=their@email.com \
  --temporary-password 'TempPass123!' \
  --message-action SUPPRESS
```

They'll be prompted to set a real password on first login (the "new password" field appears automatically) — but they'll still need to fill in their profile (name/designation) once signed in, since admin-created users skip the registration form. There's no in-app profile editor yet; re-run `POST /profile` (or add a UI for it) if that's needed.

## Everyday use

Just visit the `SiteUrl` — it's a bookmark-able page. Log a task, see it land in the ledger and the weekday strip. Reports appear under "Past reports" once generated, and each user only ever sees their own tasks and reports.

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
- The mayor's name/title ("Noted by" on every report) is fixed at the top of `lambda/report_generator/app.py` — update it there and redeploy if it changes. Each employee's own name/position comes from their profile, set at registration.
- The scheduled reports (1st and 16th) generate one PDF per registered user who has a profile on file. The on-demand "Generate PDF" button in the app only generates the signed-in user's own report.
- No custom domain is set up — the app lives at the CloudFront-generated URL. Let me know if you want one added later.
