#!/usr/bin/env bash
# 小改动快车道：分支 → 提交 → PR → 开启自动合并（CI 绿后自动 squash 合入）。
#
# 用途：文档修正、错别字、注释、README 等 S 级改动。仍然走 PR（main 已被保护，
#       无法直推），但免除独立审查，通常 30 秒内完成。
#
# 用法：
#   bash scripts/small-change.sh "docs: 修正错别字" docs/BACKLOG.md
#   bash scripts/small-change.sh "docs: 更新 README" README.md docs/xxx.md
#
# 拒绝条件（这些属于 M/L 级，必须走完整流程）：
#   - 改动落在 nebula/、.github/、scripts/、pyproject*.toml、WORKFLOW.md
#   - 未指定文件、或指定文件没有实际改动

set -euo pipefail

MSG="${1:-}"
shift || true
FILES=("$@")

if [[ -z "$MSG" || ${#FILES[@]} -eq 0 ]]; then
  echo "用法：bash scripts/small-change.sh \"<commit message>\" <file> [file...]" >&2
  exit 2
fi

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "请先切回 main 再执行（当前：$(git branch --show-current)）" >&2
  exit 2
fi

# 只允许指定的、确实有改动的文件
git add -- "${FILES[@]}"
CHANGED="$(git diff --cached --name-only)"
if [[ -z "$CHANGED" ]]; then
  echo "指定的文件没有实际改动，已退出。" >&2
  exit 2
fi

# S 级边界：碰到代码/流程/依赖一律拒绝
if echo "$CHANGED" | grep -qE '^(nebula/|\.github/|scripts/|pyproject.*\.toml$|WORKFLOW\.md$)'; then
  echo "拒绝：以下改动属于 M/L 级，请走完整流程（方案 → 分支 → 审查 → PR）：" >&2
  echo "$CHANGED" | grep -E '^(nebula/|\.github/|scripts/|pyproject.*\.toml$|WORKFLOW\.md$)' | sed 's/^/  - /' >&2
  git reset >/dev/null
  exit 3
fi

SLUG="$(printf '%s' "$MSG" | tr -cs '[:alnum:]' '-' | tr '[:upper:]' '[:lower:]' | cut -c1-40)"
BRANCH="chore/${SLUG:-small}-$(date +%H%M%S)"

echo "== 分支：$BRANCH =="
git switch -c "$BRANCH"
git commit -q -m "$MSG"
git push -q -u origin "$BRANCH"

echo "== 创建 PR 并开启自动合并 =="
gh pr create --base main --head "$BRANCH" --title "$MSG" --body "S 级小改动（免审查，走快车道）。改动文件：$(echo "$CHANGED" | tr '\n' ' ')" >/dev/null
gh pr merge --squash --auto --delete-branch "$BRANCH" >/dev/null

git switch -q main
echo "已提交 PR，CI 通过后自动 squash 合入 main。查看：gh pr list --state open"
