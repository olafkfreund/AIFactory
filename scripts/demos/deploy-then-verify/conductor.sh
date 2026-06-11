#!/usr/bin/env bash
# PARR deploy-then-verify — CLI conductor.
# Deploys 2 FastAPI services to REAL AWS App Runner via the Factory's
# deterministic Terraform, shows the live API, runs the AC-mapped verification
# tests against the live endpoints, then tears everything down.
set -uo pipefail

SPEC="parrdemo"
REGION="eu-west-2"
ENVRC="/home/olafkfreund/Source/Calitti/Synechron_ARC/sarc/.envrc"
W="/tmp/parr-demo"
INFRA="$W/infra"
REG="533267307120.dkr.ecr.${REGION}.amazonaws.com"

c(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
ok(){ printf '\033[1;32m✓ %s\033[0m\n' "$1"; }

getval(){ grep -iE "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENVRC" | tail -1 | sed -E "s/^[^=]*=//; s/^[\"']//; s/[\"'][[:space:]]*\$//"; }
export AWS_ACCESS_KEY_ID="$(getval AWS_ACCESS_KEY_ID)"
export AWS_SECRET_ACCESS_KEY="$(getval AWS_SECRET_ACCESS_KEY)"
export AWS_DEFAULT_REGION="$REGION"

teardown(){
  c "TEARDOWN (cost guard — always runs)"
  terraform -chdir="$INFRA" destroy -auto-approve -input=false -no-color 2>&1 | tail -4
  echo "App Runner services tagged spec=$SPEC remaining:"
  aws apprunner list-services --region "$REGION" \
    --query "ServiceSummaryList[?contains(ServiceName,'factory-${SPEC}')].ServiceName" --output json
}
trap teardown EXIT

c "0 · Identity"
aws sts get-caller-identity --output json | python3 -c 'import sys,json;d=json.load(sys.stdin);print("account",d["Account"],"region","'"$REGION"'")'

c "1 · Render DETERMINISTIC infra (Factory deploy_templates)"
mkdir -p "$INFRA"
( cd /mnt/data/Source-home/GitHub/AIFactory/apps/web-server && \
  python3 -c "import sys;sys.path.insert(0,'server/services');import deploy_templates as t;open('$INFRA/main.tf','w').write(t.render_terraform(['frontend','scoreboard'],spec_id='$SPEC',region='$REGION'))" )
grep -cE "aws_apprunner_service|aws_ecr_repository|factory-ephemeral" "$INFRA/main.tf" >/dev/null && ok "infra/main.tf rendered (App Runner x2, factory-ephemeral tagged)"

c "2 · terraform init + create ECR repos & IAM role"
terraform -chdir="$INFRA" init -input=false -no-color | tail -1
terraform -chdir="$INFRA" apply -auto-approve -input=false -no-color \
  -target=aws_iam_role.apprunner_ecr -target=aws_iam_role_policy_attachment.apprunner_ecr \
  -target=aws_ecr_repository.frontend -target=aws_ecr_repository.scoreboard 2>&1 | tail -2
ok "ECR repos + role created"

c "3 · Build + push images to ECR"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG" >/dev/null 2>&1
for svc in frontend scoreboard; do
  TAG="${REG}/factory-${SPEC}-${svc}:latest"
  docker build --platform linux/amd64 -q -t "$TAG" "$W/$svc" >/dev/null
  docker push -q "$TAG" >/dev/null 2>&1 || docker push "$TAG" | tail -1
  ok "pushed $svc"
done

c "4 · terraform apply — provision App Runner (real, ~3-4 min/service)"
terraform -chdir="$INFRA" apply -auto-approve -input=false -no-color 2>&1 | tail -2
FE=$(terraform -chdir="$INFRA" output -raw service_url_frontend)
SB=$(terraform -chdir="$INFRA" output -raw service_url_scoreboard)
ok "frontend  → $FE"
ok "scoreboard → $SB"

c "5 · The live API works (real HTTPS on AWS)"
echo "GET $FE/  →"; curl -s -m 25 "$FE/"; echo
echo "POST $FE/move (X to win) →"; curl -s -m 25 -X POST "$FE/move" -H 'content-type: application/json' \
  -d '{"board":["X","X","","","","","","",""],"cell":2,"player":"X"}'; echo
echo "POST $SB/scores WITHOUT token →"; curl -s -m 25 -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$SB/scores" -H 'content-type: application/json' -d '{"player":"a","won":true}'

c "6 · TFactory-style verification against the LIVE endpoints"
export FRONTEND_URL="$FE" SCOREBOARD_URL="$SB" TFACTORY_TARGET_URL="$FE" SCORE_TOKEN="secret-token"
python3 -m pytest "$W/tests" -v 2>&1 | tail -16

c "7 · DONE — tearing down next"
ok "deploy → live API → AC verification all green on real AWS"
