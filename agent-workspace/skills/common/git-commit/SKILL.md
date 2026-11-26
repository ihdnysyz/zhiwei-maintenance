---
name: git-commit
description: Git 提交信息规则。执行 git commit、编写或修改提交信息前使用；commit-msg hook 拒绝提交时也用它。说明必须使用的标签和多项改动的写法。
---

# Git 提交

未经用户要求，不 commit、不 push。

## 格式

每笔提交信息必须以标签开头：`[标签] 简述`

| 标签 | 用途 |
|---|---|
| `[fix]` | bug 修复 |
| `[feature]` | 新增业务功能 |
| `[research]` | 调研记录、方案对比、实验验证与结论 |
| `[del]` | 删除代码、功能或文件 |
| `[update]` | 代码逻辑更新；不属于以上类型时的兜底标签；多件事合并在一笔提交时也用它 |

一笔提交包含多件事时，不必拆开，统一使用 `[update]` 标签，但提交信息需要列举改动，多个修改用分号（;）隔开，例如：

```
[update] setup.sh 拆出 init.sh；新增 SDD 工作区；Note 迁移到个人目录
```

## 校验

`setup.sh` 为项目安装 `commit-msg` hook，转发到 `agent-workspace/skills/common/git-commit/commit-msg`：首行不以上述标签开头时拒绝提交；
git 自动生成的 `Merge ` / `Revert ` / `fixup! ` / `squash! ` 信息放行。被拒绝时按上面的格式改写提交信息后重新提交。
