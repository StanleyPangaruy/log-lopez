#!/usr/bin/env python3
import os

import aws_cdk as cdk

from log_lopez.log_lopez_stack import LogLopezStack

app = cdk.App()

LogLopezStack(
    app,
    "LogLopezStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION", "ap-southeast-1"),
    ),
)

app.synth()
