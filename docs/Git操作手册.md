# Git 操作手册（本机专用）

> 建立时间：2026/09/26
> 面向：本机（Windows + Git for Windows 2.47.1 + Git Credential Manager 2.6.0）的所有者
> 目的：**看懂 git 在说什么、遇到弹窗知道该填什么、日常操作照抄即可**

---

## 0. 先说一件做不到的事

**Git 的命令行提示无法变成中文。**

实测结论：Git for Windows **没有内置中文翻译文件**（`share/locale/` 下没有 git 的 `.mo`），
所以设 `i18n.locale = zh_CN` 也**不会**让 `error:` / `fatal:` 变成中文。
网上有人说能改，那是自己编译过带翻译的版本，或者装的是第三方中文 Git。

**替代方案**：见本手册 §4「常见英文提示 → 中文对照」，那是你真正需要的那部分。

---

## 1. 已经给你配好的设置

| 设置 | 值 | 作用 |
|---|---|---|
| `credential.helper` | `helper-selector` → 已选中 **manager** (GCM) | 凭据由 Git Credential Manager 管理 |
| `credential.credentialStore` | `wincredman` | 令牌存进 **Windows 凭据管理器**（加密，非明文文件） |
| `i18n.commitEncoding` | `utf-8` | 提交信息支持中文 |
| `i18n.logOutputEncoding` | `utf-8` | `git log` 里中文不乱码 |
| `core.quotepath` | `false` | 中文文件名正常显示（不再显示 `\346\216...` 转义码） |
| `core.pager` | 空（关闭） | 避免 less 分页器把中文搞乱码 |

配置文件位置：`C:\Users\Administrator\.gitconfig`（备份在 `~\.gitconfig.bak.*`）

---

## 2. 第一次推送：会弹什么、填什么

**以后不用再贴令牌了** —— 第一次成功后，GCM 会把令牌存进凭据管理器，之后自动使用。

### 操作

打开 **PowerShell**，进入仓库目录，执行：

```powershell
cd D:\工作区表\工作区4
git push origin main
```

### 接下来会发生什么（按顺序）

```
① 弹出一个小窗口：选择账号类型
   → 选 "Token"（或 "Personal Access Token"）

② 弹出输入框：
   用户名：填你的 GitHub 用户名        ← lhy302
   密码：  粘贴你的 GitHub 令牌        ← 输入时不显示字符，正常现象

③ 若弹出浏览器授权页（Device Code 流程）
   → 按提示在浏览器里点授权即可（这种模式下不用手输令牌）

④ 成功后提示：Everything up-to-date  或  main -> main
```

### ⚠️ 令牌需要什么权限

创建令牌时（https://github.com/settings/tokens/new）勾选：

| scope | 为什么需要 |
|---|---|
| `repo` | 推送提交 |
| `workflow` | 本仓库有 `.github/workflows/`，缺它会**直接被拒** |

建议 **Expiration 设 90 天**（到期 GCM 会再弹一次，重新粘一个即可）。
**不要再创建永不过期的令牌。**

### 验证是否存住了

```powershell
cmdkey /list | findstr github
```
有输出 = 已存进凭据管理器。

### 想换令牌 / 删除已存的

```powershell
cmdkey /list | findstr github          # 先找到目标名
cmdkey /delete:git:https://github.com  # 删除
```
删除后下次推送会重新弹窗。

---

## 3. 日常操作流程（照抄即可）

### 3.1 看清现状

```powershell
cd D:\工作区表\工作区4
git status          # 看有哪些改动
```

`git status` 的输出读法：

```
On branch main                              ← 当前在 main 分支
Your branch is up to date with 'origin/main'.  ← 本地与 GitHub 一致

Changes not staged for commit:              ← 改了但还没准备提交
        modified:   README.md

Untracked files:                            ← 新文件，git 还没管
        docs/新文档.md

nothing to commit, working tree clean       ← 没有任何改动（这是好状态）
```

### 3.2 提交改动（三/四步）

```powershell
# ① 把改动放进暂存区
git add README.md                  # 只加这一个文件
git add docs/                      # 加整个目录
git add -A                         # 加所有改动

# ② 看一眼"到底要提交什么"（强烈建议，防手滑）
git status
git diff --cached                  # 看具体改了什么内容

# ③ 提交，写说明
git commit -m "docs: 更新 README 说明"

# ④ 推到 GitHub
git push origin main
```

### 3.3 提交说明的写法（本仓库惯例）

```
docs:  只改文档
feat:  新功能
fix:   修 bug
test:  只改测试
chore: 杂项（配置、清理）
ci:    改 CI 配置
refactor: 重构，不改行为
```

### 3.4 看历史

```powershell
git log --oneline -10              # 最近 10 条，一行一条
git log --oneline --graph -10      # 带分支图形
git show b31f58c                   # 看某次提交改了什么
```

