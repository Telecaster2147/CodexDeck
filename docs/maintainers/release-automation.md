# 自动发布

正常发布只需要两步：

1. 更新 `src/codexdeck/config.py` 的 `__version__`。
2. 添加 `docs/releases/v版本号.md`，第一行为 `# CodexDeck 版本号`，简短说明变化。

合并到 `main` 后，CI 的类型检查、完整测试、最新 Python smoke、性能/PTY、供应链和构建
全部通过，才会调用 Release workflow。它使用 CI 对应的精确 commit 构建、安装验证，随后创建
同版本标签和 GitHub Release，上传 wheel、sdist 和 wheel 校验文件。

无需个人 Token，也不需要手动构建或运行本地发布脚本。发布 job 使用 GitHub 自动签发的
`GITHUB_TOKEN`；写权限仅授予发布流程。PR、其他分支和 fork 不发布。

普通提交沿用现有版本号时会跳过发布。已有公开版本保持不变，不移动标签、不替换安装包。
附件先上传到 draft，全部完成后才公开；并发发布会排队而非互相取消。

## 失败后重试

先查看失败的 CI/Release 日志。修复代码后重新提交即可；已经公开的版本需要使用新版本号。
如果只是发布接口或上传的临时错误，可重跑失败 job，或者在 Actions 中手动运行 Release：

```bash
gh workflow run release.yml --repo Telecaster2147/CodexDeck --ref main
```

手动运行也会核实该 commit 的六项 CI gate 全部通过。失败的 draft 可以继续上传；已存在的
标签必须指向当前 commit，流程不会强制移动标签。若发布失败且随后又提交了代码，可重跑原
失败 job；或审阅并处理尚未公开的 draft/tag 后重新发布，避免同版本指向不同代码。
