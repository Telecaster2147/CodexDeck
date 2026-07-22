# README screenshots

这些图片由 Textual `App.save_screenshot()` 从匿名 fixture 生成 SVG 后，再预渲染为 PNG。
README 使用固定像素图片，避免不同平台缺少 SVG 字体时出现字符宽度错位或文本重叠。

- `overview.png`：`120x30` 六会话宽屏导航与待审批会话的 Diagnosis Inspector
- `settings.png`：`120x30` 设置界面
- `narrow.png`：`72x24` 窄屏 Diagnosis 下钻

截图内容仅使用 `CODEX_HOME`、`workspace-a`、`session-1` 等文档占位符，六个会话分别展示
生成、待审批、后台终端、恢复、网络停顿和采集盲区。
