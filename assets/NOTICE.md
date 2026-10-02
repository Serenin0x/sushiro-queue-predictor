# README 标题字体来源

SUSHIWAIT 的全大写标题使用 **Montserrat Light**，字重 300；字体作者为 The Montserrat.Git Project Authors，采用 SIL Open Font License 1.1。这里展示的是本项目文字排版，不代表寿司郎官方字体或品牌授权。

- 作者仓库：[JulietaUla/Montserrat](https://github.com/JulietaUla/Montserrat)
- 锁定提交：`555facfb2a18c72c3c0380f0d9c0f060453a9058`
- 原始字体：[Montserrat-Light.ttf](https://raw.githubusercontent.com/JulietaUla/Montserrat/555facfb2a18c72c3c0380f0d9c0f060453a9058/fonts/ttf/Montserrat-Light.ttf)
- 原始字体 SHA-256：`853de0dd715d6681f7038cf6cd53c1695b614657cbc57c8f542fe08d5b84e006`
- 许可原文：[Montserrat-OFL.txt](Montserrat-OFL.txt)

标题由 macOS CoreText 以 100 点排版 SUSHIWAIT，提取各字形及位置，用 CGPath 合并轮廓，取路径边界并四周留 18 点，输出为纯路径 SVG。浅色版使用 #111827，深色版使用 #f4f4f5；两份路径与 viewBox 相同。没有嵌入或分发字体文件，无需阅读者安装字体。OFL 对字体的许可要求不适用于用字体创作的文档；保留本说明和原文用于来源记录。

`README.md` 使用 picture 的 prefers-color-scheme 条件选择深色版本，浅色图片为默认回退，alt 为 SUSHIWAIT。正文样式继续由 GitHub 渲染。