### 3.5 撤销（最常用的三个）

```powershell
# 改了文件想恢复原样（未 add）
git restore README.md

# 已经 add 了，想撤出暂存区（文件内容不变）
git restore --staged README.md

# 提交写错说明（还没 push）
git commit --amend -m "新的说明"
```

⚠️ **已经 push 过的提交不要用 `--amend`** —— 会造成本地与远程不一致。
本仓库历史上有过 `push --force` 的流程，**已作废，不要再 force**。

---

## 4. 常见英文提示 → 中文对照

| 英文提示 | 中文意思 | 怎么办 |
|---|---|---|
| `nothing to commit, working tree clean` | 没有改动，工作区干净 | 正常，无需操作 |
| `Changes not staged for commit` | 改了但没 `git add` | 先 `git add` |
| `Untracked files` | 新文件，git 还没跟踪 | `git add` 后才会纳入 |
| `Your branch is ahead of 'origin/main' by N commits` | 本地比 GitHub 多 N 个提交 | `git push origin main` |
| `Your branch is behind 'origin/main' by N commits` | GitHub 比本地新 | `git pull` 先拉下来 |
| `rejected ... non-fast-forward` | 远程有你本地没有的提交，推不上去 | **先 `git pull --rebase`**，别 force |
| `could not read Username` | 凭据没给上 | 重跑 `git push`，按 §2 弹窗填令牌 |
| `Authentication failed` | 令牌错/过期/权限不足 | 重新生成令牌（要 `repo` + `workflow`） |
| `refusing to allow ... without 'workflow' scope` | 令牌缺 `workflow` 权限 | 重签令牌，同时勾 `repo` + `workflow` |
| `Recv failure: Connection was reset` | 网络抖动（国内访问 GitHub 常见） | **直接重试**，通常第二次就成功 |
| `Push cannot contain secrets`（GH013） | GitHub 检测到提交里有密钥 | **别绕过！** 先删掉密钥再提交（见 §5） |
| `fatal: not a git repository` | 当前目录不是 git 仓库 | 先 `cd` 到正确目录 |
| `pathspec ... did not match` | 文件名写错了 | 检查文件名，用 `git status` 看准确名称 |

---

## 5. ⚠️ 两条本仓库专属红线

### 红线 1：不要把密钥提交进去

本仓库**真实发生过一次**：会话转录文档里含明文 GitHub 令牌，推送时被 GitHub
Push Protection 拦下（`GH013: Push cannot contain secrets`）。

**如果你看到这个提示**：

```powershell
# ① 先找出令牌在哪个文件
git grep -n -E "ghp_[A-Za-z0-9]{20,}" HEAD

# ② 用本仓库自带的脱敏脚本（会把令牌替换为 [REDACTED-TOKEN]）
py -3.12 "docs\协同进化\experiments\redact_secrets.py"

# ③ 重新提交（若那次提交还没 push 成功，用 amend）
git add -A
git commit --amend --no-edit
```

**绝对不要**去点 GitHub 提示里的 "allow the secret" 绕过它。

### 红线 2：不要推 `协同进化-资产/` 与运维手册

| 路径 | 为什么 |
|---|---|
| `协同进化-资产/` | 模型权重 + 框架源码，约 **1.78 GB**，会把仓库撑爆 |
| `GitHub推送操作指南.md` | 纯本地运维备忘，**历史上被提交过，专门做了一次历史改写才清掉** |

两者都已在 `.gitignore` 里。**自查两条**：

```powershell
git check-ignore -v "GitHub推送操作指南.md"     # 必须有输出
git check-ignore -v 协同进化-资产/models        # 必须有输出
```

**不要用 `git add -f` 强行加它们。**

---

## 6. 一页速查

```powershell
# 进入仓库
cd D:\工作区表\工作区4

# 标准流程
git status                     # 1. 看现状
git add -A                     # 2. 暂存全部改动
git status                     # 3. 确认要提交什么（防手滑）
git commit -m "docs: 说明"      # 4. 提交
git push origin main           # 5. 推送（首次会弹窗填令牌）

# 常用查询
git log --oneline -10          # 最近提交
git diff                       # 未暂存的改动
git diff --cached              # 已暂存的改动
git remote -v                  # 远程地址

# 撤销
git restore <文件>              # 放弃未暂存的修改
git restore --staged <文件>     # 撤出暂存区
git commit --amend -m "改说明"   # 改最后一次提交说明（未 push）
```

---

## 7. 关于本仓库的当前状态

| 项 | 值 |
|---|---|
| 本地 HEAD | `b31f58c` |
| 远程 `main` | `b31f58c`（一致） |
| 工作树 | 干净 |
| CI | `main` 上全绿 |

**要推新东西，直接从 §3.2 的四步走即可**，凭据会由 GCM 自动处理。
