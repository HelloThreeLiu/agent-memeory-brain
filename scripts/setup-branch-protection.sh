#!/usr/bin/env bash
# 为 main 分支配置保护规则（幂等：存在则更新，不存在则创建）。
#
# 用法：
#   bash scripts/setup-branch-protection.sh            # 应用
#   bash scripts/setup-branch-protection.sh --check    # 只打印当前状态
#
# 依赖：gh（已登录且有 admin 权限）
#
# 规则内容见 .github/rulesets/protect-main.json：
#   - 禁止删除 main
#   - 禁止强推（non_fast_forward）
#   - 要求线性历史（不允许 merge commit）
#   - 所有改动必须走 PR（审批数 0：单人开发无法自审；有第二人后改 1）
#   - 必需状态检查 gate，且分支必须与 main 同步
#   - bypass_actors 为空：管理员也不能直推（严格模式）

set -euo pipefail

REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
RULESET_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.github/rulesets/protect-main.json"

print_state() {
  echo "== 仓库：$REPO =="
  gh api "repos/$REPO" --jq '{squash: .allow_squash_merge, merge_commit: .allow_merge_commit, rebase: .allow_rebase_merge, auto_merge: .allow_auto_merge, delete_branch_on_merge: .delete_branch_on_merge}'
  echo "== rulesets =="
  gh api "repos/$REPO/rulesets" --jq '.[] | "\(.id)  \(.name)  \(.enforcement)"' || true
}

if [[ "${1:-}" == "--check" ]]; then
  print_state
  exit 0
fi

echo "== 1/3 调整合并方式（线性历史需要 squash/rebase）=="
gh api -X PATCH "repos/$REPO" \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=true \
  -F allow_auto_merge=true \
  -F delete_branch_on_merge=true >/dev/null
echo "ok"

echo "== 2/3 创建或更新 ruleset =="
RULESET_ID="$(gh api "repos/$REPO/rulesets" --jq '.[] | select(.name=="protect-main") | .id' || true)"
if [[ -n "$RULESET_ID" ]]; then
  gh api -X PUT "repos/$REPO/rulesets/$RULESET_ID" --input "$RULESET_FILE" >/dev/null
  echo "已更新 ruleset #$RULESET_ID"
else
  RULESET_ID="$(gh api -X POST "repos/$REPO/rulesets" --input "$RULESET_FILE" --jq '.id')"
  echo "已创建 ruleset #$RULESET_ID"
fi

echo "== 3/3 校验 =="
gh api "repos/$REPO/rulesets/$RULESET_ID" \
  --jq '"name=\(.name) enforcement=\(.enforcement)\n" + ([.rules[].type] | join(", "))'
echo
echo "完成。直推 main 现在会被拒绝；小改动请用 scripts/small-change.sh。"
