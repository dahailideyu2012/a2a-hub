#!/bin/sh
# 安装 git hooks：把 scripts/ 下的 hook 复制进 .git/hooks/ 并加执行权限。
#
#   sh scripts/install-hooks.sh
#
# 幂等：重复执行只是覆盖。已有同名 hook 会先备份成 <name>.bak。

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS="$ROOT/.git/hooks"

if [ ! -d "$HOOKS" ]; then
    echo "找不到 $HOOKS —— 请在 git 仓库里执行" >&2
    exit 1
fi

for src in "$ROOT"/scripts/post-commit; do
    [ -f "$src" ] || continue
    name="$(basename "$src")"
    dest="$HOOKS/$name"
    if [ -f "$dest" ]; then
        cp "$dest" "$dest.bak"
        echo "已备份旧 hook -> $dest.bak"
    fi
    cp "$src" "$dest"
    chmod +x "$dest"
    echo "已安装 hook：$name"
done

echo "完成。之后每次 commit 都会自动同步 GitHub（日志：.workbuddy/auto-push.log）"
