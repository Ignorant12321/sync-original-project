# sync-original-project

这个仓库通过 GitHub Actions 定时把多个上游仓库镜像同步到目标仓库。同步使用 `git clone --mirror` 和 `git push --mirror`，会同步分支、标签等 Git 引用。

默认每 3 天自动同步一次，也可以在 GitHub Actions 页面手动触发。

## 安全原则

不要把令牌写进仓库文件。`.github/mirror-repositories.json` 只配置令牌对应的环境变量名，真正的令牌值放在 GitHub Actions Secrets 中。

## 使用教程

### 1. 准备目标仓库

先在 GitHub 上创建用于接收镜像的目标仓库。目标仓库建议专门用于同步，不要在里面直接开发，因为镜像推送会覆盖目标仓库的 Git 引用。

例如：

- 上游仓库：`https://github.com/cmliu/edgetunnel.git`
- 目标仓库：`wischrismbers/edgetunnel`

### 2. 创建 GitHub Token

创建一个有目标仓库写入权限的 GitHub Personal Access Token。

Token 权限建议按最小权限配置：

- 目标仓库是公开仓库：给目标仓库写入权限即可。
- 目标仓库是私有仓库：需要包含对应私有仓库访问权限。

不要把 Token 写进任何仓库文件。

### 3. 添加 Actions Secret

在当前同步配置仓库中进入：

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

新增 Secret，例如：

```text
Name: GH_PAT
Secret: 你的 GitHub Token
```

如果不同目标仓库要使用不同 Token，可以创建多个 Secret，例如 `GH_PAT_PROJECT_A`、`GH_PAT_PROJECT_B`。

### 4. 配置同步仓库

仓库同步关系写在 `.github/mirror-repositories.json`：

```json
[
  {
    "name": "edgetunnel",
    "upstream": "https://github.com/cmliu/edgetunnel.git",
    "target": "wischrismbers/edgetunnel",
    "token_env": "GH_PAT"
  }
]
```

字段说明：

- `name`：日志中显示的同步名称。
- `upstream`：上游仓库 clone URL。
- `target`：目标仓库，格式为 `owner/repo`。
- `token_env`：workflow 中暴露给脚本的 token 环境变量名。
- `enabled`：可选。设置为 `false` 时跳过该仓库。

新增仓库时，在数组中追加一项：

```json
{
  "name": "project-a",
  "upstream": "https://github.com/upstream-owner/project-a.git",
  "target": "your-owner/project-a",
  "token_env": "GH_PAT_PROJECT_A"
}
```

### 5. 映射 Token 环境变量

如果配置文件使用了新的 `token_env`：

```json
"token_env": "GH_PAT_PROJECT_A"
```

除了创建同名 GitHub Actions Secret，还需要在 `.github/workflows/mirror-upstream.yml` 的 `env` 中显式映射：

```yaml
env:
  GH_PAT: ${{ secrets.GH_PAT }}
  GH_PAT_PROJECT_A: ${{ secrets.GH_PAT_PROJECT_A }}
```

这样做的好处是：配置文件可以公开提交，令牌值不会进入 Git 历史。

### 6. 手动运行一次

提交配置后，进入 GitHub 仓库的 `Actions` 页面，选择 `Mirror upstream to fork`，点击 `Run workflow`。第一次建议手动运行，确认日志中每个仓库都同步成功。

## 定时同步

Workflow 默认每 3 天运行一次：

```yaml
schedule:
  - cron: "17 0 */3 * *"
```

GitHub Actions 的 `schedule` 使用 UTC 时间。上面的配置表示 UTC 时间每 3 天的 00:17 左右运行一次。

如果想改频率，编辑 `.github/workflows/mirror-upstream.yml` 里的 cron 表达式即可。

## 失败处理

多仓库同步时，某一个仓库同步失败不会影响后续仓库继续同步。Workflow 会继续处理配置文件里的其他仓库，并在日志最后输出成功和失败数量。

常见失败原因：

- `token_env` 对应的 GitHub Actions Secret 未配置。
- workflow 的 `env` 中没有显式映射该 Secret。
- 目标仓库不存在或 Token 没有写入权限。
- 上游仓库地址不可访问。
- 目标仓库启用了分支保护，拒绝镜像推送。

## 注意事项

`git push --mirror` 会让目标仓库的引用与上游仓库保持一致，目标仓库中只存在于本地的分支或标签可能会被删除。建议目标仓库专门用于镜像同步，不要在目标仓库直接开发。
