from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_scheduler as scheduler,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct


class LogLopezStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # Data store — one table, pk="TASK" (fixed), sk="<date>#<taskId>"
        # so a single Query with a sort-key BETWEEN clause returns every
        # task in a date range without a secondary index.
        # ---------------------------------------------------------------
        table = dynamodb.Table(
            self,
            "TasksTable",
            table_name="log-lopez-tasks",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------------
        # Storage
        # ---------------------------------------------------------------
        reports_bucket = s3.Bucket(
            self,
            "ReportsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        frontend_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(frontend_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )

        s3deploy.BucketDeployment(
            self,
            "DeployFrontend",
            sources=[s3deploy.Source.asset("frontend")],
            destination_bucket=frontend_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        # ---------------------------------------------------------------
        # Auth — self sign-up with email verification. Name and
        # designation are collected at registration but stored in our
        # own table (see the PROFILE# items below), not as Cognito
        # attributes, so the user pool's schema never needs to change.
        # ---------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="log-lopez-users",
            self_sign_up_enabled=True,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            sign_in_aliases=cognito.SignInAliases(email=True, username=True),
            removal_policy=RemovalPolicy.RETAIN,
        )

        user_pool_client = user_pool.add_client(
            "SpaClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
        )

        # ---------------------------------------------------------------
        # API
        # ---------------------------------------------------------------
        report_fn = PythonFunction(
            self,
            "ReportGeneratorFunction",
            entry="lambda/report_generator",
            index="app.py",
            handler="handler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            # Scheduled runs loop over every registered user's period, one PDF each.
            timeout=Duration.seconds(120),
            environment={
                "TABLE_NAME": table.table_name,
                "REPORTS_BUCKET": reports_bucket.bucket_name,
            },
        )
        table.grant_read_data(report_fn)
        reports_bucket.grant_write(report_fn)

        tasks_fn = _lambda.Function(
            self,
            "TasksApiFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="app.handler",
            code=_lambda.Code.from_asset("lambda/tasks_api"),
            # Covers a synchronous invoke of the report generator for on-demand PDFs.
            timeout=Duration.seconds(35),
            environment={
                "TABLE_NAME": table.table_name,
                "REPORTS_BUCKET": reports_bucket.bucket_name,
                "REPORT_FUNCTION_NAME": report_fn.function_name,
            },
        )
        table.grant_read_write_data(tasks_fn)
        reports_bucket.grant_read(tasks_fn)
        reports_bucket.grant_delete(tasks_fn)
        report_fn.grant_invoke(tasks_fn)

        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PUT,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[user_pool_client.user_pool_client_id],
        )

        tasks_integration = integrations.HttpLambdaIntegration("TasksIntegration", tasks_fn)

        http_api.add_routes(
            path="/tasks",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=tasks_integration,
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/tasks/{date}/{taskId}",
            methods=[apigwv2.HttpMethod.PUT],
            integration=tasks_integration,
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/tasks/search",
            methods=[apigwv2.HttpMethod.GET],
            integration=tasks_integration,
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/reports",
            methods=[apigwv2.HttpMethod.GET],
            integration=tasks_integration,
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/reports/generate",
            methods=[apigwv2.HttpMethod.POST],
            integration=tasks_integration,
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/reports/{reportKey}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=tasks_integration,
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/profile",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=tasks_integration,
            authorizer=authorizer,
        )

        # ---------------------------------------------------------------
        # Scheduling — 01:00 UTC = 09:00 Asia/Manila, so the calendar
        # date is already correct in Manila without any TZ math.
        # ---------------------------------------------------------------
        scheduler_role = iam.Role(
            self,
            "SchedulerInvokeRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        report_fn.grant_invoke(scheduler_role)

        def make_schedule(construct_id: str, cron_expr: str, trigger_name: str) -> None:
            scheduler.CfnSchedule(
                self,
                construct_id,
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
                schedule_expression=f"cron({cron_expr})",
                schedule_expression_timezone="UTC",
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=report_fn.function_arn,
                    role_arn=scheduler_role.role_arn,
                    input=f'{{"trigger": "{trigger_name}"}}',
                ),
            )

        make_schedule("Schedule16th", "0 1 16 * ? *", "day16")
        make_schedule("Schedule1st", "0 1 1 * ? *", "day1")

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "ReportsBucketName", value=reports_bucket.bucket_name)
