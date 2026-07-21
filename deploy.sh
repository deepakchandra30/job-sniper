#!/bin/bash
# Job Sniper - one-shot AWS deployment
# Prereqs: AWS CLI configured (aws configure), region set (eu-west-1 recommended)
set -e

REGION="${AWS_REGION:-eu-west-1}"
FUNC_NAME="job-sniper"
BUCKET="job-sniper-state-$(aws sts get-caller-identity --query Account --output text)"
ALERT_EMAIL="${1:?Usage: ./deploy.sh your@email.com}"

echo "==> Region: $REGION | State bucket: $BUCKET | Email: $ALERT_EMAIL"

# 1. State bucket
aws s3 mb "s3://$BUCKET" --region "$REGION" 2>/dev/null || true

# 2. SES: verify your email (click the link AWS sends you before first alert)
aws ses verify-email-identity --email-address "$ALERT_EMAIL" --region "$REGION" || true
echo "==> Check your inbox and click the SES verification link."

# 3. IAM role
ROLE_NAME="job-sniper-role"
cat > /tmp/trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document file:///tmp/trust.json 2>/dev/null || true
cat > /tmp/policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::$BUCKET/*"},
 {"Effect":"Allow","Action":["ses:SendEmail"],"Resource":"*"},
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"}]}
EOF
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name job-sniper-policy --policy-document file:///tmp/policy.json
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
echo "==> Role: $ROLE_ARN"
sleep 10  # IAM propagation

# 4. Package + create/update Lambda
zip -j /tmp/job-sniper.zip monitor.py companies.json
if aws lambda get-function --function-name "$FUNC_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNC_NAME" \
    --zip-file fileb:///tmp/job-sniper.zip --region "$REGION"
else
  aws lambda create-function --function-name "$FUNC_NAME" \
    --runtime python3.12 --handler monitor.lambda_handler \
    --role "$ROLE_ARN" --timeout 240 --memory-size 512 \
    --zip-file fileb:///tmp/job-sniper.zip --region "$REGION" \
    --environment "Variables={STATE_BUCKET=$BUCKET,ALERT_EMAIL=$ALERT_EMAIL,SENDER_EMAIL=$ALERT_EMAIL,SES_REGION=$REGION}"
fi

# 5. EventBridge schedule: every 5 minutes
aws events put-rule --name job-sniper-5min --schedule-expression "rate(5 minutes)" --region "$REGION"
aws lambda add-permission --function-name "$FUNC_NAME" --statement-id evb \
  --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn "$(aws events describe-rule --name job-sniper-5min --region $REGION --query Arn --output text)" \
  --region "$REGION" 2>/dev/null || true
FUNC_ARN=$(aws lambda get-function --function-name "$FUNC_NAME" --region "$REGION" --query Configuration.FunctionArn --output text)
aws events put-targets --rule job-sniper-5min --targets "Id"="1","Arn"="$FUNC_ARN" --region "$REGION"

# 6. Kick off the baseline run (records current jobs, no email spam)
aws lambda invoke --function-name "$FUNC_NAME" --region "$REGION" /tmp/out.json
cat /tmp/out.json

echo ""
echo "✅ Deployed. Runs every 5 minutes. First run was a silent baseline;"
echo "   from now on you get an email within ~5 min of any new matching job."
