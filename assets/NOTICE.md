# SUSHIWAIT Logo 与字体来源

## 当前选定 Logo：Unbounded 浅/深色版

用户于 2026-10-03 确认最后一款 Unbounded 的白色背景方案并授权推送 GitHub。用户随后要求 GitHub 深色模式显示同字体深色方案。正式文件为 [浅色 SVG](sushiwait-logo-light.svg) 与 [深色 SVG](sushiwait-logo-dark.svg)，设计说明见 [BRAND_DESIGN.md](../docs/BRAND_DESIGN.md)。

- 字体：Unbounded Regular，字重 400，版本 1.701；版权归 The Unbounded Project Authors。
- 来源：用户提供的 Cinzel、Kaushan Script、Unbounded 字体包中的 `Unbounded/static/Unbounded-Regular.ttf`。
- 字体包 SHA-256：`2776b67dd68ee474a9872c8f1768788a0e859cb3021538a5ff81a5cf8b236db2`。
- 原始字体 SHA-256：`4aa6498d627f3059698277f5e28212edbdb6e28ba33759861da28fff0129f23c`。
- 上游：[googlefonts/unbounded](https://github.com/googlefonts/unbounded)；此次制作使用用户原包，没有重新下载字体。
- 许可：SIL Open Font License 1.1，完整原文见 [Unbounded-OFL.txt](Unbounded-OFL.txt)。

用 CoreText 正常排版 SUSHIWAIT、提取 CGPath 字形轮廓，保持自然比例，词标宽 780、中心 (600,220)。原创红色刷痕由 3,471 个矢量像素构成，边缘通过逐像素透明度渐隐。文字通过轮廓裁剪与 80 条原生 SVG 渐变带填色，按当前刷痕周边明暗由近黑柔和转白；没有描边。1200×440 画布内增加明确白色底板，以保持此次选定的白底效果。没有嵌入或分发 TTF，读者无需安装字体。

深色 SVG 沿用已审阅的 Unbounded 400 字形与同一像素笔触，词标为浅色纯路径、画布透明，外围透明度自然融入 GitHub 深色背景。README 顶部通过 picture / prefers-color-scheme: dark 自动选深色 SVG，浅色 SVG 默认回退，alt 为 SUSHIWAIT。原油漆/颗粒参考只用于理解整体感觉，没有复制参考图或分发参考文件。字体来源与项目文字排版不代表寿司郎官方字体、色号或品牌授权。

# 旧文字标题：Montserrat Light

SUSHIWAIT 的全大写标题使用 **Montserrat Light**，字重 300；字体作者为 The Montserrat.Git Project Authors，采用 SIL Open Font License 1.1。这里展示的是本项目文字排版，不代表寿司郎官方字体或品牌授权。

- 作者仓库：[JulietaUla/Montserrat](https://github.com/JulietaUla/Montserrat)
- 锁定提交：`555facfb2a18c72c3c0380f0d9c0f060453a9058`
- 原始字体：[Montserrat-Light.ttf](https://raw.githubusercontent.com/JulietaUla/Montserrat/555facfb2a18c72c3c0380f0d9c0f060453a9058/fonts/ttf/Montserrat-Light.ttf)
- 原始字体 SHA-256：`853de0dd715d6681f7038cf6cd53c1695b614657cbc57c8f542fe08d5b84e006`
- 许可原文：[Montserrat-OFL.txt](Montserrat-OFL.txt)

标题由 macOS CoreText 以 100 点排版 SUSHIWAIT，提取各字形及位置，用 CGPath 合并轮廓，取路径边界并四周留 18 点，输出为纯路径 SVG。浅色版使用 #111827，深色版使用 #f4f4f5；两份路径与 viewBox 相同。没有嵌入或分发字体文件，无需阅读者安装字体。OFL 对字体的许可要求不适用于用字体创作的文档；保留本说明和原文用于来源记录。

旧版 README 曾使用 picture 的 prefers-color-scheme 条件切换这两份标题。它们作为历史资产保留，当前 README 已采用上方的 Unbounded 像素刷痕 Logo。正文样式继续由 GitHub 渲染。


## README 用餐体验矢量版面

2026-10-04 用户确认无框刷痕设计并授权同步 GitHub。正式版面为 [浅色](sushiwait-experiences-light.svg)、[深色](sushiwait-experiences-dark.svg)、[手机浅色](sushiwait-experiences-light-mobile.svg) 与 [手机深色](sushiwait-experiences-dark-mobile.svg)。

六项标题、说明和像素笔触来自已确认的对话设计；笔触沿用正式 Logo 的红色、局部橙黄与外围渐隐，每版含 776 个装饰像素。文字保留为 SVG 文本，使用阅读设备的本地系统字体，不嵌入或分发字体文件。版面使用本项目原创排版与笔触，不含脚本、位图或外部资源。

六个小图标为 Lucide 的 clock-3、calendar-days、list-ordered、bell、chart-no-axes-combined、ticket。路径从实际预览运行时的图标提取；官方来源为 [lucide-icons/lucide](https://github.com/lucide-icons/lucide)，许可文本锁定 [500620a2e8123f8d1db191538886dc0c223f69a9](https://github.com/lucide-icons/lucide/blob/500620a2e8123f8d1db191538886dc0c223f69a9/LICENSE)，完整保留于 [Lucide-LICENSE.txt](Lucide-LICENSE.txt)。Lucide 采用 ISC；其中由 Feather 派生的图标另含 Cole Bemis 的 MIT 许可，保留官方完整归属。此处固定的是许可来源版本，不将预览运行时冒称为该提交构建。

README 通过 picture 的浅深主题及 600px 视口条件选图；桌面两列三行、手机单列，详细业务规则仍为可展开的原生文本，图像 alt 含六项体验说明。此展示不代表规划功能已经上线。
